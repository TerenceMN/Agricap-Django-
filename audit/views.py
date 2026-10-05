"""API de lecture du journal d'audit (`AuditLog.jsx`, `AuditJournal.tsx`) — réservée au
personnel disposant de la capacité `audit`. LECTURE SEULE ABSOLUE : aucune écriture n'est
possible depuis cet écran (c'est l'écran de l'auditeur)."""
from __future__ import annotations

import csv
import json

from django.db.models import Q
from django.http import StreamingHttpResponse
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response

from accounts import directory
from rbac.permissions import HasCapability

from .models import AuditEntry

#: Plafond de lignes renvoyées (aligné sur `AUDIT_ROWS_CAP` du front). Au-delà, `totalRows`
#: signale la troncature pour que l'auditeur affine ses filtres plutôt que de croire tout voir.
ROWS_CAP = 500

# Préfixes d'action = opérations financières/métier réellement effectuées sur la
# plateforme (Supervision.jsx « Activités Récentes ») — PAR OPPOSITION à `agency.*`/
# `rbac.*`, qui sont des actions de configuration/administration système, pas des
# opérations d'AGRICAP FINTECH à superviser financièrement. Liste blanche délibérée (pas
# une exclusion) : un futur type d'action système ne fuite pas silencieusement dans ce
# flux tant qu'il n'est pas explicitement ajouté ici.
FINANCIAL_ACTION_PREFIXES = (
    "transaction.", "investments.", "portfolio.", "caisses.", "savings.",
    "contract.", "fx.", "ledger.", "kyc.", "assets.",
)


#: Résolution `sub` → nom : service d'annuaire de `accounts` (propriétaire du miroir
#: IdP), pas une requête locale. Défini ici à l'origine, remonté quand un deuxième
#: écran a eu le même besoin (colonne « Gestionnaire » des investisseurs).
_names_by_sub = directory.names_by_sub


def _row(e: AuditEntry, names_by_sub: dict[str, str]) -> dict:
    return {
        "id": e.id,
        "timestamp": e.created_at.isoformat(),
        "user": e.actor,
        "userName": names_by_sub.get(e.actor, e.actor or "Système"),
        "role": e.actor_role,
        "action": e.action,
        "entityType": e.entity_type,
        "entityId": e.entity_id,
        "details": e.details,
        "ip": e.ip_address,
    }


def _apply_filters(qs, params):
    """Applique les filtres de lecture. Tous optionnels, tous additifs — aucun n'écrit rien.

    Filtres du contrat (§4) : `dossier`, `acteur`, `etape`, `depuis`, `jusqu`.
    Filtres historiques conservés : `entity_type`, `entity_id`, `actor`, `category`.
    """
    entity_type = params.get("entity_type")
    entity_id = params.get("entity_id")
    # `acteur` (contrat) ou `actor` (historique) — même cible : le sub de l'acteur.
    actor = params.get("acteur") or params.get("actor")
    category = params.get("category")
    # Le code du dossier de crédit vit dans `details.applicationCode` (clé canonique posée
    # par `credits.analyse`/`credits.guarantees`) — jamais dans une colonne dédiée.
    dossier = params.get("dossier")
    # L'« étape » d'instruction = le nom d'action (workflow). Sous-chaîne : « analyse »
    # attrape `credits.analyse.execute` et `credits.analyse.justifier`.
    etape = params.get("etape")
    depuis = params.get("depuis")
    jusqu = params.get("jusqu")

    if entity_type:
        qs = qs.filter(entity_type=entity_type)
    if entity_id:
        qs = qs.filter(entity_id=entity_id)
    if actor:
        qs = qs.filter(actor=actor)
    if dossier:
        qs = qs.filter(Q(details__applicationCode=dossier) | Q(details__reference=dossier))
    if etape:
        qs = qs.filter(action__icontains=etape)
    if category == "financial":
        prefix_filter = Q()
        for prefix in FINANCIAL_ACTION_PREFIXES:
            prefix_filter |= Q(action__startswith=prefix)
        qs = qs.filter(prefix_filter)

    if depuis:
        dt = parse_datetime(depuis)
        if dt is not None:
            qs = qs.filter(created_at__gte=dt)
        else:
            d = parse_date(depuis)
            if d is not None:
                qs = qs.filter(created_at__date__gte=d)
    if jusqu:
        dt = parse_datetime(jusqu)
        if dt is not None:
            qs = qs.filter(created_at__lte=dt)
        else:
            d = parse_date(jusqu)
            if d is not None:
                # Borne haute inclusive du jour entier.
                qs = qs.filter(created_at__date__lte=d)
    return qs


@api_view(["GET"])
@permission_classes([HasCapability("audit")])
def entries(request):
    qs = _apply_filters(AuditEntry.objects.all(), request.GET)

    total = qs.count()
    rows = list(qs[:ROWS_CAP])
    truncated = total > ROWS_CAP

    # Résolution `sub` -> nom lisible en un seul aller-retour.
    names_by_sub = _names_by_sub(e.actor for e in rows)
    data = [_row(e, names_by_sub) for e in rows]

    # `totalRows` / troncature (contrat §4). Rétro-compatibilité : la réponse reste une
    # LISTE nue par défaut (les consommateurs existants — Dashboard, Supervision, Users,
    # Transactions… — la lisent comme un tableau). Le total est TOUJOURS exposé en en-tête ;
    # `?meta=1` le renvoie aussi dans le corps pour l'écran auditeur qui l'affiche.
    if request.GET.get("meta") in ("1", "true", "yes"):
        response = Response({
            "entries": data,
            "totalRows": total,
            "returned": len(data),
            "truncated": truncated,
            "cap": ROWS_CAP,
        })
    else:
        response = Response(data)
    response["X-Total-Rows"] = str(total)
    response["X-Truncated"] = "1" if truncated else "0"
    return response


# ── Export CSV (écran auditeur) ──────────────────────────────────────────────
#
# Colonnes de l'export, dans l'ordre. Français (l'auditeur ouvre le fichier dans
# Excel/LibreOffice). `Détails` contient le JSON brut de l'entrée — c'est là que vit
# le code du dossier (`applicationCode`), donc rien n'est perdu à l'export.
EXPORT_HEADER = [
    "Horodatage", "Acteur (sub)", "Acteur (nom)", "Rôle", "Action",
    "Type entité", "Id entité", "Détails", "IP",
]


class _Echo:
    """Buffer pseudo-fichier pour `csv.writer` : `write` renvoie la ligne au lieu de la
    stocker, ce qui permet de streamer l'export sans le matérialiser en mémoire."""

    def write(self, value: str) -> str:
        return value


def _export_rows(qs, names_by_sub: dict[str, str]):
    """Générateur des lignes CSV. BOM UTF-8 en tête pour qu'Excel affiche correctement
    les accents. `qs.iterator()` : on ne charge jamais tout le journal en mémoire."""
    writer = csv.writer(_Echo())
    yield "\ufeff" + writer.writerow(EXPORT_HEADER)
    for e in qs.iterator(chunk_size=500):
        details = ""
        if e.details:
            details = json.dumps(e.details, ensure_ascii=False, sort_keys=True)
        yield writer.writerow([
            e.created_at.isoformat(),
            e.actor or "",
            names_by_sub.get(e.actor, "") if e.actor else "",
            e.actor_role or "",
            e.action,
            e.entity_type or "",
            e.entity_id or "",
            details,
            e.ip_address or "",
        ])


@api_view(["GET"])
@permission_classes([HasCapability("audit")])
def export(request):
    """Export CSV du journal filtré — mêmes filtres que `entries`, capacité `audit`,
    LECTURE SEULE. Contrairement à `entries` (plafonné à ROWS_CAP pour l'affichage),
    l'export est COMPLET sur le périmètre filtré : un auditeur doit obtenir l'intégralité
    des lignes correspondant à ses critères, jamais un sous-ensemble tronqué en silence.
    """
    qs = _apply_filters(AuditEntry.objects.all(), request.GET).order_by("-created_at")

    total = qs.count()
    # Noms résolus une fois, sur les acteurs DISTINCTS du périmètre (borné par l'effectif
    # du personnel, pas par le nombre de lignes) — puis stream sans re-requête par ligne.
    distinct_actors = (qs.exclude(actor="").order_by()
                       .values_list("actor", flat=True).distinct())
    names_by_sub = _names_by_sub(distinct_actors)

    stamp = timezone.now().strftime("%Y%m%d_%H%M%S")
    response = StreamingHttpResponse(
        _export_rows(qs, names_by_sub), content_type="text/csv; charset=utf-8",
    )
    response["Content-Disposition"] = f'attachment; filename="journal_audit_{stamp}.csv"'
    # L'export étant complet, la troncature est toujours fausse ; le total reste exposé
    # pour recoupement avec l'écran.
    response["X-Total-Rows"] = str(total)
    response["X-Truncated"] = "0"
    return response
