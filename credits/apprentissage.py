"""Boucle d'apprentissage des référentiels de filière (principe 10).

Le constat
----------
`ReferentielFiliere.n_cas_reels` était lu partout — l'analyste voit l'autorité
du référentiel (`referentielInfo.nCasReels`), le client lit « comparaison faite
contre un référentiel indicatif (N = 0 dossiers réels), fiabilité limitée » —
et n'était écrit NULLE PART : seul l'import (`referentiel_loader`) le posait à
zéro. La promesse « chaque dossier clôturé enrichit les statistiques par
filière » n'avait aucun mécanisme derrière elle. Toutes les filières restaient
indicatives à vie, quel que soit le nombre de dossiers réellement bouclés.

Ce que fait ce module
---------------------
À la clôture d'un dossier, il fige une `ObservationFiliere` — le dossier tel
qu'il s'est terminé — et RECALCULE `n_cas_reels` comme le décompte des
observations contributives de la filière. Un décompte, pas un compteur : un
compteur incrémenté se désynchronise au premier incident et personne ne peut
plus dire ce qu'il compte.

Ce qu'il ne fait PAS, et c'est délibéré
--------------------------------------
**Aucune substitution de référentiel.** Franchir le seuil de N ne bascule rien :
`source` reste `indicatif` tant qu'un comité n'a pas proposé la version apprise
et qu'un second membre ne l'a pas activée (maker-checker, comme les barèmes).
Ce module dit seulement « cette filière est ÉLIGIBLE à l'apprentissage » —
`candidats_a_l_apprentissage()`. Une plage apprise qui s'installerait toute
seule serait précisément la substitution silencieuse que le principe 10
interdit : les dossiers déjà instruits l'auraient été contre une autre autorité
que celle affichée.

Deux finesses de données, qui décident de la qualité de N
---------------------------------------------------------
1. **Un dossier dérivé du référentiel n'apprend rien au référentiel.** Quand
   aucune feuille de besoins n'est exploitable, `disbursement` ventile le
   montant sur les poids modules du référentiel (`ModuleAllocation.source =
   "referential"`). Compter ce dossier ferait grossir N de copies de la
   référence, et une filière basculerait en « appris » sur ses propres
   estimations. L'observation est enregistrée (la clôture EST un fait), mais
   `contributive = False` et elle n'entre pas dans N.
2. **Ce que N compte est nommé.** Ce sont des dossiers clos dont la ventilation
   par module vient de leur PROPRE feuille de besoins. Ce ne sont pas encore des
   coûts RÉALISÉS : le système ne suit pas la dépense par module après
   décaissement. `ObservationFiliere.origine_couts` le dit, et les statistiques
   servies le rappellent — une plage « apprise » sur des budgets validés n'a pas
   la même autorité qu'une plage apprise sur des dépenses constatées, et le jour
   où le suivi de dépense existera, il alimentera le même mécanisme.
"""
from __future__ import annotations

import logging
from decimal import Decimal, ROUND_HALF_UP

from django.db import transaction

logger = logging.getLogger(__name__)

CENTIME = Decimal("0.01")

#: Seuil de bascule indicatif → appris. VALEUR DE SECOURS uniquement (principe
#: 8, « exception : les valeurs par défaut de secours, avec warning loggé ») :
#: le seuil vit en base dans `AnalysisRule.thresholds`.
N_MIN_DEFAUT = 30

#: Règle paramétrable qui porte le seuil. `AnalysisRule` plutôt que
#: `BaremeScore` : un `BaremeScore` est une COURBE ou un jeu de règles de
#: SCORING, servi par la machinerie de révision (golden set, prévisualisation
#: d'impact sur les recommandations). Un seuil d'effectif n'a rien à y faire —
#: `previsualiser_impact` n'aurait rien à prévisualiser. `AnalysisRule` est la
#: table des seuils opérationnels, et c'est déjà celle qui porte ceux du
#: contrôle de cohérence de la feuille de besoins (principe 6 : une seule
#: nomenclature par concept).
REGLE_APPRENTISSAGE = "apprentissage_referentiel"

#: Provenances de ventilation qui APPRENNENT quelque chose au référentiel.
#: `referential` en est exclu par construction (cf. docstring, finesse 1).
ORIGINES_CONTRIBUTIVES = ("needs_sheet",)


def q2(valeur) -> Decimal:
    return Decimal(str(valeur)).quantize(CENTIME, rounding=ROUND_HALF_UP)


def seuil_n_min() -> int:
    """Nombre de dossiers réels à partir duquel une filière devient apprenable.

    Lu en base ; le défaut de secours est loggué chaque fois qu'il s'applique,
    pour qu'un paramètre absent ne se confonde jamais avec un paramètre choisi.
    """
    from credits.models import AnalysisRule

    regle = AnalysisRule.objects.filter(rule_id=REGLE_APPRENTISSAGE, active=True).first()
    valeur = (regle.thresholds or {}).get("n_min_cas_reels") if regle else None
    if valeur is None:
        logger.warning(
            "Seuil d'apprentissage non paramétré (AnalysisRule « %s ») : repli sur "
            "la valeur de secours N ≥ %s. À poser en base pour que le comité "
            "puisse la déplacer sans redéploiement.",
            REGLE_APPRENTISSAGE, N_MIN_DEFAUT,
        )
        return N_MIN_DEFAUT
    try:
        return int(Decimal(str(valeur)))
    except Exception:  # noqa: BLE001
        logger.warning(
            "Seuil d'apprentissage illisible en base (« %s ») : repli sur N ≥ %s.",
            valeur, N_MIN_DEFAUT,
        )
        return N_MIN_DEFAUT


# ── Référentiel du dossier ────────────────────────────────────────────────────

def referentiel_du_dossier(application):
    """Référentiel auquel ce dossier a été confronté — ou celui de sa filière.

    Priorité à celui de la DERNIÈRE analyse : c'est l'autorité contre laquelle
    le dossier a réellement été jugé, et elle reste juste même si le référentiel
    actif a changé depuis. À défaut d'analyse (dossier instruit avant le moteur),
    on retombe sur la résolution unique de `credits.analyse` — jamais sur une
    seconde règle de résolution, qui finirait par diverger (principe 6).
    """
    from credits.analyse import resoudre_referentiel
    from credits.models import AnalyseCredit

    derniere = (
        AnalyseCredit.objects.filter(application=application)
        .select_related("referentiel")
        .order_by("-execute_le", "-id")
        .first()
    )
    if derniere is not None and derniere.referentiel_id:
        return derniere.referentiel
    return resoudre_referentiel(application)


def _ventilation(application) -> tuple[dict, Decimal, str]:
    """Coûts par module du dossier, leur total, et LEUR PROVENANCE.

    Les `ModuleAllocation` sont la ventilation retenue au décaissement : elles
    portent leur `source`, ce qui permet de distinguer un dossier qui apprend
    quelque chose d'un dossier recopié du référentiel.
    """
    allocations = list(application.module_allocations.all())
    if not allocations:
        return {}, Decimal("0.00"), ""

    couts: dict[str, str] = {}
    total = Decimal("0.00")
    for allocation in allocations:
        montant = q2(allocation.cost or 0)
        couts[allocation.module] = str(montant)
        total += montant

    sources = {a.source for a in allocations if a.source}
    if len(sources) == 1:
        origine = sources.pop()
    elif sources:
        origine = "mixte"
    else:
        origine = ""
    return couts, q2(total), origine


# ── Enregistrement d'une clôture ──────────────────────────────────────────────

@transaction.atomic
def enregistrer_cloture(application, *, par: str = "", clos_le=None):
    """Fige l'observation d'un dossier clôturé et recalcule N pour sa filière.

    Idempotent : une seconde clôture du même dossier retourne l'observation
    existante sans rien recompter (`OneToOne`).

    Ne lève jamais sur un dossier dont la filière n'a pas de référentiel actif :
    la clôture est un acte métier, elle n'a pas à échouer parce que la boucle
    d'apprentissage n'a rien où déverser. L'absence est journalisée — ce qui la
    rend visible — et la fonction retourne `None`.
    """
    from django.utils import timezone

    from credits.models import ObservationFiliere

    existante = ObservationFiliere.objects.filter(application=application).first()
    if existante is not None:
        return existante

    try:
        referentiel = referentiel_du_dossier(application)
    except Exception as exc:  # noqa: BLE001 - l'absence de référentiel ne bloque pas une clôture
        logger.warning(
            "Clôture de %s non capitalisée : %s", application.code, exc,
        )
        return None

    couts, total, origine = _ventilation(application)
    chain = application.value_chain

    observation = ObservationFiliere.objects.create(
        application=application,
        referentiel=referentiel,
        value_chain_code=(chain.code if chain else ""),
        filiere=referentiel.filiere,
        devise=application.currency,
        montant_decaisse=q2(application.disbursed_amount or application.amount_approved or 0),
        quantite_reference=(
            application.quantite_reference
            if application.quantite_reference is not None else application.area_ha
        ),
        unite_reference=(application.unite_reference or referentiel.unite_reference or ""),
        couts_modules=couts,
        cout_total=total,
        origine_couts=origine,
        contributive=bool(couts) and origine in ORIGINES_CONTRIBUTIVES,
        clos_le=clos_le or timezone.now(),
        clos_par=par or "",
    )
    recalculer_n_cas_reels(referentiel)
    return observation


def recalculer_n_cas_reels(referentiel) -> int:
    """`n_cas_reels` = décompte des observations CONTRIBUTIVES de la filière.

    Un décompte plutôt qu'un `F("n_cas_reels") + 1` : il se rejoue, il se
    vérifie, et il ne dérive pas si une observation est ajoutée par un autre
    chemin. `source` n'est jamais touchée ici (cf. docstring du module).
    """
    from credits.models import ObservationFiliere

    n = ObservationFiliere.objects.filter(
        referentiel=referentiel, contributive=True,
    ).count()
    if referentiel.n_cas_reels != n:
        referentiel.n_cas_reels = n
        referentiel.save(update_fields=["n_cas_reels", "updated_at"])
    return n


# ── Restitution : ce que la filière a appris ──────────────────────────────────

def statistiques_filiere(referentiel) -> dict:
    """Statistiques observées d'une filière — matière première d'une révision.

    Pas de moyenne sans effectif, pas de coût sans devise (§4.6). Les dossiers
    non contributifs sont comptés SÉPARÉMENT et jamais fondus dans les
    statistiques : ils diraient au référentiel ce que le référentiel a dit.

    La médiane est préférée à la moyenne : sur des effectifs de quelques
    dizaines de dossiers, un seul projet hors norme déplace une moyenne et donc
    la plage qui en sortirait.
    """
    from credits.models import ObservationFiliere

    observations = list(
        ObservationFiliere.objects.filter(referentiel=referentiel, contributive=True)
    )
    non_contributives = ObservationFiliere.objects.filter(
        referentiel=referentiel, contributive=False,
    ).count()

    par_module: dict[str, list[Decimal]] = {}
    totaux: list[Decimal] = []
    devises = set()
    for observation in observations:
        devises.add(observation.devise)
        totaux.append(q2(observation.cout_total))
        for module, montant in (observation.couts_modules or {}).items():
            par_module.setdefault(module, []).append(q2(montant))

    n = len(observations)
    seuil = seuil_n_min()
    return {
        "referentiel": referentiel.code,
        "filiere": referentiel.filiere,
        "source": referentiel.source,
        "nCasReels": n,
        "nNonContributifs": non_contributives,
        "seuilApprentissage": seuil,
        "eligibleApprentissage": n >= seuil,
        # Une seule devise dans le lot, ou aucune agrégation : un coût médian
        # multi-devises ne veut rien dire (CONVENTIONS.md §7.2, « KPI honnêtes »).
        "devise": devises.pop() if len(devises) == 1 else None,
        "devisesMelangees": len(devises) > 1,
        "coutTotalMedian": float(_mediane(totaux)) if totaux and len(devises) <= 1 else None,
        "coutsModulesMedians": (
            {module: float(_mediane(valeurs)) for module, valeurs in sorted(par_module.items())}
            if len(devises) <= 1 else {}
        ),
        "effectifParModule": {module: len(v) for module, v in sorted(par_module.items())},
        # Provenance assumée : ce sont des budgets validés au décaissement, pas
        # encore des dépenses constatées.
        "natureDonnees": "ventilation de la feuille de besoins au décaissement",
    }


def candidats_a_l_apprentissage() -> list[dict]:
    """Filières encore indicatives qui ont atteint le seuil.

    Sortie destinée au comité : ce sont des CANDIDATURES à instruire, pas des
    bascules. Rien n'est modifié par cet appel.
    """
    from credits.models import ReferentielFiliere

    seuil = seuil_n_min()
    candidats = []
    for referentiel in ReferentielFiliere.objects.filter(
        actif=True, source=ReferentielFiliere.Source.INDICATIF,
    ):
        if referentiel.n_cas_reels >= seuil:
            candidats.append(statistiques_filiere(referentiel))
    return candidats


def _mediane(valeurs: list[Decimal]) -> Decimal:
    ordonnees = sorted(valeurs)
    n = len(ordonnees)
    if n == 0:
        return Decimal("0.00")
    milieu = n // 2
    if n % 2 == 1:
        return q2(ordonnees[milieu])
    return q2((ordonnees[milieu - 1] + ordonnees[milieu]) / Decimal(2))
