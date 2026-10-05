"""API du moteur d'alertes (Supervision.jsx)."""
from __future__ import annotations

from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response

from accounts.permissions import IsStaff
from rbac.permissions import HasCapability

from . import services
from .models import Alert, AlertRule


def _rule_row(r: AlertRule) -> dict:
    return {
        "id": r.pk, "code": r.code, "name": r.name, "description": r.description, "metric": r.metric,
        "operator": r.operator, "threshold": r.threshold, "severity": r.severity, "enabled": r.enabled,
        "notifyPhone": r.notify_phone,
    }


def _alert_row(a: Alert) -> dict:
    return {
        "id": a.pk, "ruleCode": a.rule.code if a.rule else None, "severity": a.severity, "title": a.title,
        "body": a.body, "sourceType": a.source_type, "sourceId": a.source_id, "status": a.status,
        "triggeredAt": a.triggered_at.isoformat(),
        "acknowledgedBy": a.acknowledged_by, "acknowledgedAt": a.acknowledged_at.isoformat() if a.acknowledged_at else None,
        "resolvedBy": a.resolved_by, "resolvedAt": a.resolved_at.isoformat() if a.resolved_at else None,
        "resolutionNote": a.resolution_note,
    }


@api_view(["GET", "POST"])
@permission_classes([HasCapability("config")])
def alert_rules(request):
    if request.method == "GET":
        return Response([_rule_row(r) for r in AlertRule.objects.all()])
    data = request.data or {}
    rule = services.create_alert_rule(
        code=data.get("code", ""), name=data.get("name", ""), metric=data.get("metric", ""),
        operator=data.get("operator", ""), threshold=data.get("threshold", 0), severity=data.get("severity", ""),
        description=data.get("description", ""), notify_phone=data.get("notifyPhone", ""),
        by=getattr(request.user, "sub", ""),
    )
    return Response(_rule_row(rule), status=201)


@api_view(["PATCH"])
@permission_classes([HasCapability("config")])
def alert_rule_detail(request, rule_id):
    rule = AlertRule.objects.filter(pk=rule_id).first()
    if not rule:
        return Response({"detail": "Règle introuvable."}, status=404)
    data = request.data or {}
    rule = services.update_alert_rule(
        rule=rule, by=getattr(request.user, "sub", ""),
        name=data.get("name"), description=data.get("description"), operator=data.get("operator"),
        threshold=data.get("threshold"), severity=data.get("severity"), enabled=data.get("enabled"),
        notify_phone=data.get("notifyPhone"),
    )
    return Response(_rule_row(rule))


@api_view(["GET"])
@permission_classes([IsStaff, HasCapability("read")])
def alerts(request):
    """Les alertes de supervision de l'institution (seuils de trésorerie, agences en
    écart). Interne : aucune ne concerne un membre en particulier."""
    services.evaluate_and_sync_alerts()
    qs = Alert.objects.select_related("rule").all()
    status = request.GET.get("status")
    severity = request.GET.get("severity")
    if status:
        qs = qs.filter(status=status)
    if severity:
        qs = qs.filter(severity=severity)
    return Response([_alert_row(a) for a in qs[:200]])


@api_view(["POST"])
@permission_classes([HasCapability("validate")])
def alert_acknowledge(request, alert_id):
    alert = Alert.objects.filter(pk=alert_id).first()
    if not alert:
        return Response({"detail": "Alerte introuvable."}, status=404)
    alert = services.acknowledge_alert(alert=alert, by=getattr(request.user, "sub", ""))
    return Response(_alert_row(alert))


@api_view(["POST"])
@permission_classes([HasCapability("validate")])
def alert_resolve(request, alert_id):
    alert = Alert.objects.filter(pk=alert_id).first()
    if not alert:
        return Response({"detail": "Alerte introuvable."}, status=404)
    alert = services.resolve_alert(alert=alert, note=(request.data or {}).get("resolutionNote", ""),
                                    by=getattr(request.user, "sub", ""))
    return Response(_alert_row(alert))
