"""API KYC/AML + documents (Compliance.jsx, ClientDocuments.jsx, InvestorDocuments.jsx).

Deux publics strictement séparés, et c'est la ligne de partage de tout ce module :

* les vues **`.../mine`** servent au titulaire SES données — filtrées par
  `user=request.user`, donc jamais adressables par identifiant d'un tiers ;
* toutes les autres servent le dossier KYC/AML **d'autrui** et relèvent de la conformité :
  `IsStaff` cumulé à la capacité.

`kyc_profiles` était l'exception qui ne devait pas exister : gardé par
`HasCapability("read")`, il renvoyait pour TOUS les utilisateurs leur statut KYC et leur
**score de risque AML** — le jugement que l'institution porte sur ses membres. Or les
rôles clients portent `read=True` : n'importe quel membre lisait la liste complète.
"""
from __future__ import annotations

from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from accounts.permissions import IsStaff
from audit.services import record as audit_record
from common.choices import FlowStatus
from rbac.permissions import HasCapability

from . import kyc_levels
from .models import Document, KycProfile


@api_view(["GET"])
@permission_classes([IsStaff, HasCapability("read")])
def kyc_profiles(request):
    """Le registre KYC/AML de l'institution — statut et score de risque de chaque membre.

    Réservé au personnel : un score de risque AML est une appréciation interne, et la
    connaître (la sienne comme celle d'un tiers) permet de calibrer son comportement pour
    en sortir. Le titulaire a `GET /compliance/kyc/mine`, qui sert son niveau, son statut
    et son plafond — jamais son score.
    """
    return Response([
        {"userSub": k.user_id, "kycStatus": k.kyc_status, "riskScore": k.risk_score, "kycLevel": k.kyc_level,
         "monthlyLimit": float(k.monthly_limit)}
        for k in KycProfile.objects.select_related("user").all()
    ])


@api_view(["POST"])
@permission_classes([IsStaff, HasCapability("validate")])
def validate_kyc(request, user_sub):
    profile, _ = KycProfile.objects.get_or_create(user_id=user_sub)
    profile.kyc_status = KycProfile.Status.VALIDE
    profile.save(update_fields=["kyc_status", "updated_at"])
    audit_record(actor=getattr(request.user, "sub", ""), action="kyc.validate", entity_type="KycProfile",
                 entity_id=user_sub)
    return Response({"userSub": user_sub, "kycStatus": profile.kyc_status})


def _doc_row(d: Document) -> dict:
    return {"id": d.pk, "type": d.type, "name": d.name, "date": d.created_at.isoformat(), "status": d.status,
             "fileUrl": d.file_url}


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def my_documents(request):
    if request.method == "GET":
        return Response([_doc_row(d) for d in Document.objects.filter(user=request.user)])
    data = request.data or {}
    doc = Document.objects.create(user=request.user, type=data.get("type", "other"), name=data.get("name", ""),
                                   file_url=data.get("fileUrl", ""))
    return Response(_doc_row(doc), status=201)


@api_view(["POST"])
@permission_classes([IsStaff, HasCapability("validate")])
def document_review(request, doc_id):
    doc = Document.objects.filter(pk=doc_id).first()
    if not doc:
        return Response({"detail": "Document introuvable."}, status=404)
    decision = (request.data or {}).get("status")
    if decision not in (FlowStatus.APPROVED, FlowStatus.REJECTED):
        return Response({"detail": "status doit être 'approved' ou 'rejected'."}, status=400)
    doc.status = decision
    doc.save(update_fields=["status"])
    audit_record(actor=getattr(request.user, "sub", ""), action="compliance.document.review",
                 entity_type="Document", entity_id=str(doc.pk), details={"status": decision})
    profile = kyc_levels.sync_kyc_level(user=doc.user)
    return Response({"id": doc.pk, "status": doc.status, "kycLevel": profile.kyc_level,
                      "monthlyLimit": float(profile.monthly_limit)})


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def my_kyc(request):
    profile = kyc_levels.sync_kyc_level(user=request.user)
    withdrawn_this_month = {
        currency: float(kyc_levels.monthly_withdrawal_total(user=request.user, currency=currency))
        for currency in ("USD", "CDF")
    }
    return Response({
        "kycStatus": profile.kyc_status, "kycLevel": profile.kyc_level,
        "monthlyLimit": float(profile.monthly_limit), "withdrawnThisMonth": withdrawn_this_month,
    })
