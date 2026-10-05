"""
Comparaison à une référence — séries externes importées, référence interne bornée.

Une comparaison n'informe que si les deux termes sont comparables. Trois conditions,
vérifiées ici et DÉCLARÉES dans la réponse plutôt que supposées :

- **même nature** : un rendement se compare à un TAUX, jamais à un niveau d'indice ;
  un indice base 100 ne devient un taux qu'à travers une variation, et le module
  n'invente pas cette variation ;
- **même devise** : un rendement en USD comparé à un taux en CDF produit un écart
  qui ne veut rien dire — la série est alors servie, l'écart ne l'est pas ;
- **même horizon** : la période RÉELLEMENT couverte par les flux du portefeuille est
  celle sur laquelle la série est lue, et l'écart porte le nombre de mois comparés.

La référence INTERNE (rendement médian des offres clôturées d'une filière) obéit à
la boucle d'apprentissage du projet : elle n'est publiée qu'à partir de 30 dossiers.
En deçà, on annonce l'insuffisance d'effectif — on ne publie pas une médiane sur
quatre offres en la présentant comme une référence.

Aucune saisie libre : les points de série arrivent par la commande d'import
(`import_benchmark_series`), append-only et versionnée.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from django.utils import timezone

from audit.services import record as audit_record
from common.exceptions import ConflictError, ValidationFailed
from common.money import quantifier

#: Devise de tenue du module — RELUE dans `metrics`, jamais redéfinie ici : deux
#: constantes pour une seule grandeur finissent toujours par diverger (principe 6).
#: Le sens de l'import est à sens unique — `metrics` importe ce module DANS une
#: fonction, il n'y a donc pas de cycle.
from .metrics import BASE_CURRENCY
from .models import BenchmarkPoint, BenchmarkSeries, Offer

RATIO = Decimal("0.000001")

#: Effectif minimal avant qu'une statistique interne devienne une référence
#: (principe 10 — la boucle d'apprentissage). En deçà : insuffisance annoncée.
EFFECTIF_MINIMAL_REFERENCE = 30

UNITS = {
    "portfolioReturn": "fraction",
    "series[].value": "fraction_ou_niveau_selon_kind",
    "series[].gap": "fraction",
    "internalReference.medianReturn": "fraction",
}


# ── Référentiel : maker ≠ checker ────────────────────────────────────────────

def declarer_serie(*, code: str, label: str, currency: str, frequency: str, kind: str,
                   source: str, licence: str, notes: str = "", by: str = "") -> BenchmarkSeries:
    """Déclare une série — en BROUILLON. Elle ne publie rien tant qu'un checker
    ne l'a pas activée."""
    code = (code or "").strip().upper()
    if not code:
        raise ValidationFailed("Le code de la série est requis.", code="SERIES_CODE_REQUIRED")
    if not (source or "").strip():
        raise ValidationFailed(
            "La source de la série est requise : une référence sans émetteur identifié "
            "n'est pas une référence.", code="SERIES_SOURCE_REQUIRED")
    if not (licence or "").strip():
        raise ValidationFailed(
            "La licence de rediffusion est requise : une série qu'on n'a pas le droit "
            "de republier ne se publie pas.", code="SERIES_LICENCE_REQUIRED")
    if frequency not in BenchmarkSeries.Frequency.values:
        raise ValidationFailed(f"Fréquence inconnue : « {frequency} ».",
                                code="SERIES_FREQUENCY_UNKNOWN")
    if kind not in BenchmarkSeries.Kind.values:
        raise ValidationFailed(f"Nature de série inconnue : « {kind} ».",
                                code="SERIES_KIND_UNKNOWN")
    if BenchmarkSeries.objects.filter(code=code).exists():
        raise ConflictError(f"La série « {code} » existe déjà.")

    serie = BenchmarkSeries.objects.create(
        code=code, label=label or code, currency=(currency or BASE_CURRENCY).upper(),
        frequency=frequency, kind=kind, source=source.strip(), licence=licence.strip(),
        notes=notes or "", created_by=by or "",
    )
    audit_record(actor=by, action="investments.benchmark.series.declare",
                 entity_type="BenchmarkSeries", entity_id=serie.code,
                 details={"currency": serie.currency, "kind": kind, "source": serie.source})
    return serie


def activer_serie(*, code: str, by: str = "") -> BenchmarkSeries:
    """Active une série — par un acteur DIFFÉRENT de celui qui l'a déclarée.

    Publier une référence, c'est engager l'institution sur ce à quoi ses
    investisseurs se comparent : la règle maker ≠ checker vaut ici comme sur tout
    référentiel du projet.
    """
    serie = BenchmarkSeries.objects.filter(code=(code or "").strip().upper()).first()
    if serie is None:
        raise ValidationFailed(f"Série introuvable : « {code} ».", code="SERIES_NOT_FOUND")
    if serie.status == BenchmarkSeries.Status.ACTIVE:
        return serie
    if not (by or "").strip():
        raise ValidationFailed("L'activation exige un acteur identifié.",
                                code="SERIES_ACTIVATOR_REQUIRED")
    if serie.created_by and serie.created_by == by:
        raise ConflictError(
            "Maker ≠ checker : la série a été déclarée par le même acteur. Son "
            "activation revient à quelqu'un d'autre.",
            code="SERIES_MAKER_IS_CHECKER",
        )
    serie.status = BenchmarkSeries.Status.ACTIVE
    serie.activated_by = by
    serie.activated_at = timezone.now()
    serie.save(update_fields=["status", "activated_by", "activated_at"])
    audit_record(actor=by, action="investments.benchmark.series.activate",
                 entity_type="BenchmarkSeries", entity_id=serie.code,
                 details={"declaredBy": serie.created_by})
    return serie


# ── Points : import append-only et versionné ─────────────────────────────────

def importer_points(*, serie: BenchmarkSeries, points, source_reference: str = "",
                    by: str = "") -> dict:
    """Importe des `(date, valeur)` — idempotent, append-only.

    Un point dont la valeur est INCHANGÉE n'est pas réécrit : on n'ajoute rien.
    Un point dont la valeur a changé donne une NOUVELLE version à la même date, et
    l'ancienne reste lisible — une correction de source est un fait à conserver, pas
    une valeur à écraser. Relancer l'import sur le même fichier ne crée donc aucune
    ligne (`update_or_create` aurait écrasé l'historique, `create` en aurait empilé).
    """
    ajoutes, inchanges, corriges = 0, 0, 0
    for point_date, valeur in points:
        if not isinstance(point_date, date):
            raise ValidationFailed(f"Date de point illisible : « {point_date} ».",
                                    code="POINT_DATE_INVALID")
        montant = Decimal(str(valeur))
        dernier = (BenchmarkPoint.objects.filter(series=serie, point_date=point_date)
                   .order_by("-import_version").first())
        if dernier is not None and Decimal(dernier.value) == montant:
            inchanges += 1
            continue
        BenchmarkPoint.objects.create(
            series=serie, point_date=point_date, value=montant,
            import_version=(dernier.import_version + 1) if dernier else 1,
            source_reference=source_reference or "", imported_by=by or "",
        )
        if dernier is None:
            ajoutes += 1
        else:
            corriges += 1
    resume = {"seriesCode": serie.code, "added": ajoutes, "unchanged": inchanges,
              "corrected": corriges}
    if ajoutes or corriges:
        audit_record(actor=by, action="investments.benchmark.points.import",
                     entity_type="BenchmarkSeries", entity_id=serie.code, details=resume)
    return resume


def points_courants(serie: BenchmarkSeries, *, depuis: date | None = None,
                    jusqu_a: date | None = None) -> list[BenchmarkPoint]:
    """Points d'une série sur une période — la version la PLUS RÉCENTE de chaque date."""
    qs = BenchmarkPoint.objects.filter(series=serie)
    if depuis:
        qs = qs.filter(point_date__gte=depuis)
    if jusqu_a:
        qs = qs.filter(point_date__lte=jusqu_a)
    retenus: dict[date, BenchmarkPoint] = {}
    for point in qs.order_by("point_date", "import_version"):
        retenus[point.point_date] = point          # la dernière version écrase la précédente
    return [retenus[d] for d in sorted(retenus)]


def _mediane(valeurs: list[Decimal]) -> Decimal:
    ordonnees = sorted(valeurs)
    n = len(ordonnees)
    milieu = n // 2
    if n % 2:
        return ordonnees[milieu]
    return (ordonnees[milieu - 1] + ordonnees[milieu]) / Decimal("2")


# ── Référence interne : publiée seulement au-delà de 30 dossiers ─────────────

def reference_interne(*, secteur: str) -> dict:
    """Rendement médian des offres CLÔTURÉES d'une filière — publié si N ≥ 30.

    Ce n'est pas un indice de marché et la réponse ne le présente jamais comme tel :
    c'est la statistique de l'institution sur ses propres dossiers. Elle n'a de
    valeur de référence qu'à partir d'un effectif suffisant (principe 10) ; en deçà,
    l'effectif est annoncé et AUCUNE médiane n'est servie — publier une médiane sur
    quatre offres puis la corriger à la trentième serait pire que se taire.
    """
    secteur = (secteur or "").strip()
    base = {
        "sector": secteur,
        "basis": "Taux de coupon contractuel des offres clôturées de la filière.",
        "minimumSampleSize": EFFECTIF_MINIMAL_REFERENCE,
        "isMarketIndex": False,
    }
    if not secteur:
        return {**base, "sampleSize": 0, "medianReturn": None, "published": False,
                "unavailableReason": "Aucune filière dominante dans ce portefeuille."}

    offres = [
        o for o in Offer.objects.filter(status=Offer.Status.CLOTURE)
        .select_related("project__credit_application__value_chain")
        if o.project.value_chain_label == secteur
    ]
    effectif = len(offres)
    if effectif < EFFECTIF_MINIMAL_REFERENCE:
        return {
            **base, "sampleSize": effectif, "medianReturn": None, "published": False,
            "unavailableReason": (
                f"{effectif} offre(s) clôturée(s) sur la filière « {secteur} » : "
                f"l'effectif minimal de {EFFECTIF_MINIMAL_REFERENCE} dossiers n'est pas "
                "atteint. Aucune médiane n'est publiée tant qu'elle ne repose pas sur "
                "une base suffisante."
            ),
        }
    taux = [Decimal(o.coupon_rate) / Decimal("100") for o in offres]
    return {
        **base, "sampleSize": effectif, "published": True, "unavailableReason": None,
        "medianReturn": float(quantifier(_mediane(taux), RATIO)),
    }


# ── Bloc servi dans `metrics/mine` ───────────────────────────────────────────

def _mois_entre(debut: date, fin: date) -> int:
    return max(0, (fin.year - debut.year) * 12 + (fin.month - debut.month))


def _serie_row(serie: BenchmarkSeries, *, depuis: date | None, jusqu_a: date,
               rendement, mois_portefeuille: int) -> dict:
    points = points_courants(serie, depuis=depuis, jusqu_a=jusqu_a)
    valeurs = [Decimal(p.value) for p in points]
    moyenne = (quantifier(sum(valeurs, Decimal("0")) / Decimal(len(valeurs)), RATIO)
               if valeurs else None)

    obstacles: list[str] = []
    if serie.kind != BenchmarkSeries.Kind.RATE:
        obstacles.append(
            "cette série est un niveau d'indice, pas un taux : elle ne se compare pas "
            "directement à un rendement")
    if serie.currency != BASE_CURRENCY:
        obstacles.append(
            f"la série est libellée en {serie.currency} et votre portefeuille en "
            f"{BASE_CURRENCY}, sans taux de conversion journalisé")
    if not valeurs:
        obstacles.append("aucun point de la série ne couvre la période de votre portefeuille")
    if rendement is None:
        obstacles.append("votre rendement réalisé n'est pas encore calculable")

    comparable = not obstacles
    ecart = (quantifier(Decimal(str(rendement)) - moyenne, RATIO)
             if comparable and moyenne is not None else None)
    return {
        "code": serie.code, "label": serie.label, "currency": serie.currency,
        "frequency": serie.frequency, "kind": serie.kind, "source": serie.source,
        "licence": serie.licence,
        "value": float(moyenne) if moyenne is not None else None,
        "valueMethod": ("Moyenne des points publiés sur la période réelle de votre "
                        "portefeuille."),
        "pointsCount": len(points),
        "firstDate": points[0].point_date.isoformat() if points else None,
        "lastDate": points[-1].point_date.isoformat() if points else None,
        "comparable": comparable,
        "comparabilityNote": ("Comparable : même nature, même devise, même période."
                              if comparable else
                              "Écart non servi — " + " ; ".join(obstacles) + "."),
        "gap": float(ecart) if ecart is not None else None,
        "horizonMonths": mois_portefeuille,
        "horizonNote": (
            f"Votre portefeuille couvre {mois_portefeuille} mois de flux réels ; la "
            f"série est publiée à fréquence « {serie.get_frequency_display().lower()} » "
            "et son taux est annuel."
        ),
    }


def bloc_investisseur(*, realized_return, period: dict, secteur_dominant: str = "") -> dict:
    """Le bloc `benchmark` de `GET /investments/metrics/mine`.

    Aligné sur la période RÉELLE du portefeuille (celle de ses flux), pas sur une
    période déclarée : comparer douze mois d'indice à huit mois de portefeuille
    produirait un écart faux.
    """
    depuis = date.fromisoformat(period["from"]) if period.get("from") else None
    jusqu_a = date.fromisoformat(period["to"]) if period.get("to") else date.today()
    mois = _mois_entre(depuis, jusqu_a) if depuis else 0
    series = BenchmarkSeries.objects.filter(status=BenchmarkSeries.Status.ACTIVE)
    lignes = [_serie_row(s, depuis=depuis, jusqu_a=jusqu_a, rendement=realized_return,
                          mois_portefeuille=mois) for s in series]
    return {
        "portfolioReturn": realized_return,
        "currency": BASE_CURRENCY,
        "periodFrom": period.get("from"),
        "periodTo": period.get("to"),
        "horizonMonths": mois,
        "seriesCount": len(lignes),
        "series": lignes,
        "internalReference": reference_interne(secteur=secteur_dominant),
        "unavailableReason": (
            "Aucune série de référence n'est activée : l'institution n'en publie "
            "aucune pour l'instant." if not lignes else None
        ),
        "units": UNITS,
    }


def series_publiables(*, depuis: date | None = None, jusqu_a: date | None = None) -> list[dict]:
    """`GET /investments/benchmarks` — les séries ACTIVES et leurs points sur la période.

    Une série en brouillon n'est servie à personne : tant qu'un checker ne l'a pas
    activée, elle n'engage pas l'institution.
    """
    lignes = []
    for serie in BenchmarkSeries.objects.filter(status=BenchmarkSeries.Status.ACTIVE):
        points = points_courants(serie, depuis=depuis, jusqu_a=jusqu_a)
        lignes.append({
            "code": serie.code, "label": serie.label, "currency": serie.currency,
            "frequency": serie.frequency, "kind": serie.kind, "source": serie.source,
            "licence": serie.licence, "notes": serie.notes,
            "activatedAt": serie.activated_at.isoformat() if serie.activated_at else None,
            "pointsCount": len(points),
            "totalRows": len(points),
            "points": [
                {"date": p.point_date.isoformat(), "value": float(p.value),
                 "version": p.import_version, "sourceReference": p.source_reference}
                for p in points
            ],
        })
    return lignes
