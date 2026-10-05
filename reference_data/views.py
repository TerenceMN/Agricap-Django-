from __future__ import annotations

from django.core.cache import cache
from rest_framework.decorators import api_view, parser_classes, permission_classes
from rest_framework.parsers import MultiPartParser
from rest_framework.response import Response

from accounts.permissions import IsStaff
from rbac.permissions import HasCapability

from .models import ReferenceFileUpload, ValueChain
from .services import CACHE_KEY_ACTIVE, activate_file, process_upload


def _vc_row(vc: ValueChain) -> dict:
    return {
        "code": vc.code,
        "label": vc.label,
        "cycleMonths": vc.cycle_months,
        "costPerHectareUsd": str(vc.cost_per_hectare_usd),
        "costPerHectareCdf": str(vc.cost_per_hectare_cdf),
        "moduleWeights": vc.module_weights,
        "riskFactor": str(vc.risk_factor),
        "minScoreRequired": vc.min_score_required,
        "baseRate": str(vc.base_rate),
        "harvestMonths": vc.harvest_months,
        "eligibleGuarantees": vc.eligible_guarantees,
    }


# ── POST /api/reference-data/upload/ ─────────────────────────────────────────

@api_view(["POST"])
@permission_classes([HasCapability("config")])
@parser_classes([MultiPartParser])
def upload_reference_file(request):
    file = request.FILES.get("file")
    if not file:
        return Response({"detail": "Aucun fichier fourni (champ 'file')."}, status=400)
    if not file.name.lower().endswith(".xlsx"):
        return Response({"detail": "Format attendu : fichier .xlsx"}, status=400)

    file_type = request.data.get("file_type", ReferenceFileUpload.FileType.VALUE_CHAINS)
    if file_type not in ReferenceFileUpload.FileType.values:
        return Response({
            "detail": f"Type invalide. Valeurs acceptées : {ReferenceFileUpload.FileType.values}",
        }, status=400)

    version = request.data.get("version", "") or file.name

    upload = ReferenceFileUpload.objects.create(
        file=file,
        file_type=file_type,
        version=version,
        uploaded_by=request.user.sub,
    )

    result = process_upload(upload)
    upload.refresh_from_db()

    if not result.get("valid", False):
        return Response({
            "valid": False,
            "uploadId": upload.pk,
            "status": upload.status,
            "structureError": result.get("structureError"),
            "errors": result.get("errors", []),
        }, status=422)

    return Response({
        "valid": True,
        "uploadId": upload.pk,
        "status": upload.status,
        "rowCount": upload.row_count,
        "diff": upload.diff_summary,
        "message": (
            f"{upload.row_count} chaîne(s) de valeur validée(s). "
            "En attente d'activation par un second administrateur."
        ),
    }, status=201)


# ── POST /api/reference-data/uploads/{id}/activate/ ──────────────────────────

@api_view(["POST"])
@permission_classes([HasCapability("config")])
def activate_reference_file(request, upload_id: int):
    try:
        upload = ReferenceFileUpload.objects.get(pk=upload_id)
    except ReferenceFileUpload.DoesNotExist:
        return Response({"detail": "Fichier introuvable."}, status=404)

    try:
        activate_file(upload, activator_sub=request.user.sub)
    except ValueError as exc:
        return Response({"detail": str(exc)}, status=400)

    return Response({
        "status": upload.status,
        "activatedAt": upload.activated_at.isoformat(),
        "activatedBy": upload.activated_by,
        "chainsCreated": upload.row_count,
        "message": f"Référentiel activé. {upload.row_count} chaîne(s) de valeur disponibles.",
    })


# ── GET /api/reference-data/value-chains/ ────────────────────────────────────

@api_view(["GET"])
@permission_classes([IsStaff, HasCapability("read")])
def list_value_chains(request):
    """Le référentiel des filières — coût/hectare, poids des modules, facteur de risque,
    score minimum requis et taux de base. C'est un BARÈME : principe 7 (anti-gaming) veut
    qu'il ne descende jamais chez un demandeur, qui y lirait où viser. Le seul écran qui
    le consomme est l'onglet Référence du backoffice (`ValueChainsPanel.tsx`), déjà prêt à
    afficher un refus. Le parcours client passe, lui, par `application/prefill/`, qui ne
    sert que le libellé des filières."""
    cached = cache.get(CACHE_KEY_ACTIVE)
    if cached is not None:
        return Response(cached)

    chains = (
        ValueChain.objects
        .filter(source_file__status=ReferenceFileUpload.Status.ACTIVE, active=True)
        .select_related("source_file")
        .order_by("label")
    )
    data = [_vc_row(vc) for vc in chains]
    cache.set(CACHE_KEY_ACTIVE, data, timeout=300)
    return Response(data)


# ── GET /api/reference-data/uploads/ ─────────────────────────────────────────

@api_view(["GET"])
@permission_classes([HasCapability("config")])
def list_uploads(request):
    qs = ReferenceFileUpload.objects.order_by("-uploaded_at")
    if ft := request.query_params.get("type"):
        qs = qs.filter(file_type=ft)

    data = [
        {
            "id": u.pk,
            "fileType": u.file_type,
            "version": u.version,
            "uploadedBy": u.uploaded_by,
            "uploadedAt": u.uploaded_at.isoformat(),
            "activatedBy": u.activated_by or None,
            "activatedAt": u.activated_at.isoformat() if u.activated_at else None,
            "status": u.status,
            "rowCount": u.row_count,
            "diff": u.diff_summary,
        }
        for u in qs[:50]
    ]
    return Response(data)
