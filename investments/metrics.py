"""
Métriques investisseur — Annexe D du cahier HAZINA.

Une seule règle tient tout le fichier : **un chiffre est calculé sur des flux datés
réels, ou il n'est pas affiché.** Il n'y a ici ni simulation, ni lissage, ni moyenne
de taux affichés présentée comme un TRI — le `Math.random()` de la courbe de
performance du prototype est l'anti-modèle explicite.

Conséquences pratiques :

- le rendement réalisé est un **XIRR** sur les encaissements (négatifs) et les
  distributions (positifs), aux dates où ils ont réellement eu lieu ;
- quand il n'y a pas assez de flux pour qu'un XIRR existe (moins de deux flux, ou pas
  de changement de signe : de l'argent versé, rien encore reçu), la valeur retournée
  est `None` avec son motif — jamais 0, jamais une extrapolation ;
- le gain latent est étiqueté latent et porte sa méthode de valorisation ;
- chaque agrégat porte sa **période**, sa **devise** et son **effectif** : pas de
  moyenne sans effectif, pas de pourcentage sans base, pas de montant sans devise.

`Decimal` de bout en bout : les fonctions internes (`_concentration`, `_default_rates`,
`_late`, `_health`, `_valuation`) calculent et retournent des `Decimal` ; la conversion
en `float` n'a lieu qu'au moment de sérialiser, dans `_public()`. Un aller-retour
`Decimal(str(float(...)))` au milieu d'un calcul serait une perte de précision gratuite
sur des chiffres qui finissent dans un reporting investisseur.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal, getcontext

from django.db.models import Sum
from django.utils import timezone

from .models import (
    Distribution, DistributionLine, InvestmentConfig, Investor, Offer, Project,
    RepaymentSchedule, Subscription,
)
#: `serializers` ne dépend que de `models` : l'import est sûr (aucun cycle) et évite de
#: redéfinir ici une seconde table d'unités, ce qui serait exactement la faute que ces
#: tables servent à empêcher.
from .serializers import IDENTITY_DISCLOSED_STATUSES, OFFER_RATE_UNITS
from common.money import quantifier

getcontext().prec = 40

CENT = Decimal("0.01")
RATIO = Decimal("0.000001")
DAYS_PER_YEAR = Decimal("365")
#: Devise de tenue du module. `funding.py` écrit exclusivement en USD : c'est un FAIT
#: du code, pas une hypothèse — et `currency_note()` le re-vérifie à chaque agrégat
#: plutôt que de l'affirmer (principe 4 : aucune agrégation multi-devises sans taux).
BASE_CURRENCY = "USD"

#: Unité de CHAQUE taux servi par ce module — déclarée, pas devinée.
#:
#: **Une seule convention : la fraction** (0,09 = 9 %). Le module servait jusqu'ici
#: deux unités dans la MÊME réponse — les taux calculés (XIRR, défaut, concentration,
#: retard) en fraction, `expectedCouponRate` en points de pourcentage parce que
#: `Offer.coupon_rate` est stocké ainsi (9,000). Deux conventions pour deux taux
#: voisins dans un même payload est un piège à conversion : l'écran en convertit un et
#: oublie l'autre, et l'erreur ne se voit que sur la copie d'écran d'un investisseur.
#: Principe 6 — une seule nomenclature par concept : la conversion se fait ici, une
#: fois, à la frontière de sortie, et non dans chaque consommateur.
#:
#: `RATE_UNITS` accompagne la réponse pour que la convention soit LISIBLE et non
#: supposée : un consommateur n'a jamais à deviner l'unité d'un taux financier.
RATE_UNITS = {
    "realizedReturn": "fraction",
    "weightedIrr": "fraction",
    "expectedCouponRate": "fraction",
    "defaultRates.byValue": "fraction",
    "defaultRates.byCount": "fraction",
    "defaultRates.alertThreshold": "fraction",
    "concentration.herfindahlSector": "fraction",
    "concentration.herfindahlGeography": "fraction",
    "concentration.largestExposureShare": "fraction",
    "lateProjects.share": "fraction",
    "health.score": "points_sur_100",
    "valuation.positions[].recoveryRate": "fraction",
}


def _public(value):
    """Frontière de sérialisation : `Decimal` → `float`, récursivement.

    Le seul endroit du module où un `Decimal` devient un `float`. Les booléens sont
    traités avant les entiers (`bool` est une sous-classe d'`int`) sans quoi
    `highConcentration: True` sortirait en `1`.
    """
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, bool):
        return value
    if isinstance(value, dict):
        return {k: _public(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_public(v) for v in value]
    return value


# ── Devise et période (le contexte obligatoire de tout KPI) ───────────────────

def currency_note(*, distributions=None, movements=None) -> dict:
    """Devise d'un agrégat — **vérifiée**, jamais postulée.

    Le module écrit tous ses montants en USD (`funding.py`), mais les modèles
    `Distribution`, `Movement` et `InvestmentEvent` portent chacun leur devise : rien
    n'empêche structurellement qu'une ligne arrive en CDF. Additionner des CDF et des
    USD sans taux journalisé violerait le principe 4 ; on préfère le dire.
    """
    devises: set[str] = set()
    if distributions is not None:
        devises |= {c for c in distributions.values_list("currency", flat=True).distinct() if c}
    if movements is not None:
        devises |= {c for c in movements.values_list("currency", flat=True).distinct() if c}
    autres = sorted(devises - {BASE_CURRENCY})
    return {
        "currency": BASE_CURRENCY,
        "currenciesObserved": sorted(devises) or [BASE_CURRENCY],
        #: Aucun taux n'est appliqué : tant que le module est mono-devise, il n'y a rien
        #: à convertir. Le jour où il ne l'est plus, ce champ doit porter le taux ET sa
        #: date — un agrégat multi-devises sans taux est un chiffre faux.
        "conversionRate": None,
        "mixedCurrency": bool(autres),
        "mixedCurrencyWarning": (
            "Des flux libellés en {} coexistent avec la devise de tenue {} sans taux de "
            "conversion journalisé : l'agrégat ci-dessous additionne des devises "
            "différentes et n'est pas exploitable en l'état.".format(", ".join(autres), BASE_CURRENCY)
            if autres else None
        ),
    }


def _period(flows: list[tuple[date, Decimal]]) -> dict:
    """Période effectivement couverte par les flux — pas une période déclarée.

    `from` est la date du premier flux RÉEL ; `None` quand il n'y a aucun flux, parce
    qu'un portefeuille sans mouvement n'a pas de période, il a un vide.
    """
    dates = sorted(d for d, _ in flows)
    return {
        "from": dates[0].isoformat() if dates else None,
        "to": date.today().isoformat(),
        "flowsCount": len(flows),
        "basis": "Flux datés réels : encaissements de souscriptions et distributions.",
    }


# ── XIRR ──────────────────────────────────────────────────────────────────────

class XirrUndefined(Exception):
    """Le XIRR n'existe pas sur ce jeu de flux — la raison est portée par le message."""


def xirr(flows: list[tuple[date, Decimal]], *, guess_low: Decimal = Decimal("-0.9999"),
         guess_high: Decimal = Decimal("100")) -> Decimal:
    """Taux de rendement interne sur flux datés irréguliers, résolu par bissection.

    `flows` = [(date, montant)] — montants signés : sortie de trésorerie de
    l'investisseur négative, encaissement positif. Le résultat est un taux annuel
    (base 365 jours réels), quantifié à 1e-6.

    La bissection est préférée à Newton-Raphson : elle ne diverge pas sur les profils
    de flux dégénérés (un gros flux tardif, des flux quasi simultanés), et 200
    itérations sur [-99,99 %, +10 000 %] valent largement la précision utile ici. Un
    TRI faux et rapide n'a aucune valeur ; un TRI juste ou rien en a une.
    """
    if len(flows) < 2:
        raise XirrUndefined("Moins de deux flux : aucun rendement n'est calculable.")
    ordered = sorted(flows, key=lambda f: f[0])
    montants = [Decimal(m) for _, m in ordered]
    if not any(m < 0 for m in montants) or not any(m > 0 for m in montants):
        raise XirrUndefined(
            "Tous les flux vont dans le même sens : le rendement n'existe pas encore "
            "(de l'argent a été investi, rien n'a encore été distribué — ou l'inverse)."
        )
    origine = ordered[0][0]
    jours = [Decimal((d - origine).days) for d, _ in ordered]

    def npv(rate: Decimal) -> Decimal:
        base = Decimal("1") + rate
        total = Decimal("0")
        for montant, j in zip(montants, jours):
            total += montant / (base ** (j / DAYS_PER_YEAR))
        return total

    lo, hi = guess_low, guess_high
    f_lo, f_hi = npv(lo), npv(hi)
    if f_lo * f_hi > 0:
        raise XirrUndefined(
            "Aucun taux dans [-99,99 %, +10 000 %] n'annule la valeur actuelle nette "
            "de ces flux."
        )
    for _ in range(200):
        mid = (lo + hi) / 2
        f_mid = npv(mid)
        if f_mid == 0:
            return quantifier(mid, RATIO)
        if f_lo * f_mid < 0:
            hi, f_hi = mid, f_mid
        else:
            lo, f_lo = mid, f_mid
    return quantifier((lo + hi) / 2, RATIO)


def xirr_or_none(flows: list[tuple[date, Decimal]]) -> tuple[Decimal | None, str]:
    """`(taux, motif)` — `taux` à `None` quand il n'existe pas, avec la raison en clair.

    Le motif est destiné à être AFFICHÉ : « pas encore de distribution » est une
    information honnête, « 0,0 % » serait un mensonge.
    """
    try:
        return xirr(flows), ""
    except XirrUndefined as exc:
        return None, str(exc)


# ── Flux réels ────────────────────────────────────────────────────────────────

def investor_flows(investor: Investor, *, project: Project | None = None) -> list[tuple[date, Decimal]]:
    """Flux datés RÉELS d'un investisseur : encaissements négatifs, retours positifs.

    Une souscription seulement réservée n'est pas un flux : aucun argent n'a bougé.
    """
    subs = Subscription.objects.filter(investor=investor).select_related("offer__project")
    if project is not None:
        subs = subs.filter(offer__project=project)

    flux: list[tuple[date, Decimal]] = []
    for sub in subs:
        if sub.settled_at and sub.settled_amount > 0:
            flux.append((sub.settled_at.date(), -Decimal(sub.settled_amount)))
        if sub.refunded_at and sub.refunded_amount > 0:
            flux.append((sub.refunded_at.date(), Decimal(sub.refunded_amount)))

    lignes = DistributionLine.objects.filter(investor=investor).select_related("distribution__offer__project")
    if project is not None:
        lignes = lignes.filter(distribution__offer__project=project)
    for ligne in lignes:
        flux.append((ligne.distribution.value_date, Decimal(ligne.amount)))
    return flux


def portfolio_flows() -> list[tuple[date, Decimal]]:
    """Union des flux de tous les investisseurs — base du TRI pondéré du portefeuille.

    « Pondéré » ne veut pas dire « moyenne de taux pondérée par les encours » : c'est
    l'union des flux datés qui pondère, mécaniquement, chaque euro par sa durée de
    présence. Une moyenne de taux affichés donnerait un autre chiffre, plus flatteur,
    et faux (Annexe F, dette n° 7).
    """
    flux: list[tuple[date, Decimal]] = []
    for sub in Subscription.objects.all():
        if sub.settled_at and sub.settled_amount > 0:
            flux.append((sub.settled_at.date(), -Decimal(sub.settled_amount)))
        if sub.refunded_at and sub.refunded_amount > 0:
            flux.append((sub.refunded_at.date(), Decimal(sub.refunded_amount)))
    for d in Distribution.objects.all():
        flux.append((d.value_date, Decimal(d.total_amount)))
    return flux


# ── Concentration ─────────────────────────────────────────────────────────────

def herfindahl(weights: dict[str, Decimal]) -> Decimal:
    """`H = Σ (partᵢ)²` sur des poids normalisés. 1 = tout sur un seul axe.

    `H > seuil` (0,25 par défaut, paramétrable) signale une concentration élevée.
    Retourne 0 quand la base est vide — un portefeuille vide n'est pas concentré,
    il est vide, et l'effectif accompagne toujours la mesure.
    """
    total = sum(weights.values(), Decimal("0"))
    if total <= 0:
        return Decimal("0")
    return quantifier(sum(((v / total) ** 2 for v in weights.values()), Decimal("0")), Decimal("0.0001"))


def _exposure_by(field: str, subs) -> dict[str, Decimal]:
    buckets: dict[str, Decimal] = {}
    for sub in subs:
        cle = getattr(sub.offer.project, field, "") or "(non renseigné)"
        buckets[cle] = buckets.get(cle, Decimal("0")) + Decimal(sub.settled_amount)
    return buckets


def _breakdown(buckets: dict[str, Decimal]) -> list[dict]:
    """Ventilation d'un axe, du plus exposé au moins exposé, part ET montant.

    Chaque ligne porte les deux : une part sans montant ne se rapproche d'aucune
    pièce comptable, un montant sans part ne se compare à rien.
    """
    base = sum(buckets.values(), Decimal("0"))
    lignes = [
        {"key": cle, "amount": quantifier(montant),
         "share": quantifier(montant / base, Decimal("0.0001")) if base else Decimal("0")}
        for cle, montant in buckets.items()
    ]
    return sorted(lignes, key=lambda l: l["amount"], reverse=True)


def _top(buckets: dict[str, Decimal]) -> tuple[str | None, Decimal]:
    base = sum(buckets.values(), Decimal("0"))
    if not buckets or base <= 0:
        return None, Decimal("0")
    cle = max(buckets, key=lambda k: buckets[k])
    return cle, quantifier(buckets[cle] / base, Decimal("0.0001"))


def _concentration(subs, *, geography_field: str = "location") -> dict:
    """Concentration mesurée sur les axes secteur et géographie + part du plus gros
    engagement — les trois chiffres de l'Annexe D, avec leur seuil d'alerte.

    Chaque part porte son **effectif** (`sectorsCount`, `locationsCount`,
    `projectsCount`) : « 60 % sur un secteur » ne veut rien dire sans savoir s'il y a
    deux secteurs ou vingt.

    `geography_field` porte l'anonymat : la vue institution ventile sur la
    localisation FINE (`location`), la vue investisseur sur la zone LARGE
    (`broad_zone`). La ventilation géographique d'un portefeuille est un axe de
    risque, mais la clé de chaque tranche nomme un lieu — servir la commune de
    l'emprunteur à un investisseur pendant la levée reviendrait à le désigner par
    l'adresse. L'axe est déclaré dans la réponse (`geographyAxisField`) : un écran ne
    compare pas deux ventilations de granularités différentes sans le savoir.
    """
    seuil = Decimal(InvestmentConfig.active().concentration_threshold)
    secteurs = _exposure_by("value_chain_label", subs)
    zones = _exposure_by(geography_field, subs)
    engagements: dict[str, Decimal] = {}
    for sub in subs:
        engagements[sub.offer.project.code] = engagements.get(
            sub.offer.project.code, Decimal("0")) + Decimal(sub.settled_amount)
    base = sum(engagements.values(), Decimal("0"))
    plus_gros_code, plus_gros_part = _top(engagements)
    h_secteur = herfindahl(secteurs)
    h_zone = herfindahl(zones)
    top_secteur, part_secteur = _top(secteurs)
    top_zone, part_zone = _top(zones)
    #: L'axe RETENU pour le score de santé est le plus concentré des deux : pénaliser
    #: sur la moyenne des axes laisserait passer un portefeuille mono-secteur bien
    #: réparti géographiquement, qui est pourtant concentré.
    axe = "sector" if h_secteur >= h_zone else "geography"
    return {
        # Ventilation complète des deux axes : le Herfindahl résume, l'exposition
        # explique. Servir le seul H obligerait l'écran à recalculer la répartition
        # côté client — c'est-à-dire à produire un chiffre métier dans le navigateur.
        "exposureBySector": _breakdown(secteurs),
        "exposureByLocation": _breakdown(zones),
        "geographyAxisField": geography_field,
        "herfindahlSector": h_secteur,
        "herfindahlGeography": h_zone,
        "herfindahlRetained": max(h_secteur, h_zone),
        "retainedAxis": axe,
        "threshold": seuil,
        "highConcentration": bool(h_secteur > seuil or h_zone > seuil),
        "largestExposureShare": plus_gros_part,
        "largestExposureProject": plus_gros_code,
        "largestSector": top_secteur,
        "largestSectorShare": part_secteur,
        "largestLocation": top_zone,
        "largestLocationShare": part_zone,
        "projectsCount": len(engagements),
        "sectorsCount": len(secteurs),
        "locationsCount": len(zones),
        "basisAmount": quantifier(base),
    }


def concentration(subs) -> dict:
    return _public(_concentration(subs))


# ── Défaut ────────────────────────────────────────────────────────────────────

def _default_rates(subs) -> dict:
    """Taux de défaut en VALEUR **et** en NOMBRE — les deux, toujours.

    Un seul projet en défaut sur trente pèse peu en nombre et peut peser énormément
    en valeur : afficher un seul des deux chiffres, c'est choisir celui qui arrange.
    Chaque taux porte sa base (`totalValue`, `totalProjects`) : un pourcentage sans
    base n'est pas une information.
    """
    cfg = InvestmentConfig.active()
    total_valeur = sum((Decimal(s.settled_amount) for s in subs), Decimal("0"))
    projets = {s.offer.project.code: s.offer.project for s in subs}
    en_defaut = {c: p for c, p in projets.items() if p.status == Project.Status.P12}
    valeur_defaut = sum(
        (Decimal(s.settled_amount) for s in subs if s.offer.project.status == Project.Status.P12),
        Decimal("0"),
    )
    taux_valeur = quantifier(valeur_defaut / total_valeur, Decimal("0.0001")) if total_valeur else Decimal("0")
    taux_nombre = quantifier(Decimal(len(en_defaut)) / Decimal(len(projets)), Decimal("0.0001")) if projets else Decimal("0")
    return {
        "byValue": taux_valeur,
        "byCount": taux_nombre,
        "defaultedValue": quantifier(valeur_defaut),
        "defaultedProjects": len(en_defaut),
        "totalProjects": len(projets),
        "totalValue": quantifier(total_valeur),
        "alertThreshold": Decimal(cfg.default_rate_alert),
        "alert": bool(taux_valeur > Decimal(cfg.default_rate_alert)),
    }


def default_rates(subs) -> dict:
    return _public(_default_rates(subs))


# ── Retard ────────────────────────────────────────────────────────────────────

def _late(subs) -> dict:
    """Projets en retard — **constatés sur les dates**, pas sur un statut.

    `RepaymentSchedule.status = OVERDUE` n'est posé par aucun producteur du module
    aujourd'hui (aucun job, aucun endpoint, aucun service). S'y fier rendrait le
    terme `c×part_projets_en_retard` du score de santé structurellement nul : le score
    ne pourrait JAMAIS pénaliser un retard, tout en affichant une formule qui prétend
    le faire. Le retard se constate donc sur `due_date < aujourd'hui` pour une échéance
    ni payée ni annulée — le statut `OVERDUE` reste honoré s'il est un jour alimenté.
    """
    projets = {s.offer.project.code: s.offer.project for s in subs}
    if not projets:
        return {"share": Decimal("0"), "lateProjects": 0, "totalProjects": 0,
                "projectsWithSchedule": 0, "scheduleCoverageWarning": None}
    codes = list(projets)
    echeances = RepaymentSchedule.objects.filter(offer__project__code__in=codes)
    avec_echeancier = set(echeances.values_list("offer__project__code", flat=True))
    en_retard = set(
        echeances.filter(due_date__lt=date.today())
        .exclude(status__in=(RepaymentSchedule.Status.PAID, RepaymentSchedule.Status.CANCELLED))
        .values_list("offer__project__code", flat=True)
    )
    sans_echeancier = len(projets) - len(avec_echeancier)
    return {
        "share": quantifier(Decimal(len(en_retard)) / Decimal(len(projets)), Decimal("0.0001")),
        "lateProjects": len(en_retard),
        "totalProjects": len(projets),
        "projectsWithSchedule": len(avec_echeancier),
        "scheduleCoverageWarning": (
            f"{sans_echeancier} projet(s) sur {len(projets)} n'ont aucun échéancier de "
            "retour enregistré : leur retard éventuel ne peut pas être constaté, la part "
            "de projets en retard est donc un plancher, pas une mesure complète."
            if sans_echeancier else None
        ),
    }


def late_share(subs) -> Decimal:
    """Part des projets en retard, en `Decimal` — voir `_late()` pour la méthode."""
    return _late(subs)["share"]


# ── Score de santé ────────────────────────────────────────────────────────────

HEALTH_FORMULA = "100 − a×taux_défaut − b×max(0, H−h₀)×100 − c×part_projets_en_retard"


def _health(*, default_rate: Decimal, hhi: Decimal, late: Decimal) -> dict:
    """`100 − a×taux_défaut − b×max(0, H−h₀)×100 − c×part_en_retard`, borné [0, 100].

    Les coefficients a, b, c et le seuil h₀ viennent de `InvestmentConfig` : la
    formule est publiée dans l'UI avec les paramètres RÉELLEMENT appliqués, jamais
    des constantes du code (principe 8). Le retour porte donc sa formule, ses
    paramètres, ses entrées et le détail de chaque pénalité — pour qu'un investisseur
    puisse refaire le calcul à la main et retrouver le même chiffre.

    Les taux entrent en base 1 (0,10 = 10 %) et sont multipliés par 100 : `a=4` retire
    donc 40 points pour 10 % de défaut.
    """
    cfg = InvestmentConfig.active()
    a = Decimal(cfg.health_coeff_default)
    b = Decimal(cfg.health_coeff_concentration)
    c = Decimal(cfg.health_coeff_late)
    h0 = Decimal(cfg.concentration_threshold)

    penalite_defaut = a * Decimal(default_rate) * Decimal("100")
    penalite_conc = b * max(Decimal("0"), Decimal(hhi) - h0) * Decimal("100")
    penalite_retard = c * Decimal(late) * Decimal("100")
    brut = Decimal("100") - penalite_defaut - penalite_conc - penalite_retard
    score = max(Decimal("0"), min(Decimal("100"), brut))
    return {
        "score": quantifier(score, Decimal("0.1")),
        "rawScore": quantifier(brut, Decimal("0.1")),
        "clamped": bool(brut != score),
        "formula": HEALTH_FORMULA,
        "parameters": {"a": a, "b": b, "c": c, "h0": h0},
        "inputs": {"defaultRate": Decimal(default_rate), "herfindahl": Decimal(hhi),
                   "lateShare": Decimal(late)},
        "penalties": {
            "default": quantifier(penalite_defaut, Decimal("0.1")),
            "concentration": quantifier(penalite_conc, Decimal("0.1")),
            "late": quantifier(penalite_retard, Decimal("0.1")),
        },
    }


def health_score(*, default_rate: Decimal, hhi: Decimal, late: Decimal) -> dict:
    return _public(_health(default_rate=default_rate, hhi=hhi, late=late))


# ── Valorisation ──────────────────────────────────────────────────────────────

#: Libellés des trois méthodes de valorisation de l'Annexe D. Une position en porte
#: toujours exactement une, et elle est affichée.
VALUATION_PAR = "PAIR"
VALUATION_PROVISION = "PROVISION_P12"
VALUATION_EXPERT = "EXPERTISE_DATEE"
VALUATION_PAR_NO_EXPERT = "PAIR_FAUTE_D_EXPERTISE"

_EQUITY_TITLES = (Offer.TypeOfTitle.ACTION, Offer.TypeOfTitle.PART_SOCIALE)


def _received_by_kind(subs) -> dict[int, dict[str, Decimal]]:
    """Distributions déjà reçues par souscription, VENTILÉES par nature.

    `Subscription.total_received` mélange coupons et remboursements de capital :
    l'utiliser pour amortir le capital restant dû fait disparaître du capital à chaque
    coupon versé, et le réutiliser pour les intérêts courus les compte deux fois. La
    ventilation vient de `Distribution.kind`, seule source qui sait ce qui a été payé.
    """
    ventile: dict[int, dict[str, Decimal]] = {}
    rows = (
        DistributionLine.objects.filter(subscription_id__in=[s.pk for s in subs])
        .values("subscription_id", "distribution__kind")
        .annotate(total=Sum("amount"))
    )
    for row in rows:
        ventile.setdefault(row["subscription_id"], {})[row["distribution__kind"]] = Decimal(
            row["total"] or 0)
    return ventile


def _expert_valuation(project: Project, cfg: InvestmentConfig) -> tuple[Decimal | None, str]:
    """Valorisation d'expert retenue pour un projet, ou `None` avec son motif.

    Une expertise n'est retenue que si elle est complète (valeur + date + source) ET
    non périmée. Une valeur sans date n'est pas une expertise : c'est une opinion, et
    une opinion ne valorise pas un portefeuille.
    """
    if project.expert_valuation is None or not project.expert_valuation_date:
        return None, "aucune valorisation d'expert datée n'est enregistrée"
    if not project.expert_valuation_source:
        return None, "valorisation d'expert sans source identifiée"
    age_mois = (date.today() - project.expert_valuation_date).days / 30.44
    limite = int(cfg.expert_valuation_max_age_months)
    if age_mois > limite:
        return None, (
            f"valorisation d'expert du {project.expert_valuation_date.isoformat()} "
            f"périmée (> {limite} mois)"
        )
    return Decimal(project.expert_valuation), ""


def _valuation(subs, *, with_positions: bool = False) -> dict:
    """Valeur courante du portefeuille — capital restant dû + gain latent ÉTIQUETÉ.

    Les trois méthodes de l'Annexe D, appliquées position par position :

    - **dette saine** → **au pair** : capital restant dû = encaissé − remboursements de
      CAPITAL reçus ; gain latent = intérêts courus non échus au taux de coupon figé à
      la souscription, prorata temporis, **nets des coupons déjà encaissés** ;
    - **projet en défaut (P12)** → **décote de provision** : capital retenu = capital
      non remboursé × (1 − taux de provision paramétré en base). Un taux de
      recouvrement RÉELLEMENT constaté (retours encaissés / décaissé) prime sur le
      paramètre : un fait bat une hypothèse. Aucun intérêt ne court sur un défaut ;
    - **titres de capital (action, part sociale)** → **valorisation d'expert datée** :
      quote-part de l'expertise du projet, l'écart au pair devenant un gain — ou une
      **perte** — latent. Sans expertise valide, retour au pair et la méthode le dit :
      on n'invente pas une valeur d'action.

    Le gain latent est TOUJOURS étiqueté latent (`latentGainIsLatent`) et peut être
    négatif : une moins-value latente est une information, la masquer serait du
    marketing.

    `with_positions` ajoute le détail **par position** : c'est ce détail qui rend une
    alerte de défaut exploitable (« le risque se montre quand il naît »), un agrégat
    ne disant jamais QUELLE ligne a décroché. Il n'est pas produit pour la vue
    institution, où il ferait une liste de toutes les positions de tous les
    investisseurs sans destinataire.
    """
    cfg = InvestmentConfig.active()
    taux_provision = Decimal(cfg.p12_provision_rate)
    recus = _received_by_kind(subs)

    capital = Decimal("0")
    latent = Decimal("0")
    par_methode: dict[str, dict] = {}
    notes: list[str] = []
    positions: list[dict] = []

    def _compte(methode: str, montant: Decimal) -> None:
        entree = par_methode.setdefault(methode, {"positionsCount": 0, "amount": Decimal("0")})
        entree["positionsCount"] += 1
        entree["amount"] += montant

    for sub in subs:
        projet = sub.offer.project
        encaisse = Decimal(sub.settled_amount)
        ventile = recus.get(sub.pk, {})
        capital_recu = ventile.get(Distribution.Kind.CAPITAL, Decimal("0"))
        coupons_recus = ventile.get(Distribution.Kind.COUPON, Decimal("0"))
        principal = max(Decimal("0"), encaisse - capital_recu)
        # Le détail par position est servi à l'investisseur qui A ENCAISSÉ sur ce
        # projet : il porte donc la filière et la zone LARGE en toutes circonstances,
        # et l'identité de l'emprunteur seulement à partir du décaissement (P08) —
        # même règle que les sérialiseurs de projet, appliquée à la même granularité.
        identite_lisible = projet.status in IDENTITY_DISCLOSED_STATUSES
        ligne = {
            "subscriptionId": sub.pk, "offerCode": sub.offer.code, "projectCode": projet.code,
            "projectStatus": projet.status, "typeOfTitle": sub.offer.type_of_title,
            "sector": projet.value_chain_label, "zone": projet.broad_zone,
            "identityDisclosed": identite_lisible,
            "settledAmount": quantifier(encaisse), "capitalRepaid": quantifier(capital_recu),
            "couponsReceived": quantifier(coupons_recus), "principalAtPar": quantifier(principal),
            "recoveryRate": None, "impairment": Decimal("0"), "latentGain": Decimal("0"),
        }
        if identite_lisible:
            ligne["promoter"] = projet.borrower_name
            ligne["location"] = projet.fine_location

        if projet.status == Project.Status.P12:
            decaisse = Decimal(projet.disbursed_amount)
            recouvre = Decimal(projet.returned_amount)
            if decaisse > 0 and recouvre > 0:
                taux_retenu = min(Decimal("1"), recouvre / decaisse)
                origine = "taux de recouvrement constaté"
            else:
                taux_retenu = max(Decimal("0"), Decimal("1") - taux_provision)
                origine = f"provision paramétrée à {quantifier(taux_provision * 100)} %"
            valeur = quantifier(principal * taux_retenu)
            capital += valeur
            _compte(VALUATION_PROVISION, valeur)
            notes.append(f"{projet.code} : défaut, {origine}.")
            ligne.update({
                "valuationMethod": VALUATION_PROVISION, "capitalOutstanding": valeur,
                "recoveryRate": quantifier(taux_retenu, Decimal("0.0001")),
                #: Perte ESTIMÉE sur cette position, en clair : ce que l'investisseur ne
                #: reverra pas si le taux de recouvrement constaté se confirme.
                "impairment": quantifier(principal - valeur),
                "valuationNote": f"Projet en défaut — {origine}.",
            })
            positions.append(ligne)
            continue

        if sub.offer.type_of_title in _EQUITY_TITLES:
            expertise, motif = _expert_valuation(projet, cfg)
            if expertise is not None:
                base_projet = Decimal(projet.funded_amount) or encaisse
                quote_part = (encaisse / base_projet) if base_projet else Decimal("0")
                valeur = quantifier(expertise * quote_part)
                capital += principal
                latent += valeur - principal
                _compte(VALUATION_EXPERT, valeur)
                note = (f"Expertise du {projet.expert_valuation_date.isoformat()} "
                        f"({projet.expert_valuation_source}).")
                notes.append(f"{projet.code} : {note[0].lower()}{note[1:]}")
                ligne.update({
                    "valuationMethod": VALUATION_EXPERT, "capitalOutstanding": quantifier(principal),
                    "latentGain": quantifier(valeur - principal), "valuationNote": note,
                })
            else:
                capital += principal
                _compte(VALUATION_PAR_NO_EXPERT, principal)
                notes.append(f"{projet.code} : titre de capital au pair — {motif}.")
                ligne.update({
                    "valuationMethod": VALUATION_PAR_NO_EXPERT, "capitalOutstanding": quantifier(principal),
                    "valuationNote": f"Titre de capital valorisé au pair — {motif}.",
                })
            positions.append(ligne)
            continue

        capital += principal
        courus_nets = Decimal("0")
        taux = Decimal(sub.coupon_rate_snapshot) / Decimal("100")
        if sub.settled_at and taux > 0:
            # TROIS CALENDRIERS TENAIENT DANS L'EXPRESSION D'ORIGINE :
            #   `date.today()`          → date de la MACHINE (fuseau du serveur)
            #   `sub.settled_at.date()` → date d'UTC (l'instant est stocké aware)
            #
            # Sur une durée d'intérêts, mélanger les deux décale le compteur d'un
            # jour pendant tout l'écart entre les deux fuseaux : 8 000 à 9 % font
            # 1,97 de plus par jour compté en trop, sur chaque position, une
            # partie de chaque journée. Un intérêt couru n'est pas un affichage —
            # il entre dans la valorisation servie à l'investisseur.
            #
            # `localdate()` ramène les DEUX bornes au calendrier de l'institution
            # (Africa/Kinshasa, `settings.TIME_ZONE`), le seul qui ait un sens ici.
            jours = Decimal((timezone.localdate() - timezone.localdate(sub.settled_at)).days)
            courus = quantifier(principal * taux * jours / DAYS_PER_YEAR)
            courus_nets = max(Decimal("0"), courus - coupons_recus)
            latent += courus_nets
        _compte(VALUATION_PAR, principal)
        ligne.update({
            "valuationMethod": VALUATION_PAR, "capitalOutstanding": quantifier(principal),
            "latentGain": courus_nets,
            "valuationNote": "Dette saine au pair, intérêts courus non échus nets des "
                             "coupons déjà versés.",
        })
        positions.append(ligne)

    capital = quantifier(capital)
    latent = quantifier(latent)
    return {
        "capitalOutstanding": capital,
        **({"positions": positions} if with_positions else {}),
        "latentGain": latent,
        "latentGainIsLatent": True,
        #: Valeur totale (Annexe D) = capital restant dû + gain latent valorisé. Bornée
        #: à zéro : une position ne vaut jamais moins que rien.
        "totalValue": max(Decimal("0"), quantifier(capital + latent)),
        "positionsCount": len(subs),
        "byMethod": {
            methode: {"positionsCount": v["positionsCount"], "amount": quantifier(v["amount"])}
            for methode, v in par_methode.items()
        },
        "methodNotes": notes,
        "method": (
            "Dette saine valorisée au pair (capital restant dû = encaissé − capital "
            "remboursé) ; intérêts courus non échus prorata temporis au taux de coupon "
            "figé à la souscription, nets des coupons déjà versés ; projet en défaut "
            "(P12) déprécié par le taux de recouvrement constaté, à défaut par la "
            "décote de provision paramétrée en base ; titres de capital valorisés par "
            "expertise datée non périmée, et au pair — en le disant — quand aucune "
            "expertise valide n'existe."
        ),
    }


def latent_value(subs, *, with_positions: bool = False) -> dict:
    return _public(_valuation(subs, with_positions=with_positions))


# ── Prochain paiement ─────────────────────────────────────────────────────────

def _next_payment(subs) -> dict:
    """Prochain paiement = **min des échéances à venir** sur les échéanciers de retour.

    L'Annexe D lit cette date sur les échéanciers (B12), pas sur le champ dénormalisé
    `Subscription.next_payment_date` — lequel n'est alimenté par AUCUN service du
    module aujourd'hui et vaut donc toujours `None`. Il reste consulté en repli, mais
    la source retenue est toujours annoncée : un investisseur a le droit de savoir si
    la date qu'il lit vient d'un échéancier ou d'un champ resté vide.
    """
    offres = {s.offer_id for s in subs}
    aujourd_hui = date.today()
    echeances = RepaymentSchedule.objects.filter(
        offer_id__in=offres, due_date__gte=aujourd_hui,
    ).exclude(status__in=(RepaymentSchedule.Status.PAID, RepaymentSchedule.Status.CANCELLED))
    prochaine = echeances.order_by("due_date").values_list("due_date", flat=True).first()
    source = "repayment_schedule"
    if prochaine is None:
        prochaine = (
            Subscription.objects.filter(pk__in=[s.pk for s in subs], next_payment_date__gte=aujourd_hui)
            .order_by("next_payment_date").values_list("next_payment_date", flat=True).first()
        )
        source = "subscription.next_payment_date" if prochaine else None
    offres_avec_echeancier = set(
        RepaymentSchedule.objects.filter(offer_id__in=offres).values_list("offer_id", flat=True))
    return {
        "nextPaymentDate": prochaine.isoformat() if prochaine else None,
        "nextPaymentSource": source,
        "upcomingCount": echeances.count(),
        "offersWithSchedule": len(offres_avec_echeancier),
        "offersCount": len(offres),
        "unavailableReason": (
            "Aucun échéancier de retour n'est enregistré sur les offres de ce "
            "portefeuille : la date du prochain paiement ne peut pas être établie."
            if prochaine is None and not offres_avec_echeancier and offres else None
        ),
    }


# ── Agrégats ──────────────────────────────────────────────────────────────────

def investor_metrics(investor: Investor) -> dict:
    """Le tableau de bord d'UN investisseur — sur SON argent uniquement.

    Trois grandeurs distinctes qui ne se confondent jamais : **total investi**
    (argent réellement sorti de sa poche, net des remboursements), **valeur totale**
    (capital restant dû + gain latent, ce dernier étiqueté latent avec sa méthode) et
    **rendement réalisé** (XIRR sur flux réels). Plus une quatrième, contractuelle et
    non acquise : le rendement attendu.

    Concentration, défaut et score de santé sont calculés **sur son seul portefeuille** :
    aucune donnée d'un autre investisseur n'entre dans ces chiffres (principe 7).
    """
    subs = list(
        Subscription.objects.filter(investor=investor, status__in=Subscription.FUNDED_STATUSES)
        .select_related("offer__project")
    )
    encaisse = sum((Decimal(s.settled_amount) for s in subs), Decimal("0"))
    rembourse = sum(
        (Decimal(s.refunded_amount) for s in Subscription.objects.filter(investor=investor)),
        Decimal("0"),
    )
    distribue = DistributionLine.objects.filter(investor=investor).aggregate(
        t=Sum("amount"))["t"] or Decimal("0")
    flux = investor_flows(investor)
    taux, motif = xirr_or_none(flux)
    valorisation = _valuation(subs, with_positions=True)
    defauts = _default_rates(subs)
    # Vue INVESTISSEUR : ventilation géographique sur la zone LARGE (anonymat).
    conc = _concentration(subs, geography_field="broad_zone")
    retard = _late(subs)
    sante = _health(default_rate=defauts["byValue"], hhi=conc["herfindahlRetained"],
                    late=retard["share"])
    attendu = Decimal("0")
    if encaisse:
        # `coupon_rate_snapshot` est stocké en points de pourcentage (9,000) ; il sort
        # en FRACTION comme tous les autres taux du payload (cf. `RATE_UNITS`).
        attendu = quantifier(
            sum((Decimal(s.settled_amount) * Decimal(s.coupon_rate_snapshot) for s in subs),
                Decimal("0")) / encaisse / Decimal("100"),
            Decimal("0.00001"),
        )
    paiement = _next_payment(subs)
    devise = currency_note(
        distributions=Distribution.objects.filter(lines__investor=investor).distinct(),
    )
    periode = _period(flux)
    # Imports DANS la fonction : `benchmarks` relit `BASE_CURRENCY` ici même, et un
    # import de module à module dans les deux sens ferait un cycle. Le sens unique
    # est donc : `benchmarks`/`esg` connaissent `metrics`, `metrics` les appelle.
    from . import benchmarks as benchmarks_module
    from . import esg as esg_module
    return _public({
        "totalInvested": quantifier(encaisse - rembourse),
        "totalSettled": quantifier(encaisse),
        "totalRefunded": quantifier(rembourse),
        "totalDistributed": quantifier(Decimal(distribue)),
        "totalValue": valorisation["totalValue"],
        "positionsCount": len(subs),
        "realizedReturn": taux,
        "realizedReturnUnavailableReason": motif or None,
        #: Moyenne pondérée des taux CONTRACTUELS — explicitement pas un rendement, et
        #: livrée avec sa base (`expectedCouponBasis`) et son effectif : une moyenne
        #: sans effectif n'est pas une information (CONVENTIONS.md §4.6).
        "expectedCouponRate": attendu,
        "expectedCouponBasis": quantifier(encaisse),
        "expectedCouponPositions": len(subs),
        "valuation": valorisation,
        "defaultRates": defauts,
        "concentration": conc,
        "lateProjects": retard,
        "health": sante,
        "nextPayment": paiement,
        "nextPaymentDate": paiement["nextPaymentDate"],
        "period": periode,
        #: Comparaison à une référence — alignée sur la période RÉELLE des flux
        #: ci-dessus. La filière retenue pour la référence interne est celle qui pèse
        #: le plus dans le portefeuille : comparer un portefeuille à la médiane d'une
        #: filière qu'il ne finance pas n'apprendrait rien.
        "benchmark": benchmarks_module.bloc_investisseur(
            realized_return=taux, period=periode,
            secteur_dominant=conc["largestSector"] or "",
        ),
        #: Impact ESG agrégé sur les seules positions de cet investisseur, pondéré par
        #: les montants encaissés, servi avec effectif et couverture (§4.6).
        "esg": esg_module.bloc_investisseur(investor),
        "units": RATE_UNITS,
        **devise,
        "asOf": date.today().isoformat(),
        "scope": "Portefeuille de cet investisseur uniquement.",
    })


def portfolio_metrics() -> dict:
    """Vue institution : TRI pondéré, défaut, concentration, santé — avec effectifs."""
    subs = list(
        Subscription.objects.filter(status__in=Subscription.FUNDED_STATUSES)
        .select_related("offer__project")
    )
    flux = portfolio_flows()
    taux, motif = xirr_or_none(flux)
    defauts = _default_rates(subs)
    conc = _concentration(subs)
    retard = _late(subs)
    valorisation = _valuation(subs)
    sante = _health(default_rate=defauts["byValue"], hhi=conc["herfindahlRetained"],
                    late=retard["share"])
    return _public({
        "weightedIrr": taux,
        "weightedIrrUnavailableReason": motif or None,
        "totalInvested": defauts["totalValue"],
        "totalValue": valorisation["totalValue"],
        "valuation": valorisation,
        "defaultRates": defauts,
        "concentration": conc,
        "lateProjects": retard,
        "lateProjectsShare": retard["share"],
        "health": sante,
        "nextPayment": _next_payment(subs),
        "investorsCount": Investor.objects.filter(status=Investor.Status.ACTIVE).count(),
        "subscriptionsCount": len(subs),
        "period": _period(flux),
        "units": RATE_UNITS,
        **currency_note(distributions=Distribution.objects.all()),
        "asOf": date.today().isoformat(),
        "scope": "Toutes offres, tous investisseurs.",
    })


def anonymised_pipeline() -> list[dict]:
    """Pipeline P01→P05 vu par un investisseur : des COMPTEURS, jamais des dossiers.

    Asymétrie d'information (§5.2) : un investisseur n'a pas à savoir quels projets sont
    en due diligence — ni leur nom, ni leur promoteur, ni leur montant individuel. Il a
    en revanche le droit de savoir qu'il y a du flux dans le tuyau.
    """
    # Le pipeline amont ne compte plus que deux étapes : « Analyse initiale »,
    # « Due diligence » et « Comité d'investissement » ont été retirés avec le
    # scoring et le comité.
    etapes = [Project.Status.P01, Project.Status.P05]
    libelles = dict(Project.Status.choices)
    lignes = []
    for etape in etapes:
        qs = Project.objects.filter(status=etape)
        lignes.append({
            "stage": etape,
            "label": libelles[etape],
            "count": qs.count(),
            "aggregateTarget": float(quantifier(qs.aggregate(t=Sum("funding_target"))["t"] or Decimal("0"))),
        })
    return lignes


def open_offers_summary() -> list[dict]:
    """Offres réellement ouvertes à la souscription — le seul détail projet auquel un
    investisseur a droit avant d'engager son argent.

    Les bornes de souscription (`minBonds`, `maxBonds`) font partie de l'offre : sans
    elles, l'écran de souscription doit inventer ses propres bornes, et `funding.reserve`
    refuse en fin de parcours ce qu'il aurait fallu empêcher au début. Une règle
    appliquée côté serveur et invisible côté écran est une règle qui se découvre par
    l'échec.

    **Catalogue ANONYME par construction** : il ne sert que des offres de projets en
    P06, c'est-à-dire en pleine levée — la phase où l'emprunteur n'est identifié pour
    personne (décision du fondateur : identité révélée à partir de P08). Ni promoteur,
    ni contact, ni localisation fine n'y figurent donc, pour aucun appelant ; la
    géographie servie est la zone LARGE. Le personnel dispose de `GET /projects` et
    `GET /offers` pour l'instruction.
    """
    return [
        {
            "offerId": o.pk, "offerCode": o.code, "projectCode": o.project.code,
            "title": o.project.title, "sector": o.project.value_chain_label,
            "zone": o.project.broad_zone,
            "identityDisclosed": False,
            "typeOfTitle": o.type_of_title, "paymentFrequency": o.payment_frequency,
            "couponRate": float(o.coupon_rate), "maturityMonths": o.maturity_months,
            "minTicket": float(o.min_ticket), "bondUnitValue": float(o.bond_unit_value),
            "minBonds": o.min_bonds, "maxBonds": o.max_bonds,
            "availableBonds": o.available_bonds, "fundingGoal": float(o.funding_goal),
            # `riskScore` et `globalScore` retirés avec le moteur de scoring : ce
            # module ne produit plus de note. `riskCategory` reste — c'est un
            # libellé qui décrit le projet, pas une note calculée.
            "riskCategory": o.project.risk_category,
            # `couponRate` est ici le taux CONTRACTUEL brut de `Offer.coupon_rate`, en
            # points de pourcentage — pas le `fraction` des taux calculés de
            # `RATE_UNITS`. Deux projections, deux unités, les deux DÉCLARÉES : c'est
            # ce que cet endpoint devait à ses consommateurs et ne leur donnait pas.
            "units": OFFER_RATE_UNITS,
            "reservedAmount": float(o.reserved_amount), "fundedAmount": float(o.funded_amount),
            "minFundingAmount": float(o.min_funding_amount),
            "oversubscriptionPolicy": o.oversubscription_policy,
            "subscriptionDeadline": (o.subscription_deadline.isoformat()
                                      if o.subscription_deadline else None),
        }
        for o in Offer.objects.filter(status=Offer.Status.OUVERT,
                                       project__status=Project.Status.P06)
        .select_related("project")
    ]
