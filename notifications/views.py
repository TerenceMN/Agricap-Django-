"""API notifications (ClientNotifications.jsx / InvestorNotifications.jsx)."""
from __future__ import annotations

from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .models import Notification


def _row(n: Notification) -> dict:
    return {"id": n.pk, "title": n.title, "body": n.body, "read": n.read, "createdAt": n.created_at.isoformat()}


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def my_notifications(request):
    return Response([_row(n) for n in Notification.objects.filter(user=request.user)])


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def mark_read(request, notification_id):
    n = Notification.objects.filter(pk=notification_id, user=request.user).first()
    if not n:
        return Response({"detail": "Notification introuvable."}, status=404)
    n.read = True
    n.save(update_fields=["read"])
    return Response(_row(n))
