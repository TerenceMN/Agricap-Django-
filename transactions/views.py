"""API des transactions (Transactions.jsx, ValidationJournal.jsx, SpecialCases.jsx,
Supervision.jsx).

Ces quatre écrans sont des écrans de BACKOFFICE : ils servent le flux de validation de
l'institution, tous émetteurs et tous bénéficiaires confondus. Le client, lui, voit ses
mouvements par `caisses` (`/caisses/wallets/mine`, `/caisses/movements/mine`), filtrés
par propriétaire. Aucune vue d'ici n'a donc de public client, et toutes cumulent `IsStaff`
avec leur capacité.

Le garde `HasCapability("read")` seul ne disait rien de tel : les rôles clients portent
`read=True`. Il laissait lire le journal complet des transactions de la coopérative — et
surtout CRÉER une transaction (`POST /`), qui n'est pas un acte de lecture.
"""
from __future__ import annotations

from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response

from accounts.permissions import IsStaff
from common.choices import FlowStatus
from common.exceptions import BusinessError
from rbac.permissions import CapaciteSelonMethode, HasCapability
from rbac.role_registry import get_role

from . import serializers, services
from .models import SpecialCase, Transaction, ValidationThreshold


@api_view(["GET", "POST"])
@permission_classes([IsStaff, CapaciteSelonMethode(GET="read", POST="create")])
def transactions(request):
    if request.method == "GET":
        # Le filtre s'applique AVANT la découpe. Filtrer après un slice lève
        # `TypeError: Cannot filter a query once a slice has been taken` — autrement dit
        # `GET /api/transactions/?status=posted` (le filtre de statut de Transactions.jsx)
        # tombait en 500. Et même si Django l'avait toléré, filtrer les 500 premières
        # lignes n'est pas filtrer les transactions : l'écran aurait affiché un
        # sous-ensemble arbitraire de son propre filtre.
        qs = Transaction.objects.prefetch_related("approvals").all()
        status = request.GET.get("status")
        if status:
            qs = qs.filter(status=status)
        return Response([serializers.tx_row(t) for t in qs[:500]])
    data = request.data or {}
    key = data.get("idempotencyKey")
    if not key:
        return Response({"detail": "idempotencyKey requis."}, status=400)
    tx = services.create_transaction(
        agency_id=data.get("agencyId"), kind=data.get("type", "debit"), amount=data.get("amount", "0"),
        currency=data.get("currency", "USD"), operation_type=data.get("operationType", "PAYMENT"),
        emitter=data.get("emitter", ""), receiver=data.get("receiver", ""),
        description=data.get("description", ""), idempotency_key=key,
        by=getattr(request.user, "sub", ""),
    )
    return Response(serializers.tx_row(tx), status=201)


@api_view(["GET"])
@permission_classes([IsStaff, HasCapability("read")])
def transaction_detail(request, tx_id):
    tx = Transaction.objects.filter(pk=tx_id).first()
    if not tx:
        return Response({"detail": "Transaction introuvable."}, status=404)
    return Response(serializers.tx_row(tx))


@api_view(["POST"])
@permission_classes([IsStaff, HasCapability("validate")])
def transaction_approve(request, tx_id):
    data = request.data or {}
    role = get_role(getattr(request.user, "role", ""))
    tx = services.approve(
        transaction_id=tx_id, approver_sub=getattr(request.user, "sub", ""), approver_role=role.id,
        otp_code=data.get("otpCode"),
    )
    return Response(serializers.tx_row(tx))


@api_view(["POST"])
@permission_classes([IsStaff, HasCapability("validate")])
def transaction_reject(request, tx_id):
    role = get_role(getattr(request.user, "role", ""))
    tx = services.reject(
        transaction_id=tx_id, approver_sub=getattr(request.user, "sub", ""), approver_role=role.id,
        reason=(request.data or {}).get("reason", ""),
    )
    return Response(serializers.tx_row(tx))


@api_view(["POST"])
@permission_classes([IsStaff, HasCapability("validate")])
def transaction_reverse(request, tx_id):
    tx = services.reverse(transaction_id=tx_id, reason=(request.data or {}).get("reason", ""),
                           by=getattr(request.user, "sub", ""))
    return Response(serializers.tx_row(tx))


@api_view(["POST"])
@permission_classes([IsStaff, HasCapability("validate")])
def otp_request(request, tx_id):
    challenge = services.request_step_up_otp(transaction_id=tx_id, approver_sub=getattr(request.user, "sub", ""))
    return Response({"challengeId": challenge.pk, "expiresAt": challenge.expires_at.isoformat()})


@api_view(["POST"])
@permission_classes([IsStaff, HasCapability("validate")])
def otp_verify(request, tx_id):
    data = request.data or {}
    ok = services.verify_step_up_otp(challenge_id=data.get("challengeId", ""), code=data.get("code", ""))
    return Response({"verified": ok})


@api_view(["POST"])
@permission_classes([IsStaff, HasCapability("validate")])
def bulk_action(request):
    data = request.data or {}
    ids = data.get("ids", [])
    action = data.get("action")
    role = get_role(getattr(request.user, "role", ""))
    by = getattr(request.user, "sub", "")
    results = []
    for tx_id in ids:
        try:
            if action == "approve":
                tx = services.approve(transaction_id=tx_id, approver_sub=by, approver_role=role.id)
            elif action == "reject":
                tx = services.reject(transaction_id=tx_id, approver_sub=by, approver_role=role.id)
            elif action == "suspend":
                tx = Transaction.objects.filter(pk=tx_id).first()
                if tx:
                    tx.status = FlowStatus.PENDING_VALIDATION
                    tx.save(update_fields=["status", "updated_at"])
            else:
                results.append({"id": tx_id, "ok": False, "detail": f"Action inconnue : {action}"})
                continue
            results.append({"id": tx_id, "ok": True, "status": tx.status if tx else None})
        except BusinessError as exc:
            results.append({"id": tx_id, "ok": False, "detail": str(exc)})
    return Response(results)


def _case_row(c: SpecialCase) -> dict:
    tx = c.transaction
    return {
        "id": c.pk, "transactionId": c.transaction_id, "ref": f"TX-{tx.pk}",
        "client": tx.emitter or tx.receiver or "-", "type": tx.operation_type,
        "amount": float(tx.amount), "currency": tx.currency, "date": tx.created_at.isoformat(),
        "alertLevel": c.alert_level, "status": c.status,
        "recommendation": c.recommendation, "escalatedTo": c.escalated_to_sub,
        "transactionStatus": tx.status,
    }


@api_view(["GET"])
@permission_classes([IsStaff, HasCapability("audit")])
def special_cases(request):
    return Response([_case_row(c) for c in SpecialCase.objects.select_related("transaction").all()[:500]])


@api_view(["POST"])
@permission_classes([IsStaff, HasCapability("audit")])
def special_case_escalate(request, case_id):
    case = SpecialCase.objects.filter(pk=case_id).first()
    if not case:
        return Response({"detail": "Cas introuvable."}, status=404)
    supervisor_sub = (request.data or {}).get("supervisorSub", "")
    case = services.escalate_special_case(case=case, supervisor_sub=supervisor_sub)
    return Response(_case_row(case))


@api_view(["GET", "PATCH"])
@permission_classes([IsStaff, HasCapability("config")])
def thresholds(request):
    if request.method == "GET":
        return Response([
            {"operationType": t.operation_type, "autoLimit": float(t.auto_limit),
             "managerLimit": float(t.manager_limit), "manualTimeoutHours": t.manual_timeout_hours}
            for t in ValidationThreshold.objects.all()
        ])
    data = request.data or {}
    threshold, _ = ValidationThreshold.objects.update_or_create(
        operation_type=data.get("operationType"),
        defaults={"auto_limit": data.get("autoLimit", "1000"), "manager_limit": data.get("managerLimit", "5000"),
                  "manual_timeout_hours": data.get("manualTimeoutHours", 24)},
    )
    return Response({"operationType": threshold.operation_type, "autoLimit": float(threshold.auto_limit),
                      "managerLimit": float(threshold.manager_limit)})


@api_view(["GET"])
@permission_classes([IsStaff, HasCapability("read")])
def supervision(request):
    return Response({
        "pendingCount": Transaction.objects.filter(status=FlowStatus.PENDING_VALIDATION).count(),
        "postedCount": Transaction.objects.filter(status=FlowStatus.POSTED).count(),
        "specialCasesCount": SpecialCase.objects.exclude(status=SpecialCase.Status.BLOQUE).count(),
        "overdueCount": services.overdue_pending_count(),
    })
