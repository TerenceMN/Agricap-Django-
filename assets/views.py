"""API des actifs gageables.

Deux surfaces distinctes :
  - client  : déclare et décrit ses actifs (`/mine`) — ne fixe JAMAIS leur statut
              ni leur valeur retenue ;
  - agent   : file de vérification terrain (`/pending`, `/verify`, `/reject`).

Faille corrigée : l'ancien `PATCH /mine/<id>` acceptait `status` depuis le
payload client. Un client pouvait donc déclarer un actif puis le passer lui-même
en « libre » et l'engager en garantie, sans qu'aucun agent ne l'ait jamais vu.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation

from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from audit.services import record as audit_record
from credits.roles import CAN_VERIFY_ASSET, in_group

from .models import Asset
from .services import AssetError, reject_asset, verify_asset


#: Champs qu'un client peut écrire. `status`, `valeur_retenue`, `gage_application`
#: et les champs de vérification en sont volontairement absents.
CLIENT_WRITABLE = ("name", "type", "value", "currency", "description",
                   "localisation", "image", "documents")


def _row(a: Asset, *, staff: bool = False) -> dict:
    row = {
        "id": a.pk,
        "name": a.name,
        "type": a.type,
        "value": float(a.value),
        "currency": a.currency,
        "description": a.description,
        "localisation": a.localisation,
        "status": a.status,
        "image": a.image,
        "documents": a.documents or [],
        "valeurRetenue": float(a.valeur_retenue) if a.valeur_retenue is not None else None,
        "isPledgeable": a.is_pledgeable,
        "guaranteeType": a.guarantee_type or None,
        "motifRejet": a.motif_rejet or None,
        "verifieLe": a.verifie_le.isoformat() if a.verifie_le else None,
        "createdAt": a.created_at.isoformat(),
    }
    if staff:
        row["verifieParSub"] = a.verifie_par_sub or None
        row["gageApplication"] = (
            a.gage_application.code if a.gage_application_id else None
        )
    return row


def _decimal(value, field: str) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise AssetError(f"{field} : valeur numérique invalide.")


# ── Surface client ────────────────────────────────────────────────────────────

@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def my_assets(request):
    """GET  : mes actifs, filtrables par `?status=` et `?pledgeable=true`.
    POST : déclare un nouvel actif — toujours créé au statut `declare`.
    """
    if request.method == "GET":
        qs = Asset.objects.filter(user=request.user)
        status_filter = request.query_params.get("status")
        if status_filter:
            qs = qs.filter(status=status_filter)
        rows = [_row(a) for a in qs]
        if request.query_params.get("pledgeable") == "true":
            rows = [r for r in rows if r["isPledgeable"]]
        return Response({"total_rows": len(rows), "items": rows})

    data = request.data or {}
    asset = Asset.objects.create(
        user=request.user,
        name=data.get("name", ""),
        type=data.get("type", Asset.Type.AUTRE),
        value=data.get("value", "0"),
        currency=data.get("currency", "USD"),
        description=data.get("description", ""),
        localisation=data.get("localisation", ""),
        image=data.get("image", ""),
        documents=data.get("documents", []),
        status=Asset.Status.DECLARE,      # jamais autre chose à la création
    )
    audit_record(actor=getattr(request.user, "sub", ""), action="assets.create",
                 entity_type="Asset", entity_id=str(asset.pk))
    return Response(_row(asset), status=201)


@api_view(["GET", "PATCH", "DELETE"])
@permission_classes([IsAuthenticated])
def asset_detail(request, asset_id):
    asset = Asset.objects.filter(pk=asset_id, user=request.user).first()
    if not asset:
        return Response({"detail": "Actif introuvable."}, status=404)

    if request.method == "DELETE":
        # Un actif nanti est une pièce du dossier de crédit : il ne se supprime pas.
        if asset.status == Asset.Status.GAGE:
            return Response(
                {"detail": "Cet actif est nanti sur un dossier de crédit et ne peut pas "
                           "être supprimé.", "code": "ASSET_PLEDGED"},
                status=409,
            )
        asset.delete()
        audit_record(actor=getattr(request.user, "sub", ""), action="assets.delete",
                     entity_type="Asset", entity_id=str(asset_id))
        return Response({"detail": "Actif supprimé."})

    if request.method == "PATCH":
        if asset.status == Asset.Status.GAGE:
            return Response(
                {"detail": "Cet actif est nanti : sa description ne peut plus être modifiée.",
                 "code": "ASSET_PLEDGED"},
                status=409,
            )
        data = request.data or {}
        rejected = [f for f in data if f not in CLIENT_WRITABLE]
        if rejected:
            return Response(
                {"detail": f"Champs non modifiables par le client : {', '.join(rejected)}.",
                 "code": "FIELD_NOT_WRITABLE"},
                status=403,
            )
        for field in CLIENT_WRITABLE:
            if field in data:
                setattr(asset, field, data[field])
        # Toute modification invalide la vérification précédente (`verifie` ET
        # `libere` — la règle vit dans le service, elle est testée là-bas).
        from assets.services import invalidate_verification
        invalidate_verification(asset)
        asset.save()

    return Response(_row(asset))


# ── Surface agent terrain ─────────────────────────────────────────────────────

@api_view(["GET"])
@permission_classes([IsAuthenticated])
def pending_verification(request):
    """File de vérification : actifs déclarés en attente de contrôle terrain."""
    if not in_group(request, CAN_VERIFY_ASSET):
        return Response({"detail": "Réservé aux agents de terrain."}, status=403)

    qs = Asset.objects.filter(status=Asset.Status.DECLARE).select_related(
        "user", "gage_application",
    )
    rows = []
    for a in qs:
        row = _row(a, staff=True)
        row["owner"] = {
            "sub": a.user.sub,
            "displayName": a.user.full_name or a.user.sub,
            "phone": a.user.phone,
        }
        rows.append(row)
    return Response({"total_rows": len(rows), "items": rows})


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def verification_history(request):
    """Historique des actifs déjà instruits — ce que l'agent a vérifié ou rejeté.

    `pending_verification` ne sert QUE les `declare` : une fois l'acte posé,
    l'actif disparaissait de l'écran sans laisser de trace consultable. L'agent
    ne pouvait donc ni revoir une valeur qu'il avait retenue, ni relire le motif
    d'un rejet qu'on lui opposait, ni constater qu'un actif qu'il avait vérifié
    était depuis gagé. Une file sans historique oblige à se souvenir.

    Lecture seule : instruire se fait par `verify`/`reject`, jamais ici.
    `?status=` restreint à un statut ; sans paramètre, tout le post-déclaratif.
    """
    if not in_group(request, CAN_VERIFY_ASSET):
        return Response({"detail": "Réservé aux agents de terrain."}, status=403)

    #: Tout sauf `declare`, qui est la file d'attente et vit dans l'autre vue.
    instruits = [s for s in Asset.Status.values if s != Asset.Status.DECLARE]
    demande = (request.GET.get("status") or "").strip()
    if demande and demande not in instruits:
        return Response(
            {"detail": f"Statut inconnu. Valeurs acceptées : {instruits}.",
             "code": "STATUT_INCONNU"},
            status=400,
        )

    qs = Asset.objects.filter(status=demande or None) if demande else \
        Asset.objects.filter(status__in=instruits)
    # `verifie_le` décroissant : l'agent cherche ce qu'il vient de faire. Les
    # actifs sans horodatage (rejets anciens) restent visibles en fin de liste
    # plutôt que d'être exclus par un tri qui les ignorerait.
    qs = qs.select_related("user", "gage_application").order_by("-verifie_le", "-id")

    rows = []
    for a in qs:
        row = _row(a, staff=True)
        row["owner"] = {
            "sub": a.user.sub,
            "displayName": a.user.full_name or a.user.sub,
            "phone": a.user.phone,
        }
        rows.append(row)
    return Response({"total_rows": len(rows), "items": rows})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def verify(request, asset_id):
    """POST /api/assets/<id>/verify/ — `{valeur_verifiee, documents?}`.

    La valeur retenue est calculée par le serveur (valeur constatée moins décote
    institutionnelle) : l'agent constate, il ne négocie pas la décote.
    """
    if not in_group(request, CAN_VERIFY_ASSET):
        return Response({"detail": "Réservé aux agents de terrain."}, status=403)

    asset = Asset.objects.filter(pk=asset_id).first()
    if not asset:
        return Response({"detail": "Actif introuvable."}, status=404)

    data = request.data or {}
    if "valeur_verifiee" not in data:
        return Response({"detail": "valeur_verifiee requise."}, status=400)

    try:
        montant = _decimal(data["valeur_verifiee"], "valeur_verifiee")
        verify_asset(
            asset,
            verifier_sub=getattr(request.user, "sub", "") or "",
            valeur_verifiee=montant,
            documents=data.get("documents"),
        )
    except AssetError as exc:
        return Response({"detail": str(exc), "code": "ASSET_VERIFY_REFUSED"}, status=422)

    audit_record(actor=getattr(request.user, "sub", ""), action="assets.verify",
                 entity_type="Asset", entity_id=str(asset.pk))
    return Response(_row(asset, staff=True))


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def reject(request, asset_id):
    """POST /api/assets/<id>/reject/ — `{motif}` obligatoire."""
    if not in_group(request, CAN_VERIFY_ASSET):
        return Response({"detail": "Réservé aux agents de terrain."}, status=403)

    asset = Asset.objects.filter(pk=asset_id).first()
    if not asset:
        return Response({"detail": "Actif introuvable."}, status=404)

    try:
        reject_asset(
            asset,
            verifier_sub=getattr(request.user, "sub", "") or "",
            motif=(request.data or {}).get("motif", ""),
        )
    except AssetError as exc:
        return Response({"detail": str(exc), "code": "ASSET_REJECT_REFUSED"}, status=422)

    audit_record(actor=getattr(request.user, "sub", ""), action="assets.reject",
                 entity_type="Asset", entity_id=str(asset.pk))
    return Response(_row(asset, staff=True))
