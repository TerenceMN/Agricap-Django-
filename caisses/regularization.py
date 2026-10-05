"""Ordres de régularisation (Support.jsx « Crédit forcé ») — même mécanisme OTP + palier
auto/manager/quorum que `withdrawal_tiers`, mais en sens CRÉDIT et optionnellement rattaché
à un `support.Ticket`. Remplace l'action `force_credit` simulée (« action simulée, aucun
mouvement réel créé ») par un vrai `WalletMovement`."""
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
from common.exceptions import ConflictError, NotFoundError, StepUpRequiredError, ValidationFailed
from common.parsing import to_decimal
from rbac.role_registry import get_role

from . import serializers
from .models import (
    ClientWallet,
    RegularizationApproval,
    RegularizationOrder,
    RegularizationOtpChallenge,
    RegularizationThreshold,
    WalletMovement,
)

logger = logging.getLogger("agricap")

_OTP_TTL_MINUTES = 5
_RESOLVED_STATUSES = (FlowStatus.POSTED, FlowStatus.REJECTED, FlowStatus.REVERSED)
_DEFAULT_THRESHOLD = (Decimal("200"), Decimal("2000"))


def _threshold_for(currency: str) -> RegularizationThreshold:
    threshold = RegularizationThreshold.objects.filter(currency=currency).first()
    if threshold:
        return threshold
    auto_limit, manager_limit = _DEFAULT_THRESHOLD
    return RegularizationThreshold(currency=currency, auto_limit=auto_limit, manager_limit=manager_limit)


def _required_approvals(amount: Decimal, threshold: RegularizationThreshold) -> int:
    return 3 if amount >= threshold.manager_limit else 1


def quorum_met(order: RegularizationOrder) -> bool:
    threshold = _threshold_for(order.wallet.currency)
    needed = _required_approvals(order.amount, threshold)
    approved = order.approvals.filter(decision=RegularizationApproval.Decision.APPROVED).count()
    return approved >= needed


def _post_regularization(*, order: RegularizationOrder, by: str = "") -> RegularizationOrder:
    wallet = ClientWallet.objects.select_for_update().get(pk=order.wallet_id)
    movement = WalletMovement.objects.create(wallet=wallet, kind=WalletMovement.Kind.REGULARIZATION,
                                              amount=order.amount)
    ClientWallet.objects.filter(pk=wallet.pk).update(balance=F("balance") + order.amount)
    order.movement = movement
    order.status = FlowStatus.POSTED
    order.save(update_fields=["movement", "status", "updated_at"])
    audit_record(actor=by, action="caisses.regularization.post", entity_type="RegularizationOrder",
                 entity_id=str(order.pk), details={"amount": str(order.amount)})

    if order.ticket_id:
        from support.models import Ticket, TicketMessage
        ticket = Ticket.objects.select_for_update().filter(pk=order.ticket_id).first()
        if ticket:
            TicketMessage.objects.create(
                ticket=ticket, author_sub=by, author_role="",
                text=f"Crédit de régularisation de {order.amount} {wallet.currency} effectué. "
                     f"Motif : {order.reason or 'non précisé'}.",
                is_internal=False,
            )
            if ticket.status not in (Ticket.Status.RESOLU, Ticket.Status.REJETE):
                ticket.status = Ticket.Status.RESOLU
                ticket.resolved_at = timezone.now()
                ticket.save(update_fields=["status", "resolved_at"])
    return order


@transaction.atomic
def create_regularization_order(*, wallet_id: int, amount: Decimal | str, reason: str = "",
                                 ticket_id: int | None = None, idempotency_key: str,
                                 by: str = "") -> RegularizationOrder:
    amount = to_decimal(amount)
    if amount <= 0:
        raise ValidationFailed("Le montant de la régularisation doit être strictement positif.")

    rec = idempotency.begin(scope="caisses.regularization_order", key=idempotency_key,
                             params={"wallet": wallet_id, "amount": str(amount), "ticket": ticket_id}, by=by)

    wallet = ClientWallet.objects.select_for_update().filter(pk=wallet_id).first()
    if not wallet:
        raise NotFoundError("Portefeuille introuvable.")

    threshold = _threshold_for(wallet.currency)
    auto = amount < threshold.auto_limit
    order = RegularizationOrder.objects.create(
        wallet=wallet, ticket_id=ticket_id, amount=amount, reason=reason,
        status=FlowStatus.PENDING_VALIDATION, auto_validated=auto, idempotency_key=idempotency_key, created_by=by,
    )
    audit_record(actor=by, action="caisses.regularization_order.create", entity_type="RegularizationOrder",
                 entity_id=str(order.pk), details={"amount": str(amount), "auto_validated": auto,
                                                    "ticket": ticket_id})
    if auto:
        order = _post_regularization(order=order, by=by)

    response = serializers.regularization_order_row(order)
    idempotency.complete(rec, response=response, entity_type="RegularizationOrder", entity_id=str(order.pk))
    return order


def request_step_up_otp(*, order_id: int, approver_sub: str) -> RegularizationOtpChallenge:
    order = RegularizationOrder.objects.filter(pk=order_id).first()
    if not order:
        raise NotFoundError("Ordre de régularisation introuvable.")
    code = f"{secrets.randbelow(1_000_000):06d}"
    salt = secrets.token_hex(8)
    code_hash = hashlib.sha256(f"{salt}:{code}".encode()).hexdigest()
    challenge = RegularizationOtpChallenge.objects.create(
        order=order, approver_sub=approver_sub, code_hash=f"{salt}${code_hash}",
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
    logger.info("OTP régularisation (DEV) order=%s approver=%s code=%s", order_id, approver_sub, code)
    return challenge


def verify_step_up_otp(*, challenge_id: str, code: str) -> bool:
    challenge = RegularizationOtpChallenge.objects.filter(pk=challenge_id).first()
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
def approve(*, order_id: int, approver_sub: str, approver_role: str,
            otp_code: str | None = None) -> RegularizationOrder:
    order = RegularizationOrder.objects.select_for_update().filter(pk=order_id).first()
    if not order:
        raise NotFoundError("Ordre de régularisation introuvable.")
    if order.status in _RESOLVED_STATUSES:
        return order  # déjà résolu — idempotent, pas une erreur
    if RegularizationApproval.objects.filter(order=order, approver_sub=approver_sub).exists():
        return order  # déjà décidé par cet approbateur — no-op idempotent

    threshold = _threshold_for(order.wallet.currency)
    needed = _required_approvals(order.amount, threshold)
    role = get_role(approver_role)
    if needed >= 3 and not role.is_supervisor:
        raise ConflictError("Seuls les rôles superviseurs peuvent approuver ce montant.")

    step_up_required = needed >= 3 or role.mfa_step_up_required
    otp_verified_at = None
    if step_up_required:
        challenge = RegularizationOtpChallenge.objects.filter(
            order=order, approver_sub=approver_sub, verified_at__isnull=False,
        ).order_by("-created_at").first()
        if not challenge or not otp_code:
            raise StepUpRequiredError("Un code OTP vérifié est requis pour approuver cette régularisation.")
        otp_verified_at = challenge.verified_at

    RegularizationApproval.objects.create(
        order=order, approver_sub=approver_sub, approver_role=approver_role,
        decision=RegularizationApproval.Decision.APPROVED, otp_verified_at=otp_verified_at,
    )
    audit_record(actor=approver_sub, actor_role=approver_role, action="caisses.regularization_order.approve",
                 entity_type="RegularizationOrder", entity_id=str(order.pk),
                 details={"needed": needed, "amount": str(order.amount)})

    if quorum_met(order):
        order = _post_regularization(order=order, by=approver_sub)
    return order


@transaction.atomic
def reject(*, order_id: int, approver_sub: str, approver_role: str, reason: str = "") -> RegularizationOrder:
    order = RegularizationOrder.objects.select_for_update().filter(pk=order_id).first()
    if not order:
        raise NotFoundError("Ordre de régularisation introuvable.")
    if order.status in _RESOLVED_STATUSES:
        return order
    RegularizationApproval.objects.update_or_create(
        order=order, approver_sub=approver_sub,
        defaults={"approver_role": approver_role, "decision": RegularizationApproval.Decision.REJECTED},
    )
    order.status = FlowStatus.REJECTED
    order.save(update_fields=["status", "updated_at"])
    audit_record(actor=approver_sub, actor_role=approver_role, action="caisses.regularization_order.reject",
                 entity_type="RegularizationOrder", entity_id=str(order.pk), details={"reason": reason})
    return order
