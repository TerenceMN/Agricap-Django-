"""Machine à états et logique métier pour les tickets de support.
Toute action sensible passe ici (pas dans les vues) — même principe que
`agencies.maker_checker` et `transactions.services`."""
from __future__ import annotations

import re
import uuid

from django.utils import timezone

from common.exceptions import ConflictError, PermissionDeniedError, ValidationFailed

from . import sla
from .models import (
    MobileMoneyVerification, PendingFinancialAction, Ticket, TicketAuditLog, TicketMessage,
)

import logging

logger = logging.getLogger("agricap")


# ── Helpers ────────────────────────────────────────────────────────────────────

def _audit(ticket: Ticket, actor: str, action: str, payload: dict = None,
           ip: str = None) -> None:
    TicketAuditLog.objects.create(
        ticket=ticket, actor=actor, action=action,
        payload=payload or {}, ip_address=ip,
    )


def _sys_msg(ticket: Ticket, text: str, *, is_internal: bool = False,
             action_source: str = "") -> TicketMessage:
    return TicketMessage.objects.create(
        ticket=ticket, author_sub="system", author_role="system",
        text=text, is_internal=is_internal, action_source=action_source,
    )


def _sms(ticket: Ticket, text: str) -> None:
    try:
        from common.sms import send_sms_to_user
        send_sms_to_user(user_sub=ticket.user_id, message=text)
    except Exception:  # noqa: BLE001
        pass


def _priority_bump(p: str) -> str:
    order = [Ticket.Priority.FAIBLE, Ticket.Priority.NORMAL,
             Ticket.Priority.URGENT, Ticket.Priority.CRITIQUE]
    idx = order.index(p) if p in order else 1
    return order[min(idx + 1, 3)]


# ── Création améliorée ─────────────────────────────────────────────────────────

def create_ticket(*, user, category: str, subject: str, description: str,
                  priority: str = "normal", ip: str = None) -> Ticket:
    # Détection doublons
    dup = Ticket.find_duplicate(user.sub, category, description)
    if dup:
        raise ConflictError(
            f"Un ticket similaire est déjà ouvert (#{dup.pk} — {dup.public_id}). "
            f"Ajoutez un commentaire sur ce ticket plutôt qu'en créer un nouveau.",
            code="duplicate_ticket",
        )

    # Priorité automatique
    priority = Ticket.compute_auto_priority(description, priority, getattr(user, "role", ""))

    now = timezone.now()
    first_response_due, resolution_due = sla.compute_sla_deadlines(priority=priority, created_at=now)
    ticket = Ticket.objects.create(
        user=user, category=category, priority=priority,
        subject=subject, description=description,
        sla_first_response_due=first_response_due, sla_resolution_due=resolution_due,
    )

    _sys_msg(ticket, "Votre demande a été enregistrée. Un agent vous répondra dans les meilleurs délais.")
    _audit(ticket, user.sub, "ticket.create", {"category": category, "priority": priority}, ip)

    # Alerte superviseur sur mots-clés sensibles
    text_lower = description.lower()
    if any(k in text_lower for k in ("fraude", "bloqué", "bloque", "urgent")):
        logger.warning("TICKET PRIORITAIRE ticket=%s subject=%r user=%s", ticket.pk, subject, user.sub)

    return ticket


# ── Assign / Claim ─────────────────────────────────────────────────────────────

def assign_ticket(*, ticket: Ticket, agent_sub: str, actor_sub: str, ip: str = None) -> Ticket:
    if ticket.status in (Ticket.Status.RESOLU, Ticket.Status.REJETE):
        raise ConflictError("Impossible d'assigner un ticket clôturé.")
    prev = ticket.assigned_to_sub
    ticket.assigned_to_sub = agent_sub
    if ticket.status == Ticket.Status.OUVERT:
        ticket.status = Ticket.Status.EN_TRAITEMENT
    ticket.save(update_fields=["assigned_to_sub", "status"])
    _sys_msg(ticket, f"Ticket assigné à {agent_sub}.", is_internal=True)
    _audit(ticket, actor_sub, "ticket.assign", {"from": prev, "to": agent_sub}, ip)
    return ticket


def claim_ticket(*, ticket: Ticket, agent_sub: str, agent_role: str = "",
                 ip: str = None) -> Ticket:
    if ticket.status in (Ticket.Status.RESOLU, Ticket.Status.REJETE):
        raise ConflictError("Ce ticket est clôturé.")
    if ticket.assigned_to_sub and ticket.assigned_to_sub != agent_sub:
        raise ConflictError(f"Ce ticket est déjà pris en charge.")

    # Vérification de niveau (L1 ne prend pas L3, etc.)
    level_order = [Ticket.Level.L1, Ticket.Level.L2, Ticket.Level.L3]
    ticket_level_idx = level_order.index(ticket.level)
    # On déduit le niveau agent du rôle — convention: le rôle contient "l2"/"l3" ou
    # on regarde le role registry plus finement. Pour l'instant, on laisse passer
    # admin/manager sur tout niveau; L1 ne peut pas prendre L3.
    from rbac.role_registry import get_role as _get_role
    role_obj = _get_role(agent_role)
    is_admin = role_obj.disburse or role_obj.audit  # admin/manager/etc.
    if not is_admin and ticket_level_idx >= 2:
        raise ConflictError("Votre niveau d'habilitation ne permet pas de prendre en charge un ticket L3.")

    now = timezone.now()
    ticket.assigned_to_sub = agent_sub
    if not ticket.first_response_at:
        ticket.first_response_at = now
    if ticket.status in (Ticket.Status.OUVERT, Ticket.Status.ESCALADE):
        ticket.status = Ticket.Status.EN_TRAITEMENT
    ticket.save(update_fields=["assigned_to_sub", "first_response_at", "status"])

    sla_info = sla.SLA_MATRIX.get(ticket.priority, (24, 72))
    _sys_msg(
        ticket,
        f"Votre demande est maintenant prise en charge. "
        f"Temps de traitement estimé : {sla_info[1]} h.",
    )
    _audit(ticket, agent_sub, "ticket.claim", {"level": ticket.level}, ip)
    _sms(ticket, f"AGRICAP Support : votre ticket #{ticket.pk} est en cours de traitement.")
    return ticket


# ── Escalade enrichie ──────────────────────────────────────────────────────────

def escalate_ticket(*, ticket: Ticket, actor_sub: str, actor_role: str = "",
                    reason: str, ip: str = None) -> Ticket:
    if ticket.status in (Ticket.Status.RESOLU, Ticket.Status.REJETE):
        raise ConflictError("Impossible d'escalader un ticket clôturé.")
    if not reason or len(reason.strip()) < 20:
        raise ValidationFailed("Le motif d'escalade doit comporter au moins 20 caractères.")

    level_order = [Ticket.Level.L1, Ticket.Level.L2, Ticket.Level.L3]
    idx = level_order.index(ticket.level)
    if idx >= len(level_order) - 1:
        raise ConflictError("Ce ticket est déjà au niveau maximum (L3).")

    from rbac.role_registry import get_role as _get_role
    role_obj = _get_role(actor_role)
    is_admin = role_obj.disburse or role_obj.audit
    # Un L1 escalade vers L2; un L2 vers L3; seul un admin peut sauter un niveau
    if not is_admin and idx < 1 and level_order[idx + 1] == Ticket.Level.L3:
        raise ConflictError("Vous ne pouvez escalader que d'un seul niveau à la fois.")

    prev_level = ticket.level
    ticket.level = level_order[idx + 1]
    ticket.status = Ticket.Status.ESCALADE
    team = Ticket.ESCALATION_TEAMS.get(ticket.category, "Équipe Support")
    ticket.assigned_team = team
    ticket.assigned_to_sub = ""  # le niveau supérieur doit se l'attribuer via /claim/
    ticket.save(update_fields=["level", "status", "assigned_team", "assigned_to_sub"])

    new_sla = sla.SLA_MATRIX.get(ticket.priority, (24, 72))
    _sys_msg(
        ticket,
        f"Escaladé {prev_level}→{ticket.level} par {actor_sub}. Motif : \"{reason}\". "
        f"Routé vers : {team}.",
        is_internal=True,
    )
    _sys_msg(
        ticket,
        f"Votre dossier nécessite l'expertise de nos spécialistes. "
        f"Il a été transmis à notre équipe de niveau supérieur ({team}) "
        f"qui vous répondra sous {new_sla[1]} h.",
    )
    _audit(ticket, actor_sub, "ticket.escalate",
           {"from": prev_level, "to": ticket.level, "reason": reason, "team": team}, ip)
    _sms(ticket, f"AGRICAP Support : votre ticket #{ticket.pk} a été transmis à notre équipe spécialisée.")
    return ticket


# ── Résolution enrichie ────────────────────────────────────────────────────────

def resolve_ticket(*, ticket: Ticket, actor_sub: str, resolution_summary: str,
                   ip: str = None) -> Ticket:
    if ticket.status in (Ticket.Status.RESOLU, Ticket.Status.REJETE):
        raise ConflictError("Ce ticket est déjà clôturé.")
    if not resolution_summary or len(resolution_summary.strip()) < 30:
        raise ValidationFailed("Un résumé de résolution d'au moins 30 caractères est requis.")

    # Bloquer la clôture si anomalie financière non réglée
    if ticket.category in Ticket.FINANCIAL_CATEGORIES:
        has_anomaly = ticket.mm_verifications.filter(
            status=MobileMoneyVerification.Status.FOUND,
        ).exists()
        if has_anomaly:
            action_done = ticket.financial_actions.filter(
                status=PendingFinancialAction.Status.APPROVED,
            ).exists()
            if not action_done:
                raise ConflictError(
                    "Une anomalie financière a été confirmée mais la régularisation n'est pas encore "
                    "terminée. Completez le forçage de crédit avant de clôturer."
                )

    ticket.status = Ticket.Status.RESOLU
    ticket.resolved_at = timezone.now()
    ticket.save(update_fields=["status", "resolved_at"])

    _sys_msg(
        ticket,
        f"Votre demande {ticket.public_id} a été résolue : {resolution_summary}. "
        f"Si le problème persiste, répondez à ce message sous 72 h pour rouvrir le dossier "
        f"— passé ce délai, il sera archivé. Merci de noter votre expérience de 1 à 5 .",
    )
    TicketMessage.objects.create(
        ticket=ticket, author_sub=actor_sub, author_role="",
        text=resolution_summary, is_internal=False,
    )
    _audit(ticket, actor_sub, "ticket.resolve", {"summary": resolution_summary}, ip)
    _sms(ticket, f"AGRICAP Support : votre dossier {ticket.public_id} a été résolu. "
                 f"Notez votre expérience dans l'application.")
    return ticket


# ── Rejet structuré ────────────────────────────────────────────────────────────

_REJECT_CLIENT_MESSAGES = {
    Ticket.RejectType.DOUBLON: lambda t:
        f"Votre demande est identique au dossier {t.original_ticket.public_id if t.original_ticket else ''} "
        f"déjà en cours. Nous poursuivons le traitement sur ce dossier unique pour plus d'efficacité.",
    Ticket.RejectType.HORS_PERIMETRE: lambda _:
        "Votre demande ne relève pas du périmètre d'intervention de ce service. "
        "Veuillez contacter le service compétent.",
    Ticket.RejectType.INFO_INSUFFISANTES: lambda _:
        "Malgré nos relances, nous n'avons pas reçu les éléments nécessaires au traitement de votre dossier. "
        "Votre dossier est clos, mais vous pouvez en ouvrir un nouveau dès que vous disposez des informations.",
    Ticket.RejectType.FRAUDE: lambda _:
        "Votre demande a été transmise à notre service spécialisé pour analyse complémentaire.",
}


def reject_ticket(*, ticket: Ticket, actor_sub: str, reject_type: str, reason: str,
                  original_ticket_id: int = None, ip: str = None) -> Ticket:
    if ticket.status in (Ticket.Status.RESOLU, Ticket.Status.REJETE):
        raise ConflictError("Ce ticket est déjà clôturé.")
    if not reason or not reason.strip():
        raise ValidationFailed("Un motif de rejet est requis.")
    valid_types = [v for v, _ in Ticket.RejectType.choices]
    if reject_type not in valid_types:
        raise ValidationFailed(f"Type de rejet invalide. Valeurs : {', '.join(valid_types)}")

    if reject_type == Ticket.RejectType.DOUBLON and original_ticket_id:
        orig = Ticket.objects.filter(pk=original_ticket_id).first()
        if orig:
            ticket.original_ticket = orig

    ticket.status = Ticket.Status.REJETE
    ticket.reject_type = reject_type
    ticket.rejected_reason = reason
    ticket.resolved_at = timezone.now()
    ticket.save(update_fields=["status", "reject_type", "rejected_reason", "resolved_at", "original_ticket_id"])

    client_msg_fn = _REJECT_CLIENT_MESSAGES.get(reject_type)
    client_msg = client_msg_fn(ticket) if client_msg_fn else f"Votre ticket a été rejeté. Motif : {reason}"
    _sys_msg(ticket, client_msg)

    # Fraude → alerte interne Conformité
    if reject_type == Ticket.RejectType.FRAUDE:
        _sys_msg(
            ticket,
            f"ALERTE FRAUDE SUSPECTÉE — ticket #{ticket.pk} rejeté par {actor_sub}. "
            f"Motif : {reason}. Transmission automatique à la Conformité.",
            is_internal=True,
        )
        logger.warning("FRAUDE_SUSPECTEE ticket=%s actor=%s reason=%r", ticket.pk, actor_sub, reason)

    _audit(ticket, actor_sub, "ticket.reject",
           {"reject_type": reject_type, "reason": reason}, ip)
    _sms(ticket, f"AGRICAP Support : votre dossier {ticket.public_id} a été traité. "
                 f"Consultez l'application pour les détails.")
    return ticket


# ── Vérification Mobile Money ──────────────────────────────────────────────────

_MM_REF_PATTERN = re.compile(r'\b(AG-\d+|MP\d+|OM\d+|REF[\w\d]+)\b', re.I)
_MM_OPERATOR_HINTS = {
    "AG-": MobileMoneyVerification.Operator.AIRTEL,
    "MP": MobileMoneyVerification.Operator.MPESA,
    "OM": MobileMoneyVerification.Operator.ORANGE,
}


def _guess_operator(ref: str) -> str:
    for prefix, op in _MM_OPERATOR_HINTS.items():
        if ref.upper().startswith(prefix):
            return op
    return MobileMoneyVerification.Operator.AIRTEL


def _mock_mm_gateway(operator: str, ref: str) -> dict:
    """Sandbox : retourne found_operator_side pour les refs connues (contient '889900'),
    already_credited pour les refs contenant 'OK', not_found sinon."""
    ref_upper = ref.upper()
    if "889900" in ref_upper or "TEST_FOUND" in ref_upper:
        return {
            "verdict": MobileMoneyVerification.Status.FOUND,
            "amount": 80000, "currency": "CDF",
            "operator": operator, "date": timezone.now().isoformat(),
        }
    if "OK" in ref_upper or "TEST_OK" in ref_upper:
        return {
            "verdict": MobileMoneyVerification.Status.CREDITED,
            "amount": 50000, "currency": "CDF",
        }
    if "FAIL" in ref_upper:
        return {"verdict": MobileMoneyVerification.Status.FAILED}
    return {"verdict": MobileMoneyVerification.Status.NOT_FOUND}


_MM_INTERNAL_MESSAGES = {
    MobileMoneyVerification.Status.FOUND: lambda op, ref, r:
        f"Transaction {ref} confirmée chez {op} : {r.get('amount', '?')} {r.get('currency', '')} "
        f"le {r.get('date', '?')}. Non créditée dans notre système. **Anomalie confirmée.**",
    MobileMoneyVerification.Status.NOT_FOUND: lambda op, ref, _:
        f"Aucune transaction {ref} trouvée chez {op}. "
        f"Demander au client une capture du SMS de confirmation.",
    MobileMoneyVerification.Status.CREDITED: lambda op, ref, r:
        f"Transaction {ref} déjà créditée. Le solde du client est correct.",
    MobileMoneyVerification.Status.FAILED: lambda op, ref, _:
        f"L'API {op} ne répond pas. Nouvelle tentative automatique dans 15 min (3 max).",
}


def verify_mobile_money(*, ticket: Ticket, actor_sub: str,
                         transaction_ref: str = None, ip: str = None) -> MobileMoneyVerification:
    if ticket.status in (Ticket.Status.RESOLU, Ticket.Status.REJETE):
        raise ConflictError("Impossible de lancer une vérification sur un ticket clôturé.")

    # Extraction de la référence si non fournie
    if not transaction_ref:
        matches = _MM_REF_PATTERN.findall(ticket.description or "")
        if not matches:
            raise ValidationFailed(
                "Aucune référence de transaction détectée dans la description. "
                "Fournissez la référence via le champ `transactionRef`.",
                code="ref_required",
            )
        transaction_ref = matches[0]

    operator = _guess_operator(transaction_ref)
    verif = MobileMoneyVerification.objects.create(
        ticket=ticket, operator=operator, transaction_ref=transaction_ref,
    )

    _sys_msg(
        ticket,
        f"Vérification lancée auprès de {operator} pour la transaction {transaction_ref}…",
        is_internal=True,
    )
    _audit(ticket, actor_sub, "ticket.mm_verify_start",
           {"ref": transaction_ref, "operator": operator, "verif_id": verif.pk}, ip)

    # Appel gateway (mock synchrone — en prod ce serait une tâche Celery)
    try:
        result = _mock_mm_gateway(operator, transaction_ref)
        verdict = result.get("verdict", MobileMoneyVerification.Status.FAILED)
        verif.status = verdict
        verif.raw_response = result
        if verdict != MobileMoneyVerification.Status.PENDING:
            verif.verified_at = timezone.now()
        if result.get("amount"):
            verif.amount = result["amount"]
            verif.currency = result.get("currency", "CDF")
        verif.save()
    except Exception as exc:  # noqa: BLE001
        logger.error("MM gateway error ticket=%s ref=%s err=%s", ticket.pk, transaction_ref, exc)
        verif.status = MobileMoneyVerification.Status.FAILED
        verif.save(update_fields=["status"])
        verdict = MobileMoneyVerification.Status.FAILED

    msg_fn = _MM_INTERNAL_MESSAGES.get(verdict)
    if msg_fn:
        _sys_msg(ticket, msg_fn(operator, transaction_ref, verif.raw_response), is_internal=True)

    _audit(ticket, actor_sub, "ticket.mm_verify_done",
           {"verif_id": verif.pk, "verdict": verdict}, ip)
    return verif


# ── Force-crédit (maker-checker) ───────────────────────────────────────────────

def initiate_force_credit(*, ticket: Ticket, initiator_sub: str,
                           idempotency_key: str, ip: str = None) -> PendingFinancialAction:
    # Prérequis : une vérification confirme l'anomalie chez l'opérateur
    confirmed = ticket.mm_verifications.filter(
        status=MobileMoneyVerification.Status.FOUND,
    ).first()
    if not confirmed:
        raise PermissionDeniedError(
            "Le forçage de crédit n'est possible qu'après une vérification MM confirmant "
            "l'anomalie (statut found_operator_side)."
        )

    # Idempotence
    existing = PendingFinancialAction.objects.filter(idempotency_key=idempotency_key).first()
    if existing:
        return existing

    # Bloquer si déjà une action pending sur ce ticket
    if ticket.financial_actions.filter(status=PendingFinancialAction.Status.PENDING).exists():
        raise ConflictError("Une régularisation est déjà en cours pour ce ticket.")

    action = PendingFinancialAction.objects.create(
        ticket=ticket,
        amount=confirmed.amount or 0,
        currency=confirmed.currency,
        initiated_by=initiator_sub,
        idempotency_key=idempotency_key,
    )

    _sys_msg(
        ticket,
        f"Régularisation de {action.amount} {action.currency} initiée par {initiator_sub}. "
        f"En attente d'approbation par un second administrateur.",
        is_internal=True,
    )
    _audit(ticket, initiator_sub, "ticket.force_credit.initiate",
           {"action_id": action.pk, "amount": str(action.amount), "currency": action.currency}, ip)
    return action


def approve_force_credit(*, ticket: Ticket, action: PendingFinancialAction,
                          approver_sub: str, ip: str = None) -> PendingFinancialAction:
    if action.status != PendingFinancialAction.Status.PENDING:
        raise ConflictError("Cette action n'est plus en attente d'approbation.")
    if action.initiated_by == approver_sub:
        raise ConflictError("Le même administrateur ne peut pas approuver sa propre initiation (principe maker-checker).")

    # Simulation de l'écriture comptable
    accounting_ref = f"ADJ-{uuid.uuid4().hex[:8].upper()}"
    action.approved_by = approver_sub
    action.status = PendingFinancialAction.Status.APPROVED
    action.accounting_ref = accounting_ref
    action.decided_at = timezone.now()
    action.save(update_fields=["approved_by", "status", "accounting_ref", "decided_at"])

    # Clôturer le ticket
    ticket.status = Ticket.Status.RESOLU
    ticket.resolved_at = timezone.now()
    ticket.save(update_fields=["status", "resolved_at"])

    _sys_msg(
        ticket,
        f"Régularisation approuvée par {approver_sub}. "
        f"Écriture comptable {accounting_ref} générée.",
        is_internal=True,
    )
    client_first_name = ticket.user.full_name.split()[0] if ticket.user.full_name else "Client"
    _sys_msg(
        ticket,
        f"Bonne nouvelle {client_first_name} ! Nous avons identifié et corrigé l'anomalie. "
        f"Votre compte a été crédité de {action.amount} {action.currency}. "
        f"Nous vous prions de nous excuser pour ce désagrément. "
        f"Votre nouveau solde est disponible dans votre application.",
    )
    _audit(ticket, approver_sub, "ticket.force_credit.approve",
           {"action_id": action.pk, "accounting_ref": accounting_ref}, ip)
    _sms(
        ticket,
        f"AGRICAP : Votre compte a été crédité de {action.amount} {action.currency}. "
        f"Réf : {accounting_ref}.",
    )
    return action


def reject_force_credit(*, ticket: Ticket, action: PendingFinancialAction,
                         approver_sub: str, note: str, ip: str = None) -> PendingFinancialAction:
    if action.status != PendingFinancialAction.Status.PENDING:
        raise ConflictError("Cette action n'est plus en attente d'approbation.")
    if action.initiated_by == approver_sub:
        raise ConflictError("Le même administrateur ne peut pas rejeter sa propre initiation.")
    action.approved_by = approver_sub
    action.status = PendingFinancialAction.Status.REJECTED
    action.rejection_note = note
    action.decided_at = timezone.now()
    action.save(update_fields=["approved_by", "status", "rejection_note", "decided_at"])

    _sys_msg(
        ticket,
        f"Régularisation rejetée par {approver_sub}. Motif : {note}",
        is_internal=True,
    )
    _audit(ticket, approver_sub, "ticket.force_credit.reject",
           {"action_id": action.pk, "note": note}, ip)
    return action


# ── En attente client ──────────────────────────────────────────────────────────

def await_client(*, ticket: Ticket, actor_sub: str, question: str, ip: str = None) -> Ticket:
    """Bascule le ticket en 'en-attente-client', pause le SLA, planifie les relances."""
    ALLOWED = (Ticket.Status.EN_TRAITEMENT, Ticket.Status.ESCALADE)
    if ticket.status not in ALLOWED:
        raise ConflictError(f"Impossible de basculer depuis l'état '{ticket.status}'.")
    if not ticket.assigned_to_sub or ticket.assigned_to_sub != actor_sub:
        raise ConflictError("Seul l'agent assigné peut mettre un ticket en attente client.")
    if not question or len(question.strip()) < 15:
        raise ValidationFailed("La question au client doit comporter au moins 15 caractères.")

    now = timezone.now()
    ticket.status = Ticket.Status.EN_ATTENTE_CLIENT
    ticket.awaiting_since = now
    ticket.await_client_question = question.strip()
    ticket.sla_paused_at = now
    ticket.save(update_fields=["status", "awaiting_since", "await_client_question", "sla_paused_at"])

    first = ticket.user.full_name.split()[0] if ticket.user.full_name else "Client"
    _sys_msg(
        ticket,
        f"Bonjour {first}, pour poursuivre le traitement de votre dossier {ticket.public_id}, "
        f"nous avons besoin de : {question.strip()}. Vous pouvez répondre directement à ce message.",
        action_source="await_client",
    )
    _audit(ticket, actor_sub, "ticket.await_client", {"question": question.strip()}, ip)

    # Planification Celery (dégradation gracieuse si non configuré)
    try:
        from .tasks import autoclose_await_client_j7, remind_await_client_j2, remind_await_client_j5
        t2 = remind_await_client_j2.apply_async(args=[ticket.pk], countdown=2 * 24 * 3600)
        t5 = remind_await_client_j5.apply_async(args=[ticket.pk], countdown=5 * 24 * 3600)
        t7 = autoclose_await_client_j7.apply_async(args=[ticket.pk], countdown=7 * 24 * 3600)
        ticket.await_task_j2_id = t2.id
        ticket.await_task_j5_id = t5.id
        ticket.await_task_j7_id = t7.id
        ticket.save(update_fields=["await_task_j2_id", "await_task_j5_id", "await_task_j7_id"])
    except Exception as exc:  # noqa: BLE001
        logger.warning("Celery unavailable for await_client tasks: %s", exc)

    return ticket


def resume_from_client(*, ticket: Ticket, actor_sub: str, ip: str = None) -> Ticket:
    """Reprise automatique quand le client poste un message sur un ticket en-attente-client."""
    if ticket.status != Ticket.Status.EN_ATTENTE_CLIENT:
        return ticket

    now = timezone.now()
    if ticket.sla_paused_at:
        pause_secs = int((now - ticket.sla_paused_at).total_seconds())
        ticket.sla_accumulated_pause_seconds += pause_secs
        ticket.sla_paused_at = None

    ticket.status = Ticket.Status.EN_TRAITEMENT
    ticket.awaiting_since = None

    # Annulation des tâches Celery
    for task_id in [ticket.await_task_j2_id, ticket.await_task_j5_id, ticket.await_task_j7_id]:
        if task_id:
            try:
                from celery import current_app
                current_app.control.revoke(task_id, terminate=False)
            except Exception:  # noqa: BLE001
                pass
    ticket.await_task_j2_id = ""
    ticket.await_task_j5_id = ""
    ticket.await_task_j7_id = ""

    ticket.save(update_fields=[
        "status", "awaiting_since", "sla_paused_at",
        "sla_accumulated_pause_seconds", "await_task_j2_id", "await_task_j5_id", "await_task_j7_id",
    ])

    _sys_msg(ticket, "Le client a répondu. Le dossier reprend son cours.", is_internal=True,
             action_source="resume_from_client")
    _audit(ticket, actor_sub, "ticket.resume_from_client", {}, ip)
    logger.info("TICKET_RESUMED ticket=%s client=%s", ticket.pk, actor_sub)
    return ticket


# ── availableActions (calculées serveur, pilotent les boutons) ─────────────────

def compute_available_actions(ticket: Ticket, requester_sub: str = "",
                               requester_role: str = "", is_staff: bool = False) -> list[str]:
    """Actions RÉELLEMENT autorisées pour ce requêteur. Le frontend n'affiche QUE ces boutons."""
    from rbac.role_registry import get_role as _gr
    try:
        role_obj = _gr(requester_role)
        is_admin = bool(role_obj.disburse or role_obj.audit)
    except Exception:
        is_admin = False

    is_assigned_to_me = ticket.assigned_to_sub and ticket.assigned_to_sub == requester_sub
    is_closed = ticket.status in (Ticket.Status.RESOLU, Ticket.Status.REJETE)

    # ── Tickets clôturés ──────────────────────────────────────────────────────
    if is_closed:
        if ticket.status == Ticket.Status.RESOLU and ticket.resolved_at:
            hours = (timezone.now() - ticket.resolved_at).total_seconds() / 3600
            if hours < 72:
                return ["reopen"] if not is_staff else ["add_note", "reopen"]
        return ["add_note"] if is_staff else []

    # ── En attente client ─────────────────────────────────────────────────────
    if ticket.status == Ticket.Status.EN_ATTENTE_CLIENT:
        return (["close", "reject", "add_note"] if is_staff else [])

    # ── Ouvert / Escaladé non assigné ────────────────────────────────────────
    if ticket.status in (Ticket.Status.OUVERT, Ticket.Status.ESCALADE):
        if not is_staff:
            return []
        actions = ["claim"]
        if is_admin:
            actions.append("reassign")
        return actions

    # ── En traitement ─────────────────────────────────────────────────────────
    if ticket.status == Ticket.Status.EN_TRAITEMENT:
        if not is_staff:
            return []
        if not ticket.assigned_to_sub:
            result = ["claim"]
            if is_admin:
                result.append("reassign")
            return result

        if not is_assigned_to_me and not is_admin:
            return ["add_note"]

        actions = []
        if ticket.category == Ticket.Category.MOBILE_MONEY:
            has_anomaly = ticket.mm_verifications.filter(
                status=MobileMoneyVerification.Status.FOUND,
            ).exists()
            if not has_anomaly:
                no_pending = not ticket.mm_verifications.filter(
                    status=MobileMoneyVerification.Status.PENDING,
                ).exists()
                if no_pending:
                    actions.append("verify_mobile_money")
            elif is_admin:
                has_action = ticket.financial_actions.filter(
                    status__in=[PendingFinancialAction.Status.PENDING,
                                PendingFinancialAction.Status.APPROVED],
                ).exists()
                if not has_action:
                    actions.append("force_credit")

        if is_assigned_to_me or is_admin:
            actions.extend(["await_client", "add_note", "reply_public"])
            level_order = [Ticket.Level.L1, Ticket.Level.L2, Ticket.Level.L3]
            if level_order.index(ticket.level) < 2:
                actions.append("escalate")
            actions.extend(["close", "reject"])
        else:
            actions.append("add_note")
            if is_admin:
                actions.append("reassign")

        return actions

    return []


# ── suggestedActions (rétrocompatibilité) ─────────────────────────────────────

def compute_suggested_actions(ticket: Ticket) -> list[str]:
    """Calcule les actions suggérées selon l'état du ticket — pilote l'affichage conditionnel
    des boutons côté frontend."""
    if ticket.status in (Ticket.Status.RESOLU, Ticket.Status.REJETE):
        return ["rate"] if ticket.satisfaction_rating is None else []

    actions = []
    if ticket.category == Ticket.Category.MOBILE_MONEY:
        verified = ticket.mm_verifications.filter(
            status=MobileMoneyVerification.Status.FOUND,
        ).exists()
        if not verified:
            has_pending_verif = ticket.mm_verifications.filter(
                status=MobileMoneyVerification.Status.PENDING,
            ).exists()
            if not has_pending_verif:
                actions.append("verify_mobile_money")
        else:
            action_pending = ticket.financial_actions.filter(
                status__in=[PendingFinancialAction.Status.PENDING,
                            PendingFinancialAction.Status.APPROVED],
            ).exists()
            if not action_pending:
                actions.append("force_credit")

    if not ticket.assigned_to_sub:
        actions.append("claim")

    level_order = [Ticket.Level.L1, Ticket.Level.L2, Ticket.Level.L3]
    if ticket.status not in (Ticket.Status.RESOLU, Ticket.Status.REJETE):
        if level_order.index(ticket.level) < 2:
            actions.append("escalate")
        actions.append("resolve")
        actions.append("reject")

    return actions
