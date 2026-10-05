"""Score de conformité pondéré multi-composantes (Supervision.jsx) — remplace le simple
« % de KYC validés » calculé auparavant côté client. Chaque composante qui n'a aucune
donnée source (ex. aucun profil KYC créé, aucun rapprochement complété sur 30 jours) vaut
`None` et ses poids sont RENORMALISÉS sur les composantes restantes — plutôt que de compter
silencieusement cette composante comme 0/100, ce qui pénaliserait injustement un système
juste encore vide de données."""
from __future__ import annotations

import logging
from datetime import date as date_cls, timedelta
from decimal import Decimal

from django.db.models import Count, Sum
from django.utils import timezone

from .models import ComplianceScoreSnapshot
from common.money import en_float
from common.money import quantifier

logger = logging.getLogger("analytics")

# Poids v1 — composantes calculables avec les données réellement présentes dans le
# système (pas de "KYC agences"/"reporting réglementaire"/"actions correctives" : ces
# concepts n'existent nulle part ailleurs dans AGRICAP FINTECH, les inventer ici serait
# fabriquer un chiffre sans substance).
COMPONENT_WEIGHTS = {
    "KYC_CLIENTS": 0.35,
    "RAPPRO_PONCTUALITE": 0.25,
    "ALERTES_CRITIQUES": 0.20,
    "RESEAU_ACTIF": 0.20,
}

_RECONCILIATION_WINDOW_DAYS = 30
_RECONCILIATION_PUNCTUAL_HOURS = 24


def _kyc_clients_score() -> float | None:
    from compliance.models import KycProfile
    total = KycProfile.objects.count()
    if not total:
        return None
    validated = KycProfile.objects.filter(kyc_status=KycProfile.Status.VALIDE).count()
    return round(validated / total * 100, 1)


def _rapprochement_ponctualite_score() -> float | None:
    from agencies.models import AgencyReconciliation
    since = timezone.now() - timedelta(days=_RECONCILIATION_WINDOW_DAYS)
    completed = AgencyReconciliation.objects.filter(
        status=AgencyReconciliation.Status.COMPLETED, closed_at__gte=since,
    )
    total = completed.count()
    if not total:
        return None
    punctual = sum(
        1 for r in completed
        if r.closed_at and (r.closed_at - r.opened_at) <= timedelta(hours=_RECONCILIATION_PUNCTUAL_HOURS)
    )
    return round(punctual / total * 100, 1)


def _alertes_critiques_score() -> float | None:
    from transactions.models import SpecialCase
    active_critical = SpecialCase.objects.filter(
        alert_level=SpecialCase.AlertLevel.CRITIQUE,
    ).exclude(status=SpecialCase.Status.BLOQUE).count()
    return max(0.0, 100.0 - 10.0 * active_critical)


def _reseau_actif_score() -> float | None:
    from agencies.models import Agency
    denom = Agency.objects.exclude(status=Agency.Status.FERMEE).count()
    if not denom:
        return None
    active = Agency.objects.filter(status=Agency.Status.ACTIF).count()
    return round(active / denom * 100, 1)


_COMPONENT_FUNCS = {
    "KYC_CLIENTS": ("KYC clients validés", _kyc_clients_score),
    "RAPPRO_PONCTUALITE": ("Rapprochements clôturés < 24h", _rapprochement_ponctualite_score),
    "ALERTES_CRITIQUES": ("Absence d'alertes critiques", _alertes_critiques_score),
    "RESEAU_ACTIF": ("Agences actives du réseau", _reseau_actif_score),
}


def compute_compliance_score(*, persist: bool = True) -> dict:
    components = []
    for code, (label, fn) in _COMPONENT_FUNCS.items():
        components.append({"code": code, "label": label, "weight": COMPONENT_WEIGHTS[code], "score": fn()})

    available = [c for c in components if c["score"] is not None]
    if not available:
        global_score = None
    else:
        weight_sum = sum(c["weight"] for c in available)
        global_score = round(sum(c["score"] * c["weight"] for c in available) / weight_sum, 1)

    delta_wow = None
    if global_score is not None:
        reference = ComplianceScoreSnapshot.objects.filter(
            computed_at__lte=timezone.now() - timedelta(days=7),
        ).order_by("-computed_at").first()
        if reference:
            delta_wow = round(global_score - reference.global_score, 1)

    # Persistance limitée à un instantané par heure (pas de planificateur dans ce projet
    # pour un vrai recalcul horaire en tâche de fond) — évite qu'un simple rafraîchissement
    # de la page Supervision ne crée un instantané à chaque requête.
    if persist and global_score is not None:
        recent = ComplianceScoreSnapshot.objects.filter(
            computed_at__gte=timezone.now() - timedelta(hours=1),
        ).exists()
        if not recent:
            ComplianceScoreSnapshot.objects.create(global_score=global_score, components=components)

    return {"score": global_score, "components": components, "deltaWow": delta_wow}


# ══════════════════════════════════ TABLEAU DE BORD MULTI-DEVISES ══════════════════════
# Agrégat servi à `src/components/dashboard/MultiCurrencyDashboard.jsx`, qui calculait
# jusqu'ici ses KPI CÔTÉ NAVIGATEUR en additionnant cinq listes (`/portfolio/loans`,
# `/savings/plans`, `/transactions/`, `/rbac/users`, `/fx/rates/current`). Trois défauts
# structurels, tous corrigés ici :
#
#   1. « zéro chiffre métier calculé côté client » (CONVENTIONS.md §5) était violé — l'écran
#      sommait des montants et divisait des ratios en JavaScript, en `float` ;
#   2. le compteur de transactions portait sur une liste PLAFONNÉE à 500 lignes côté
#      serveur : au-delà, l'écran affichait un compte faux sans le dire. Ici, chaque
#      effectif vient d'un `.count()` sur le queryset — jamais d'une liste tronquée (§4.6) ;
#   3. afficher un COMPTE de clients exigeait la capacité `config` (annuaire RBAC
#      nominatif). Un agrégat anonyme n'est pas un annuaire : cette vue s'en tient à
#      `read`, comme les deux autres de l'app.
#
# Rien n'est recalculé de ce qui existe (principe 6) : les taux et leur fraîcheur viennent
# de `fx.services`, l'encours des propriétés de `portfolio.models.Loan`, la catégorie des
# rôles de `rbac.role_registry`, les libellés de statut des `TextChoices` des modèles.

CENT = Decimal("0.01")          # quantum des montants (principe 4)
DIXIEME = Decimal("0.1")        # quantum des pourcentages servis
ZERO = Decimal("0")

#: Les deux seules devises portées par `portfolio.Loan.Currency` et par les plans
#: d'épargne. Une devise inattendue est LOGGÉE (jamais fondue en silence dans l'une des
#: deux, ce qui fabriquerait un agrégat faux sans trace).
DEVISES = ("USD", "CDF")

#: Palier et devise du taux servi : ceux que l'écran demandait déjà
#: (`api.fx.current('CLIENT', 'USD')`). Le CDF est le pivot implicite de `fx` — un taux
#: CLIENT/USD dit « combien de CDF pour 1 USD ».
FX_TIER = "CLIENT"
FX_CURRENCY = "USD"

#: Fenêtres GLISSANTES, en jours — rigoureusement les durées du sélecteur de
#: `MultiCurrencyDashboard.jsx` (`PERIOD_DAYS`), pour qu'un même libellé désigne la même
#: période des deux côtés du réseau. Bornes calculées en date LOCALE
#: (`timezone.localdate()`, Africa/Kinshasa) : une journée d'agence n'est pas une journée
#: UTC, et un compteur « aujourd'hui » qui bascule à 2 h du matin n'est pas un compteur.
PERIODS: dict[str, tuple[int, str]] = {
    "today": (1, "Aujourd'hui"),
    "week": (7, "Cette semaine"),
    "month": (30, "Ce mois"),
    "quarter": (90, "Ce trimestre"),
    "year": (365, "Cette année"),
}

#: Blocs dont les données portent RÉELLEMENT un rattachement agence en base.
#: `transactions.Transaction.agency` existe ; `portfolio.Loan`, `savings.SavingsPlan` et
#: `accounts.FintechUser` n'ont AUCUN champ agence (vérifié sur les trois modèles). Le
#: filtre agence ne s'applique donc qu'aux transactions — et la réponse le DIT, plutôt
#: que de fabriquer un rattachement approximatif (par gestionnaire, par ville…) qui
#: donnerait des chiffres d'apparence ventilée sans fondement.
SCOPED_BLOCKS = ["transactions"]
UNSCOPED_BLOCKS = ["credits", "savings", "clients"]
SCOPE_NOTE = (
    "Crédits, épargne et clients ne portent aucun rattachement agence en base : "
    "ces cartes restent à périmètre institution."
)

CREDITS_BASIS = (
    "Encours = décaissé validé − remboursé, sur les dossiers au statut « En cours »"
)
SAVINGS_BASIS = "Solde des plans au statut « Actif »"
CLIENTS_BASIS = (
    "Comptes dont le rôle appartient à la catégorie « Client » du registre RBAC"
)


def _empty_amounts() -> dict[str, Decimal]:
    return {devise: ZERO for devise in DEVISES}


def _accumulate(bucket: dict[str, Decimal], currency: str, amount: Decimal, *, source: str) -> None:
    """Ajoute `amount` au seau de sa devise. Une devise hors USD/CDF n'est pas silencieusement
    versée dans un autre seau : elle est écartée AVEC un log — un agrégat multi-devises faux
    est pire qu'un agrégat incomplet signalé."""
    if currency not in bucket:
        logger.warning(
            "Tableau multi-devises : devise « %s » inattendue sur %s (montant %s) — "
            "exclue de l'agrégat, aucune conversion n'est inventée.", currency, source, amount,
        )
        return
    bucket[currency] += Decimal(amount or 0)


def _amounts_payload(bucket: dict[str, Decimal]) -> dict[str, float]:
    return {devise.lower(): en_float(quantifier(bucket[devise])) for devise in DEVISES}


def _status_rows(choices, counts: dict[str, int]) -> list[dict]:
    """`[{code, label, count}]` sur la nomenclature CANONIQUE du modèle.

    Les libellés viennent des `TextChoices` (donc de la même source que
    `get_status_display()`), jamais d'une table de correspondance recopiée ici — c'est la
    dette « plusieurs vocabulaires pour un concept » (principe 6). Tous les statuts sont
    servis, y compris à zéro : la forme de la réponse ne dépend pas des données, et
    « 0 dossier en défaut » est une information, pas une absence d'information.
    """
    return [{"code": code, "label": label, "count": counts.get(code, 0)}
            for code, label in choices]


# ── Paramètres de la requête ──────────────────────────────────────────────────────────

def validate_dashboard_params(*, agency: str, period: str):
    """`(agence|None, clé_de_période, erreurs)` — TOUTES les erreurs de l'étape, jamais la
    première seule (principe 5), et jamais de repli silencieux sur une valeur par défaut :
    un paramètre inconnu qui retomberait sur « toutes agences » ferait lire à l'écran un
    périmètre qu'il n'a pas demandé.
    """
    from agencies.models import Agency

    errors: list[dict] = []
    agency_obj = None

    brut = (agency or "all").strip()
    if brut.lower() not in ("all", ""):
        if not brut.isdigit():
            errors.append({
                "code": "AGENCY_INVALID",
                "message": f"Agence « {agency} » : identifiant attendu (entier) ou « all ».",
            })
        else:
            agency_obj = Agency.objects.filter(pk=int(brut)).first()
            if agency_obj is None:
                errors.append({
                    "code": "AGENCY_UNKNOWN",
                    "message": f"Aucune agence d'identifiant {brut}.",
                })

    key = (period or "month").strip().lower()
    if key not in PERIODS:
        errors.append({
            "code": "PERIOD_UNKNOWN",
            "message": "Période « {} » inconnue (attendu : {}).".format(
                period, ", ".join(PERIODS)),
        })
        key = ""

    return agency_obj, key, errors


def _period_payload(key: str) -> tuple[dict, date_cls, date_cls]:
    """Bornes de la fenêtre glissante, en date locale, `from` et `to` INCLUS.

    `from = to − N jours` : c'est exactement la fenêtre que l'écran appliquait
    (`Date.now() − N × 86 400 000`), ramenée au calendrier de l'institution. `days` porte
    N, la taille de la fenêtre au sens du sélecteur — les bornes étant inclusives, la
    plage couvre N+1 dates calendaires ; les deux bornes sont servies pour que l'écran
    affiche la période RÉELLEMENT appliquée plutôt que de la redériver.
    """
    days, label = PERIODS[key]
    to = timezone.localdate()
    frm = to - timedelta(days=days)
    return (
        {"key": key, "label": label, "from": frm.isoformat(), "to": to.isoformat(), "days": days},
        frm,
        to,
    )


# ── Blocs ─────────────────────────────────────────────────────────────────────────────

def _fx_block(*, on: date_cls):
    """`(payload, taux)` — `(None, None)` quand aucun taux actif n'existe.

    Aucune lecture de taux maison : `fx.services.current_rate` applique déjà la règle
    « un taux EN_ATTENTE n'est jamais servi », et `rate_provenance` porte déjà
    `effectiveDate`, `stale` et `stalenessDays`. Les cours sont servis TELS QUE
    JOURNALISÉS (chaînes de la provenance), sans re-formatage : le chiffre affiché est
    celui qu'un auditeur retrouve en base.
    """
    from fx.services import current_rate, rate_provenance

    rate = current_rate(tier=FX_TIER, currency=FX_CURRENCY, on=on)
    if rate is None:
        return None, None
    prov = rate_provenance(rate, asked_for=on)
    return {
        "tier": prov["tier"],
        "currency": prov["currency"],
        "buy": prov["buy"],
        "sell": prov["sell"],
        "effectiveDate": prov["effectiveDate"],
        "stale": prov["stale"],
        "stalenessDays": prov["stalenessDays"],
    }, rate


def _credits_block(*, today: date_cls, frm: date_cls, to: date_cls
                   ) -> tuple[dict, dict[str, Decimal]]:
    """`(payload, encours par devise)` — encours, décaissé et flux de demandes du crédit.

    **Base de l'encours : `Loan.disbursed_validated`**, c'est-à-dire les décaissements au
    statut « Validé » — pas `Loan.disbursed`, qui additionne aussi les mouvements en
    attente. On ne compte comme sorti que l'argent dont on est sûr qu'il a quitté la
    caisse ; c'est la définition qu'utilisent déjà `accounting.provisions` et l'échéancier
    client. Le choix est écrit dans `basis`, servi à l'écran : un encours dont on ne
    connaît pas la base n'est pas un chiffre, c'est une impression.

    Aucune re-sommation de `LoanTransaction` ici : les propriétés `disbursed_validated` et
    `repaid` du modèle sont les seules implémentations (principe 6).
    """
    from portfolio.models import Loan

    tous = Loan.objects.all()
    actifs = tous.filter(status=Loan.Status.EN_COURS).prefetch_related("transactions")

    encours = _empty_amounts()
    decaisse = _empty_amounts()
    for loan in actifs:
        valide = Decimal(loan.disbursed_validated)
        rembourse = Decimal(loan.repaid)
        _accumulate(decaisse, loan.currency, valide, source=f"crédit {loan.reference}")
        _accumulate(encours, loan.currency, valide - rembourse, source=f"crédit {loan.reference}")

    par_statut = {
        row["status"]: row["n"]
        for row in tous.values("status").annotate(n=Count("pk"))
    }

    # Flux de la période : les dossiers DEMANDÉS dans la fenêtre. `Loan.date` est la date
    # de la demande (le fait métier, déjà servi par `portfolio.serializers.loan_row`), pas
    # `created_at` qui n'horodate que l'insertion en base.
    nouveaux = tous.filter(date__gte=frm, date__lte=to)
    demande = _empty_amounts()
    for row in nouveaux.values("currency").annotate(total=Sum("amount_requested")):
        _accumulate(demande, row["currency"], row["total"] or ZERO,
                    source="demandes de la période")

    return {
        "asOf": today.isoformat(),
        "outstanding": _amounts_payload(encours),
        "disbursed": _amounts_payload(decaisse),
        "activeCount": actifs.count(),
        "totalCount": tous.count(),
        "byStatus": _status_rows(Loan.Status.choices, par_statut),
        "basis": CREDITS_BASIS,
        "newInPeriod": {
            "count": nouveaux.count(),
            "requestedUsd": en_float(quantifier(demande["USD"])),
            "requestedCdf": en_float(quantifier(demande["CDF"])),
        },
    }, encours


def _savings_block(*, today: date_cls) -> tuple[dict, dict[str, Decimal]]:
    """`(payload, solde par devise)` — plans d'épargne au statut « Actif »."""
    from savings.models import SavingsPlan

    tous = SavingsPlan.objects.all()
    actifs = tous.filter(status=SavingsPlan.Status.ACTIF)
    solde = _empty_amounts()
    for row in actifs.values("currency").annotate(total=Sum("balance")):
        _accumulate(solde, row["currency"], row["total"] or ZERO, source="plans d'épargne")

    return {
        "asOf": today.isoformat(),
        "balance": _amounts_payload(solde),
        "activePlans": actifs.count(),
        "totalPlans": tous.count(),
        "basis": SAVINGS_BASIS,
    }, solde


def _transactions_block(*, agency, frm: date_cls, to: date_cls) -> dict:
    """Le SEUL bloc réellement filtrable par agence (cf. `SCOPED_BLOCKS`).

    Les effectifs sortent de `.count()` sur le queryset : c'est le point qui corrige le
    compteur de l'écran, lequel comptait les lignes d'une liste plafonnée à 500 et
    affichait donc « 500 » comme s'il s'agissait du total réel.
    """
    from common.choices import FlowStatus
    from transactions.models import Transaction

    qs = Transaction.objects.filter(created_at__date__gte=frm, created_at__date__lte=to)
    if agency is not None:
        qs = qs.filter(agency_id=agency.pk)
    par_statut = {row["status"]: row["n"] for row in qs.values("status").annotate(n=Count("pk"))}
    return {
        "count": qs.count(),
        "scopedByAgency": agency is not None,
        "byStatus": _status_rows(FlowStatus.choices, par_statut),
    }


def _clients_block() -> dict:
    """Compte des comptes de catégorie « Client » — SANS annuaire nominatif.

    La catégorie vient de `rbac.role_registry.get_role(...).type`, seule autorité du
    projet sur ce qu'est un « client » (c'est déjà ce que lit `FintechUser.is_staff_role`).
    L'écran codait en dur `['client','investor','agri_op','invest','partner']` : une
    sixième nomenclature de rôles, qui aurait divergé au premier rôle ajouté — et qui
    ignorait les `RoleOverride` édités depuis Roles.jsx, que `get_role` honore.
    """
    from accounts.models import FintechUser
    from rbac.role_registry import get_role

    total = 0
    clients = 0
    for row in FintechUser.objects.values("role").annotate(n=Count("sub")):
        effectif = row["n"]
        total += effectif
        if get_role(row["role"]).type == "Client":
            clients += effectif
    return {"count": clients, "total": total, "basis": CLIENTS_BASIS}


def _conversion_block(*, rate, credits: dict[str, Decimal], savings: dict[str, Decimal],
                      on: date_cls) -> dict:
    """Contre-valeurs USD et ratios — ou `None` quand aucun taux n'est journalisé.

    La conversion passe par `fx.services.to_usd` (aucune arithmétique de change écrite
    ici), au MÊME palier, à la MÊME date que le bloc `fx` : le taux affiché est celui qui
    a servi.

    Les deux ratios valent `None` quand leur dénominateur est nul. Servir `0` là où la
    réponse honnête est « indéfini » ferait lire « 0 % de couverture » à un écran qui
    n'a simplement rien à couvrir (§4.6 : pas de pourcentage sans base).
    """
    from fx.services import rate_provenance, to_usd

    prov = rate_provenance(rate, asked_for=on)
    credits_usd_eq = credits["USD"] + to_usd(credits["CDF"], "CDF", tier=FX_TIER, on=on)[0]
    savings_usd_eq = savings["USD"] + to_usd(savings["CDF"], "CDF", tier=FX_TIER, on=on)[0]

    ratio = (en_float(quantifier(savings_usd_eq / credits_usd_eq * Decimal("100"), DIXIEME))
             if credits_usd_eq else None)
    part_usd = (en_float(quantifier(credits["USD"] / credits_usd_eq * Decimal("100"), DIXIEME))
                if credits_usd_eq else None)

    return {
        "rateUsed": prov["sell"],
        "rateDate": prov["effectiveDate"],
        "tier": prov["tier"],
        "creditsUsdEq": en_float(quantifier(credits_usd_eq)),
        "savingsUsdEq": en_float(quantifier(savings_usd_eq)),
        "savingsToCreditPct": ratio,
        "usdSharePct": part_usd,
    }


# ── Agrégat ───────────────────────────────────────────────────────────────────────────

def multi_currency_dashboard(*, agency=None, period_key: str = "month") -> dict:
    """Agrégat complet servi à `GET /api/analytics/multi-currency`.

    Chaque carte porte sa période, son périmètre, sa devise et sa base de calcul (§7.2 :
    « KPI honnêtes ») — et l'agrégat multi-devises n'existe QUE si une conversion
    journalisée existe : sans taux actif, `fx` et `conversion` valent `None` ensemble
    plutôt que d'additionner des USD et des CDF (principe 4).
    """
    periode, frm, to = _period_payload(period_key)
    today = to

    fx_payload, rate = _fx_block(on=today)
    credits_payload, credits_amounts = _credits_block(today=today, frm=frm, to=to)
    savings_payload, savings_amounts = _savings_block(today=today)

    return {
        "generatedAt": timezone.now().isoformat(),
        "period": periode,
        "scope": {
            "agencyId": agency.pk if agency is not None else None,
            "agencyName": agency.name if agency is not None else "Toutes les agences",
            "scopedBlocks": list(SCOPED_BLOCKS),
            "unscopedBlocks": list(UNSCOPED_BLOCKS),
            "note": SCOPE_NOTE,
        },
        "fx": fx_payload,
        "credits": credits_payload,
        "savings": savings_payload,
        "transactions": _transactions_block(agency=agency, frm=frm, to=to),
        "clients": _clients_block(),
        "conversion": (
            _conversion_block(rate=rate, credits=credits_amounts, savings=savings_amounts,
                              on=today)
            if rate is not None else None
        ),
    }
