import hmac

from django.conf import settings
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from rbac.permissions import HasCapability

from .sms import send_sms


@api_view(["POST"])
@permission_classes([HasCapability("config")])
def sms_test(request):
    """Envoi SMS de test manuel — réservé aux administrateurs (capacité config)."""
    data = request.data or {}
    phone = (data.get("phone") or "").strip()
    message = (data.get("message") or "").strip()
    if not phone or not message:
        return Response({"detail": "phone et message sont requis."}, status=400)
    sent = send_sms(phone=phone, message=message)
    return Response({"sent": sent, "phone": phone, "message": message})


@api_view(["POST"])
@authentication_classes([])
@permission_classes([AllowAny])
def sms_relay(request):
    """Relais SMS pour les services internes AGRICAP (l'IdP notamment).

    Ces services n'ont pas de jeton utilisateur à présenter : l'autorisation
    passe par une clé partagée (`X-Internal-Key`), pas par le Bearer OIDC —
    d'où `authentication_classes([])` : on ne veut même pas que le middleware
    d'auth IdP tente de résoudre un jeton absent.

    Objectif : un seul chemin d'envoi SMS (`common/sms.py`, encodage Unicode
    correct pour les accents français) au lieu d'une passerelle Dream Digital
    dupliquée côté IdP — la duplication avait un mauvais paramètre d'encodage
    et les SMS étaient acceptés par la passerelle sans jamais être remis.
    """
    key = request.headers.get("X-Internal-Key", "")
    expected = getattr(settings, "INTERNAL_SMS_RELAY_KEY", "") or ""
    if not expected or not hmac.compare_digest(key, expected):
        return Response({"detail": "Non autorisé."}, status=401)

    data = request.data or {}
    phone = (data.get("phone") or "").strip()
    message = (data.get("message") or "").strip()
    if not phone or not message:
        return Response({"detail": "phone et message sont requis."}, status=400)
    sent = send_sms(phone=phone, message=message)
    return Response({"sent": sent})
