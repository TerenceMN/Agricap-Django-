"""Retraits de portefeuille client par palier (auto/manager/quorum) — même mécanisme OTP
auto-hébergé + multi-signature que `transactions.services` (voir la justification dans ce
module : l'auth est déléguée et stateless, le MFA IdP n'est pas re-déclenchable par action).
Contrairement à `transactions.Transaction` (objet de workflow pur, ne débite rien lui-même),
un `WithdrawalRequest` déclenche un VRAI mouvement de solde (`WalletMovement` + décrément
`ClientWallet.balance`) au moment où le palier requis est atteint."""
from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import timedelta
from decimal import Decimal

from django.db import transaction
from django.db.models import F
from django.utils import timezone

from audit.services import record as audit_record
from common import idempotency
from common.choices import FlowStatus
from common.exceptions import (
    ConflictError,
    InsufficientFundsError,
    NotFoundError,
    StepUpRequiredError,
    ValidationFailed,
)
from common.parsing import to_decimal
from rbac.role_registry import get_role

from . import serializers
from .models import ClientWallet, WalletMovement, WithdrawalApproval, WithdrawalOtpChallenge, WithdrawalRequest, \
    WithdrawalThreshold

logger = logging.getLogger("agricap")

_OTP_TTL_MINUTES = 5
_RESOLVED_STATUSES = (FlowStatus.POSTED, FlowStatus.REJECTED, FlowStatus.REVERSED)

# Valeurs par défaut tant qu'aucun seuil n'a été configuré pour cette devise (admin) — même
# ordre de grandeur que `transactions._DEFAULT_THRESHOLDS["TRANSFER"]`.
_DEFAULT_THRESHOLD = (Decimal("500"), Decimal("5000"))


def _threshold_for(currency: str) -> WithdrawalThreshold:
    """Palier configuré pour la devise, ou le repli codé en dur — AVEC un warning.

    `WithdrawalThreshold` vide n'empêche rien : le repli `_DEFAULT_THRESHOLD`
    (500 / 5000) s'applique et les retraits continuent. C'est voulu — un palier
    non configuré ne doit pas bloquer une agence.

    Mais il s'appliquait EN SILENCE, et c'est le défaut. Le principe 8 autorise
    les valeurs de secours à une condition : « avec warning loggé quand elles
    s'appliquent ». Sans ce log, l'institution valide automatiquement tout retrait
    sous 500 en croyant appliquer SA politique, alors qu'elle applique une
    constante de ce fichier que personne n'a votée — et le seul symptôme visible
    est une table `WithdrawalThreshold` vide que rien ne relie au comportement
    observé.
    """
    threshold = WithdrawalThreshold.objects.filter(currency=currency).first()
    if threshold:
        return threshold
    auto_limit, manager_limit = _DEFAULT_THRESHOLD
    logger.warning(
        "Palier de retrait non configuré pour %s : repli sur les valeurs de secours "
        "(auto ≤ %s, quorum ≥ %s). Tout retrait sous %s %s est donc validé "
        "AUTOMATIQUEMENT, sans approbation ni OTP — ce seuil n'a été décidé par "
        "personne. Configurez `WithdrawalThreshold` pour cette devise.",
        currency, auto_limit, manager_limit, auto_limit, currency,
    )
    return WithdrawalThreshold(currency=currency, auto_limit=auto_limit, manager_limit=manager_limit)


def _required_approvals(amount: Decimal, threshold: WithdrawalThreshold) -> int:
    return 3 if amount >= threshold.manager_limit else 1


def quorum_met(request_obj: WithdrawalRequest) -> bool:
    threshold = _threshold_for(request_obj.wallet.currency)
    needed = _required_approvals(request_obj.amount, threshold)
    approved = request_obj.approvals.filter(decision=WithdrawalApproval.Decision.APPROVED).count()
    return approved >= needed


def _post_withdrawal(*, request_obj: WithdrawalRequest, by: str = "") -> WithdrawalRequest:
    """Débit réel — appelé soit immédiatement (palier auto), soit une fois le palier requis
    atteint (palier manager/quorum). Verrouille le portefeuille pour re-vérifier le solde au
    moment du débit (il a pu changer entre la demande et l'approbation).

    Retrait EXTERNE : une fois le portefeuille débité, on adosse un ordre de décaissement
    Makuta (jambe fournisseur) — jamais avant, jamais sans l'approbation qui vient d'avoir
    lieu (P2)."""
    wallet = ClientWallet.objects.select_for_update().get(pk=request_obj.wallet_id)
    if wallet.balance < request_obj.amount:
        raise InsufficientFundsError(account_id=wallet.pk)
    movement = WalletMovement.objects.create(wallet=wallet, kind=WalletMovement.Kind.WITHDRAW,
                                              amount=request_obj.amount)
    ClientWallet.objects.filter(pk=wallet.pk).update(balance=F("balance") - request_obj.amount)
    request_obj.movement = movement
    request_obj.status = FlowStatus.POSTED
    request_obj.save(update_fields=["movement", "status", "updated_at"])
    audit_record(actor=by, action="caisses.withdrawal_request.post", entity_type="WithdrawalRequest",
                 entity_id=str(request_obj.pk), details={"amount": str(request_obj.amount)})
    _settle_external_leg(request_obj=request_obj, by=by)
    return request_obj


def _settle_external_leg(*, request_obj: WithdrawalRequest, by: str = "") -> None:
    """Adosse le décaissement Makuta d'un retrait EXTERNE au portefeuille déjà débité, et
    programme son envoi après le commit. Interne (canal vide/`agent`) ou payout déjà créé :
    ne fait rien. La création de l'ordre est dans la MÊME transaction que le débit (tout ou
    rien) ; l'envoi réseau, lui, est post-commit et hors transaction."""
    from . import channels, payments
    if not channels.is_external(request_obj.channel) or request_obj.payout_order_id:
        return
    order = payments.create_settlement_payout(
        withdrawal_request=request_obj,
        operation=channels.payout_operation(request_obj.channel),
        counterparty=request_obj.counterparty, by=by,
    )
    request_obj.payout_order = order
    request_obj.save(update_fields=["payout_order", "updated_at"])
    transaction.on_commit(lambda: _dispatch_settlement_payout(reference=order.reference, by=by))


def _dispatch_settlement_payout(*, reference: str, by: str = "") -> None:
    """Envoi post-commit du décaissement — hors transaction (appel réseau) et SEULEMENT si
    Makuta est configuré. Non configuré : l'ordre reste PENDING, le staff le règle via la
    réconciliation / `force_settle`. Un échec réseau n'annule pas le retrait (déjà réglé) : il
    laisse l'ordre à réconcilier, il ne casse rien."""
    from common import makuta

    from . import payments
    if not makuta.is_configured():
        return
    try:
        payments.dispatch_payment_order(reference=reference, by=by)
    except Exception:  # noqa: BLE001 — le retrait est réglé ; l'envoi se réconcilie.
        logger.exception("Dispatch du décaissement %s en échec — ordre à réconcilier.", reference)


@transaction.atomic
def create_withdrawal_request(*, wallet_id: int, amount: Decimal | str, idempotency_key: str,
                               by: str = "", channel: str = "", counterparty: str = "") -> WithdrawalRequest:
    from compliance.kyc_levels import monthly_limit_for, monthly_withdrawal_total

    from . import channels, payments

    amount = to_decimal(amount)
    if amount <= 0:
        raise ValidationFailed("Le montant du retrait doit être strictement positif.")

    channel = channel or ""
    counterparty = counterparty or ""
    if not channels.is_known(channel):
        raise ValidationFailed(f"Canal de retrait inconnu : {channel}.")
    if channels.is_external(channel):
        if not counterparty:
            raise ValidationFailed("La contrepartie (numéro Mobile Money / compte) est requise "
                                   "pour un retrait externe.")
        # Échoue AVANT tout débit si le catalogue fournisseur manque : ne pas régler un
        # retrait externe qu'on ne saura jamais verser (le versement se fait au règlement).
        payments.operation_config(channels.payout_operation(channel))

    rec = idempotency.begin(scope="caisses.withdrawal_request", key=idempotency_key,
                             params={"wallet": wallet_id, "amount": str(amount),
                                     "channel": channel, "counterparty": counterparty}, by=by)

    wallet = ClientWallet.objects.select_for_update().filter(pk=wallet_id).first()
    if not wallet:
        raise NotFoundError("Portefeuille introuvable.")
    if wallet.balance < amount:
        raise InsufficientFundsError(account_id=wallet.pk)

    limit = monthly_limit_for(user=wallet.user)
    withdrawn_this_month = monthly_withdrawal_total(user=wallet.user, currency=wallet.currency)
    if withdrawn_this_month + amount > limit:
        raise ValidationFailed(
            f"Plafond de retrait mensuel KYC dépassé ({withdrawn_this_month + amount} > "
            f"{limit} {wallet.currency})."
        )

    threshold = _threshold_for(wallet.currency)
    auto = amount < threshold.auto_limit
    request_obj = WithdrawalRequest.objects.create(
        wallet=wallet, amount=amount, status=FlowStatus.PENDING_VALIDATION, auto_validated=auto,
        channel=channel, counterparty=counterparty,
        idempotency_key=idempotency_key, created_by=by,
    )
    audit_record(actor=by, action="caisses.withdrawal_request.create", entity_type="WithdrawalRequest",
                 entity_id=str(request_obj.pk), details={"amount": str(amount), "auto_validated": auto})
    if auto:
        request_obj = _post_withdrawal(request_obj=request_obj, by=by)

    response = serializers.withdrawal_request_row(request_obj)
    idempotency.complete(rec, response=response, entity_type="WithdrawalRequest", entity_id=str(request_obj.pk))
    return request_obj


def request_step_up_otp(*, request_id: int, approver_sub: str) -> WithdrawalOtpChallenge:
    request_obj = WithdrawalRequest.objects.filter(pk=request_id).first()
    if not request_obj:
        raise NotFoundError("Demande de retrait introuvable.")
    code = f"{secrets.randbelow(1_000_000):06d}"
    salt = secrets.token_hex(8)
    code_hash = hashlib.sha256(f"{salt}:{code}".encode()).hexdigest()
    challenge = WithdrawalOtpChallenge.objects.create(
        request=request_obj, approver_sub=approver_sub, code_hash=f"{salt}${code_hash}",
        expires_at=timezone.now() + timedelta(minutes=_OTP_TTL_MINUTES),
    )
    try:
        from django.conf import settings
        from django.core.mail import send_mail
        send_mail(
            "Code de validation AGRICAP", f"Votre code de validation : {code}",
            getattr(settings, "DEFAULT_FROM_EMAIL", "no-reply@agricap.local"), [approver_sub],
            fail_silently=True,
        )
    except Exception:  # noqa: BLE001 — l'envoi ne doit jamais bloquer l'approbation (dégrade en log).
        pass
    try:
        from common.sms import send_sms_to_user
        send_sms_to_user(user_sub=approver_sub, message=f"Code de validation AGRICAP : {code}")
    except Exception:  # noqa: BLE001 — canal secondaire, ne doit jamais bloquer l'approbation.
        pass
    logger.info("OTP retrait (DEV) request=%s approver=%s code=%s", request_id, approver_sub, code)
    return challenge


def verify_step_up_otp(*, challenge_id: str, code: str) -> bool:
    challenge = WithdrawalOtpChallenge.objects.filter(pk=challenge_id).first()
    if not challenge:
        raise NotFoundError("Challenge OTP introuvable.")
    if challenge.verified_at:
        return True
    if challenge.attempts >= challenge.max_attempts:
        raise ConflictError("Nombre maximal de tentatives OTP dépassé.")
    if timezone.now() > challenge.expires_at:
        raise ConflictError("Code OTP expiré.")
    salt, _, expected_hash = challenge.code_hash.partition("$")
    ok = hashlib.sha256(f"{salt}:{code}".encode()).hexdigest() == expected_hash
    challenge.attempts += 1
    if ok:
        challenge.verified_at = timezone.now()
    challenge.save(update_fields=["attempts", "verified_at"])
    return ok


@transaction.atomic
def approve(*, request_id: int, approver_sub: str, approver_role: str,
            otp_code: str | None = None) -> WithdrawalRequest:
    request_obj = WithdrawalRequest.objects.select_for_update().filter(pk=request_id).first()
    if not request_obj:
        raise NotFoundError("Demande de retrait introuvable.")
    if request_obj.status in _RESOLVED_STATUSES:
        return request_obj  # déjà résolue — idempotent, pas une erreur
    if WithdrawalApproval.objects.filter(request=request_obj, approver_sub=approver_sub).exists():
        return request_obj  # déjà décidé par cet approbateur — no-op idempotent

    threshold = _threshold_for(request_obj.wallet.currency)
    needed = _required_approvals(request_obj.amount, threshold)
    role = get_role(approver_role)
    if needed >= 3 and not role.is_supervisor:
        raise ConflictError("Seuls les rôles superviseurs peuvent approuver ce montant.")

    step_up_required = needed >= 3 or role.mfa_step_up_required
    otp_verified_at = None
    if step_up_required:
        challenge = WithdrawalOtpChallenge.objects.filter(
            request=request_obj, approver_sub=approver_sub, verified_at__isnull=False,
        ).order_by("-created_at").first()
        if not challenge or not otp_code:
            raise StepUpRequiredError("Un code OTP vérifié est requis pour approuver ce retrait.")
        otp_verified_at = challenge.verified_at

    WithdrawalApproval.objects.create(
        request=request_obj, approver_sub=approver_sub, approver_role=approver_role,
        decision=WithdrawalApproval.Decision.APPROVED, otp_verified_at=otp_verified_at,
    )
    audit_record(actor=approver_sub, actor_role=approver_role, action="caisses.withdrawal_request.approve",
                 entity_type="WithdrawalRequest", entity_id=str(request_obj.pk),
                 details={"needed": needed, "amount": str(request_obj.amount)})

    if quorum_met(request_obj):
        request_obj = _post_withdrawal(request_obj=request_obj, by=approver_sub)
    return request_obj


@transaction.atomic
def reject(*, request_id: int, approver_sub: str, approver_role: str, reason: str = "") -> WithdrawalRequest:
    request_obj = WithdrawalRequest.objects.select_for_update().filter(pk=request_id).first()
    if not request_obj:
        raise NotFoundError("Demande de retrait introuvable.")
    if request_obj.status in _RESOLVED_STATUSES:
        return request_obj
    WithdrawalApproval.objects.update_or_create(
        request=request_obj, approver_sub=approver_sub,
        defaults={"approver_role": approver_role, "decision": WithdrawalApproval.Decision.REJECTED},
    )
    request_obj.status = FlowStatus.REJECTED
    request_obj.save(update_fields=["status", "updated_at"])
    audit_record(actor=approver_sub, actor_role=approver_role, action="caisses.withdrawal_request.reject",
                 entity_type="WithdrawalRequest", entity_id=str(request_obj.pk), details={"reason": reason})
    return request_obj
