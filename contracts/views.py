"""API contrats (Contracts.jsx)."""
from __future__ import annotations

from django.utils import timezone
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from audit.services import record as audit_record
from common.exceptions import ValidationFailed

from .models import Contract


def _row(c: Contract) -> dict:
    return {"id": c.pk, "title": c.title, "date": c.created_at.isoformat(), "type": c.type, "status": c.status}


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def my_contracts(request):
    return Response([_row(c) for c in Contract.objects.filter(user=request.user)])


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def sign_contract(request, contract_id):
    contract = Contract.objects.filter(pk=contract_id, user=request.user).first()
    if not contract:
        return Response({"detail": "Contrat introuvable."}, status=404)
    data = request.data or {}
    if not data.get("agreed") or not data.get("signature"):
        raise ValidationFailed("Signature et acceptation des conditions requises.")
    contract.signature = data["signature"]
    contract.signed_at = timezone.now()
    contract.status = Contract.Status.ACTIF
    contract.save()
    audit_record(actor=getattr(request.user, "sub", ""), action="contract.sign", entity_type="Contract",
                 entity_id=str(contract.pk))
    return Response(_row(contract))
