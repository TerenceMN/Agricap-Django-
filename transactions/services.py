"""Workflow de validation adaptative (multi-signature + step-up MFA). Voir le plan pour la
justification du choix OTP auto-hébergé : le backend fintech n'a aucune session/mot de
passe local (auth déléguée, stateless), et le MFA de l'IdP est un contrôle AU LOGIN, pas
re-déclenchable proprement pour un quorum de 3 approbateurs distincts, chacun déjà
authentifié dans sa propre session — un OTP par email auto-hébergé est le mécanisme minimal
et autonome pour une preuve d'intention par action."""
from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import timedelta
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from audit.services import record as audit_record
from common import idempotency
from common.choices import FlowStatus
from common.exceptions import ConflictError, NotFoundError, StepUpRequiredError, ValidationFailed
from common.parsing import to_decimal
from rbac.role_registry import get_role

from . import serializers
from .models import OtpChallenge, SpecialCase, Transaction, TransactionApproval, ValidationThreshold

logger = logging.getLogger("agricap")

_OTP_TTL_MINUTES = 5
_RESOLVED_STATUSES = (FlowStatus.POSTED, FlowStatus.REJECTED, FlowStatus.REVERSED)

# Valeurs par défaut du cahier de conception (Annexe F / Scénario 7), utilisées tant
# qu'aucun seuil n'a été configuré pour ce type d'opération via `thresholds` (admin).
_DEFAULT_THRESHOLDS = {
    "PAYMENT": (Decimal("1000"), Decimal("5000")),
    "REIMBURSEMENT": (Decimal("500"), Decimal("5000")),
    "TRANSFER": (Decimal("5000"), Decimal("5000")),
}


def _threshold_for(operation_type: str) -> ValidationThreshold:
    threshold = ValidationThreshold.objects.filter(operation_type=operation_type).first()
    if threshold:
        return threshold
    auto_limit, manager_limit = _DEFAULT_THRESHOLDS.get(operation_type, (Decimal("1000"), Decimal("5000")))
    return ValidationThreshold(operation_type=operation_type, auto_limit=auto_limit, manager_limit=manager_limit)


@transaction.atomic
def create_transaction(*, agency_id: int | None, kind: str, amount: Decimal | str, currency: str,
                        operation_type: str, emitter: str = "", receiver: str = "",
                        description: str = "", idempotency_key: str, by: str = "") -> Transaction:
    amount = to_decimal(amount)
    if amount <= 0:
        raise ValidationFailed("Le montant de la transaction doit être strictement positif.")

    rec = idempotency.begin(
        scope="transactions.create", key=idempotency_key,
        params={"kind": kind, "amount": str(amount), "currency": currency, "operation_type": operation_type,
                "emitter": emitter, "receiver": receiver}, by=by,
    )

    threshold = _threshold_for(operation_type)
    auto = amount < threshold.auto_limit
    tx = Transaction.objects.create(
        agency_id=agency_id, kind=kind, amount=amount, currency=currency, operation_type=operation_type,
        emitter=emitter, receiver=receiver, description=description,
        status=FlowStatus.POSTED if auto else FlowStatus.PENDING_VALIDATION, auto_validated=auto, created_by=by,
    )
    audit_record(actor=by, action="transaction.create", entity_type="Transaction", entity_id=str(tx.pk),
                 details={"amount": str(amount), "auto_validated": auto})
    idempotency.complete(rec, response=serializers.tx_row(tx),
                          entity_type="Transaction", entity_id=str(tx.pk))
    return tx


def _required_approvals(amount: Decimal, threshold: ValidationThreshold) -> int:
    return 3 if amount >= threshold.manager_limit else 1


def quorum_met(transaction_obj: Transaction) -> bool:
    threshold = _threshold_for(transaction_obj.operation_type)
    needed = _required_approvals(transaction_obj.amount, threshold)
    approved = transaction_obj.approvals.filter(decision=TransactionApproval.Decision.APPROVED).count()
    return approved >= needed


def request_step_up_otp(*, transaction_id: int, approver_sub: str) -> OtpChallenge:
    tx = Transaction.objects.filter(pk=transaction_id).first()
    if not tx:
        raise NotFoundError("Transaction introuvable.")
    code = f"{secrets.randbelow(1_000_000):06d}"
    salt = secrets.token_hex(8)
    code_hash = hashlib.sha256(f"{salt}:{code}".encode()).hexdigest()
    challenge = OtpChallenge.objects.create(
        transaction=tx, approver_sub=approver_sub, code_hash=f"{salt}${code_hash}",
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
    logger.info("OTP transaction (DEV) tx=%s approver=%s code=%s", transaction_id, approver_sub, code)
    return challenge


def verify_step_up_otp(*, challenge_id: str, code: str) -> bool:
    challenge = OtpChallenge.objects.filter(pk=challenge_id).first()
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
def approve(*, transaction_id: int, approver_sub: str, approver_role: str,
            otp_code: str | None = None) -> Transaction:
    tx = Transaction.objects.select_for_update().filter(pk=transaction_id).first()
    if not tx:
        raise NotFoundError("Transaction introuvable.")
    if tx.status in _RESOLVED_STATUSES:
        return tx  # déjà résolue — idempotent, pas une erreur (double-clic après quorum atteint)
    if TransactionApproval.objects.filter(transaction=tx, approver_sub=approver_sub).exists():
        return tx  # déjà décidé par cet approbateur — no-op idempotent

    threshold = _threshold_for(tx.operation_type)
    needed = _required_approvals(tx.amount, threshold)
    role = get_role(approver_role)
    if needed >= 3 and not role.is_supervisor:
        raise ConflictError("Seuls les rôles superviseurs peuvent approuver ce montant.")

    step_up_required = needed >= 3 or role.mfa_step_up_required
    otp_verified_at = None
    if step_up_required:
        challenge = OtpChallenge.objects.filter(
            transaction=tx, approver_sub=approver_sub, verified_at__isnull=False,
        ).order_by("-created_at").first()
        if not challenge or not otp_code:
            raise StepUpRequiredError("Un code OTP vérifié est requis pour approuver ce montant.")
        otp_verified_at = challenge.verified_at

    TransactionApproval.objects.create(
        transaction=tx, approver_sub=approver_sub, approver_role=approver_role,
        decision=TransactionApproval.Decision.APPROVED, otp_verified_at=otp_verified_at,
    )
    audit_record(actor=approver_sub, actor_role=approver_role, action="transaction.approve",
                 entity_type="Transaction", entity_id=str(tx.pk), details={"needed": needed, "amount": str(tx.amount)})

    if quorum_met(tx):
        tx.status = FlowStatus.POSTED
        tx.save(update_fields=["status", "updated_at"])
        audit_record(actor=approver_sub, action="transaction.post", entity_type="Transaction", entity_id=str(tx.pk))
    return tx


@transaction.atomic
def reject(*, transaction_id: int, approver_sub: str, approver_role: str, reason: str = "") -> Transaction:
    tx = Transaction.objects.select_for_update().filter(pk=transaction_id).first()
    if not tx:
        raise NotFoundError("Transaction introuvable.")
    if tx.status in _RESOLVED_STATUSES:
        return tx
    TransactionApproval.objects.update_or_create(
        transaction=tx, approver_sub=approver_sub,
        defaults={"approver_role": approver_role, "decision": TransactionApproval.Decision.REJECTED},
    )
    tx.status = FlowStatus.REJECTED
    tx.save(update_fields=["status", "updated_at"])
    audit_record(actor=approver_sub, actor_role=approver_role, action="transaction.reject",
                 entity_type="Transaction", entity_id=str(tx.pk), details={"reason": reason})
    return tx


@transaction.atomic
def reverse(*, transaction_id: int, reason: str, by: str = "") -> Transaction:
    tx = Transaction.objects.select_for_update().filter(pk=transaction_id).first()
    if not tx:
        raise NotFoundError("Transaction introuvable.")
    if tx.status != FlowStatus.POSTED:
        raise ConflictError("Seule une transaction comptabilisée peut être annulée.")
    tx.status = FlowStatus.REVERSED
    tx.save(update_fields=["status", "updated_at"])
    audit_record(actor=by, action="transaction.reverse", entity_type="Transaction", entity_id=str(tx.pk),
                 details={"reason": reason})
    return tx


def overdue_pending_count(*, now=None) -> int:
    """Nombre de transactions en attente de validation depuis plus longtemps que le délai
    `manual_timeout_hours` configuré pour leur type d'opération — un signal de risque
    opérationnel réel (Supervision.jsx « Alertes Critiques »), pas seulement le compte brut
    de transactions en attente : une transaction en attente depuis 2h n'est pas un problème,
    une transaction en attente depuis 3 jours en est un."""
    now = now or timezone.now()
    thresholds_by_type = {t.operation_type: t for t in ValidationThreshold.objects.all()}
    count = 0
    for tx in Transaction.objects.filter(status=FlowStatus.PENDING_VALIDATION):
        threshold = thresholds_by_type.get(tx.operation_type) or _threshold_for(tx.operation_type)
        if now - tx.created_at > timedelta(hours=threshold.manual_timeout_hours):
            count += 1
    return count


def flag_special_case(*, transaction_obj: Transaction, alert_level: str, recommendation: str = "") -> SpecialCase:
    return SpecialCase.objects.create(transaction=transaction_obj, alert_level=alert_level,
                                       recommendation=recommendation)


@transaction.atomic
def escalate_special_case(*, case: SpecialCase, supervisor_sub: str) -> SpecialCase:
    case.status = SpecialCase.Status.EN_OBSERVATION
    case.escalated_to_sub = supervisor_sub
    case.save(update_fields=["status", "escalated_to_sub"])
    audit_record(actor=supervisor_sub, action="special_case.escalate", entity_type="SpecialCase",
                 entity_id=str(case.pk))
    return case
