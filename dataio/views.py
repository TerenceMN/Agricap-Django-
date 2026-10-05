"""
API de la couche générique (admin) : upload → aperçu (sans écriture), enregistrement
manuel (commit), historique des versions, exploration des tables générées.
"""
from __future__ import annotations

from rest_framework.decorators import api_view, parser_classes, permission_classes
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.response import Response
from rest_framework import status

from accounts.permissions import IsStaff
from . import services
from .models import DataSource, DataTable


def _source_dict(s: DataSource) -> dict:
    return {
        "id": s.pk, "original_name": s.original_name, "kind": s.kind, "status": s.status,
        "revision": s.revision, "is_current": s.is_current, "dataset_key": s.dataset_key,
        "uploaded_at": s.uploaded_at, "committed_at": s.committed_at,
        "supersedes": s.supersedes_id, "n_tables": s.tables.count(),
        "sha256": s.sha256,
        "credit_application": s.credit_application.code if s.credit_application_id else None,
    }


@api_view(["GET", "POST"])
@parser_classes([MultiPartParser, FormParser])
@permission_classes([IsStaff])
def sources(request):
    """GET : liste (historique). POST : upload d'un classeur → aperçu (aucune écriture de lignes)."""
    if request.method == "GET":
        return Response([_source_dict(s) for s in DataSource.objects.all()])

    f = request.FILES.get("file")
    if not f:
        return Response({"detail": "Fichier « file » requis."}, status=400)
    src = DataSource(
        original_name=f.name, dataset_key=services.dataset_key(f.name),
        uploaded_by=getattr(request.user, "sub", ""),
    )
    src.file = f
    src.save()
    preview = services.inspect(src)
    return Response({**_source_dict(src), "preview": preview}, status=status.HTTP_201_CREATED)


@api_view(["GET", "DELETE"])
@permission_classes([IsStaff])
def source_detail(request, pk):
    src = DataSource.objects.filter(pk=pk).first()
    if not src:
        return Response({"detail": "Introuvable."}, status=404)
    if request.method == "DELETE":
        try:
            result = services.delete_source(src)
        except services.SourceProtected as exc:
            return Response(
                {"detail": exc.message, "errors": [{"code": exc.code, "message": exc.message}]},
                status=status.HTTP_409_CONFLICT,
            )
        detail = "Source et données supprimées."
        if result.get("typed_removed"):
            detail += f" Version référentiel retirée ({result['typed_removed']})."
        if result.get("promoted"):
            detail += " Révision précédente promue courante."
        return Response({"detail": detail, **result})
    return Response({**_source_dict(src), "preview": src.preview})


@api_view(["POST"])
@permission_classes([IsStaff])
def commit_source(request, pk):
    """Enregistrement MANUEL : écrit les tables + versionne (historique) + alimente le typé."""
    src = DataSource.objects.filter(pk=pk).first()
    if not src:
        return Response({"detail": "Introuvable."}, status=404)
    if src.status == "COMMITTED":
        return Response({"detail": "Déjà enregistré.", **_source_dict(src)})
    result = services.commit(src, by=getattr(request.user, "sub", ""))
    return Response({"detail": "Enregistré.", **_source_dict(src), "commit": result})


@api_view(["GET"])
@permission_classes([IsStaff])
def source_tables(request, pk):
    """Tables/colonnes/lignes générées pour une source (exploration)."""
    src = DataSource.objects.filter(pk=pk).first()
    if not src:
        return Response({"detail": "Introuvable."}, status=404)
    out = []
    for t in src.tables.all():
        out.append({
            "id": t.pk, "name": t.name, "n_rows": t.n_rows, "n_cols": t.n_cols,
            "editable": True,
            "columns": [{"name": c.name, "dtype": c.dtype} for c in t.columns.all()],
            # Chaque ligne porte son id → édition ciblée (PATCH) côté admin.
            "rows": [{"id": r.pk, "values": r.values} for r in t.records.all()[:500]],
        })
    return Response({"source": _source_dict(src), "tables": out})


@api_view(["PATCH"])
@permission_classes([IsStaff])
def table_detail(request, pk):
    """Renommer le titre d'une table (édition admin). Corps : { name }."""
    table = DataTable.objects.filter(pk=pk).select_related("source").first()
    if not table:
        return Response({"detail": "Table introuvable."}, status=404)
    name = (request.data.get("name") or "").strip() if isinstance(request.data, dict) else ""
    if not name:
        return Response({"detail": "Le nom de la table est requis."}, status=400)
    name = name[:200]
    if DataTable.objects.filter(source=table.source, name=name).exclude(pk=table.pk).exists():
        return Response({"detail": "Une table porte déjà ce nom dans cette source."}, status=409)
    old = table.name
    table.name = name
    table.save(update_fields=["name"])
    return Response({"detail": f"Table renommée « {name} ».", "id": table.pk, "name": name, "old_name": old})


@api_view(["POST", "PATCH"])
@permission_classes([IsStaff])
def update_table_records(request, pk):
    """Corrections manuelles des lignes d'une table (édition admin), corps { records:[{id,values}] }."""
    table = DataTable.objects.filter(pk=pk).select_related("source").first()
    if not table:
        return Response({"detail": "Table introuvable."}, status=404)
    body = request.data if isinstance(request.data, dict) else {}
    updates = body.get("records")
    deletions = body.get("delete")
    if not isinstance(updates, list) and not isinstance(deletions, list):
        return Response({"detail": "Corps attendu : { records: [{ id, values }], delete: [id] }."}, status=400)
    result = services.update_records(
        table,
        updates if isinstance(updates, list) else [],
        deletions if isinstance(deletions, list) else [],
        by=getattr(request.user, "sub", ""),
    )

    parts = []
    if result["changed"]:
        parts.append(f"{result['changed']} ligne(s) modifiée(s)")
    if result["deleted"]:
        parts.append(f"{result['deleted']} supprimée(s)")
    detail = (", ".join(parts) or "Aucun changement") + "."
    typed = result.get("typed")
    if typed and "plages" in typed:
        detail += f" Référentiel re-synchronisé ({typed['plages']} plage(s) relues par le moteur)."
    elif typed and "config" in typed:
        detail += " Paramètres de l'institution re-synchronisés."
    return Response({"detail": detail, **result})


@api_view(["GET"])
@permission_classes([IsStaff])
def history(request):
    """Historique d'un dataset (toutes les révisions), clé ?key=<dataset_key>."""
    key = request.GET.get("key", "")
    qs = DataSource.objects.filter(dataset_key=key).order_by("-revision") if key else DataSource.objects.all()
    return Response([_source_dict(s) for s in qs])
