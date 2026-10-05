"""API fournisseurs (Suppliers.jsx)."""
from __future__ import annotations

from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response

from audit.services import record as audit_record
from accounts.permissions import IsStaff
from rbac.permissions import CapaciteSelonMethode, HasCapability

from .models import Supplier


def _row(s: Supplier) -> dict:
    return {"id": s.pk, "name": s.name, "category": s.category, "rating": s.rating,
            "complianceStatus": s.compliance_status, "blacklisted": s.blacklisted}


@api_view(["GET", "POST"])
@permission_classes([IsStaff, CapaciteSelonMethode(GET="read", POST="create")])
def suppliers(request):
    if request.method == "GET":
        return Response([_row(s) for s in Supplier.objects.all()])
    data = request.data or {}
    supplier = Supplier.objects.create(name=data.get("name", ""), category=data.get("category", ""),
                                        compliance_status=data.get("complianceStatus", ""))
    return Response(_row(supplier), status=201)


@api_view(["POST"])
@permission_classes([IsStaff, HasCapability("validate")])
def supplier_action(request, supplier_id):
    supplier = Supplier.objects.filter(pk=supplier_id).first()
    if not supplier:
        return Response({"detail": "Fournisseur introuvable."}, status=404)
    action = (request.data or {}).get("action")
    if action == "blacklist":
        supplier.blacklisted = True
    elif action == "suspend":
        supplier.compliance_status = "Suspendu"
    else:
        return Response({"detail": f"Action inconnue : {action}"}, status=400)
    supplier.save()
    audit_record(actor=getattr(request.user, "sub", ""), action=f"supplier.{action}", entity_type="Supplier",
                 entity_id=str(supplier.pk))
    return Response(_row(supplier))
