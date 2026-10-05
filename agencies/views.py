"""API des agences (Agencies.jsx) — CRUD + actions de cycle de vie.

Une agence est un objet de l'institution : sa balance USD, son score de conformité, ses
alertes ouvertes et ses rapprochements ne regardent aucun membre. Toutes les vues cumulent
donc `IsStaff` — `HasCapability("read")` seul laissait un client lister les soldes de
toutes les agences (`_row` sert `balanceUSD`), et `POST /reconciliations` ouvrir un
rapprochement.
"""
from __future__ import annotations

from rest_framework.decorators import api_view, parser_classes, permission_classes
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response

from common.parsing import to_date
from accounts.permissions import IsStaff
from rbac.permissions import CapaciteSelonMethode, HasCapability
from rbac.role_registry import get_role

from . import evolution, maker_checker, services
from .models import ActionApproverConfig, Agency, AgencyActionRequest, AgencyReconciliation, EvolutionPlan, EvolutionPlanItem


def _row(a: Agency) -> dict:
    from caisses.services import agency_balance  # import différé (caisses dépend d'agencies)
    return {
        "id": a.pk,
        "code": a.code,
        "name": a.name,
        "type": a.type,
        "city": a.city,
        "province": a.province,
        "manager": a.manager_sub,
        "complianceScore": a.compliance_score,
        "balanceUSD": float(agency_balance(agency_id=a.pk)),
        "status": a.status,
        "alerts": [
            {"id": al.pk, "level": al.level, "message": al.message}
            for al in a.alerts.filter(resolved_at__isnull=True)
        ],
    }


def _require(request, capability: str):
    role = get_role(getattr(request.user, "role", ""))
    return getattr(role, capability, False)


@api_view(["GET", "POST"])
@permission_classes([IsStaff, CapaciteSelonMethode(GET="read", POST="create")])
def agencies(request):
    if request.method == "GET":
        return Response([_row(a) for a in Agency.objects.all()])
    if not _require(request, "create"):
        return Response({"detail": "Capacité requise : create."}, status=403)
    data = request.data or {}
    agency = services.create_agency(
        code=data.get("code", ""), name=data.get("name", ""),
        type_=data.get("type") or Agency.Type.URBAINE,
        city=data.get("city", ""), province=data.get("province", ""),
        manager_sub=data.get("manager", ""), by=getattr(request.user, "sub", ""),
    )
    return Response(_row(agency), status=201)


@api_view(["GET", "PATCH"])
@permission_classes([IsStaff, CapaciteSelonMethode(GET="read", PATCH="create")])
def agency_detail(request, code):
    agency = Agency.objects.filter(code=code).first()
    if not agency:
        return Response({"detail": "Agence introuvable."}, status=404)
    if request.method == "PATCH":
        if not _require(request, "create"):
            return Response({"detail": "Capacité requise : create."}, status=403)
        data = request.data or {}
        for field, model_field in (("name", "name"), ("city", "city"), ("province", "province"),
                                    ("manager", "manager_sub"), ("complianceScore", "compliance_score")):
            if field in data:
                setattr(agency, model_field, data[field])
        agency.save()
    return Response(_row(agency))


@api_view(["POST"])
@parser_classes([MultiPartParser, FormParser, JSONParser])
@permission_classes([HasCapability("validate")])
def agency_action(request, code):
    agency = Agency.objects.filter(code=code).first()
    if not agency:
        return Response({"detail": "Agence introuvable."}, status=404)
    data = request.data or {}
    action = data.get("action")
    by = getattr(request.user, "sub", "")
    reason = data.get("reason", "")
    if action == "suspend":
        agency = services.suspend(agency=agency, reason=reason, by=by)
    elif action == "unlock_temporary":
        agency = services.unlock_temporary(agency=agency, reason=reason, document=request.FILES.get("document"), by=by)
    elif action == "reopen":
        agency = services.reopen(agency=agency, reason=reason, document=request.FILES.get("document"), by=by)
    elif action == "close":
        agency = services.close(agency=agency, reason=reason, by=by)
    elif action == "evolve_type":
        agency = services.evolve_type(agency=agency, new_type=data.get("newType", ""), reason=reason, by=by)
    else:
        return Response({"detail": f"Action inconnue : {action}"}, status=400)
    return Response(_row(agency))


@api_view(["GET"])
@permission_classes([HasCapability("audit")])
def agency_reconciliation(request, code):
    agency = Agency.objects.filter(code=code).first()
    if not agency:
        return Response({"detail": "Agence introuvable."}, status=404)
    return Response(services.reconciliation_report(agency=agency))


@api_view(["GET"])
@permission_classes([HasCapability("audit")])
def agency_status_history(request, code):
    """Historique chronologique des changements de statut (suspension/fermeture/
    déverrouillage/réouverture) avec motif — et, pour une réactivation, le document à
    l'appui. Combine le journal d'audit (seule source des motifs de suspend/close, qui
    n'ont pas de modèle dédié) et `AgencyReactivation` (source exacte, pas de correspondance
    fragile par horodatage nécessaire pour les motifs de réactivation)."""
    from audit.models import AuditEntry
    agency = Agency.objects.filter(code=code).first()
    if not agency:
        return Response({"detail": "Agence introuvable."}, status=404)

    ACTION_TO_KIND = {"agency.suspend": "SUSPEND", "agency.close": "CLOSE"}
    rows = [
        {
            "kind": ACTION_TO_KIND[e.action], "reason": e.details.get("reason", ""),
            "documentUrl": None, "createdAt": e.created_at.isoformat(), "createdBy": e.actor,
        }
        for e in AuditEntry.objects.filter(entity_type="Agency", entity_id=agency.code,
                                            action__in=ACTION_TO_KIND.keys())
    ]
    rows += [
        {
            "kind": r.kind, "reason": r.reason,
            "documentUrl": request.build_absolute_uri(r.document.url) if r.document else None,
            "createdAt": r.created_at.isoformat(), "createdBy": r.created_by,
        }
        for r in agency.reactivations.all()
    ]
    rows.sort(key=lambda r: r["createdAt"], reverse=True)
    return Response(rows)


@api_view(["GET"])
@permission_classes([HasCapability("audit")])
def agency_audit(request, code):
    from audit.models import AuditEntry
    agency = Agency.objects.filter(code=code).first()
    if not agency:
        return Response({"detail": "Agence introuvable."}, status=404)
    entries = AuditEntry.objects.filter(entity_type="Agency", entity_id=agency.code)[:200]
    return Response([
        {"id": e.pk, "timestamp": e.created_at.isoformat(), "action": e.action, "details": e.details}
        for e in entries
    ])


@api_view(["GET"])
@permission_classes([IsStaff, HasCapability("read")])
def agency_compliance_score(request, code):
    from . import compliance

    agency = Agency.objects.filter(code=code).first()
    if not agency:
        return Response({"detail": "Agence introuvable."}, status=404)
    return Response(compliance.compute_agency_compliance_score(agency=agency))


def _evolution_item_row(item: EvolutionPlanItem) -> dict:
    return {
        "id": item.pk, "label": item.label, "order": item.order, "isDone": item.is_done,
        "doneBy": item.done_by, "doneAt": item.done_at.isoformat() if item.done_at else None,
    }


def _evolution_plan_row(plan: EvolutionPlan) -> dict:
    return {
        "id": plan.pk, "agencyCode": plan.agency.code, "fromType": plan.from_type, "toType": plan.to_type,
        "reason": plan.reason, "status": plan.status, "createdBy": plan.created_by,
        "createdAt": plan.created_at.isoformat(),
        "completedAt": plan.completed_at.isoformat() if plan.completed_at else None,
        "items": [_evolution_item_row(i) for i in plan.items.all()],
    }


@api_view(["GET", "POST"])
@permission_classes([HasCapability("validate")])
def agency_evolution_plans(request, code):
    agency = Agency.objects.filter(code=code).first()
    if not agency:
        return Response({"detail": "Agence introuvable."}, status=404)
    if request.method == "GET":
        plans = agency.evolution_plans.prefetch_related("items").all()
        return Response([_evolution_plan_row(p) for p in plans])
    data = request.data or {}
    plan = evolution.start_evolution_plan(
        agency=agency, to_type=data.get("toType", ""), reason=data.get("reason", ""),
        by=getattr(request.user, "sub", ""),
    )
    return Response(_evolution_plan_row(plan), status=201)


@api_view(["POST"])
@permission_classes([HasCapability("validate")])
def evolution_plan_check_item(request, plan_id, item_id):
    item = EvolutionPlanItem.objects.filter(pk=item_id, plan_id=plan_id).select_related("plan").first()
    if not item:
        return Response({"detail": "Étape introuvable."}, status=404)
    item = evolution.check_evolution_item(item=item, by=getattr(request.user, "sub", ""))
    return Response(_evolution_item_row(item))


@api_view(["POST"])
@permission_classes([HasCapability("validate")])
def evolution_plan_complete(request, plan_id):
    plan = EvolutionPlan.objects.filter(pk=plan_id).select_related("agency").first()
    if not plan:
        return Response({"detail": "Plan d'évolution introuvable."}, status=404)
    evolution.complete_evolution_plan(plan=plan, by=getattr(request.user, "sub", ""))
    plan.refresh_from_db()
    return Response(_evolution_plan_row(plan))


@api_view(["POST"])
@permission_classes([HasCapability("validate")])
def evolution_plan_cancel(request, plan_id):
    plan = EvolutionPlan.objects.filter(pk=plan_id).select_related("agency").first()
    if not plan:
        return Response({"detail": "Plan d'évolution introuvable."}, status=404)
    plan = evolution.cancel_evolution_plan(plan=plan, reason=(request.data or {}).get("reason", ""),
                                            by=getattr(request.user, "sub", ""))
    return Response(_evolution_plan_row(plan))


def _reconciliation_row(r: AgencyReconciliation) -> dict:
    return {
        "id": r.pk, "agencyCode": r.agency.code, "periodStart": r.period_start.isoformat(),
        "periodEnd": r.period_end.isoformat(), "status": r.status,
        "deltaAmount": float(r.delta_amount) if r.delta_amount is not None else None,
        "currency": r.currency, "assignedTo": r.assigned_to, "notes": r.notes,
        "isFinalClosure": r.is_final_closure, "openedAt": r.opened_at.isoformat(),
        "closedAt": r.closed_at.isoformat() if r.closed_at else None,
    }


@api_view(["GET", "POST"])
@permission_classes([IsStaff, CapaciteSelonMethode(GET="read", POST="validate")])
def reconciliations(request):
    """Liste globale (filtrable `?status=`/`?agency=`) et ouverture d'un nouveau
    rapprochement structuré — distinct de `agency_reconciliation` (rapport de balance en
    lecture seule)."""
    if request.method == "GET":
        qs = AgencyReconciliation.objects.select_related("agency").all()
        status = request.GET.get("status")
        agency_code = request.GET.get("agency")
        if status:
            qs = qs.filter(status=status)
        if agency_code:
            qs = qs.filter(agency__code=agency_code)
        return Response([_reconciliation_row(r) for r in qs])
    if not get_role(getattr(request.user, "role", "")).validate:
        return Response({"detail": "Capacité requise : validate."}, status=403)
    data = request.data or {}
    agency = Agency.objects.filter(code=data.get("agencyCode")).first()
    if not agency:
        return Response({"detail": "Agence introuvable."}, status=404)
    recon = services.open_reconciliation(
        agency=agency, period_start=to_date(data.get("periodStart")), period_end=to_date(data.get("periodEnd")),
        is_final_closure=bool(data.get("isFinalClosure", False)), by=getattr(request.user, "sub", ""),
    )
    return Response(_reconciliation_row(recon), status=201)


@api_view(["POST"])
@permission_classes([HasCapability("validate")])
def reconciliation_assign(request, reconciliation_id):
    recon = AgencyReconciliation.objects.filter(pk=reconciliation_id).first()
    if not recon:
        return Response({"detail": "Rapprochement introuvable."}, status=404)
    recon = services.assign_reconciliation(
        reconciliation=recon, assignee_sub=(request.data or {}).get("assigneeSub", ""),
        by=getattr(request.user, "sub", ""),
    )
    return Response(_reconciliation_row(recon))


@api_view(["POST"])
@permission_classes([HasCapability("validate")])
def reconciliation_complete(request, reconciliation_id):
    recon = AgencyReconciliation.objects.filter(pk=reconciliation_id).first()
    if not recon:
        return Response({"detail": "Rapprochement introuvable."}, status=404)
    data = request.data or {}
    recon = services.complete_reconciliation(
        reconciliation=recon, delta_amount=data.get("deltaAmount", "0"), currency=data.get("currency", "USD"),
        notes=data.get("notes", ""), by=getattr(request.user, "sub", ""),
    )
    return Response(_reconciliation_row(recon))


def _action_request_row(r: AgencyActionRequest) -> dict:
    return {
        "id": r.pk, "agencyCode": r.agency.code, "actionType": r.action_type, "reason": r.reason,
        "hasDocument": bool(r.document), "requestedBy": r.requested_by, "status": r.status,
        "approvedBy": r.approved_by, "decidedAt": r.decided_at.isoformat() if r.decided_at else None,
        "rejectionNote": r.rejection_note, "createdAt": r.created_at.isoformat(),
    }


@api_view(["GET", "POST"])
@parser_classes([MultiPartParser, FormParser, JSONParser])
@permission_classes([HasCapability("validate")])
def action_requests(request):
    """Maker-checker pour les 4 actions sensibles de cycle de vie (suspend/close/
    unlock_temporary/reopen) : POST crée la demande (maker), ne l'exécute PAS."""
    if request.method == "GET":
        qs = AgencyActionRequest.objects.select_related("agency").all()
        status = request.GET.get("status")
        agency_code = request.GET.get("agency")
        if status:
            qs = qs.filter(status=status)
        if agency_code:
            qs = qs.filter(agency__code=agency_code)
        return Response([_action_request_row(r) for r in qs])
    data = request.data or {}
    agency = Agency.objects.filter(code=data.get("agencyCode")).first()
    if not agency:
        return Response({"detail": "Agence introuvable."}, status=404)
    req = maker_checker.request_agency_action(
        agency=agency, action_type=data.get("actionType", ""), reason=data.get("reason", ""),
        document=request.FILES.get("document"), by=getattr(request.user, "sub", ""),
    )
    # Envoyer le code OTP à chaque approbateur désigné dès la création
    maker_checker.notify_approvers_with_code(req)
    return Response(_action_request_row(req), status=201)


@api_view(["POST"])
@permission_classes([HasCapability("validate")])
def action_request_code(request, request_id):
    """Le checker (approbateur pressenti) demande un code de vérification."""
    approver_sub = getattr(request.user, "sub", "")
    req = AgencyActionRequest.objects.filter(pk=request_id).first()
    if not req:
        return Response({"detail": "Demande introuvable."}, status=404)
    challenge, sms_sent = maker_checker.request_approval_code(
        action_request=req, approver_sub=approver_sub)
    return Response({"challengeId": challenge.pk, "expiresAt": challenge.expires_at.isoformat(),
                     "smsSent": sms_sent})


@api_view(["POST"])
@permission_classes([HasCapability("validate")])
def action_request_cancel(request, request_id):
    """Le maker annule sa propre demande en attente (permet d'en soumettre une nouvelle)."""
    import logging as _logging
    _log = _logging.getLogger("agricap")
    requester_sub = getattr(request.user, "sub", "")
    req = AgencyActionRequest.objects.filter(pk=request_id).first()
    if not req:
        return Response({"detail": "Demande introuvable."}, status=404)
    _log.info("cancel request=%s requester=%s req_status=%s req_by=%s", request_id, requester_sub, req.status, req.requested_by)
    req = maker_checker.cancel_agency_action(
        action_request=req, requester_sub=requester_sub)
    return Response(_action_request_row(req))


@api_view(["POST"])
@permission_classes([IsStaff, HasCapability("read")])
def action_request_notify(request, request_id):
    """Notifie par SMS les approbateurs désignés qu'une demande les attend.
    Peut être appelé par le maker (re-notification manuelle)."""
    req = AgencyActionRequest.objects.filter(pk=request_id).first()
    if not req:
        return Response({"detail": "Demande introuvable."}, status=404)
    if req.status != AgencyActionRequest.Status.PENDING_APPROVAL:
        return Response({"detail": "La demande n'est plus en attente d'approbation."}, status=409)
    results = maker_checker.notify_approvers_with_code(req)
    sms_sent = any(r.get("smsSent") for r in results)
    return Response({"notified": len(results), "smsSent": sms_sent})


@api_view(["POST"])
@permission_classes([HasCapability("validate")])
def action_verify_code(request, request_id):
    data = request.data or {}
    code = data.get("code", "")
    challenge_id = data.get("challengeId", "")
    # Si pas de challengeId fourni, chercher le challenge en attente pour cet approbateur
    if not challenge_id:
        req = AgencyActionRequest.objects.filter(pk=request_id).first()
        if not req:
            return Response({"detail": "Demande introuvable."}, status=404)
        challenge = maker_checker.find_pending_challenge(
            action_request=req, approver_sub=getattr(request.user, "sub", ""),
        )
        if not challenge:
            return Response({"detail": "Aucun code en attente. Demandez un nouveau code."}, status=404)
        challenge_id = challenge.pk
    ok = maker_checker.verify_approval_code(challenge_id=challenge_id, code=code)
    return Response({"verified": ok, "challengeId": challenge_id})


@api_view(["POST"])
@permission_classes([HasCapability("validate")])
def action_request_approve(request, request_id):
    req = AgencyActionRequest.objects.filter(pk=request_id).first()
    if not req:
        return Response({"detail": "Demande introuvable."}, status=404)
    req = maker_checker.approve_agency_action(
        action_request=req, approver_sub=getattr(request.user, "sub", ""),
        approver_role=getattr(request.user, "role", ""), code=(request.data or {}).get("code"),
    )
    return Response(_action_request_row(req))


@api_view(["POST"])
@permission_classes([HasCapability("validate")])
def action_request_reject(request, request_id):
    req = AgencyActionRequest.objects.filter(pk=request_id).first()
    if not req:
        return Response({"detail": "Demande introuvable."}, status=404)
    req = maker_checker.reject_agency_action(
        action_request=req, approver_sub=getattr(request.user, "sub", ""),
        note=(request.data or {}).get("note", ""),
    )
    return Response(_action_request_row(req))


# ---------------------------------------------------------------------------
# Configuration des approbateurs désignés (maker-checker)
# ---------------------------------------------------------------------------

def _approver_config_row(c: ActionApproverConfig) -> dict:
    return {
        "id": c.pk, "scope": c.scope, "actionType": c.action_type,
        "approverSub": c.approver_sub, "approverName": c.approver_name,
        "approverRole": c.approver_role, "approverPhone": c.approver_phone,
        "assignedBy": c.assigned_by, "assignedAt": c.assigned_at.isoformat(),
    }


@api_view(["GET", "POST"])
@permission_classes([HasCapability("config")])
def approver_configs(request):
    """Liste et création des approbateurs désignés par (scope, action_type)."""
    if request.method == "GET":
        qs = ActionApproverConfig.objects.all()
        scope = request.GET.get("scope")
        if scope:
            qs = qs.filter(scope=scope)
        return Response([_approver_config_row(c) for c in qs])

    data = request.data or {}
    scope = data.get("scope", "agency")
    action_type = data.get("actionType", "")
    approver_sub = data.get("approverSub", "")
    if not action_type or not approver_sub:
        return Response({"detail": "actionType et approverSub sont requis."}, status=400)

    config, created = ActionApproverConfig.objects.get_or_create(
        scope=scope, action_type=action_type, approver_sub=approver_sub,
        defaults={
            "approver_name": data.get("approverName", ""),
            "approver_role": data.get("approverRole", ""),
            "approver_phone": data.get("approverPhone", ""),
            "assigned_by": getattr(request.user, "sub", ""),
        },
    )
    if not created and data.get("approverPhone"):
        config.approver_phone = data["approverPhone"]
        config.save(update_fields=["approver_phone"])
    return Response(_approver_config_row(config), status=201 if created else 200)


@api_view(["DELETE"])
@permission_classes([HasCapability("config")])
def approver_config_detail(request, config_id):
    """Suppression d'une configuration d'approbateur."""
    config = ActionApproverConfig.objects.filter(pk=config_id).first()
    if not config:
        return Response({"detail": "Configuration introuvable."}, status=404)
    config.delete()
    return Response(status=204)


@api_view(["PATCH"])
@permission_classes([HasCapability("config")])
def approver_config_phone(request, config_id):
    """Mise à jour du numéro de téléphone OTP d'un approbateur existant."""
    config = ActionApproverConfig.objects.filter(pk=config_id).first()
    if not config:
        return Response({"detail": "Configuration introuvable."}, status=404)
    phone = (request.data or {}).get("approverPhone", "").strip()
    config.approver_phone = phone
    config.save(update_fields=["approver_phone"])
    return Response({"id": config.pk, "approverPhone": config.approver_phone})
