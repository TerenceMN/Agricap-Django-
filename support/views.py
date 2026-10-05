"""API support (tickets CRM + conversations investisseur/gestionnaire)."""
from __future__ import annotations

from datetime import timedelta

from django.core.cache import cache
from django.db.models import Avg, Count, Q
from django.utils import timezone
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from accounts.permissions import IsStaff
from accounts.services import UserDisplayService
from common.exceptions import ConflictError, NotFoundError, PermissionDeniedError, ValidationFailed
from rbac.permissions import HasCapability
from rbac.role_registry import get_role

from . import sla, workflow
from .models import (
    Conversation, Message, MobileMoneyVerification, PendingFinancialAction,
    Ticket, TicketAuditLog, TicketMessage,
)


def _serialize_messages(qs) -> list[dict]:
    """Sérialise une queryset de TicketMessage — auteur résolu via UserDisplayService."""
    rows = list(qs)
    subs = {m.author_sub for m in rows}
    resolved = UserDisplayService.resolve_many(subs)
    result = []
    for m in rows:
        info = resolved.get(m.author_sub, UserDisplayService.SYSTEM)
        result.append({
            "id": m.pk,
            "author": {
                "displayName": info["displayName"],
                "initials": info.get("initials", "??"),
                "role": info["role"],
                "isSystem": info.get("isSystem", False),
            },
            "authorSub": m.author_sub,
            "authorName": info["displayName"],
            "authorRole": m.author_role or info["role"],
            "text": m.text,
            "isInternal": m.is_internal,
            "createdAt": m.created_at.isoformat(),
            "meta": {"actionSource": m.action_source, "simulated": False},
        })
    return result

TICKET_REOPEN_DAYS = 7
RATING_WINDOW_DAYS = 7


# ── Sérialiseurs ───────────────────────────────────────────────────────────────

def _ticket_row(t: Ticket, *, include_messages: bool = False,
                is_staff: bool = True, requester_sub: str = "",
                requester_role: str = "") -> dict:
    assignee_info = None
    if t.assigned_to_sub:
        info = UserDisplayService.resolve(t.assigned_to_sub)
        assignee_info = {
            "sub": t.assigned_to_sub,
            "displayName": info["displayName"],
            "role": info["role"],
        }

    row = {
        "id": t.pk,
        "publicId": t.public_id,
        "category": t.category,
        "priority": t.priority,
        "status": t.status,
        "level": t.level,
        "assignedTo": t.assigned_to_sub,
        "assignee": assignee_info,
        "assignedTeam": t.assigned_team,
        "subject": t.subject,
        "description": t.description,
        "createdAt": t.created_at.isoformat(),
        "clientName": t.user.full_name or t.user.email,
        "clientSub": t.user_id,
        "waitingOn": t.waiting_on,
        "awaitingSince": t.awaiting_since.isoformat() if t.awaiting_since else None,
        "rejectType": t.reject_type,
        "slaFirstResponseDue": t.sla_first_response_due.isoformat() if t.sla_first_response_due else None,
        "slaResolutionDue": t.sla_resolution_due.isoformat() if t.sla_resolution_due else None,
        "slaBreachedFirstResponse": t.sla_breached_first_response,
        "slaBreachedResolution": t.sla_breached_resolution,
        "firstResponseAt": t.first_response_at.isoformat() if t.first_response_at else None,
        "resolvedAt": t.resolved_at.isoformat() if t.resolved_at else None,
        "rejectedReason": t.rejected_reason,
        "reopenedCount": t.reopened_count,
        "satisfactionRating": t.satisfaction_rating,
        "satisfactionComment": t.satisfaction_comment,
        "availableActions": workflow.compute_available_actions(
            t, requester_sub=requester_sub, requester_role=requester_role, is_staff=is_staff,
        ),
        # Rétrocompatibilité frontend (renommé ultérieurement)
        "suggestedActions": workflow.compute_suggested_actions(t) if is_staff else [],
        "hasMmAnomaly": t.mm_verifications.filter(
            status=MobileMoneyVerification.Status.FOUND,
        ).exists(),
        "pendingFinancialAction": None,
    }

    if is_staff:
        pfa = t.financial_actions.filter(status=PendingFinancialAction.Status.PENDING).first()
        if pfa:
            initiator_info = UserDisplayService.resolve(pfa.initiated_by)
            row["pendingFinancialAction"] = {
                "id": pfa.pk, "amount": str(pfa.amount), "currency": pfa.currency,
                "initiatedBy": pfa.initiated_by,
                "initiatedByName": initiator_info["displayName"],
                "createdAt": pfa.created_at.isoformat(),
            }

    if include_messages:
        qs = t.messages.all() if is_staff else t.messages.filter(is_internal=False)
        row["messages"] = _serialize_messages(qs)
    return row


def _is_staff(request) -> bool:
    return get_role(getattr(request.user, "role", "")).type != "Client"


def _ip(request) -> str:
    return (request.META.get("HTTP_X_FORWARDED_FOR") or request.META.get("REMOTE_ADDR") or "")


def _require_ticket(ticket_id) -> Ticket:
    t = Ticket.objects.select_related("user").filter(pk=ticket_id).first()
    if not t:
        raise NotFoundError("Ticket introuvable.")
    return t


# ── CRUD ───────────────────────────────────────────────────────────────────────

@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def tickets(request):
    if request.method == "GET":
        sla.check_sla_breaches()
        if _is_staff(request):
            qs = Ticket.objects.select_related("user").prefetch_related(
                "mm_verifications", "financial_actions",
            )
            # Filtres
            p = request.query_params
            if p.get("status"):
                qs = qs.filter(status=p["status"])
            if p.get("category"):
                qs = qs.filter(category=p["category"])
            if p.get("priority"):
                qs = qs.filter(priority=p["priority"])
            if p.get("level"):
                qs = qs.filter(level=p["level"])
            if p.get("agent"):
                qs = qs.filter(assigned_to_sub=p["agent"])
            if p.get("search"):
                q = p["search"]
                qs = qs.filter(
                    Q(subject__icontains=q) | Q(description__icontains=q)
                    | Q(user__full_name__icontains=q) | Q(user__email__icontains=q)
                )
        else:
            qs = Ticket.objects.select_related("user").prefetch_related(
                "mm_verifications", "financial_actions",
            ).filter(user=request.user)
        is_staff = _is_staff(request)
        rsub = getattr(request.user, "sub", "")
        rrole = getattr(request.user, "role", "")
        return Response([_ticket_row(t, is_staff=is_staff, requester_sub=rsub, requester_role=rrole) for t in qs])

    # POST — création
    data = request.data or {}
    try:
        ticket = workflow.create_ticket(
            user=request.user,
            category=data.get("category", "technique"),
            subject=data.get("subject", ""),
            description=data.get("description", ""),
            priority=data.get("priority", "normal"),
            ip=_ip(request),
        )
    except ConflictError as exc:
        return Response({"detail": exc.message, "code": exc.code}, status=409)
    return Response(_ticket_row(ticket, is_staff=_is_staff(request)), status=201)


@api_view(["GET", "PATCH"])
@permission_classes([IsAuthenticated])
def ticket_detail(request, ticket_id):
    try:
        ticket = _require_ticket(ticket_id)
    except NotFoundError as e:
        return Response({"detail": e.message}, status=404)
    is_staff = _is_staff(request)
    if not is_staff and ticket.user_id != getattr(request.user, "sub", ""):
        return Response({"detail": "Accès refusé."}, status=403)

    if request.method == "GET":
        return Response(_ticket_row(ticket, include_messages=True, is_staff=is_staff))

    # PATCH — mise à jour libre (legacy, pour Support.jsx existant)
    if not is_staff:
        return Response({"detail": "Accès réservé au personnel."}, status=403)
    data = request.data or {}
    if "status" in data:
        ticket.status = data["status"]
        if data["status"] in (Ticket.Status.RESOLU, Ticket.Status.REJETE) and not ticket.resolved_at:
            ticket.resolved_at = timezone.now()
    if "priority" in data:
        ticket.priority = data["priority"]
    if "level" in data:
        ticket.level = data["level"]
    if "assignedTo" in data:
        ticket.assigned_to_sub = data["assignedTo"]
    ticket.save()
    return Response(_ticket_row(ticket, is_staff=True))


# ── Actions sur tickets ────────────────────────────────────────────────────────

@api_view(["POST"])
@permission_classes([IsStaff, HasCapability("validate")])
def ticket_assign(request, ticket_id):
    try:
        ticket = _require_ticket(ticket_id)
    except NotFoundError as e:
        return Response({"detail": e.message}, status=404)
    agent_sub = (request.data or {}).get("agentSub", "")
    if not agent_sub:
        return Response({"detail": "agentSub requis."}, status=400)
    try:
        ticket = workflow.assign_ticket(
            ticket=ticket, agent_sub=agent_sub,
            actor_sub=getattr(request.user, "sub", ""), ip=_ip(request),
        )
    except ConflictError as e:
        return Response({"detail": e.message}, status=409)
    return Response(_ticket_row(ticket, is_staff=True))


@api_view(["POST"])
@permission_classes([IsStaff, HasCapability("read")])
def ticket_claim(request, ticket_id):
    try:
        ticket = _require_ticket(ticket_id)
    except NotFoundError as e:
        return Response({"detail": e.message}, status=404)
    if not _is_staff(request):
        return Response({"detail": "Accès réservé au personnel."}, status=403)
    try:
        ticket = workflow.claim_ticket(
            ticket=ticket,
            agent_sub=getattr(request.user, "sub", ""),
            agent_role=getattr(request.user, "role", ""),
            ip=_ip(request),
        )
    except ConflictError as e:
        return Response({"detail": e.message}, status=409)
    except ValidationFailed as e:
        return Response({"detail": e.message}, status=400)
    return Response(_ticket_row(ticket, is_staff=True))


@api_view(["POST"])
@permission_classes([IsStaff, HasCapability("read")])
def ticket_escalate(request, ticket_id):
    try:
        ticket = _require_ticket(ticket_id)
    except NotFoundError as e:
        return Response({"detail": e.message}, status=404)
    if not _is_staff(request):
        return Response({"detail": "Accès réservé au personnel."}, status=403)
    reason = (request.data or {}).get("reason", "")
    try:
        ticket = workflow.escalate_ticket(
            ticket=ticket,
            actor_sub=getattr(request.user, "sub", ""),
            actor_role=getattr(request.user, "role", ""),
            reason=reason, ip=_ip(request),
        )
    except (ConflictError, ValidationFailed) as e:
        return Response({"detail": e.message}, status=e.http_status)
    return Response(_ticket_row(ticket, is_staff=True))


@api_view(["POST"])
@permission_classes([IsStaff, HasCapability("read")])
def ticket_resolve(request, ticket_id):
    try:
        ticket = _require_ticket(ticket_id)
    except NotFoundError as e:
        return Response({"detail": e.message}, status=404)
    if not _is_staff(request):
        return Response({"detail": "Accès réservé au personnel."}, status=403)
    summary = (request.data or {}).get("resolutionSummary", "") or (request.data or {}).get("resolutionNote", "")
    try:
        ticket = workflow.resolve_ticket(
            ticket=ticket,
            actor_sub=getattr(request.user, "sub", ""),
            resolution_summary=summary, ip=_ip(request),
        )
    except (ConflictError, ValidationFailed) as e:
        return Response({"detail": e.message}, status=e.http_status)
    return Response(_ticket_row(ticket, is_staff=True))


@api_view(["POST"])
@permission_classes([IsStaff, HasCapability("read")])
def ticket_reject(request, ticket_id):
    try:
        ticket = _require_ticket(ticket_id)
    except NotFoundError as e:
        return Response({"detail": e.message}, status=404)
    if not _is_staff(request):
        return Response({"detail": "Accès réservé au personnel."}, status=403)
    data = request.data or {}
    try:
        ticket = workflow.reject_ticket(
            ticket=ticket,
            actor_sub=getattr(request.user, "sub", ""),
            reject_type=data.get("rejectType", Ticket.RejectType.HORS_PERIMETRE),
            reason=data.get("reason", ""),
            original_ticket_id=data.get("originalTicketId"),
            ip=_ip(request),
        )
    except (ConflictError, ValidationFailed) as e:
        return Response({"detail": e.message}, status=e.http_status)
    return Response(_ticket_row(ticket, is_staff=True))


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def ticket_reopen(request, ticket_id):
    try:
        ticket = _require_ticket(ticket_id)
    except NotFoundError as e:
        return Response({"detail": e.message}, status=404)
    if not _is_staff(request) and ticket.user_id != getattr(request.user, "sub", ""):
        return Response({"detail": "Accès refusé."}, status=403)
    if ticket.status not in (Ticket.Status.RESOLU, Ticket.Status.REJETE):
        return Response({"detail": "Seul un ticket clôturé peut être réouvert."}, status=409)
    if not ticket.resolved_at or (timezone.now() - ticket.resolved_at).days > TICKET_REOPEN_DAYS:
        return Response({"detail": f"Ce ticket ne peut plus être réouvert après {TICKET_REOPEN_DAYS} jours."}, status=409)
    now = timezone.now()
    _, resolution_due = sla.compute_sla_deadlines(priority=ticket.priority, created_at=now)
    ticket.status = Ticket.Status.EN_TRAITEMENT
    ticket.resolved_at = None
    ticket.reopened_count += 1
    ticket.sla_resolution_due = resolution_due
    ticket.sla_breached_resolution = False
    ticket.sla_paused_at = None
    ticket.waiting_on = Ticket.WaitingOn.AGENT
    ticket.save()
    workflow._sys_msg(ticket, "Réouvert suite à la réponse du client.", is_internal=True)
    workflow._audit(ticket, getattr(request.user, "sub", ""), "ticket.reopen",
                    {"reopenedCount": ticket.reopened_count})
    return Response(_ticket_row(ticket, is_staff=_is_staff(request)))


@api_view(["POST"])
@permission_classes([IsStaff, HasCapability("read")])
def ticket_waiting_on(request, ticket_id):
    try:
        ticket = _require_ticket(ticket_id)
    except NotFoundError as e:
        return Response({"detail": e.message}, status=404)
    if not _is_staff(request):
        return Response({"detail": "Accès réservé au personnel."}, status=403)
    value = (request.data or {}).get("value", "")
    if value not in Ticket.WaitingOn.values:
        return Response({"detail": "Valeur waitingOn invalide."}, status=400)
    sla.set_waiting_on(ticket=ticket, value=value)
    workflow._audit(ticket, getattr(request.user, "sub", ""), "ticket.waiting_on", {"value": value})
    return Response(_ticket_row(ticket, is_staff=True))


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def ticket_rate(request, ticket_id):
    try:
        ticket = _require_ticket(ticket_id)
    except NotFoundError as e:
        return Response({"detail": e.message}, status=404)
    if ticket.user_id != getattr(request.user, "sub", ""):
        return Response({"detail": "Accès refusé."}, status=403)
    if ticket.status != Ticket.Status.RESOLU:
        return Response({"detail": "Seul un ticket résolu peut être noté."}, status=409)
    if ticket.satisfaction_rating is not None:
        return Response({"detail": "Ce ticket a déjà été noté."}, status=409)
    if not ticket.resolved_at or (timezone.now() - ticket.resolved_at).days > RATING_WINDOW_DAYS:
        return Response({"detail": f"La notation n'est plus possible après {RATING_WINDOW_DAYS} jours."}, status=409)
    data = request.data or {}
    try:
        rating = int(data.get("rating"))
    except (TypeError, ValueError):
        return Response({"detail": "Note invalide."}, status=400)
    if rating < 1 or rating > 5:
        return Response({"detail": "La note doit être comprise entre 1 et 5."}, status=400)
    ticket.satisfaction_rating = rating
    ticket.satisfaction_comment = data.get("comment", "")
    ticket.save(update_fields=["satisfaction_rating", "satisfaction_comment"])
    workflow._audit(ticket, getattr(request.user, "sub", ""), "ticket.rate", {"rating": rating})
    return Response(_ticket_row(ticket, is_staff=False))


# ── Mobile Money ───────────────────────────────────────────────────────────────

@api_view(["POST"])
@permission_classes([IsStaff, HasCapability("read")])
def ticket_verify_mm(request, ticket_id):
    try:
        ticket = _require_ticket(ticket_id)
    except NotFoundError as e:
        return Response({"detail": e.message}, status=404)
    if not _is_staff(request):
        return Response({"detail": "Accès réservé au personnel."}, status=403)
    transaction_ref = (request.data or {}).get("transactionRef")
    try:
        verif = workflow.verify_mobile_money(
            ticket=ticket,
            actor_sub=getattr(request.user, "sub", ""),
            transaction_ref=transaction_ref,
            ip=_ip(request),
        )
    except (ConflictError, ValidationFailed) as e:
        return Response({"detail": e.message, "code": getattr(e, "code", "")},
                        status=e.http_status)
    return Response({
        "verificationId": verif.pk,
        "operator": verif.operator,
        "transactionRef": verif.transaction_ref,
        "status": verif.status,
        "amount": str(verif.amount) if verif.amount else None,
        "currency": verif.currency,
        "verifiedAt": verif.verified_at.isoformat() if verif.verified_at else None,
    }, status=202)


# ── Force-crédit ───────────────────────────────────────────────────────────────

@api_view(["POST"])
@permission_classes([IsStaff, HasCapability("validate")])
def ticket_force_credit(request, ticket_id):
    try:
        ticket = _require_ticket(ticket_id)
    except NotFoundError as e:
        return Response({"detail": e.message}, status=404)
    from rbac.role_registry import get_role as _get_role
    role_obj = _get_role(getattr(request.user, "role", ""))
    if not (role_obj.disburse or role_obj.audit):
        return Response({"detail": "Action réservée aux administrateurs."}, status=403)

    data = request.data or {}
    actor_sub = getattr(request.user, "sub", "")
    action_id = data.get("actionId")

    try:
        if action_id:
            # Étape 2 — approbation ou rejet par le 2e admin
            action = PendingFinancialAction.objects.filter(pk=action_id).first()
            if not action or action.ticket_id != ticket.pk:
                return Response({"detail": "Action introuvable."}, status=404)
            decision = data.get("decision", "approve")
            if decision == "reject":
                note = data.get("note", "")
                if not note:
                    return Response({"detail": "Un motif de rejet est requis."}, status=400)
                action = workflow.reject_force_credit(
                    ticket=ticket, action=action, approver_sub=actor_sub,
                    note=note, ip=_ip(request),
                )
            else:
                action = workflow.approve_force_credit(
                    ticket=ticket, action=action, approver_sub=actor_sub, ip=_ip(request),
                )
        else:
            # Étape 1 — initiation
            idempotency_key = data.get("idempotencyKey") or f"fc-{ticket.pk}-{actor_sub}"
            action = workflow.initiate_force_credit(
                ticket=ticket, initiator_sub=actor_sub,
                idempotency_key=idempotency_key, ip=_ip(request),
            )
    except (ConflictError, ValidationFailed) as e:
        return Response({"detail": e.message}, status=e.http_status)
    return Response({
        "actionId": action.pk,
        "status": action.status,
        "amount": str(action.amount),
        "currency": action.currency,
        "initiatedBy": action.initiated_by,
        "approvedBy": action.approved_by,
        "accountingRef": action.accounting_ref,
        "decidedAt": action.decided_at.isoformat() if action.decided_at else None,
    })


# ── Messages ───────────────────────────────────────────────────────────────────

@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def ticket_messages(request, ticket_id):
    try:
        ticket = _require_ticket(ticket_id)
    except NotFoundError as e:
        return Response({"detail": e.message}, status=404)
    is_staff = _is_staff(request)
    if not is_staff and ticket.user_id != getattr(request.user, "sub", ""):
        return Response({"detail": "Accès refusé."}, status=403)
    if request.method == "GET":
        qs = ticket.messages.all() if is_staff else ticket.messages.filter(is_internal=False)
        return Response(_serialize_messages(qs))
    data = request.data or {}
    is_internal = bool(data.get("isInternal")) and is_staff
    msg = TicketMessage.objects.create(
        ticket=ticket, author_sub=getattr(request.user, "sub", ""),
        author_role=getattr(request.user, "role", ""), text=data.get("text", ""),
        is_internal=is_internal,
    )
    actor_sub = getattr(request.user, "sub", "")
    is_client = ticket.user_id == actor_sub

    # Reprise automatique si le client répond sur un ticket en-attente-client
    if is_client and not is_internal and ticket.status == Ticket.Status.EN_ATTENTE_CLIENT:
        workflow.resume_from_client(ticket=ticket, actor_sub=actor_sub, ip=_ip(request))

    if is_staff and not is_internal and not ticket.first_response_at:
        ticket.first_response_at = timezone.now()
        if ticket.status == Ticket.Status.OUVERT:
            ticket.status = Ticket.Status.EN_TRAITEMENT
        ticket.save(update_fields=["first_response_at", "status"])
        try:
            from common.sms import send_sms_to_user
            send_sms_to_user(user_sub=ticket.user_id,
                             message=f"AGRICAP Support : un agent a répondu à votre ticket #{ticket.pk}.")
        except Exception:  # noqa: BLE001
            pass

    author_info = UserDisplayService.resolve(actor_sub)
    return Response(
        {
            "id": msg.pk,
            "author": {
                "displayName": author_info["displayName"],
                "initials": author_info.get("initials", "??"),
                "role": author_info["role"],
                "isSystem": False,
            },
            "authorSub": msg.author_sub,
            "authorName": author_info["displayName"],
            "authorRole": msg.author_role,
            "text": msg.text,
            "isInternal": msg.is_internal,
            "createdAt": msg.created_at.isoformat(),
            "meta": {"actionSource": "", "simulated": False},
        },
        status=201,
    )


# ── Dashboard stats ────────────────────────────────────────────────────────────

@api_view(["GET"])
@permission_classes([IsStaff, HasCapability("read")])
def dashboard_stats(request):
    sla.check_sla_breaches()
    open_statuses = (Ticket.Status.OUVERT, Ticket.Status.EN_TRAITEMENT, Ticket.Status.ESCALADE)
    now = timezone.now()
    since_24h = now - timedelta(hours=24)

    qs_all = Ticket.objects.all()
    open_count = qs_all.filter(status__in=open_statuses).count()
    escalated_count = qs_all.filter(status=Ticket.Status.ESCALADE).count()
    resolved_24h = qs_all.filter(status=Ticket.Status.RESOLU, resolved_at__gte=since_24h).count()
    avg_sat = qs_all.filter(
        satisfaction_rating__isnull=False,
    ).aggregate(avg=Avg("satisfaction_rating"))["avg"]
    out_of_sla = qs_all.filter(
        status__in=open_statuses,
        sla_resolution_due__lt=now,
        waiting_on=Ticket.WaitingOn.AGENT,
    ).count()

    by_category_qs = qs_all.filter(status__in=open_statuses).values("category").annotate(n=Count("id"))
    by_category = {row["category"]: row["n"] for row in by_category_qs}
    for cat, _ in Ticket.Category.choices:
        by_category.setdefault(cat, 0)

    return Response({
        "open": open_count,
        "escalated": escalated_count,
        "resolved24h": resolved_24h,
        "avgSatisfaction": round(avg_sat, 2) if avg_sat else None,
        "byCategory": by_category,
        "outOfSla": out_of_sla,
    })


# ── En attente client ──────────────────────────────────────────────────────────

@api_view(["POST"])
@permission_classes([IsAuthenticated])
def ticket_await_client(request, ticket_id):
    try:
        ticket = _require_ticket(ticket_id)
    except NotFoundError as e:
        return Response({"detail": e.message}, status=404)
    if not _is_staff(request):
        return Response({"detail": "Réservé aux agents."}, status=403)
    data = request.data or {}
    actor_sub = getattr(request.user, "sub", "")
    try:
        ticket = workflow.await_client(
            ticket=ticket, actor_sub=actor_sub,
            question=data.get("question", ""), ip=_ip(request),
        )
    except (ConflictError, ValidationFailed, PermissionDeniedError) as e:
        return Response({"detail": e.message}, status=e.http_status)
    rsub = getattr(request.user, "sub", "")
    rrole = getattr(request.user, "role", "")
    return Response(_ticket_row(ticket, is_staff=True, requester_sub=rsub, requester_role=rrole))


# ── Fiche client 360° ──────────────────────────────────────────────────────────

_KYC_LEVEL_MAP = {
    "T0": "KYC0", "T1": "KYC1", "T2": "KYC2", "T3": "KYC3", "T4": "KYC3",
}
_KYC_LIMITATIONS = {
    "KYC0": "Compte en lecture seule — retraits bloqués. Orienter vers la complétion KYC.",
    "KYC1": "Retraits plafonnés à 100 USD/jour.",
    "KYC2": "Retraits plafonnés à 500 USD/jour.",
    "KYC3": "Sans plafond (client Corporate/VIP — escalade prioritaire).",
}


def _mask_phone(phone: str) -> str:
    if not phone or len(phone) < 6:
        return "Non renseigné"
    return phone[:4] + " •• ••• " + phone[-4:]


def _mask_email(email: str) -> str:
    if not email or "@" not in email:
        return "Non renseigné"
    local, domain = email.split("@", 1)
    return local[0] + "•" * max(1, len(local) - 1) + "@" + domain


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def ticket_client_360(request, ticket_id):
    if not _is_staff(request):
        return Response({"detail": "Accès réservé aux agents."}, status=403)
    try:
        ticket = _require_ticket(ticket_id)
    except NotFoundError as e:
        return Response({"detail": e.message}, status=404)

    client = ticket.user
    cache_key = f"client360:{client.sub}"
    cached = cache.get(cache_key)
    if cached:
        return Response(cached)

    partial_headers = {}
    profile_info = UserDisplayService.resolve(client.sub)

    # ── KYC ───────────────────────────────────────────────────────────────────
    kyc_data = {"status": "indisponible", "level": "N/D", "missingDocuments": [],
                "lastReviewedAt": None, "limitations": "Données KYC indisponibles."}
    kyc_warnings = []
    try:
        from compliance.models import Document, KycProfile
        kyc = KycProfile.objects.filter(user=client).first()
        if kyc:
            level_mapped = _KYC_LEVEL_MAP.get(kyc.kyc_level, kyc.kyc_level)
            validated_docs = set(Document.objects.filter(
                user=client, status="validated",
            ).values_list("type", flat=True))
            required_docs = {"id_card"}
            if level_mapped in ("KYC2", "KYC3"):
                required_docs.add("proof_address")
            missing = [d for d in required_docs if d not in validated_docs]
            kyc_status = "verifie" if kyc.kyc_status == "Validé" else (
                "en_revue" if kyc.kyc_status == "En attente" else "incomplet"
            )
            kyc_data = {
                "status": kyc_status,
                "level": level_mapped,
                "missingDocuments": missing,
                "lastReviewedAt": kyc.updated_at.isoformat(),
                "limitations": _KYC_LIMITATIONS.get(level_mapped, ""),
            }
            if ticket.category in Ticket.FINANCIAL_CATEGORIES and kyc_status != "verifie":
                kyc_warnings.append(
                    "KYC incomplet : toute régularisation financière nécessite la validation Conformité."
                )
        else:
            kyc_data["status"] = "non_demarre"
            partial_headers["X-Partial-Response"] = "kyc"
    except Exception:  # noqa: BLE001
        partial_headers["X-Partial-Response"] = "kyc"

    # ── Risque ────────────────────────────────────────────────────────────────
    risk_data = {"level": "moyen", "flags": [], "score": None}
    try:
        from compliance.models import KycProfile as _KP
        kp = _KP.objects.filter(user=client).first()
        if kp:
            risk_map = {"Bas": "faible", "Moyen": "moyen", "Élevé": "eleve"}
            risk_data["level"] = risk_map.get(kp.risk_score, "moyen")
            flags = []
            if (timezone.now() - client.created_at).days < 30:
                flags.append("nouveau_compte")
            risk_data["flags"] = flags
    except Exception:  # noqa: BLE001
        pass

    # ── Historique support ────────────────────────────────────────────────────
    all_tickets = Ticket.objects.filter(user=client).order_by("-created_at")
    total = all_tickets.count()
    open_t = all_tickets.filter(
        status__in=[Ticket.Status.OUVERT, Ticket.Status.EN_TRAITEMENT,
                    Ticket.Status.ESCALADE, Ticket.Status.EN_ATTENTE_CLIENT],
    ).count()
    last_tickets = [
        {
            "id": t.public_id, "subject": t.subject,
            "status": t.status, "created": t.created_at.date().isoformat(),
        }
        for t in all_tickets[:5]
    ]
    avg_sat_val = all_tickets.filter(
        satisfaction_rating__isnull=False,
    ).aggregate(avg=Avg("satisfaction_rating"))["avg"]

    # Récurrence : ≥2 tickets même catégorie sur 30 jours glissants
    since_30d = timezone.now() - timedelta(days=30)
    repeat_count = all_tickets.filter(
        category=ticket.category, created_at__gte=since_30d,
    ).count()
    is_repeat = repeat_count >= 2
    repeat_hint = (
        f"{repeat_count}e ticket '{ticket.category}' en 30 jours — "
        f"vérifier un problème récurrent d'intégration."
        if is_repeat else ""
    )

    # ── Contact masqué ────────────────────────────────────────────────────────
    contact = {
        "phoneMasked": _mask_phone(client.phone),
        "emailMasked": _mask_email(client.email),
        "preferredChannel": "whatsapp",
        "language": "fr",
    }

    body = {
        "id": client.sub,
        "publicRef": f"CLT-{str(client.pk)[-4:].zfill(4)}",
        "displayName": profile_info["displayName"],
        "initials": profile_info.get("initials", "??"),
        "segment": "Corporate" if client.company_name else "Particulier",
        "memberSince": client.created_at.date().isoformat() if hasattr(client, "created_at") else None,
        "kyc": kyc_data,
        "risk": risk_data,
        "finances": {
            "balanceFc": None, "balanceUsd": None,
            "loansActive": 0, "loansInArrears": 0,
            "lastTransactionAt": None,
        },
        "supportHistory": {
            "totalTickets": total,
            "openTickets": open_t,
            "lastTickets": last_tickets,
            "avgSatisfaction": round(avg_sat_val, 2) if avg_sat_val else None,
            "isRepeatIssue": is_repeat,
            "repeatIssueHint": repeat_hint,
        },
        "contact": contact,
        "warnings": kyc_warnings,
    }

    # Finances (dégradation gracieuse si module indisponible)
    try:
        from caisses.models import CaisseAccount
        wallets = CaisseAccount.objects.filter(client_sub=client.sub)
        if wallets.exists():
            body["finances"]["balanceFc"] = str(
                sum(w.balance for w in wallets if w.currency == "CDF") or 0
            )
            body["finances"]["balanceUsd"] = str(
                sum(w.balance for w in wallets if w.currency == "USD") or 0
            )
    except Exception:  # noqa: BLE001
        partial_headers["X-Partial-Response"] = (
            partial_headers.get("X-Partial-Response", "") + " finances"
        ).strip()

    cache.set(cache_key, body, 60)
    resp = Response(body)
    for k, v in partial_headers.items():
        resp[k] = v
    return resp


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def ticket_reveal_contact(request, ticket_id):
    """Révèle les coordonnées du client en clair + enregistre un audit de consultation."""
    if not _is_staff(request):
        return Response({"detail": "Réservé aux agents autorisés."}, status=403)
    # Permission spécifique : support.can_view_contact (géré par rôle admin/manager)
    role_obj = get_role(getattr(request.user, "role", ""))
    if not (role_obj.audit or role_obj.disburse):
        return Response({"detail": "Permission support.can_view_contact requise."}, status=403)
    try:
        ticket = _require_ticket(ticket_id)
    except NotFoundError as e:
        return Response({"detail": e.message}, status=404)

    actor_sub = getattr(request.user, "sub", "")
    TicketAuditLog.objects.create(
        ticket=ticket,
        actor=actor_sub,
        action="contact.reveal",
        payload={"client_sub": ticket.user_id},
        ip_address=_ip(request),
    )
    return Response({
        "phone": ticket.user.phone or None,
        "email": ticket.user.email or None,
        "revealedAt": timezone.now().isoformat(),
        "revealedBy": actor_sub,
    })


# ── Conversations investisseur/gestionnaire ────────────────────────────────────

@api_view(["GET"])
@permission_classes([IsAuthenticated])
def my_conversations(request):
    sub = getattr(request.user, "sub", "")
    convs = Conversation.objects.filter(Q(investor_sub=sub) | Q(manager_sub=sub))
    return Response([{"id": c.pk, "investorSub": c.investor_sub, "managerSub": c.manager_sub} for c in convs])


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def start_conversation(request):
    manager_sub = (request.data or {}).get("managerSub", "")
    if not manager_sub:
        return Response({"detail": "managerSub requis."}, status=400)
    investor_sub = getattr(request.user, "sub", "")
    conv, _ = Conversation.objects.get_or_create(investor_sub=investor_sub, manager_sub=manager_sub)
    return Response({"id": conv.pk, "investorSub": conv.investor_sub, "managerSub": conv.manager_sub})


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def conversation_messages(request, conversation_id):
    conv = Conversation.objects.filter(pk=conversation_id).first()
    if not conv:
        return Response({"detail": "Conversation introuvable."}, status=404)
    return Response([
        {"id": m.pk, "senderSub": m.sender_sub, "text": m.text, "createdAt": m.created_at.isoformat()}
        for m in conv.messages.all()
    ])


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def send_message(request, conversation_id):
    conv = Conversation.objects.filter(pk=conversation_id).first()
    if not conv:
        return Response({"detail": "Conversation introuvable."}, status=404)
    text = (request.data or {}).get("text", "")
    msg = Message.objects.create(conversation=conv, sender_sub=getattr(request.user, "sub", ""), text=text)
    return Response({"id": msg.pk, "text": msg.text, "createdAt": msg.created_at.isoformat()}, status=201)
