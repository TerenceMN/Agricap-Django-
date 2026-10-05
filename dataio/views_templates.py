"""
API des templates de fichiers versionnés (principe 11), onglet Référence de l'admin.

    GET  /api/dataio/templates/            → liste + template actif (id/version)
    POST /api/dataio/templates/upload      → upload (maker, SHA-256)
    POST /api/dataio/templates/<id>/activate → activation (checker ≠ maker)

Réservé à la capacité RBAC `config` (référentiel maker-checker), comme `reference_data`.
"""
from __future__ import annotations

from rest_framework.decorators import api_view, parser_classes, permission_classes
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.response import Response

from rbac.permissions import HasCapability

from . import services_templates as tpl_svc
from .models import FileTemplate, KIND_FEUILLE_BESOINS


def _template_row(t: FileTemplate, *, full: bool = False) -> dict:
    row = {
        "id": t.pk,
        "kind": t.kind,
        "chainCode": t.chain_code or None,
        "version": t.version,
        "status": t.status,
        "originalName": t.original_name,
        "sha256": t.sha256,
        "uploadedBy": t.uploaded_by or None,
        "uploadedAt": t.uploaded_at.isoformat(),
        "activatedBy": t.activated_by or None,
        "activatedAt": t.activated_at.isoformat() if t.activated_at else None,
        "supersedes": t.supersedes_id,
        # Résumé du schéma dérivé — suffisant pour la liste.
        "sheetNames": (t.schema or {}).get("sheet_names", []),
        "rubriques": (t.schema or {}).get("rubriques", []),
    }
    if full:
        row["schema"] = t.schema
    return row


def _active_ref(t: FileTemplate | None) -> dict | None:
    if t is None:
        return None
    return {"id": t.pk, "version": t.version, "kind": t.kind, "chainCode": t.chain_code or None,
            "activatedAt": t.activated_at.isoformat() if t.activated_at else None}


def _diff_baseline(t: FileTemplate) -> tuple[FileTemplate | None, str]:
    """Template de référence contre lequel diffuser `t`, et le libellé de ce choix.

    - `pending` → le template ACTIF de même `kind` ET même `chain_code` : c'est
      exactement la question que se pose le checker (« qu'est-ce que change
      l'activation de celui-ci, pour CETTE chaîne ? »).
    - `active` / `archived` → le template qu'il a remplacé (`supersedes`) : la trace
      historique de ce qui a changé au moment de SON activation.

    Le libellé est renvoyé au front pour qu'il annonce le périmètre du diff au lieu de
    le deviner (CONVENTIONS.md §4.6 : pas d'agrégat sans périmètre).
    """
    if t.status == FileTemplate.Status.PENDING:
        active = (FileTemplate.objects
                  .filter(kind=t.kind, chain_code=t.chain_code, status=FileTemplate.Status.ACTIVE)
                  .order_by("-activated_at", "-version").first())
        return active, "active"
    return t.supersedes, "supersedes"


@api_view(["GET"])
@permission_classes([HasCapability("config")])
def template_detail(request, pk: int):
    """Détail d'un template : schéma dérivé COMPLET + diff calculé côté serveur.

    Sans cet endpoint, le checker — qui par construction n'a pas fait l'upload — n'a
    accès au schéma détaillé et au diff que dans la réponse d'upload du maker, donc
    jamais au rechargement de l'écran : il activerait à l'aveugle, alors que c'est
    précisément l'information qui fonde sa décision (CONVENTIONS.md §7.1.5).

    Le diff n'est JAMAIS recalculé côté client (principe : zéro chiffre métier calculé
    par le front).
    """
    t = FileTemplate.objects.filter(pk=pk).first()
    if t is None:
        return Response({"detail": "Template introuvable."}, status=404)

    baseline, baseline_kind = _diff_baseline(t)
    return Response({
        **_template_row(t, full=True),
        "diff": tpl_svc.diff_schema(baseline.schema if baseline else {}, t.schema),
        "diffBaseline": (
            {"id": baseline.pk, "version": baseline.version, "relation": baseline_kind}
            if baseline else {"id": None, "version": None, "relation": baseline_kind}
        ),
    })


@api_view(["GET"])
@permission_classes([HasCapability("config")])
def templates(request):
    """Liste des templates (historique) + le template actif (id/version courante).

    `chain_code` (optionnel) restreint à une chaîne de valeur précise — `""` explicite
    (et non l'absence du paramètre) cible le générique lui-même, distinctement de
    « toutes chaînes confondues » quand le paramètre est omis.
    """
    kind = request.GET.get("kind", "")
    qs = FileTemplate.objects.all()
    if kind:
        qs = qs.filter(kind=kind)
    if "chain_code" in request.GET:
        qs = qs.filter(chain_code=request.GET.get("chain_code", ""))
    active = (qs.filter(status=FileTemplate.Status.ACTIVE)
              .order_by("-activated_at", "-version").first())
    return Response({
        "active": _active_ref(active),
        "templates": [_template_row(t) for t in qs[:100]],
    })


@api_view(["POST"])
@permission_classes([HasCapability("config")])
@parser_classes([MultiPartParser, FormParser])
def upload_template(request):
    """Upload d'un template (maker). Renvoie la ligne créée + schéma dérivé + diff/actif."""
    f = request.FILES.get("file")
    if not f:
        return Response({"detail": "Fichier « file » requis."}, status=400)

    kind = request.data.get("kind") or KIND_FEUILLE_BESOINS
    chain_code = (request.data.get("chainCode") or request.data.get("chain_code") or "").strip()
    try:
        tpl = tpl_svc.upload_template(
            f, kind=kind, uploaded_by=getattr(request.user, "sub", "") or "",
            chain_code=chain_code,
        )
    except tpl_svc.TemplateUploadError as exc:
        return Response(
            {"detail": exc.message, "errors": [{"code": exc.code, "message": exc.message}]},
            status=422,
        )

    active = tpl_svc.active_template(kind, chain_code)
    diff = tpl_svc.diff_schema(active.schema if active else {}, tpl.schema)
    chaine_msg = f" (chaîne {chain_code})" if chain_code else ""
    return Response(
        {**_template_row(tpl, full=True), "diff": diff,
         "message": (f"Template « {kind} »{chaine_msg} v{tpl.version} téléversé. "
                     f"En attente d'activation par un second administrateur (checker ≠ maker).")},
        status=201,
    )


@api_view(["POST"])
@permission_classes([HasCapability("config")])
def activate_template(request, pk: int):
    """Activation d'un template (checker ≠ maker). Le précédent actif passe `archived`."""
    tpl = FileTemplate.objects.filter(pk=pk).first()
    if tpl is None:
        return Response({"detail": "Template introuvable."}, status=404)

    try:
        tpl_svc.activate_template(tpl, activator_sub=getattr(request.user, "sub", "") or "")
    except tpl_svc.TemplateActivationError as exc:
        return Response(
            {"detail": exc.message, "errors": [{"code": exc.code, "message": exc.message}]},
            status=409,
        )

    return Response({
        **_template_row(tpl, full=True),
        "message": (f"Template « {tpl.kind} » v{tpl.version} activé. "
                    f"Le schéma dérivé devient la règle de validation des fichiers client."),
    })
