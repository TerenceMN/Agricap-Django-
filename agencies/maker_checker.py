"""Workflow maker-checker + code de vérification pour les actions sensibles de cycle de
vie d'agence (suspend/close/unlock_temporary/reopen) — même principe que le
multi-signature de `transactions.services` (demande → code envoyé au checker → vérification
→ approbation), appliqué ici à un flux à DEUX acteurs distincts au lieu d'un quorum."""
from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from audit.services import record as audit_record
from common.exceptions import ConflictError, NotFoundError, StepUpRequiredError, ValidationFailed
from rbac.role_registry import get_role

from . import services as agency_services
from .models import ActionApproverConfig, Agency, AgencyActionChallenge, AgencyActionRequest

logger = logging.getLogger("agricap")

_CODE_TTL_MINUTES = 5
_DOCUMENT_REQUIRED_ACTIONS = (AgencyActionRequest.ActionType.UNLOCK_TEMPORARY, AgencyActionRequest.ActionType.REOPEN)


@transaction.atomic
def request_agency_action(*, agency: Agency, action_type: str, reason: str, document=None,
                           by: str = "") -> AgencyActionRequest:
    valid_actions = {c for c, _ in AgencyActionRequest.ActionType.choices}
    if action_type not in valid_actions:
        raise ValidationFailed(f"Action inconnue : {action_type}.")
    if not reason or not reason.strip():
        raise ValidationFailed("Une justification écrite est requise.")
    if action_type in _DOCUMENT_REQUIRED_ACTIONS and not document:
        raise ValidationFailed("Un document justificatif est requis pour cette action.")
    if agency.action_requests.filter(status=AgencyActionRequest.Status.PENDING_APPROVAL).exists():
        raise ConflictError("Une action est déjà en attente d'approbation pour cette agence.")

    req = AgencyActionRequest.objects.create(
        agency=agency, action_type=action_type, reason=reason, document=document, requested_by=by,
    )
    audit_record(actor=by, action="agency_action_request.create", entity_type="AgencyActionRequest",
                 entity_id=str(req.pk), details={"agency": agency.code, "actionType": action_type})
    return req


def _check_designated_approver(action_type: str, approver_sub: str) -> None:
    """Lève ConflictError si des approbateurs désignés sont configurés pour cette action
    et que approver_sub n'en fait pas partie. En mode DEBUG, log un warning sans bloquer."""
    from django.conf import settings as _s
    from .models import ActionApproverConfig
    designated = ActionApproverConfig.objects.filter(scope="agency", action_type=action_type)
    if designated.exists() and not designated.filter(approver_sub=approver_sub).exists():
        if getattr(_s, "DEBUG", False):
            logger.warning("APPROBATEUR NON DÉSIGNÉ (autorisé en mode DEBUG) action=%s approver=%s",
                           action_type, approver_sub)
            return
        raise ConflictError("Vous n'êtes pas désigné comme approbateur pour cette action.")


def cancel_agency_action(*, action_request: AgencyActionRequest, requester_sub: str) -> AgencyActionRequest:
    """Annulation par le maker lui-même — permet de soumettre une nouvelle demande sur la même agence."""
    if action_request.status != AgencyActionRequest.Status.PENDING_APPROVAL:
        raise ConflictError("Seules les demandes en attente peuvent être annulées.")
    if action_request.requested_by != requester_sub:
        raise ConflictError("Seul le demandeur peut annuler sa propre demande.")
    action_request.status = AgencyActionRequest.Status.REJECTED
    action_request.approved_by = requester_sub
    action_request.decided_at = timezone.now()
    action_request.rejection_note = "Annulée par le demandeur."
    action_request.save(update_fields=["status", "approved_by", "decided_at", "rejection_note"])
    audit_record(actor=requester_sub, action="agency_action_request.cancel", entity_type="AgencyActionRequest",
                 entity_id=str(action_request.pk))
    return action_request


def request_approval_code(*, action_request: AgencyActionRequest, approver_sub: str) -> tuple:
    """Retourne (challenge, sms_sent: bool)."""
    from django.conf import settings as _s
    if action_request.status != AgencyActionRequest.Status.PENDING_APPROVAL:
        raise ConflictError("Cette demande n'est plus en attente d'approbation.")
    if approver_sub == action_request.requested_by and not getattr(_s, "DEBUG", False):
        raise ConflictError("Le demandeur ne peut pas approuver sa propre demande (principe maker-checker).")
    if approver_sub == action_request.requested_by:
        logger.warning("MAKER=CHECKER (autorisé en mode DEBUG) request=%s sub=%s", action_request.pk, approver_sub)
    _check_designated_approver(action_request.action_type, approver_sub)

    code = f"{secrets.randbelow(1_000_000):06d}"
    salt = secrets.token_hex(8)
    code_hash = hashlib.sha256(f"{salt}:{code}".encode()).hexdigest()
    challenge = AgencyActionChallenge.objects.create(
        request=action_request, approver_sub=approver_sub, code_hash=f"{salt}${code_hash}",
        expires_at=timezone.now() + timedelta(minutes=_CODE_TTL_MINUTES),
    )
    try:
        from django.conf import settings
        from django.core.mail import send_mail
        send_mail(
            "Code d'approbation AGRICAP", f"Votre code d'approbation : {code}",
            getattr(settings, "DEFAULT_FROM_EMAIL", "no-reply@agricap.local"), [approver_sub],
            fail_silently=True,
        )
    except Exception:  # noqa: BLE001
        pass
    sms_sent = False
    try:
        from common.sms import send_sms, send_sms_to_user
        msg = f"Code d'approbation AGRICAP : {code}"
        # Priorité 1 : téléphone stocké dans ActionApproverConfig
        cfg = ActionApproverConfig.objects.filter(
            scope="agency", action_type=action_request.action_type, approver_sub=approver_sub,
        ).first()
        if cfg and cfg.approver_phone:
            logger.info("SMS via phone ActionApproverConfig request=%s approver=%s phone=%s",
                        action_request.pk, approver_sub, cfg.approver_phone)
            sms_sent = send_sms(phone=cfg.approver_phone, message=msg)
        else:
            # Priorité 2 : téléphone dans FintechUser (résolution par sub)
            logger.info("SMS via FintechUser request=%s approver=%s (config phone absent)",
                        action_request.pk, approver_sub)
            sms_sent = send_sms_to_user(user_sub=approver_sub, message=msg)
        if sms_sent:
            logger.info("SMS envoyé request=%s approver=%s", action_request.pk, approver_sub)
        else:
            logger.warning("SMS non envoyé request=%s approver=%s — numéro absent ou API KO",
                           action_request.pk, approver_sub)
    except Exception as exc:  # noqa: BLE001
        logger.warning("SMS exception request=%s approver=%s err=%s", action_request.pk, approver_sub, exc)
    logger.info("Code d'approbation agence (DEV) request=%s approver=%s code=%s sms_sent=%s",
                action_request.pk, approver_sub, code, sms_sent)
    return challenge, sms_sent


def notify_approvers_with_code(action_request: AgencyActionRequest) -> list:
    """Génère un code OTP et l'envoie par SMS à chaque approbateur désigné.
    Appelé automatiquement à la création de la demande."""
    results = []
    for cfg in ActionApproverConfig.objects.filter(scope="agency", action_type=action_request.action_type):
        try:
            challenge, sms_sent = request_approval_code(
                action_request=action_request, approver_sub=cfg.approver_sub,
            )
            results.append({"approverSub": cfg.approver_sub, "challengeId": challenge.pk, "smsSent": sms_sent})
        except Exception as exc:  # noqa: BLE001
            logger.warning("notify_approvers_with_code request=%s approver=%s err=%s",
                           action_request.pk, cfg.approver_sub, exc)
    return results


def find_pending_challenge(*, action_request: AgencyActionRequest, approver_sub: str):
    """Retourne le dernier challenge non expiré et non vérifié pour cet approbateur, ou None."""
    return AgencyActionChallenge.objects.filter(
        request=action_request,
        approver_sub=approver_sub,
        verified_at__isnull=True,
        expires_at__gt=timezone.now(),
    ).order_by("-created_at").first()


def verify_approval_code(*, challenge_id: str, code: str) -> bool:
    challenge = AgencyActionChallenge.objects.filter(pk=challenge_id).first()
    if not challenge:
        raise NotFoundError("Challenge introuvable.")
    if challenge.verified_at:
        return True
    if challenge.attempts >= challenge.max_attempts:
        raise ConflictError("Nombre maximal de tentatives dépassé.")
    if timezone.now() > challenge.expires_at:
        raise ConflictError("Code expiré.")
    salt, _, expected_hash = challenge.code_hash.partition("$")
    ok = hashlib.sha256(f"{salt}:{code}".encode()).hexdigest() == expected_hash
    challenge.attempts += 1
    if ok:
        challenge.verified_at = timezone.now()
    challenge.save(update_fields=["attempts", "verified_at"])
    return ok


@transaction.atomic
def approve_agency_action(*, action_request: AgencyActionRequest, approver_sub: str, approver_role: str,
                           code: str | None = None) -> AgencyActionRequest:
    from django.conf import settings as _s
    if action_request.status != AgencyActionRequest.Status.PENDING_APPROVAL:
        return action_request  # déjà décidée — idempotent, pas une erreur (double-clic)
    if approver_sub == action_request.requested_by and not getattr(_s, "DEBUG", False):
        raise ConflictError("MAKER_CHECKER_VIOLATION : le demandeur ne peut pas approuver sa propre demande.")
    _check_designated_approver(action_request.action_type, approver_sub)
    role = get_role(approver_role)
    if not role.validate:
        raise ConflictError("Rôle non autorisé à approuver une action d'agence.")

    challenge = AgencyActionChallenge.objects.filter(
        request=action_request, approver_sub=approver_sub, verified_at__isnull=False,
    ).order_by("-created_at").first()
    if not challenge or not code:
        raise StepUpRequiredError("Un code de vérification vérifié est requis pour approuver cette action.")

    agency = action_request.agency
    if action_request.action_type == AgencyActionRequest.ActionType.SUSPEND:
        agency_services.suspend(agency=agency, reason=action_request.reason, by=approver_sub)
    elif action_request.action_type == AgencyActionRequest.ActionType.CLOSE:
        agency_services.close(agency=agency, reason=action_request.reason, by=approver_sub)
    elif action_request.action_type == AgencyActionRequest.ActionType.UNLOCK_TEMPORARY:
        agency_services.unlock_temporary(agency=agency, reason=action_request.reason,
                                          document=action_request.document, by=approver_sub)
    elif action_request.action_type == AgencyActionRequest.ActionType.REOPEN:
        agency_services.reopen(agency=agency, reason=action_request.reason,
                                document=action_request.document, by=approver_sub)

    action_request.status = AgencyActionRequest.Status.EXECUTED
    action_request.approved_by = approver_sub
    action_request.decided_at = timezone.now()
    action_request.save(update_fields=["status", "approved_by", "decided_at"])
    audit_record(actor=approver_sub, actor_role=approver_role, action="agency_action_request.approve",
                 entity_type="AgencyActionRequest", entity_id=str(action_request.pk))
    return action_request


@transaction.atomic
def reject_agency_action(*, action_request: AgencyActionRequest, approver_sub: str, note: str = "") \
        -> AgencyActionRequest:
    if action_request.status != AgencyActionRequest.Status.PENDING_APPROVAL:
        raise ConflictError("Cette demande n'est plus en attente d'approbation.")
    if approver_sub == action_request.requested_by:
        raise ConflictError("Le demandeur ne peut pas rejeter sa propre demande.")
    action_request.status = AgencyActionRequest.Status.REJECTED
    action_request.approved_by = approver_sub
    action_request.decided_at = timezone.now()
    action_request.rejection_note = note
    action_request.save(update_fields=["status", "approved_by", "decided_at", "rejection_note"])
    audit_record(actor=approver_sub, action="agency_action_request.reject", entity_type="AgencyActionRequest",
                 entity_id=str(action_request.pk), details={"note": note})
    return action_request
