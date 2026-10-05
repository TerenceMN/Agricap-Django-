"""API des taux de change gouvernés (`ExchangeRateManager`, onglet FX de `ClientWallet.jsx`).

Contrat de compatibilité : `GET /api/fx/rates` reste un TABLEAU JSON dont chaque ligne
conserve les clés historiques (`tier`, `currency`, `buy`, `sell`, `effectiveDate`) —
`src/services/api.ts` les consomme telles quelles. Les champs de gouvernance s'ajoutent,
ils ne remplacent rien.

Par défaut, cette liste ne sert QUE les taux en vigueur (`ACTIF`). Un écran qui affiche
côte à côte un taux appliqué et un taux en attente sans le dire ferait croire à un taux qui
n'est servi à aucune conversion : l'historique et la corbeille se demandent explicitement
(`?history=1`, `?status=...`, `/rates/pending`).
"""
from __future__ import annotations

from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response

from accounts.permissions import IsStaff
from common.exceptions import NotFoundError
from common.parsing import to_date, to_int
from rbac.permissions import HasCapability

from . import services
from .models import ExchangeRate
from .permissions import CapabilityByMethod

#: Plafond de lignes servies ; le total réel accompagne toujours la liste tronquée
#: (CONVENTIONS.md §4.6 : « `total_rows` sur toute liste tronquée »).
MAX_ROWS = 200


#: Champs de GOUVERNANCE du taux : qui l'a saisi, qui l'a validé, de combien il a bougé
#: et à partir de quel seuil un second acteur devient obligatoire. Ce sont les rouages
#: internes de la fabrique du taux — un membre a le droit de connaître le PRIX qu'on lui
#: applique, pas la mécanique qui décide s'il passe tout seul (principe 7 : le seuil est
#: précisément ce qu'il ne faut pas publier, sous peine d'apprendre à rester dessous).
_RATE_STAFF_FIELDS = (
    "id", "status", "version", "source", "sourceReference", "variationPct", "thresholdPct",
    "referenceRateId", "supersedesId", "supersededAt", "reason", "createdBy", "createdAt",
    "validatedBy", "validatedAt", "validationReason",
)


def _rate_row_for(request, r: ExchangeRate) -> dict:
    """Choisit le sérialiseur selon le rôle — jamais un `if` d'affichage côté front.

    `current` et `convert` restent ouverts à tout porteur de `read` : le taux appliqué à
    une conversion est une information due au membre qui la subit. C'est la GOUVERNANCE
    du taux qui reste interne, pas le taux.
    """
    ligne = _rate_row(r)
    if getattr(getattr(request, "user", None), "is_staff_role", False):
        return ligne
    return {k: v for k, v in ligne.items() if k not in _RATE_STAFF_FIELDS}


def _rate_row(r: ExchangeRate) -> dict:
    return {
        "id": r.pk, "tier": r.tier, "currency": r.currency, "buy": float(r.buy_rate),
        "sell": float(r.sell_rate), "effectiveDate": r.effective_date.isoformat(),
        # ── Gouvernance (principe 5) ──
        "usage": r.usage,
        "status": r.status,
        "version": r.version,
        "source": r.source,
        "sourceReference": r.source_reference,
        "variationPct": str(r.variation_pct) if r.variation_pct is not None else None,
        "thresholdPct": str(r.threshold_pct) if r.threshold_pct is not None else None,
        "referenceRateId": r.reference_rate_id,
        "supersedesId": r.supersedes_id,
        "supersededAt": r.superseded_at.isoformat() if r.superseded_at else None,
        "reason": r.reason,
        "createdBy": r.created_by,
        "createdAt": r.created_at.isoformat() if r.created_at else None,
        "validatedBy": r.validated_by,
        "validatedAt": r.validated_at.isoformat() if r.validated_at else None,
        "validationReason": r.validation_reason,
    }


@api_view(["GET", "POST"])
@permission_classes([IsStaff, CapabilityByMethod(safe="read", unsafe="config")])
def rates(request):
    """GET : historique filtrable. POST : saisie d'un taux (maker)."""
    if request.method == "GET":
        qs = ExchangeRate.objects.all()
        tier = request.GET.get("tier")
        currency = request.GET.get("currency")
        usage = request.GET.get("usage")
        status_filter = request.GET.get("status")
        date_from = to_date(request.GET.get("from"))
        date_to = to_date(request.GET.get("to"))
        history = request.GET.get("history") in ("1", "true", "True")

        if tier:
            qs = qs.filter(tier=tier)
        if currency:
            qs = qs.filter(currency=currency)
        if usage:
            qs = qs.filter(usage=usage)
        if status_filter:
            qs = qs.filter(status=status_filter)
        elif not history:
            qs = qs.filter(status=ExchangeRate.Status.ACTIF)
        if date_from:
            qs = qs.filter(effective_date__gte=date_from)
        if date_to:
            qs = qs.filter(effective_date__lte=date_to)

        total = qs.count()
        limit = min(to_int(request.GET.get("limit"), MAX_ROWS) or MAX_ROWS, MAX_ROWS)
        rows = [_rate_row(r) for r in qs[:limit]]
        response = Response(rows)
        # Le contrat de la liste est un tableau : le total réel voyage en en-tête plutôt
        # que d'enfermer la liste dans une enveloppe qui casserait le front existant.
        response["X-Total-Rows"] = str(total)
        response["X-Returned-Rows"] = str(len(rows))
        return response

    data = request.data or {}
    rate = services.set_rate(
        tier=data.get("tier", "CLIENT"), currency=data.get("currency", "USD"),
        buy=data.get("buy", "0"), sell=data.get("sell", "0"),
        effective_date=to_date(data.get("effectiveDate")),
        usage=data.get("usage", ExchangeRate.Usage.OPERATIONNEL),
        source=data.get("source", ExchangeRate.Source.MANUELLE),
        source_reference=data.get("sourceReference", ""),
        reason=data.get("reason", ""),
        by=getattr(request.user, "sub", ""),
    )
    row = _rate_row(rate)
    row["requiresValidation"] = rate.status == ExchangeRate.Status.EN_ATTENTE
    return Response(row, status=201)


@api_view(["GET"])
@permission_classes([HasCapability("read")])
def current(request):
    """Taux en vigueur pour un palier, une devise et un USAGE donnés.

    La réponse porte sa fraîcheur : `stale`/`stalenessDays` disent si le taux servi est
    celui du jour demandé ou une retombée sur une date antérieure. Aucun écran ne peut
    afficher « le taux » sans savoir de quel jour il date.
    """
    on = to_date(request.GET.get("on"))
    try:
        rate, meta = services.resolve_rate(
            tier=request.GET.get("tier", "CLIENT"),
            currency=request.GET.get("currency", "USD"),
            usage=request.GET.get("usage", ExchangeRate.Usage.OPERATIONNEL),
            on=on,
        )
    except NotFoundError as exc:
        # Message explicite plutôt qu'un « Aucun taux disponible » générique : l'écran doit
        # pouvoir dire QUEL taux manque (palier, devise, usage, date).
        return Response({"detail": str(exc)}, status=404)
    row = _rate_row_for(request, rate)
    row["stale"] = meta["stale"]
    row["stalenessDays"] = meta["stalenessDays"]
    row["askedFor"] = meta["askedFor"]
    return Response(row)


@api_view(["GET"])
@permission_classes([IsStaff, HasCapability("read")])
def pending(request):
    """Corbeille des taux en attente d'un second acteur (écart > seuil)."""
    rows = services.pending_rates(
        tier=request.GET.get("tier", ""), currency=request.GET.get("currency", ""),
        usage=request.GET.get("usage", ""),
    )
    return Response({
        "thresholdPct": str(services.variation_threshold_pct()),
        "totalRows": len(rows),
        "rows": [_rate_row(r) for r in rows],
    })


@api_view(["POST"])
@permission_classes([IsStaff, HasCapability("config")])
def validate(request, pk: int):
    """Second acteur : valide ou rejette un taux en attente, avec motif obligatoire.

    La capacité `config` est exigée des DEUX côtés du contrôle : un taux est un paramètre
    institutionnel, et un checker moins habilité que le maker n'est pas un contrôle. La
    séparation réelle est portée par `services.validate_rate` (checker ≠ maker sur le `sub`).
    """
    data = request.data or {}
    decision = str(data.get("decision", "approve")).lower()
    rate = services.validate_rate(
        rate_id=pk, by=getattr(request.user, "sub", ""),
        reason=data.get("reason", ""), approve=decision != "reject",
    )
    return Response(_rate_row(rate))


@api_view(["POST"])
@permission_classes([IsStaff, HasCapability("config")])
def sync_bcc(request):
    """Synchronise le taux BCC du jour depuis la page publique bcc.cd (pas d'API officielle
    — parsing HTML, cf. services.fetch_bcc_rates). En cas d'échec (site injoignable, format
    de page changé), l'admin garde la main via le formulaire manuel existant (POST /fx/rates).

    Un mouvement au-delà du seuil ressort `status=EN_ATTENTE` : la synchronisation propose,
    elle ne décide pas."""
    rates_synced = services.fetch_bcc_rates(by=getattr(request.user, "sub", ""))
    return Response([_rate_row(r) for r in rates_synced])


#: Mêmes rouages internes que `_RATE_STAFF_FIELDS`, sous les clés que `rate_provenance`
#: emploie (`rateId`, pas `id`) : la provenance est d'abord un objet de JOURNAL d'audit,
#: pas une réponse d'API client.
_PROVENANCE_STAFF_FIELDS = ("rateId", "source", "sourceReference", "version", "status",
                            "validatedBy")


@api_view(["GET"])
@permission_classes([HasCapability("read")])
def convert(request):
    """Ouvert à tout porteur de `read` : le membre qui subit la conversion a le droit de
    voir le taux appliqué, sa date et sa fraîcheur. La provenance complète — identifiant
    interne du taux, version, source, validateur — reste au personnel."""
    result, provenance = services.convert_with_provenance(
        amount=request.GET.get("amount", "0"), from_currency=request.GET.get("from", "CDF"),
        to_currency=request.GET.get("to", "USD"), tier=request.GET.get("tier", "CLIENT"),
        usage=request.GET.get("usage", ExchangeRate.Usage.OPERATIONNEL),
        on=to_date(request.GET.get("on")),
    )
    if provenance and not getattr(request.user, "is_staff_role", False):
        provenance = {k: v for k, v in provenance.items()
                      if k not in _PROVENANCE_STAFF_FIELDS}
    return Response({"amount": float(result), "from": request.GET.get("from", "CDF"),
                      "to": request.GET.get("to", "USD"), "rate": provenance})
