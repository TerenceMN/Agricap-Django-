"""API partenaires (ApiPartners.jsx)."""
from __future__ import annotations

from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response

from accounts.permissions import IsStaff
from rbac.permissions import HasCapability

from . import services
from .models import Partner


def _row(p: Partner) -> dict:
    return {
        "id": p.pk, "name": p.name, "type": p.type, "status": p.status,
        "lastSync": p.last_sync.isoformat() if p.last_sync else None,
        "baseUrl": p.base_url, "circuitState": p.circuit_state, "consecutiveFailures": p.consecutive_failures,
    }


@api_view(["GET"])
@permission_classes([IsStaff, HasCapability("read")])
def partners(request):
    return Response([_row(p) for p in Partner.objects.all()])


@api_view(["PATCH"])
@permission_classes([HasCapability("config")])
def partner_configure(request, partner_id):
    p = Partner.objects.filter(pk=partner_id).first()
    if not p:
        return Response({"detail": "Partenaire introuvable."}, status=404)
    data = request.data or {}
    p = services.configure_partner(partner=p, base_url=data.get("baseUrl", ""), type_=data.get("type"),
                                    by=getattr(request.user, "sub", ""))
    return Response(_row(p))


@api_view(["POST"])
@permission_classes([HasCapability("config")])
def partner_sync(request, partner_id):
    p = Partner.objects.filter(pk=partner_id).first()
    if not p:
        return Response({"detail": "Partenaire introuvable."}, status=404)
    services.sync_partner(partner=p, by=getattr(request.user, "sub", ""))
    p.refresh_from_db()
    return Response(_row(p))


@api_view(["POST"])
@permission_classes([HasCapability("config")])
def partner_test(request, partner_id):
    p = Partner.objects.filter(pk=partner_id).first()
    if not p:
        return Response({"detail": "Partenaire introuvable."}, status=404)
    check = services.check_health(partner=p, by=getattr(request.user, "sub", ""))
    p.refresh_from_db()
    return Response({
        "partner": _row(p),
        "check": {"ok": check.ok, "latencyMs": check.latency_ms, "httpStatus": check.http_status,
                  "errorText": check.error_text, "checkedAt": check.checked_at.isoformat()},
    })


@api_view(["GET"])
@permission_classes([IsStaff, HasCapability("read")])
def partner_logs(request, partner_id):
    p = Partner.objects.filter(pk=partner_id).first()
    if not p:
        return Response({"detail": "Partenaire introuvable."}, status=404)
    rows = [
        {"type": "health", "ok": c.ok, "detail": c.error_text or f"HTTP {c.http_status}",
         "latencyMs": c.latency_ms, "timestamp": c.checked_at.isoformat()}
        for c in p.health_checks.all()[:50]
    ] + [
        {"type": "sync", "ok": s.status == "SUCCESS", "detail": s.error_text or s.status,
         "latencyMs": None, "timestamp": s.started_at.isoformat()}
        for s in p.sync_logs.all()[:50]
    ]
    rows.sort(key=lambda r: r["timestamp"], reverse=True)
    return Response(rows[:50])
