"""
Tableau de bord crédits — Étape 7.

Agrège des KPIs adaptés au rôle de l'utilisateur.

GET /api/credits/dashboard/

Rôles et vue (identifiants canoniques de `rbac.role_registry`, groupés dans
`credits.roles`) :
  Client (client, agri_op…)      → mes dossiers, prochains remboursements, encours
  Terrain + gestion crédit       → SES dossiers en attente de traitement, volume mensuel
  gest_zone                      → stats agence : volume, taux de défaut, en-cours
  dir_ops                        → stats multi-agences
  Comité (dg, admin)             → dossiers en attente d'accord comité
  Superadmin (admin, dg)         → vue globale + anomalies

Le comité de crédit n'a pas de rôle propre : il est exercé par `dg` et `admin`
(décision de juillet 2026, aucun nouveau rôle). La vue comité est donc servie
aux rôles de direction, qui voient aussi la vue globale — l'ordre de priorité
ci-dessous tranche : la vue globale prime pour l'admin.

── Règles d'honnêteté appliquées ici (CONVENTIONS.md §7.2, §4.6, principe 4) ────────
1. **Aucun agrégat monétaire multi-devises sans conversion.** `CreditApplication.
   currency` vaut USD **ou** CDF. `Sum("disbursed_amount")` sur un tel queryset
   additionnait des CDF à des USD et appelait le résultat `totalEncoursUsd` : le
   suffixe « Usd » était un nom, pas une conversion. Tout montant passe désormais
   par `_agregat_usd()`, qui somme PAR devise puis convertit, et sert le détail
   par devise ainsi que le taux utilisé et sa date.
2. **Pas de pourcentage sans base.** Chaque taux servi est accompagné de son
   numérateur et de son dénominateur (`…Base`), pour que l'écran puisse dire
   « 3 sur 8 » plutôt qu'un « 37,5 % » invérifiable.
3. **Un pourcentage porte le nom de ce qu'il mesure.** `defaultRatePct` désigne
   désormais un vrai taux de défaut (prêts `portfolio.Loan` en DEFAUT ÷ prêts
   décaissés). L'ancien calcul — rejetés ÷ résolus — mesurait la sélectivité de
   l'instruction : il est conservé sous son nom exact, `rejectionRatePct`.
4. **Chaque vue déclare son périmètre réel** (`scope`), y compris quand il est
   plus large que voulu : un périmètre non restreint se dit, il ne se devine pas.
"""
from __future__ import annotations

import logging
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from django.db.models import Count, Sum, Q
from django.utils import timezone

logger = logging.getLogger(__name__)

# Statuts "en cours de vie" du crédit
_ACTIVE_STATUSES = ("submitted", "in_analysis", "approved", "pending_disbursement", "active")
_PENDING_ANALYSIS = ("submitted", "in_analysis", "adjourned")
_ANALYSIS_PENDING_APPROVAL = ("in_analysis",)

#: Champs qui rattachent un dossier à un membre du personnel.
#:
#: `CreditApplication` n'a pas de champ d'AFFECTATION (pas de `assigned_to`) : la
#: notion « SES dossiers » (§7.1) se lit donc comme « ceux sur lesquels il est
#: intervenu ». C'est une approximation assumée et documentée — la vraie prise en
#: charge (file d'instruction analyste, §7.1.2) demandera un champ d'affectation.
#:
#: Le rattachement à une AGENCE, lui, n'est plus une approximation : il passe par
#: `CreditApplication.agency` (cf. `_dossiers_de_l_agence`). Ces champs n'y
#: servent plus que de repli, et seulement pour les dossiers sans agence.
_CHAMPS_INTERVENANT = (
    "initiated_by_sub", "submitted_by_sub", "reviewed_by_sub", "disbursed_by_sub",
)


def get_dashboard(sub: str, roles: set[str], view: str = "") -> dict[str, Any]:
    """
    Point d'entrée principal. Retourne le tableau de bord adapté au rôle.

    `view` sélectionne explicitement une lentille pour les rôles qui en portent
    plusieurs. Le comité de crédit n'ayant pas de rôle propre (il est exercé par
    `dg` et `admin`, qui voient par défaut la vue globale), sa corbeille est
    atteignable via `?view=committee` — sinon elle serait inaccessible.
    """
    from credits.models import CreditApplication

    from credits.roles import (
        BRANCH_ROLES, COMMITTEE_ROLES, DIRECTION_ROLES, STAFF_ROLES, SUPERADMIN_ROLES,
    )

    roles = set(roles or ())
    is_admin = bool(roles & SUPERADMIN_ROLES)
    is_staff = bool(roles & STAFF_ROLES)
    is_committee = bool(roles & COMMITTEE_ROLES)
    is_regional = bool(roles & DIRECTION_ROLES)
    is_branch = bool(roles & BRANCH_ROLES)

    # Lentille explicite — refusée si le rôle n'y a pas droit (pas de silence)
    if view == "committee":
        if not is_committee:
            raise PermissionError("Vue comité réservée à la direction.")
        return _committee_dashboard()

    if is_admin:
        return _admin_dashboard()
    if is_committee:
        return _committee_dashboard()
    if is_regional:
        return _regional_dashboard()
    if is_branch:
        return _branch_dashboard(sub)
    if is_staff:
        return _agent_dashboard(sub, roles)

    # Client par défaut
    return _client_dashboard(sub)


# ── Conversion des agrégats monétaires (principe 4, §7.2) ─────────────────────

def _taux_cdf_par_usd() -> tuple[Decimal, dict]:
    """Taux CDF→USD à appliquer, et sa provenance journalisée.

    Source normale : le taux BCC du module `fx` (`ExchangeRate`, tiers BCC), qui
    porte sa date d'effet — c'est ce qu'exige le principe 4 (« toute conversion
    CDF/USD journalise le taux utilisé et sa date »). À défaut, repli sur
    `settings.CREDIT_FALLBACK_CDF_PER_USD` avec warning loggé ET marqué dans la
    réponse (`"secours": true`) : un KPI converti à un taux de secours non daté
    reste un KPI approximatif, l'écran doit pouvoir le dire.
    """
    try:
        from fx.services import current_rate
        taux = current_rate(tier="BCC", currency="USD")
        if taux is not None and taux.sell_rate:
            return Decimal(taux.sell_rate), {
                "cdfParUsd": float(taux.sell_rate),
                "source": "fx.ExchangeRate — BCC, taux vendeur",
                "date": taux.effective_date.isoformat(),
                "secours": False,
            }
    except Exception as exc:  # noqa: BLE001 — un module FX absent ne casse pas un KPI
        logger.warning("Taux BCC illisible (%s) — repli sur le taux de secours.", exc)

    from django.conf import settings
    brut = getattr(settings, "CREDIT_FALLBACK_CDF_PER_USD", 2800) or 2800
    logger.warning(
        "Conversion CDF→USD du tableau de bord au taux de secours %s (non daté, "
        "non journalisé) : aucun taux BCC en base (`fx.ExchangeRate`).", brut,
    )
    return Decimal(str(brut)), {
        "cdfParUsd": float(brut),
        "source": "settings.CREDIT_FALLBACK_CDF_PER_USD — taux de secours",
        "date": None,
        "secours": True,
    }


def _agregat_usd(qs, field: str) -> dict:
    """Somme multi-devises RÉELLEMENT convertie en USD.

    Retourne `{"usd", "parDevise", "taux"}` : le total converti, le détail brut
    par devise (pour que le chiffre soit reconstituable), et la provenance du
    taux. Sommer d'abord par devise puis convertir, plutôt que l'inverse, évite
    d'appliquer une division sur chaque ligne.
    """
    taux, provenance = _taux_cdf_par_usd()
    par_devise: dict[str, float] = {}
    non_converties: list[str] = []
    total = Decimal("0")

    for row in qs.values("currency").annotate(total=Sum(field)).order_by():
        devise = (row["currency"] or "USD").upper()
        montant = row["total"] or Decimal("0")
        if not montant:
            continue
        par_devise[devise] = float(par_devise.get(devise, 0)) + float(montant)
        if devise == "USD":
            total += montant
        elif devise == "CDF":
            total += montant / taux
        else:
            # Aucune autre devise n'existe dans `CreditApplication.Currency`, mais
            # une donnée importée pourrait en porter une : l'exclure du total et le
            # DIRE vaut mieux que l'additionner en douce.
            non_converties.append(devise)
            logger.warning(
                "Devise « %s » sans taux connu — exclue du total USD du tableau de bord.",
                devise,
            )

    bloc: dict[str, Any] = {
        "usd": float(total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)),
        "parDevise": par_devise,
        "taux": provenance,
    }
    if non_converties:
        bloc["devisesNonConverties"] = sorted(set(non_converties))
    return bloc


# ── Périmètre servi (§7.2 : chaque carte précise son périmètre) ───────────────

def _perimetre(type_: str, libelle: str, avertissement: str = "", **extra) -> dict:
    """Périmètre RÉEL du queryset servi — jamais le périmètre souhaité.

    `extra` porte les précisions propres à un type de périmètre — notamment
    `rattachement` (« exact » / « approche » / « mixte ») pour la vue agence :
    un périmètre calculé sur un lien direct et un périmètre déduit des personnes
    ne se présentent pas de la même façon, et l'écran doit pouvoir dire lequel il
    affiche sans avoir à le deviner.
    """
    bloc = {"type": type_, "libelle": libelle}
    bloc.update({k: v for k, v in extra.items() if v is not None})
    if avertissement:
        bloc["avertissement"] = avertissement
    return bloc


def _mes_dossiers(qs, sub: str):
    """Restreint `qs` aux dossiers sur lesquels `sub` est intervenu.

    Un `sub` vide ne doit JAMAIS élargir le périmètre : il retourne l'ensemble
    vide. Sans cette garde, `Q(initiated_by_sub="")` matcherait tous les dossiers
    dont le champ est blanc — soit l'inverse exact de la restriction demandée.
    """
    if not sub:
        logger.warning(
            "Périmètre personnel demandé sans identifiant utilisateur — aucun "
            "dossier servi (le filtre ne doit jamais s'ouvrir en silence).",
        )
        return qs.none()
    condition = Q()
    for champ in _CHAMPS_INTERVENANT:
        condition |= Q(**{champ: sub})
    return qs.filter(condition)


def _equipe_agence(sub: str):
    """(subs de l'équipe, agence) du responsable d'agence `sub`, ou (None, None).

    L'agence du demandeur est résolue par `credits.models.resolve_agency_for_sub`
    — la MÊME règle que celle qui renseigne `CreditApplication.agency` à la
    création du dossier. Deux règles distinctes finiraient par diverger, et un
    dossier serait alors « dans » l'agence pour le filtre et « hors » d'elle pour
    l'écran.

    L'équipe (personnel affecté à la même agence) reste nécessaire : elle sert de
    REPLI pour les dossiers antérieurs au champ `agency`, qui n'ont pas d'autre
    rattachement possible que les personnes intervenues.
    """
    from credits.models import resolve_agency_for_sub
    from rbac.models import StaffProfile

    agence = resolve_agency_for_sub(sub)
    if agence is None:
        return None, None

    equipe = set(
        StaffProfile.objects.filter(assignment_id=agence.pk)
        .values_list("user_id", flat=True)
    )
    if sub:
        equipe.add(sub)
    return equipe, agence


def _dossiers_de_l_agence(qs, agence, equipe: set[str]):
    """Dossiers de `agence` : lien direct quand il existe, repli sinon.

    Deux populations cohabitent, et cohabiteront tant que les dossiers d'avant le
    champ `agency` vivront :

      - **rattachement exact** — le dossier porte `agency` : il est de cette
        agence, ou il ne l'est pas. Aucune interprétation.
      - **rattachement approché** — le dossier n'a PAS d'agence : on retombe sur
        les personnes intervenues (`_CHAMPS_INTERVENANT`), c'est-à-dire sur
        l'ancienne heuristique. Sans ce repli, tous les dossiers antérieurs
        disparaîtraient de la vue agence du jour au lendemain.

    Le repli ne s'applique QU'aux dossiers sans agence (`agency__isnull=True`).
    Un dossier rattaché à une AUTRE agence n'entre pas dans le périmètre même si
    un membre de l'équipe y est intervenu : c'est exactement ce que le lien
    direct vient trancher — sinon il ne servirait à rien.
    """
    exact = Q(agency=agence)
    membres = {s for s in equipe or () if s}
    if not membres:
        return qs.filter(exact)

    intervenu = Q()
    for champ in _CHAMPS_INTERVENANT:
        intervenu |= Q(**{f"{champ}__in": membres})
    return qs.filter(exact | (Q(agency__isnull=True) & intervenu))


# ── Vue client ────────────────────────────────────────────────────────────────

def _client_dashboard(sub: str) -> dict[str, Any]:
    from credits.models import CreditApplication

    my_apps = CreditApplication.objects.filter(client__sub=sub)

    counts = my_apps.aggregate(
        total=Count("id"),
        active=Count("id", filter=Q(status="active")),
        pending=Count("id", filter=Q(status__in=_PENDING_ANALYSIS)),
        rejected=Count("id", filter=Q(status="rejected")),
        closed=Count("id", filter=Q(status="closed")),
    )

    encours = _agregat_usd(my_apps.filter(status="active"), "disbursed_amount")

    # Dossier nécessitant consentement client
    consent_needed = my_apps.filter(
        client_consent_expires__gt=timezone.now(),
        client_consent_at__isnull=True,
    ).count()

    recent = list(
        my_apps.order_by("-created_at").values(
            "code", "status", "amount_requested", "currency", "created_at",
        )[:5]
    )
    _serialize_qs_values(recent)

    return {
        "role": "client",
        "scope": _perimetre("own", "Vos dossiers uniquement."),
        "summary": {
            "totalApplications": counts["total"],
            "activeCredits": counts["active"],
            "pendingApplications": counts["pending"],
            "rejectedApplications": counts["rejected"],
            "closedCredits": counts["closed"],
            "totalEncoursUsd": encours["usd"],
            "totalEncoursDetail": encours,
            "consentNeeded": consent_needed,
        },
        "recentApplications": recent,
    }


# ── Vue agent / analyst ───────────────────────────────────────────────────────

def _agent_dashboard(sub: str, roles: set[str] | None = None) -> dict[str, Any]:
    """File de travail d'un membre du personnel.

    Le `sub` reçu est désormais UTILISÉ : le queryset était
    `CreditApplication.objects.all()`, donc un agent de terrain voyait les KPI de
    l'institution entière sous l'étiquette « ses dossiers » (§7.1).

    La restriction ne s'applique qu'aux rôles qui ont un portefeuille personnel
    (terrain, gestion crédit). Les rôles d'audit, de conformité et de
    configuration reçoivent la même vue mais NON restreinte : leur métier est
    précisément la lecture transverse (§7.1.8), et leur restreindre le périmètre
    serait un second bug symétrique du premier. Dans les deux cas, `scope` dit
    lequel des deux périmètres a été servi.
    """
    from credits.models import CreditApplication
    from credits.roles import CREDIT_OFFICER_ROLES, FIELD_AGENT_ROLES

    roles = set(roles or ())
    portefeuille_personnel = bool(roles & (FIELD_AGENT_ROLES | CREDIT_OFFICER_ROLES))

    now = timezone.now()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    if portefeuille_personnel:
        qs = _mes_dossiers(CreditApplication.objects.all(), sub)
        scope = _perimetre(
            "own",
            "Vos dossiers — ceux que vous avez initiés, soumis, instruits ou décaissés.",
            avertissement=(
                "`CreditApplication` n'a pas de champ d'affectation : le rattachement "
                "se fait par intervention, pas par prise en charge. Le champ `agency` "
                "ne résout pas cette approximation-ci — il rattache le dossier à une "
                "AGENCE, pas à une personne (file d'instruction, §7.1.2)."
            ),
        )
    else:
        qs = CreditApplication.objects.all()
        scope = _perimetre(
            "institution",
            "Institution entière — lecture transverse propre aux rôles d'audit, "
            "de conformité et de configuration.",
        )

    counts = qs.aggregate(
        total=Count("id"),
        pending_submission=Count("id", filter=Q(status="submitted")),
        in_analysis=Count("id", filter=Q(status="in_analysis")),
        adjourned=Count("id", filter=Q(status="adjourned")),
        pending_disbursement=Count("id", filter=Q(status="pending_disbursement")),
        approved=Count("id", filter=Q(status="approved")),
        active=Count("id", filter=Q(status="active")),
    )

    decaisses_du_mois = qs.filter(status="active", disbursed_at__gte=month_start)
    volume = _agregat_usd(decaisses_du_mois, "disbursed_amount")

    # Dossiers bloqués sans action depuis > 7 jours
    stale_threshold = now - timezone.timedelta(days=7)
    stale = qs.filter(
        status__in=_PENDING_ANALYSIS,
        updated_at__lt=stale_threshold,
    ).count()

    # Consentements qui expirent dans 24h
    expiring_soon = qs.filter(
        client_consent_at__isnull=True,
        client_consent_expires__gte=now,
        client_consent_expires__lt=now + timezone.timedelta(hours=24),
    ).count()

    return {
        "role": "agent",
        "scope": scope,
        "summary": {
            "totalApplications": counts["total"],
            "pendingSubmission": counts["pending_submission"],
            "inAnalysis": counts["in_analysis"],
            "adjourned": counts["adjourned"],
            "pendingDisbursement": counts["pending_disbursement"],
            "approved": counts["approved"],
            "activeCredits": counts["active"],
            "staleApplications": stale,
            "consentExpiringSoon": expiring_soon,
        },
        "monthlyDisbursements": {
            "count": decaisses_du_mois.count(),
            "volumeUsd": volume["usd"],
            "volumeDetail": volume,
        },
    }


# ── Vue credit_committee ──────────────────────────────────────────────────────

def _committee_dashboard() -> dict[str, Any]:
    from credits.models import CreditApplication
    from config import settings

    from credits.committee import _amount_usd, committee_threshold_usd, requires_committee

    qs = CreditApplication.objects.select_related("client", "value_chain")

    # La corbeille applique EXACTEMENT la règle qui autorise le vote
    # (`requires_committee`, qui convertit en USD). Le filtre précédent comparait
    # `amount_requested` BRUT à un seuil en USD, donc des devises entre elles :
    # un dossier en CDF porte un nombre énorme en brut et entrait toujours dans
    # la corbeille — pour se voir ensuite refuser le vote en
    # `COMMITTEE_NOT_REQUIRED`. Le comité voyait des dossiers sur lesquels il ne
    # pouvait rien faire.
    # `in_analysis` est la file d'instruction : ensemble borné, donc filtrable en
    # Python. C'est le prix assumé pour n'avoir QU'UNE règle — une seconde règle
    # au niveau SQL, c'est une divergence qui revient au premier changement.
    candidats = [a for a in qs.filter(status="in_analysis") if requires_committee(a)]
    candidats.sort(key=_amount_usd, reverse=True)

    # Agrégat converti : sommer des montants de devises différentes et appeler le
    # total « USD » produisait un chiffre qui ne veut rien dire (§7.2, KPI honnêtes).
    total_usd = sum(_amount_usd(a) for a in candidats)

    pending_list = [
        {
            "code": a.code,
            "status": a.status,
            "amount_requested": a.amount_requested,
            "currency": a.currency,
            "value_chain__label": a.value_chain.label if a.value_chain_id else None,
            "created_at": a.created_at,
            # Montant comparable entre dossiers : c'est lui qui a décidé de la
            # présence dans la corbeille, il doit donc être lisible à l'écran.
            "amountUsd": _amount_usd(a),
        }
        for a in candidats[:20]
    ]
    _serialize_qs_values(pending_list)

    return {
        "role": "credit_committee",
        "scope": _perimetre(
            "institution",
            "Dossiers en analyse dont le montant converti en USD dépasse le plafond "
            "de délégation d'agence.",
        ),
        "summary": {
            "pendingReview": len(candidats),
            "totalVolumeUsd": float(total_usd),
            "delegationThresholdUsd": float(committee_threshold_usd()),
        },
        "pendingApplications": pending_list,
    }


# ── Vue branch_manager ────────────────────────────────────────────────────────

def _branch_dashboard(sub: str) -> dict[str, Any]:
    """Vue agence — restreinte à l'agence du demandeur, par lien direct.

    Le `sub` reçu était ignoré (`CreditApplication.objects.all()`) : un
    responsable de zone lisait les chiffres de l'institution entière comme si
    c'étaient ceux de son agence. Quand le rattachement est introuvable (aucune
    affectation en base), on NE feint PAS un périmètre d'agence : on sert
    l'institution et `scope.avertissement` le dit explicitement.

    Le périmètre repose désormais sur `CreditApplication.agency` pour les
    dossiers qui le portent, et sur l'ancienne heuristique par les personnes pour
    ceux qui n'en ont pas. `scope.rattachement` dit lequel des deux compose la
    vue affichée (`exact`, `approche` ou `mixte`), et `scope.dossiers` donne les
    effectifs de chacun : un chiffre calculé sur un lien direct et un chiffre
    déduit d'une intervention n'ont pas la même autorité (§4.6).
    """
    from credits.models import CreditApplication

    base = CreditApplication.objects.all()
    equipe, agence = _equipe_agence(sub)

    if agence is not None:
        qs = _dossiers_de_l_agence(base, agence, equipe or set())
        exacts = qs.filter(agency=agence).count()
        approches = qs.count() - exacts
        rattachement = (
            "exact" if not approches else ("approche" if not exacts else "mixte")
        )
        libelle = (
            f"Agence « {agence.name} » ({agence.code}) — {exacts} dossier(s) "
            f"rattaché(s) à l'agence"
        )
        if approches:
            libelle += (
                f", plus {approches} dossier(s) sans agence rattaché(s) par les "
                f"{len(equipe or ())} membres affectés"
            )
        scope = _perimetre(
            "branch",
            libelle + ".",
            rattachement=rattachement,
            dossiers={"exact": exacts, "approche": approches},
            avertissement=(
                (
                    f"{approches} dossier(s) de cette vue n'ont pas d'agence en base "
                    "(antérieurs au champ, ou montés par un compte sans affectation) : "
                    "ils sont rattachés par approximation, via les personnes "
                    "intervenues. Les dossiers portant une agence, eux, sont exacts."
                )
                if approches else ""
            ),
        )
    else:
        qs = base
        scope = _perimetre(
            "institution",
            "Institution entière.",
            # Le régime de rattachement se déclare AUSSI quand il n'y en a pas :
            # un front qui lit `scope.rattachement` pour choisir sa mention doit
            # trouver une valeur ici plutôt qu'un trou, sans quoi le périmètre le
            # plus large de tous serait le seul à ne rien annoncer.
            # (`dossiers` n'est PAS servi ici : il compte les dossiers d'une
            # agence, et il n'y a pas d'agence. Deux zéros seraient lus comme
            # « aucun dossier » alors que la vue en sert toute l'institution.)
            rattachement="indetermine",
            avertissement=(
                "Aucune agence d'affectation trouvée pour votre compte : les chiffres "
                "ci-dessous couvrent TOUTE l'institution, pas votre agence. Faites "
                "renseigner votre affectation (Utilisateurs → Affectation)."
            ),
        )
        logger.warning(
            "Vue agence demandée par « %s » sans affectation (`StaffProfile."
            "assignment` ni `Agency.manager_sub`) — périmètre institution servi.", sub,
        )

    now = timezone.now()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    counts = qs.aggregate(
        total=Count("id"),
        pending_approval=Count("id", filter=Q(status="in_analysis")),
        approved=Count("id", filter=Q(status="approved")),
        active=Count("id", filter=Q(status="active")),
        rejected=Count("id", filter=Q(status="rejected")),
        closed=Count("id", filter=Q(status="closed")),
    )

    decaisses_du_mois = qs.filter(status="active", disbursed_at__gte=month_start)
    volume = _agregat_usd(decaisses_du_mois, "disbursed_amount")

    rejet = _taux_rejet(qs)
    defaut = _taux_defaut(qs)

    return {
        "role": "branch_manager",
        "scope": scope,
        "summary": {
            "totalApplications": counts["total"],
            "pendingApproval": counts["pending_approval"],
            "approved": counts["approved"],
            "activeCredits": counts["active"],
            "rejectedApplications": counts["rejected"],
            "closedCredits": counts["closed"],
            "defaultRatePct": defaut["pct"],
            "defaultRateBase": defaut["base"],
            "rejectionRatePct": rejet["pct"],
            "rejectionRateBase": rejet["base"],
        },
        "monthlyDisbursements": {
            "count": decaisses_du_mois.count(),
            "volumeUsd": volume["usd"],
            "volumeDetail": volume,
        },
    }


# ── Vue regional_director ─────────────────────────────────────────────────────

def _regional_dashboard() -> dict[str, Any]:
    from credits.models import CreditApplication

    qs = CreditApplication.objects.all()

    global_counts = qs.aggregate(
        total=Count("id"),
        active=Count("id", filter=Q(status="active")),
        pending=Count("id", filter=Q(status__in=_PENDING_ANALYSIS)),
    )

    actifs = qs.filter(status="active")
    encours = _agregat_usd(actifs, "disbursed_amount")

    # Répartition par filière — convertie elle aussi : un encours par filière
    # additionnant CDF et USD classe les filières dans un ordre qui ne reflète
    # que la devise employée.
    by_vc = _encours_par_filiere(actifs)

    rejet = _taux_rejet(qs)
    defaut = _taux_defaut(qs)

    return {
        "role": "regional_director",
        "scope": _perimetre("institution", "Institution entière, toutes agences."),
        "summary": {
            "totalApplications": global_counts["total"],
            "activeCredits": global_counts["active"],
            "pendingApplications": global_counts["pending"],
            "totalEncoursUsd": encours["usd"],
            "totalEncoursDetail": encours,
            "defaultRatePct": defaut["pct"],
            "defaultRateBase": defaut["base"],
            "rejectionRatePct": rejet["pct"],
            "rejectionRateBase": rejet["base"],
        },
        "activeByValueChain": by_vc,
    }


# ── Vue admin ─────────────────────────────────────────────────────────────────

def _admin_dashboard() -> dict[str, Any]:
    from credits.models import CreditApplication, ScoringCriterion, CreditGuarantee

    qs = CreditApplication.objects.all()

    counts = qs.aggregate(
        total=Count("id"),
        draft=Count("id", filter=Q(status="draft")),
        submitted=Count("id", filter=Q(status="submitted")),
        in_analysis=Count("id", filter=Q(status="in_analysis")),
        approved=Count("id", filter=Q(status="approved")),
        pending_disbursement=Count("id", filter=Q(status="pending_disbursement")),
        active=Count("id", filter=Q(status="active")),
        rejected=Count("id", filter=Q(status="rejected")),
        adjourned=Count("id", filter=Q(status="adjourned")),
        closed=Count("id", filter=Q(status="closed")),
    )

    encours = _agregat_usd(qs.filter(status="active"), "disbursed_amount")

    # Critères de scoring configurés
    scoring_criteria_count = ScoringCriterion.objects.filter(active=True).count()

    # Garanties en attente de confirmation (morale PENDING)
    pending_moral = CreditGuarantee.objects.filter(
        guarantee_type=CreditGuarantee.GuaranteeType.MORALE,
        status=CreditGuarantee.Status.PENDING,
    ).count()

    # Consentements expirés non traités
    now = timezone.now()
    expired_consents = qs.filter(
        client_consent_expires__lt=now,
        client_consent_at__isnull=True,
        status__in=("submitted", "in_analysis"),
    ).count()

    rejet = _taux_rejet(qs)
    defaut = _taux_defaut(qs)

    return {
        "role": "admin",
        "scope": _perimetre(
            "institution", "Institution entière, toutes agences, tous statuts.",
        ),
        "counts": {
            "draft": counts["draft"],
            "submitted": counts["submitted"],
            "in_analysis": counts["in_analysis"],
            "approved": counts["approved"],
            "pending_disbursement": counts["pending_disbursement"],
            "active": counts["active"],
            "rejected": counts["rejected"],
            "adjourned": counts["adjourned"],
            "closed": counts["closed"],
            "total": counts["total"],
        },
        "financials": {
            "totalEncoursUsd": encours["usd"],
            "totalEncoursDetail": encours,
            "defaultRatePct": defaut["pct"],
            "defaultRateBase": defaut["base"],
            "rejectionRatePct": rejet["pct"],
            "rejectionRateBase": rejet["base"],
        },
        "alerts": {
            "pendingMoralGuarantees": pending_moral,
            "expiredConsents": expired_consents,
            "scoringCriteriaActive": scoring_criteria_count,
        },
    }


# ── Helpers ───────────────────────────────────────────────────────────────────

def _encours_par_filiere(actifs, limite: int = 10) -> list[dict]:
    """Encours par filière, converti en USD, trié décroissant et tronqué.

    `total_rows` accompagne la troncature (§4.6 : « `total_rows` sur toute liste
    tronquée ») — l'ancienne version coupait à 10 sans dire combien de filières
    existaient.
    """
    taux, _ = _taux_cdf_par_usd()
    par_filiere: dict[tuple, dict] = {}

    rows = (
        actifs.values("value_chain__code", "value_chain__label", "currency")
        .annotate(count=Count("id"), encours=Sum("disbursed_amount"))
        .order_by()
    )
    for row in rows:
        cle = (row["value_chain__code"], row["value_chain__label"])
        entree = par_filiere.setdefault(cle, {
            "value_chain__code": cle[0],
            "value_chain__label": cle[1],
            "count": 0,
            "_encours": Decimal("0"),
            "parDevise": {},
        })
        entree["count"] += row["count"] or 0
        montant = row["encours"] or Decimal("0")
        devise = (row["currency"] or "USD").upper()
        entree["parDevise"][devise] = float(montant)
        if devise == "USD":
            entree["_encours"] += montant
        elif devise == "CDF":
            entree["_encours"] += montant / taux

    lignes = []
    for entree in par_filiere.values():
        entree["encours"] = float(
            entree.pop("_encours").quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        )
        lignes.append(entree)
    lignes.sort(key=lambda r: r["encours"], reverse=True)

    tronquees = lignes[:limite]
    for ligne in tronquees:
        ligne["total_rows"] = len(lignes)
    return tronquees


def _taux_rejet(qs) -> dict:
    """Sélectivité de l'instruction : rejetés ÷ (actifs + clôturés + rejetés).

    C'est le calcul historiquement servi sous le nom `defaultRatePct`. Il ne
    mesure PAS une sinistralité : un dossier refusé à l'instruction n'a jamais
    été décaissé, donc n'a jamais pu tomber en défaut. Il porte désormais son
    vrai nom, et sa base est servie (§4.6 : pas de pourcentage sans base).
    """
    agg = qs.aggregate(
        rejected=Count("id", filter=Q(status="rejected")),
        resolved=Count("id", filter=Q(status__in=("active", "closed", "rejected"))),
    )
    rejetes = agg["rejected"] or 0
    resolus = agg["resolved"] or 0
    pct = round(rejetes / resolus * 100, 1) if resolus else 0.0
    return {
        "pct": pct,
        "base": {
            "rejected": rejetes,
            "resolved": resolus,
            # Un pourcentage calculé sur zéro dossier n'est pas « 0 % » : il n'est
            # pas calculable. L'écran a besoin de la différence.
            "computable": bool(resolus),
            "definition": "Dossiers rejetés ÷ dossiers résolus (actifs + clôturés + rejetés).",
        },
    }


def _taux_defaut(qs=None) -> dict:
    """Vrai taux de défaut : prêts en DEFAUT ÷ prêts décaissés (`portfolio.Loan`).

    La sinistralité se mesure sur le prêt décaissé, pas sur la demande : elle vit
    donc dans `portfolio`, pas dans `credits`. Le dénominateur est l'ensemble des
    prêts sortis d'instruction et effectivement mis en place (en cours, en
    défaut, clôturés, suspendus, bloqués) — un prêt jamais décaissé ne peut pas
    faire défaut et n'a rien à faire dans la base.

    `qs` (dossiers de crédit du périmètre courant) restreint les prêts à ceux qui
    sont rattachés à ces dossiers, quand le lien existe. Les prêts saisis
    directement côté portefeuille (sans `application`) n'ont aucun rattachement
    d'agence : ils ne sont comptés que sur un périmètre institution.
    """
    from portfolio.models import Loan

    #: Prêts effectivement mis en place — le dénominateur de toute sinistralité.
    decaisses = (
        Loan.Status.EN_COURS, Loan.Status.DEFAUT, Loan.Status.CLOTURE,
        Loan.Status.SUSPENDU, Loan.Status.BLOQUE,
    )

    prets = Loan.objects.filter(status__in=decaisses)
    perimetre = "institution"
    if qs is not None and _est_restreint(qs):
        prets = prets.filter(application__in=qs)
        perimetre = "dossiers du périmètre courant (prêts sans dossier lié exclus)"

    agg = prets.aggregate(
        en_defaut=Count("id", filter=Q(status=Loan.Status.DEFAUT)),
        total=Count("id"),
    )
    en_defaut = agg["en_defaut"] or 0
    total = agg["total"] or 0
    pct = round(en_defaut / total * 100, 1) if total else 0.0
    return {
        "pct": pct,
        "base": {
            "loansInDefault": en_defaut,
            "loansDisbursed": total,
            "computable": bool(total),
            "perimetre": perimetre,
            "definition": (
                "Prêts en défaut ÷ prêts décaissés (portfolio.Loan : en cours, "
                "défaut, clôturés, suspendus, bloqués). Effectifs, pas montants."
            ),
        },
    }


def _est_restreint(qs) -> bool:
    """True si `qs` porte un filtre (périmètre agence/personnel), False si c'est
    l'institution entière. Évite un `IN (…)` inutile sur la table complète."""
    return bool(getattr(getattr(qs, "query", None), "where", None))


def _serialize_qs_values(rows: list[dict]) -> None:
    """Convertit les champs datetime en isoformat dans une liste de dicts .values()."""
    for row in rows:
        for key, val in list(row.items()):
            if hasattr(val, "isoformat"):
                row[key] = val.isoformat()
            elif isinstance(val, Decimal):
                row[key] = float(val)
