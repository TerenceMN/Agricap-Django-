"""
Moteur d'analyse technico-économique — **LE moteur de scoring du module crédit**.

AUTORITÉ (lot d'unification, juillet 2026)
------------------------------------------
Trois systèmes notaient un même dossier : ce module, `credits.scoring` (cinq
AUTRES critères, en `float`, écrasant `score_result`) et `credits.dataio_simulator`
(cinq critères aux codes encore différents, courbes et grille de taux en dur).
Un analyste ne pouvait pas dire lequel faisait foi, et les deux derniers
proposaient un taux différent pour la même bande de score.

Ce module fait foi, pour des raisons vérifiables : `Decimal` de bout en bout
(principe 4), barèmes / poids / seuils en base et recalibrables par le comité
(principe 8), résultat append-only tracé jusqu'à la révision et au SHA-256 de la
feuille de besoins (principes 1 et 3), journalisation d'audit, et contrat front
typé dessus. Les deux autres modules sont devenus ses clients :

  - `credits.scoring` PROJETTE la dernière `AnalyseCredit` en `score_result` ;
  - `credits.dataio_simulator` SIMULE avec les mêmes barèmes, les mêmes poids,
    la même grille de taux et le même échéancier.

Il n'existe donc plus qu'un chemin vers un score (`executer_analyse`) et qu'un
chemin vers un taux (`proposer_taux`).

Cinq critères pondérés produisent un score global et une **recommandation**.
Le moteur ne décide rien : `recommander()` renvoie une chaîne que l'analyste lit,
jamais une transition de la machine à états (principe 2). Un dossier reste dans
son statut après une analyse, quelle que soit sa note.

Discipline de calcul (principe 4) — `Decimal` partout, `float` nulle part :
  - montants  → quantize `0.01`
  - ratios    → quantize `0.001`
  - scores et points → quantize `0.1`
  - arrondi   → `ROUND_HALF_UP` systématique

Le pseudo-code de la SPEC utilise `float` (`bareme.evaluer(float(dscr))`,
`float(val)` dans les détails, lignes d'échéancier en `float`) : c'est l'erreur
que ce module corrige. Un seul `float` dans la chaîne suffit à faire diverger un
score de 0,1 point, et 0,1 point autour de 45,0 change la recommandation.

Sources de données (principe 1) — rien n'est lu dans un fichier ni dans un payload :
  - coûts par module  → `DataRecord` de `application.needs_source` (lot 2)
  - garanties         → `CreditGuarantee.retained_coverage` (décote déjà appliquée)
  - comportement      → `portfolio.Loan` du client
  - barèmes et poids  → `BaremeScore` / `referentiel.InstitutionConfig` (principe 8)

ÉCARTS SIGNALÉS AVEC LA SPEC — voir `docs/status-fragments/moteur-backend.md` :
  1. `DemandeCredit` / `PlanFinancierUpload` n'existent pas → `CreditApplication`
     et `application.needs_source`.
  2. La SPEC lit les cash-flows dans une feuille « Tresorerie ». Ce module a
     longtemps affirmé ici que cette feuille « n'existe pas » — c'était faux, et
     l'erreur portait sur 20 % du score.

     Elle existe sur les QUATORZE simulateurs (`16_Tresorerie_Mensuelle`), elle
     était déjà ingérée en `DataTable`, et seul `referentiel_loader` l'ignorait :
     il ne lisait que 3 des 19 feuilles du classeur. Le calendrier est désormais
     chargé dans `ReferentielFiliere.tresorerie_ref` et SERVI dans les détails du
     critère DSCR (`calendrierReference`).

     Il n'est pas encore CONSOMMÉ : les cash-flows restent PROJETÉS depuis
     `rendement_ref` puis étalés à plat sur les mois d'amortissement. Brancher le
     calendrier réel déplacerait le DSCR de chaque dossier, donc les
     recommandations — arbitrage du comité, pas d'un lot de lecture. L'hypothèse
     reste explicite et l'écart est désormais mesurable à l'écran.

     Ce que le classeur CLIENT ne porte toujours pas, en revanche, c'est sa
     propre trésorerie : son template s'arrête à la feuille 7.
  3. Les scores de l'exemple de référence (DSCR 19,1 ; stress 14,3) ne se
     déduisent pas des barèmes de la SPEC §5 (ils donnent 19,7 et 6,4 pour
     DSCR 0,636 / 0,477). Écart documenté et testé tel quel.
"""
from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from django.db import transaction
from django.utils import timezone

from credits.echeancier import (
    MODE_INTERETS_SEULS,
    MODES,
    EcheancierError,
    construire_echeancier,
    serialiser_echeancier,
    totaux_echeancier,
)
from credits.models import AnalyseCredit, BaremeScore, ReferentielFiliere
from credits.needs_sheet import MODULE_CODES, extract_module_totals

logger = logging.getLogger(__name__)

VERSION_MOTEUR = "4.0"

CENT = Decimal("0.01")      # montants
MILLE = Decimal("0.001")    # ratios (DSCR, couverture, écarts)
DIXIEME = Decimal("0.1")    # scores et points
ZERO = Decimal("0")

#: Ordre canonique des critères — c'est celui du contrat front (`CreditAnalyse.criteres`).
CRITERES = ("technique", "dscr", "stress", "comportemental", "garanties")

#: Poids de secours (principe 8, exception « valeurs par défaut de secours ») :
#: appliqués UNIQUEMENT si `InstitutionConfig` est absent ou incohérent, et
#: toujours avec un warning loggé.
POIDS_DEFAUT = {
    "technique": Decimal("25"), "dscr": Decimal("20"), "stress": Decimal("10"),
    "comportemental": Decimal("30"), "garanties": Decimal("15"),
}

#: Règles de décision de secours — miroir de la SPEC §4 `recommander()`.
REGLES_DECISION_DEFAUT = {
    "approbation": {"score_min": "75", "dscr_min": "1.2", "sans_hors_plage": True},
    "approbation_cond": {"score_min": "60", "dscr_min": "1.0"},
    "revue": {"score_min": "45"},
    "choc_revenus": "0.25",
}

#: Un DSCR sous ce plancher interdit toute forme d'approbation, quel que soit le
#: score. Règle de sûreté, pas un seuil de barème : elle ne se recalibre pas.
DSCR_PLANCHER_APPROBATION = Decimal("1.0")

#: Grille de TARIFICATION de secours — la seule du module (principe 6).
#:
#: Elle vivait en double et en dur : `scoring.py._propose_rate` appliquait +2,5 sur
#: la bande [55, 70[ quand `dataio_simulator` appliquait +2,0 sur la même bande.
#: Le même client recevait donc deux taux selon l'écran consulté — 20,5 % à
#: l'instruction après 20,0 % annoncés à la simulation, sur un taux de base de
#: 18 %. Arbitrage retenu : **+2,0**, pour trois raisons cumulées :
#:
#:   1. c'est la valeur DÉJÀ ANNONCÉE au client par le simulateur ; entre deux
#:      chiffres, celui sur lequel le client a fondé sa demande engage l'institution
#:      (un taux revu à la hausse après coup se plaide mal et se justifie encore
#:      moins par « deux moteurs coexistaient ») ;
#:   2. elle rend la grille symétrique (−2,0 en haut de barème, +2,0 au milieu) :
#:      une surcote explicable est une surcote défendable devant un comité comme
#:      devant un régulateur ; +2,5 n'a de trace ni dans une SPEC ni en base ;
#:   3. elle est désormais en base : le comité qui veut 2,5 le décide par révision
#:      de barème (maker ≠ checker, impact prévisualisé sur le golden set), sans
#:      redéploiement — l'arbitrage est réversible sans toucher au code.
#:
#: Les bandes de tarification (85 / 70 / 55) ne sont volontairement PAS celles de
#: la décision (75 / 60 / 45) ni celles de la lettre (85 / 70 / 50) : tarifer,
#: décider et noter sont trois actes distincts. Les confondre reviendrait à faire
#: bouger le prix d'un crédit en recalibrant une lettre affichée au client.
GRILLE_TAUX_DEFAUT = {
    "grille": [
        {"score_min": "85", "ajustement": "-2.0"},
        {"score_min": "70", "ajustement": "0.0"},
        {"score_min": "55", "ajustement": "2.0"},
        {"score_min": "0", "ajustement": "5.0"},
    ],
    #: Plancher de sécurité : la bonification ne peut pas descendre le taux sous
    #: 70 % du taux de base de la filière (hérité de `scoring._propose_rate`, seul
    #: garde-fou de l'ancienne grille qui méritait d'être conservé).
    "plancher_ratio_base": "0.7",
}

#: Barèmes indispensables : sans eux, un score serait fabriqué par l'outil.
CODES_BAREMES = ("ECART_TECHNIQUE", "DSCR", "COUVERTURE_GARANTIES", "DECISION")

#: Barèmes FACULTATIFS : leur absence n'empêche pas de scorer, elle fait retomber
#: sur la valeur de secours documentée, avec warning loggé (principe 8, exception).
#: `TAUX` en fait partie : une grille de tarification manquante ne doit pas
#: bloquer l'instruction d'un dossier, contrairement à une courbe de score
#: manquante qui, elle, fabriquerait un 0.
CODES_BAREMES_FACULTATIFS = ("TAUX",)


# ── Exceptions (convention `credits.workflow`) ───────────────────────────────

class AnalyseError(Exception):
    """Le moteur refuse d'analyser. `code` et `http_status` portés par la classe.

    Même contrat que `credits.workflow.WorkflowError` : c'est le `code` que le
    front consomme, jamais la formulation du message, et `as_errors()` détaille
    cause par cause (principe 5 — jamais un message générique).
    """

    code = "ANALYSE_ERROR"
    http_status = 422

    def __init__(self, message: str, errors: list[dict] | None = None) -> None:
        super().__init__(message)
        self.errors = errors or []

    def as_errors(self) -> list[dict]:
        return self.errors or [{"code": self.code, "message": str(self)}]


class SourceBesoinsAbsente(AnalyseError):
    """Aucune feuille de besoins ingérée : rien à scorer (principe 1)."""

    code = "SOURCE_BESOINS_ABSENTE"


class ReferentielAbsent(AnalyseError):
    """Aucun référentiel actif pour la filière du dossier.

    On refuse plutôt que de scorer contre un référentiel de repli : un score
    technique de 0 imputé à une absence de configuration serait un refus de
    crédit fabriqué par l'outil, pas par le dossier.
    """

    code = "REFERENTIEL_ABSENT"


class BaremeAbsent(AnalyseError):
    """Un barème indispensable manque en base — le comité doit le seeder."""

    code = "BAREME_ABSENT"


class ParametresInvalides(AnalyseError):
    """Durée / différé / taux inexploitables (relaie `EcheancierError`)."""

    code = "PARAMETRES_INVALIDES"


class DimensionIncoherente(AnalyseError):
    """Le dossier et le référentiel ne parlent pas de la même unité.

    Un référentiel apicole donne des USD PAR RUCHE ; un dossier qui ne porte
    qu'une superficie en hectares n'est pas comparable à lui. Multiplier l'un par
    l'autre produirait un coût de référence faux d'un facteur arbitraire — et un
    score technique (25 % de la note) tout aussi faux. On refuse.
    """

    code = "DIMENSION_INCOHERENTE"


# ── Quantize ─────────────────────────────────────────────────────────────────

def q2(value) -> Decimal:
    return Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)


def q3(value) -> Decimal:
    return Decimal(str(value)).quantize(MILLE, rounding=ROUND_HALF_UP)


def q1(value) -> Decimal:
    return Decimal(str(value)).quantize(DIXIEME, rounding=ROUND_HALF_UP)


def _points(score: Decimal, poids: Decimal) -> Decimal:
    """`points = score × poids / 100`, arrondi au dixième.

    L'arrondi se fait ICI, critère par critère, et le score global est la somme
    des points arrondis — pas l'arrondi de la somme. C'est ce que l'analyste voit
    à l'écran : une colonne « points » dont le total doit tomber juste. L'exemple
    de la SPEC §2 en dépend (3,82 + 1,43 + 15 + 9 = 29,25 → 29,3 si on arrondit
    à la fin, mais 3,8 + 1,4 + 15,0 + 9,0 = 29,2 comme annoncé).
    """
    return q1(score * poids / Decimal(100))


# ── Chargement des règles (principe 8) ───────────────────────────────────────

def charger_baremes() -> dict[str, BaremeScore]:
    """Barèmes actifs indexés par code. Lève `BaremeAbsent` si un OBLIGATOIRE manque."""
    baremes = {b.code: b for b in BaremeScore.objects.filter(actif=True)}
    manquants = [c for c in CODES_BAREMES if c not in baremes]
    if manquants:
        raise BaremeAbsent(
            "Barèmes de score absents de la base : "
            f"{', '.join(manquants)}. Exécutez « manage.py seed_analyse » "
            "ou faites-les créer par le comité de crédit.",
            errors=[{"code": "BAREME_ABSENT",
                     "message": f"Le barème « {c} » n'existe pas ou est inactif."}
                    for c in manquants],
        )
    return baremes


def poids_effectifs() -> dict[str, Decimal]:
    """Pondération des 5 critères, lue dans `InstitutionConfig` (principe 8).

    Repli sur `POIDS_DEFAUT` avec warning loggé si la config est absente ou si la
    somme n'est pas 100 : une pondération qui ne somme pas à 100 rendrait le
    score global inintelligible (« 87/100 » sur une base de 92).
    """
    try:
        from referentiel.models import InstitutionConfig
        cfg = InstitutionConfig.active()
        poids = {
            "technique": Decimal(str(cfg.poids_technique)),
            "dscr": Decimal(str(cfg.poids_financier)),
            "stress": Decimal(str(cfg.poids_stress)),
            "comportemental": Decimal(str(cfg.poids_comportemental)),
            "garanties": Decimal(str(cfg.poids_garanties)),
        }
    except Exception as exc:  # noqa: BLE001 — la config ne doit jamais casser l'analyse
        logger.warning(
            "Pondération : InstitutionConfig illisible (%s) — repli sur les poids "
            "par défaut %s.", exc, POIDS_DEFAUT)
        return dict(POIDS_DEFAUT)

    total = sum(poids.values(), ZERO)
    if total != Decimal(100):
        logger.warning(
            "Pondération : la somme des poids d'InstitutionConfig vaut %s et non "
            "100 — repli sur les poids par défaut.", total)
        return dict(POIDS_DEFAUT)
    return poids


def regles_decision(bareme: BaremeScore | None) -> dict:
    """Règles du barème de décision à 4 niveaux, avec repli loggé."""
    params = (bareme.parametres if bareme else None) or {}
    if not params:
        logger.warning(
            "Barème DECISION vide — repli sur les règles par défaut (SPEC §4).")
        return dict(REGLES_DECISION_DEFAUT)
    return params


def regles_taux(bareme: BaremeScore | None) -> dict:
    """Grille de tarification active, avec repli loggé sur `GRILLE_TAUX_DEFAUT`."""
    params = (bareme.parametres if bareme else None) or {}
    if not params.get("grille"):
        logger.warning(
            "Barème TAUX absent ou vide — repli sur la grille de tarification par "
            "défaut %s. Exécutez « manage.py seed_analyse » pour la faire vivre en "
            "base (principe 8).", GRILLE_TAUX_DEFAUT["grille"])
        return dict(GRILLE_TAUX_DEFAUT)
    return params


def proposer_taux(score_global, taux_base, regles: dict | None = None) -> dict:
    """Taux proposé = taux de base de la filière + ajustement de la bande de score.

    **Unique point de tarification du module** : `credits.scoring` (dossier
    instruit) et `credits.dataio_simulator` (simulation indicative) appellent tous
    deux cette fonction. C'est ce qui garantit qu'un client simule et instruit au
    même taux — la divergence +2,5 / +2,0 venait de deux implémentations parallèles,
    pas d'un désaccord métier.

    Retourne le détail complet, pas seulement le nombre : la bande retenue,
    l'ajustement appliqué, le plancher et son déclenchement. Un taux servi sans
    sa justification n'est pas auditable (CONVENTIONS.md §4.6).

    Args:
        score_global: score 0–100 du moteur.
        taux_base: taux annuel de base de la filière, en POINTS (18 = 18 %/an).
        regles: `BaremeScore.TAUX.parametres`, ou `None` pour la grille de secours.
    """
    regles = regles or GRILLE_TAUX_DEFAUT
    score = Decimal(str(score_global or 0))
    base = Decimal(str(taux_base or 0))

    grille = regles.get("grille") or GRILLE_TAUX_DEFAUT["grille"]
    paliers = sorted(grille, key=lambda p: Decimal(str(p.get("score_min", 0))),
                     reverse=True)

    ajustement = ZERO
    bande = None
    for palier in paliers:
        seuil = Decimal(str(palier.get("score_min", 0)))
        if score >= seuil:
            ajustement = Decimal(str(palier.get("ajustement", 0)))
            bande = seuil
            break
    if bande is None:  # grille sans palier atteignable — jamais silencieux
        logger.warning(
            "Grille de tarification sans palier applicable au score %s : aucun "
            "ajustement appliqué.", score)

    brut = q2(base + ajustement)
    ratio = Decimal(str(regles.get("plancher_ratio_base",
                                   GRILLE_TAUX_DEFAUT["plancher_ratio_base"])))
    plancher = q2(base * ratio)
    taux = max(brut, plancher)

    return {
        "tauxBase": float(q2(base)),
        "bandeScoreMin": float(bande) if bande is not None else None,
        "ajustement": float(q2(ajustement)),
        "tauxAvantPlancher": float(brut),
        "plancher": float(plancher),
        "plancherApplique": taux != brut,
        "taux": float(taux),
        "_taux": taux,          # Decimal — le seul chiffre qui entre en base
        "origine": "bareme" if regles is not GRILLE_TAUX_DEFAUT else "defaut",
    }


def choc_stress(regles: dict) -> Decimal:
    """Amplitude du choc de revenus du stress test, en fraction (0,25 = −25 %)."""
    brut = regles.get("choc_revenus", REGLES_DECISION_DEFAUT["choc_revenus"])
    try:
        choc = Decimal(str(brut))
    except Exception:  # noqa: BLE001
        choc = Decimal(REGLES_DECISION_DEFAUT["choc_revenus"])
    if not (ZERO <= choc < Decimal(1)):
        logger.warning("Choc de stress hors [0, 1[ : %s — repli sur 0.25.", choc)
        choc = Decimal("0.25")
    return choc


# ── Dimension de référence du dossier (modèle « hectare » généralisé) ────────

def resoudre_quantite_reference(application, referentiel) -> tuple[Decimal | None, str]:
    """`(quantité, unité)` du dossier, dans l'unité EXIGÉE par le référentiel.

    Le référentiel d'une filière donne des coûts par unité de référence : par
    hectare pour le maïs, par ruche pour l'apiculture, par sujet pour l'élevage,
    par m² pour la bioconversion, par sac pour la myciculture, par tonne usinée
    pour la transformation. Le dossier doit fournir sa quantité DANS CETTE UNITÉ.

    Trois cas, et un seul refus :
      - `quantite_reference` renseignée → elle fait foi ; son unité doit être celle
        du référentiel (unité vide = celle du référentiel, avec warning : le champ
        n'a de sens que rapporté à la filière du dossier) ;
      - sinon `area_ha` → l'ancienne dimension, valable UNIQUEMENT si le
        référentiel se mesure en hectares ;
      - dimension absente → `(None, unité)` : le critère technique le dira, il ne
        sera pas inventé.

    Lève `DimensionIncoherente` quand les deux unités diffèrent — jamais de
    conversion implicite, il n'en existe aucune entre une ruche et un hectare.
    """
    unite_ref = (getattr(referentiel, "unite_reference", "") or "ha").strip().lower()
    quantite = getattr(application, "quantite_reference", None)
    unite_dossier = (getattr(application, "unite_reference", "") or "").strip().lower()

    if quantite is not None:
        quantite = Decimal(str(quantite))
        if not unite_dossier:
            logger.warning(
                "Dossier %s : quantité de référence %s sans unité — l'unité du "
                "référentiel « %s » (%s) est retenue.",
                getattr(application, "code", "?"), quantite, referentiel.code, unite_ref)
            unite_dossier = unite_ref
        if unite_dossier != unite_ref:
            raise DimensionIncoherente(
                f"Le dossier est dimensionné en « {unite_dossier} » alors que le "
                f"référentiel « {referentiel.code} » de la filière donne ses coûts "
                f"par « {unite_ref} ». Aucune conversion n'existe entre ces deux "
                f"unités : l'analyse est refusée plutôt que faite sur une "
                f"multiplication qui n'a pas de sens.",
                errors=[{"code": DimensionIncoherente.code,
                         "message": f"Unité du dossier « {unite_dossier} » ≠ unité du "
                                    f"référentiel « {unite_ref} »."}],
            )
        return quantite, unite_ref

    surface = getattr(application, "area_ha", None)
    if surface is not None and unite_ref != "ha":
        raise DimensionIncoherente(
            f"Le dossier ne porte qu'une superficie en hectares alors que la "
            f"filière se mesure en « {unite_ref} » (référentiel "
            f"« {referentiel.code} »). Renseignez « quantite_reference » dans cette "
            f"unité : un plan de {surface} ne se compare pas à un coût par "
            f"{unite_ref}.",
            errors=[{"code": DimensionIncoherente.code,
                     "message": f"Dossier en hectares, référentiel en « {unite_ref} »."}],
        )
    if surface is None:
        return None, unite_ref
    return Decimal(str(surface)), unite_ref


# ── C1 — Fiabilité technique (25 %) ──────────────────────────────────────────

def ponderation_modules(couts: dict, poids_modules: dict | None) -> tuple[dict, str, str]:
    """Poids par module pour la moyenne des écarts — `(poids, origine, commentaire)`.

    POURQUOI CE BLOC EXISTE
    -----------------------
    La moyenne des écarts était ARITHMÉTIQUE : chaque module comptait pour un
    huitième, quelle que soit la filière. Un écart de 100 % sur la réserve
    d'exploitation (2 % du budget d'un cycle maïs) pesait donc autant qu'un écart
    de 100 % sur les semences (35 % du budget). Le critère de fiabilité technique
    — 25 % du score global — se laissait ainsi dominer par les postes marginaux,
    et un dossier pouvait être refusé sur des lignes qui ne portent pas le projet.

    `ValueChain.module_weights` porte la signature de coûts de chaque filière,
    versionnée en maker-checker. Elle existait déjà et n'était lue par personne
    dans le moteur : c'est elle qui donne son sens à « pondération par chaîne de
    valeur ».

    REPLI EXPLICITE, JAMAIS DE POIDS INVENTÉ
    ----------------------------------------
    On ne pondère que si CHAQUE module comparé porte un poids déclaré et que leur
    somme est strictement positive. Un module sans poids ne reçoit pas un poids
    moyen bricolé : la répartition serait fausse d'un facteur inconnu, sur le
    critère qui pèse le plus dans un refus technique. Dans ce cas on retombe sur
    la moyenne simple — le comportement d'avant, donc sans régression — et le
    critère le DIT dans ses détails.

    Un poids à zéro est légitime (« ce poste ne compte pas pour cette filière ») ;
    un poids ABSENT ne l'est pas. Les deux se distinguent par la présence de la
    clé, pas par sa valeur.
    """
    modules = list(couts.keys())
    if not modules:
        return {}, "moyenne_simple", ""

    declares = poids_modules or {}
    manquants = [m for m in modules if m not in declares]
    if manquants:
        return {}, "moyenne_simple", (
            f"Moyenne des écarts NON pondérée : la filière ne déclare pas de poids "
            f"pour {', '.join(sorted(manquants))}. Chaque module compte donc pour "
            f"1/{len(modules)}, y compris les postes marginaux. Complétez "
            f"« module_weights » de la chaîne de valeur pour que l'écart technique "
            f"suive le poids réel de chaque poste.")

    poids: dict[str, Decimal] = {}
    for module in modules:
        try:
            valeur = Decimal(str(declares[module]))
        except (InvalidOperation, TypeError, ValueError):
            return {}, "moyenne_simple", (
                f"Moyenne des écarts NON pondérée : le poids déclaré pour "
                f"« {module} » ({declares[module]!r}) n'est pas un nombre.")
        if valeur < 0:
            return {}, "moyenne_simple", (
                f"Moyenne des écarts NON pondérée : le poids déclaré pour "
                f"« {module} » est négatif ({valeur}).")
        poids[module] = valeur

    total = sum(poids.values(), ZERO)
    if total <= 0:
        return {}, "moyenne_simple", (
            "Moyenne des écarts NON pondérée : tous les poids de modules déclarés "
            "par la filière valent zéro.")

    return poids, "value_chain", ""


def scorer_technique(totaux_modules: dict, referentiel: ReferentielFiliere,
                     quantite: Decimal, bareme: BaremeScore,
                     poids: Decimal, unite: str | None = None,
                     poids_modules: dict | None = None) -> dict:
    """Compare chaque module du plan au référentiel filière, à l'unité de référence.

    Le score dépend de l'écart absolu MOYEN ; les écarts hors tolérance
    alimentent le canal de justification. Deux précisions par rapport à la SPEC :

      - un module absent du plan compte comme un écart de −100 %, pas comme une
        donnée manquante : ne rien prévoir pour la main-d'œuvre sur 5 ha est une
        information, pas un trou (CONVENTIONS.md §4.4, « l'absence est une donnée ») ;
      - une quantité de référence nulle ou absente rend la comparaison impossible ;
        on le dit au lieu de comparer un plan à un référentiel de 0.

    `quantite` s'exprime dans l'unité du référentiel (`unite`) : 25 ha de maïs,
    30 ruches, 1 000 sujets, 100 m² de bioconversion, 2 000 sacs, 300 t usinées.
    La cohérence des deux unités est établie en amont par
    `resoudre_quantite_reference`, qui refuse plutôt que de convertir.
    """
    couts = referentiel.couts_modules or {}
    unite = (unite or getattr(referentiel, "unite_reference", "") or "ha").strip().lower()
    quantite = Decimal(str(quantite or 0))

    if quantite <= 0:
        return {
            "score": Decimal("0.0"), "poids": poids, "points": Decimal("0.0"),
            "details": {
                "commentaire": (f"Dimension de référence absente du dossier : la "
                                f"comparaison au référentiel, qui donne ses coûts par "
                                f"« {unite} », n'est pas calculable."),
                "referentiel": referentiel.code,
                "quantiteReference": None,
                "uniteReference": unite,
                # Conservé pour le contrat front existant (`superficieHa`), et nul
                # dès que la filière ne se mesure pas en hectares.
                "superficieHa": None,
                "ecartsHorsPlage": [],
            },
            "hors_plage": [],
        }

    poids_par_module, origine_ponderation, note_ponderation = ponderation_modules(
        couts, poids_modules)

    ecarts: list[Decimal] = []
    ecarts_ponderes = ZERO
    hors_plage: list[dict] = []
    total_plan = ZERO
    total_ref = ZERO
    par_module: list[dict] = []

    for module, cfg in couts.items():
        ref = q2(Decimal(str(cfg.get("ref", 0))) * quantite)
        val = q2(totaux_modules.get(module, ZERO))
        total_plan += val
        total_ref += ref

        ecart_rel = q3((val - ref) / ref) if ref else ZERO
        ecarts.append(abs(ecart_rel))
        poids_module = poids_par_module.get(module, ZERO)
        ecarts_ponderes += abs(ecart_rel) * poids_module

        tol_inf = Decimal(str(cfg.get("tol_inf", "0.30")))
        tol_sup = Decimal(str(cfg.get("tol_sup", "0.40")))
        depasse = ecart_rel < -tol_inf or ecart_rel > tol_sup

        ecart_pct = q1(ecart_rel * Decimal(100))
        ligne = {
            "indicateur": f"cout_module:{module}",
            "module": module,
            "valeur": float(val),
            "reference": float(ref),
            "ecartPct": float(ecart_pct),
            "message": f"{module} : {ecart_pct:+} % vs référentiel",
        }
        # Le poids accompagne la ligne : c'est ce qui permet à l'analyste de lire
        # « −80 % sur un poste qui pèse 2 % » sans recalculer quoi que ce soit
        # (standard front — zéro chiffre métier calculé côté client).
        par_module.append({**ligne, "horsPlage": depasse,
                           "poids": float(poids_module) if poids_par_module else None})
        if depasse:
            hors_plage.append(ligne)

    # Moyenne SIMPLE conservée et servie à côté de la pondérée : c'est la seule
    # façon pour le comité de mesurer l'effet du recalibrage des poids sur ses
    # propres dossiers, et pour un auditeur de reconstituer un score d'avant.
    ecart_moyen_simple = (q3(sum(ecarts, ZERO) / Decimal(len(ecarts)))
                          if ecarts else Decimal(1))
    if poids_par_module:
        total_poids = sum(poids_par_module.values(), ZERO)
        ecart_moyen = q3(ecarts_ponderes / total_poids)
    else:
        ecart_moyen = ecart_moyen_simple

    score = bareme.evaluer(ecart_moyen)

    # Dépenses du plan sur des modules que le référentiel ne couvre pas : elles
    # n'entrent dans aucun écart, donc dans aucun score. Le taire reviendrait à
    # scorer un plan amputé sans que personne ne le sache.
    non_references = [
        {"module": m, "montant": float(q2(totaux_modules.get(m, ZERO)))}
        for m in MODULE_CODES
        if m not in couts and q2(totaux_modules.get(m, ZERO)) > 0
    ]

    commentaire = ""
    if total_ref and total_plan < total_ref / Decimal(2):
        commentaire = (
            f"Total de la feuille de besoins ({total_plan}) nettement inférieur au "
            f"référentiel ({total_ref}) : le plan ne couvre pas le cycle décrit.")
    if referentiel.est_indicatif:
        commentaire = (commentaire + " " if commentaire else "") + (
            f"Comparaison faite contre un référentiel indicatif "
            f"(N = {referentiel.n_cas_reels} dossiers réels) — fiabilité limitée.")
    if note_ponderation:
        # Le repli sur la moyenne simple change la lecture du critère : il doit
        # apparaître là où l'analyste lit le critère, pas seulement dans un
        # sous-bloc technique que l'écran n'ouvre pas.
        commentaire = (commentaire + " " if commentaire else "") + note_ponderation

    return {
        "score": score, "poids": poids, "points": _points(score, poids),
        "details": {
            "totalPlan": float(total_plan),
            "totalReferentiel": float(total_ref),
            "ecartMoyenPct": float(q1(ecart_moyen * Decimal(100))),
            # Écart NON pondéré, servi à côté : il n'entre dans aucun score, il
            # rend l'effet de la pondération lisible et un score d'avant
            # reconstituable (principe 3 — une analyse passée reste explicable).
            "ecartMoyenSimplePct": float(q1(ecart_moyen_simple * Decimal(100))),
            "ponderation": {
                "origine": origine_ponderation,
                "poidsModules": {m: float(p) for m, p in poids_par_module.items()},
                "commentaire": note_ponderation,
            },
            "referentiel": referentiel.code,
            "referentielSource": referentiel.source,
            # Dimension du dossier, dans l'unité du référentiel. `superficieHa`
            # reste servi pour le contrat front existant, mais UNIQUEMENT quand la
            # filière se mesure en hectares : le remplir avec 30 ruches ferait
            # afficher « 30 ha » à l'écran d'un analyste.
            "quantiteReference": float(quantite),
            "uniteReference": unite,
            "superficieHa": float(quantite) if unite == "ha" else None,
            "parModule": par_module,
            "modulesNonReferences": non_references,
            "ecartsHorsPlage": hors_plage,
            # Plages de vraisemblance TECHNIQUES de la filière (feuille 18 du
            # simulateur) : rendement, densité de semis, prix de vente, taux de
            # perte. Elles ne notent rien ici — elles arment le niveau N3 de
            # CONVENTIONS.md §4.1, que la seule comparaison des coûts ne couvre pas :
            # un plan de coûts parfaitement conforme peut annoncer un rendement
            # à 150 % du maximum régional, et rien ne le voyait.
            #
            # Staff uniquement : `serialiser_analyse_staff` sert `details` en
            # entier, `serialiser_analyse_resume` (client) n'en sert aucun champ
            # — l'anti-gaming du principe 7 tient sans traitement particulier.
            "controlesVraisemblance": list(getattr(referentiel, "controles_ref", None) or []),
            "commentaire": commentaire,
        },
        "hors_plage": hors_plage,
    }


# ── C2 — Capacité financière / DSCR (20 %) ───────────────────────────────────

def calculer_dscr(cash_flows: list[Decimal], echeancier: list[dict]) -> Decimal:
    """DSCR global = Σ cash-flows disponibles ÷ Σ service de la dette.

    Service nul (impossible avec `construire_echeancier`, mais un appelant peut
    passer une liste vide) → 0, jamais une division par zéro déguisée en DSCR
    infini.
    """
    service = totaux_echeancier(echeancier)["service_dette"] if echeancier else ZERO
    if service <= 0:
        return ZERO
    total_cf = sum((Decimal(str(cf)) for cf in cash_flows), ZERO)
    return q3(total_cf / service)


def dscr_mensuel_minimum(cash_flows: list[Decimal], echeancier: list[dict]) -> dict:
    """Le mois le plus tendu : un DSCR global sain peut cacher un mois à 0,2.

    C'est le diagnostic qui manque le plus souvent à l'analyste — le différé
    concentre le capital sur les derniers mois, et c'est là que le dossier casse
    (SPEC §9.3).
    """
    pire = None
    for i, ligne in enumerate(echeancier):
        echeance = Decimal(str(ligne["echeance"]))
        if echeance <= 0:
            continue
        cf = Decimal(str(cash_flows[i])) if i < len(cash_flows) else ZERO
        ratio = q3(cf / echeance)
        if pire is None or ratio < pire["dscr"]:
            pire = {"mois": ligne["mois"], "dscr": ratio,
                    "echeance": q2(echeance), "cashFlow": q2(cf)}
    return pire or {}


def diagnostiquer_levier(capital: Decimal, taux_annuel: Decimal, duree_mois: int,
                         differe_mois: int, mode_differe: str,
                         cash_flows: list[Decimal]) -> tuple[str, list[dict]]:
    """Levier chiffré : « un différé de 3 mois porterait le DSCR à X ».

    CONVENTIONS.md §4.6 — un DSCR de 0,64 ne se livre jamais seul : il se livre avec
    son facteur dominant et son levier. Ici le levier se calcule pour de vrai,
    en reconstruisant l'échéancier à différé réduit sur les MÊMES cash-flows.
    C'est un calcul, pas une approximation d'écran : le front ne doit rien
    recalculer (standard front, « zéro chiffre métier calculé côté client »).

    Le total des cash-flows est conservé (on ne réécrit pas la trésorerie du
    client) mais leur calendrier suit la nouvelle phase d'amortissement, sinon
    la comparaison serait faussée par un simple décalage de dates.
    """
    total_cf = sum((Decimal(str(cf)) for cf in cash_flows), ZERO)
    alternatives: list[dict] = []

    for candidat in sorted({0, max(differe_mois - 2, 0), max(differe_mois - 3, 0)}):
        if candidat == differe_mois or candidat >= duree_mois:
            continue
        try:
            lignes = construire_echeancier(
                capital, taux_annuel, duree_mois, candidat, mode_differe)
        except EcheancierError:
            continue
        n_amort = duree_mois - candidat
        mensuel = q2(total_cf / Decimal(n_amort)) if total_cf > 0 else ZERO
        flux = ([ZERO] * candidat + [mensuel] * n_amort)[:duree_mois]
        alternatives.append({
            "differeMois": candidat,
            "dscr": float(calculer_dscr(flux, lignes)),
            "serviceDette": float(totaux_echeancier(lignes)["service_dette"]),
        })

    if not alternatives:
        return "", []

    meilleur = max(alternatives, key=lambda a: a["dscr"])
    phrase = (f"Un différé de {meilleur['differeMois']} mois au lieu de "
              f"{differe_mois} porterait le DSCR à {meilleur['dscr']:.3f} "
              f"(service de la dette étalé sur {duree_mois - meilleur['differeMois']} "
              f"mois au lieu de {duree_mois - differe_mois}).")
    return phrase, alternatives


def scorer_dscr(dscr: Decimal, bareme: BaremeScore, poids: Decimal,
                diagnostic: dict | None = None) -> dict:
    score = bareme.evaluer(dscr)
    qualif = ("solide" if dscr >= Decimal("1.3")
              else "acceptable" if dscr >= Decimal("1.0")
              else "insuffisante")
    details = {
        "dscr": float(dscr),
        "commentaire": f"DSCR = {q2(dscr)} ({qualif})",
    }
    if diagnostic:
        # `facteurDominant` et `levier` sont remontés à la racine des détails :
        # ce sont les deux phrases que l'analyste lit en premier, pas des
        # sous-champs d'un bloc technique (contrat front `moteur-front-analyse`).
        details["facteurDominant"] = diagnostic.pop("facteurDominant", "")
        details["levier"] = diagnostic.pop("levier", "")
        details["diagnostic"] = diagnostic
    return {"score": score, "poids": poids, "points": _points(score, poids),
            "details": details}


# ── C3 — Résilience au stress (10 %) ─────────────────────────────────────────

def scorer_stress(cash_flows: list[Decimal], echeancier: list[dict],
                  bareme: BaremeScore, poids: Decimal, choc: Decimal) -> dict:
    """DSCR recalculé sous choc de revenus (−25 % par défaut, paramétrable)."""
    facteur = Decimal(1) - choc
    cf_stress = [q2(Decimal(str(cf)) * facteur) for cf in cash_flows]
    dscr_s = calculer_dscr(cf_stress, echeancier)
    score = bareme.evaluer(dscr_s)
    return {
        "score": score, "poids": poids, "points": _points(score, poids),
        "details": {
            "dscrStress": float(dscr_s),
            "chocPct": float(q1(choc * Decimal(100))),
            "commentaire": f"Stress test −{q1(choc * Decimal(100))} % sur les revenus "
                           f"→ DSCR = {q2(dscr_s)}",
        },
        "dscr_stress": dscr_s,
    }


# ── C4 — Historique comportemental (30 %) ────────────────────────────────────

def _charger_historique(client) -> dict | None:
    """Indicateurs comportementaux du client, ou `None` si aucun historique.

    Source : `portfolio.Loan` — les crédits déjà gérés par l'institution. La SPEC
    prévoyait « Wallet / Transactions » ; le module `transactions` n'expose pas
    encore d'historique de flux exploitable, ce qui est signalé plutôt que simulé.
    """
    try:
        from portfolio.models import Loan
    except Exception:  # noqa: BLE001
        return None

    sub = str(getattr(client, "pk", "") or "")
    prets = list(Loan.objects.filter(borrower_sub=sub)) if sub else []
    if not prets:
        return None

    total = len(prets)
    defauts = sum(1 for p in prets if p.status == Loan.Status.DEFAUT)
    clotures = sum(1 for p in prets if p.status == Loan.Status.CLOTURE)
    suspendus = sum(1 for p in prets if p.status in (Loan.Status.SUSPENDU, Loan.Status.BLOQUE))

    engage = sum((p.amount_approved or p.amount_requested or ZERO) for p in prets)
    rembourse = sum((p.repaid or ZERO) for p in prets)
    taux_remb = q3(rembourse / engage) if engage else ZERO

    return {
        "nbCredits": total,
        "nbClotures": clotures,
        "nbDefauts": defauts,
        "nbSuspendus": suspendus,
        "tauxRemboursement": float(taux_remb),
        "_taux_remb": taux_remb,
        "_incidents": defauts + suspendus,
        "_clotures": clotures,
        "_total": total,
    }


def scorer_comportemental(client, bareme: BaremeScore | None, poids: Decimal) -> dict:
    """Score 50/100 NEUTRE et explicitement mentionné si aucun historique.

    Le silence sur ce point serait le pire des défauts : 30 % du score global
    reposerait sur une valeur inventée sans que l'analyste le sache. La mention
    est donc obligatoire, et `historiqueDisponible` est un champ à part entière
    du contrat — pas une phrase à interpréter.
    """
    historique = _charger_historique(client)

    if historique is None:
        score = Decimal("50.0")
        return {
            "score": score, "poids": poids, "points": _points(score, poids),
            "details": {
                "historiqueDisponible": False,
                "commentaire": ("Historique comportemental non disponible : aucun "
                                "crédit antérieur au nom de ce client. Score neutre "
                                "de 50/100 appliqué — ce critère pèse "
                                f"{poids} % du score global sans être documenté."),
            },
        }

    # Part de crédits soldés + régularité du remboursement, moins les incidents.
    part_cloture = (Decimal(historique["_clotures"]) / Decimal(historique["_total"])
                    if historique["_total"] else ZERO)
    brut = (min(historique["_taux_remb"], Decimal(1)) * Decimal("60")
            + part_cloture * Decimal("40"))
    brut -= Decimal(historique["_incidents"]) * Decimal(20)
    score = q1(max(ZERO, min(Decimal(100), brut)))

    details = {k: v for k, v in historique.items() if not k.startswith("_")}
    details["historiqueDisponible"] = True
    details["commentaire"] = (
        f"{historique['nbCredits']} crédit(s) antérieur(s), "
        f"{historique['nbClotures']} soldé(s), {historique['nbDefauts']} en défaut.")
    return {"score": score, "poids": poids, "points": _points(score, poids),
            "details": details}


# ── C5 — Garanties & domiciliation (15 %) ────────────────────────────────────

def scorer_garanties(application, bareme: BaremeScore, poids: Decimal) -> dict:
    """Ratio de couverture = Σ valeurs RETENUES ÷ montant financé.

    La SPEC embarquait une table de décotes en dur dans le code (`DECOTES = {...}`),
    ce que le principe 8 interdit et que le principe 9 rend inutile :
    `CreditGuarantee.retained_coverage` applique déjà la décote de la garantie —
    valeur retenue après vérification pour un actif, montant bloqué pour l'épargne,
    pondération `decote_caution_morale` d'`InstitutionConfig` pour une caution.
    Deux tables de décotes auraient donné deux ratios de couverture différents
    pour un même dossier selon l'écran consulté.
    """
    from credits.models import CreditGuarantee

    montant = Decimal(str(application.amount_approved or application.amount_requested or 0))
    garanties = list(
        application.guarantees.exclude(
            status__in=(CreditGuarantee.Status.DECLINED,
                        CreditGuarantee.Status.RELEASED,
                        CreditGuarantee.Status.EXPIRED)
        )
    )

    couverture = ZERO
    detail_lignes = []
    constituees = bool(garanties)
    for g in garanties:
        retenue = q2(g.retained_coverage or 0)
        couverture += retenue
        active = g.status == CreditGuarantee.Status.ACTIVE
        if not active:
            constituees = False
        detail_lignes.append({
            "type": g.guarantee_type, "statut": g.status,
            "valeurRetenue": float(retenue), "constituee": active,
        })

    ratio = q3(couverture / montant) if montant else ZERO
    score = bareme.evaluer(ratio)

    commentaire = ""
    plafond = Decimal(str((bareme.parametres or {}).get("plafond_non_constituees", "60")))
    if not garanties:
        # Aucune garantie déclarée : la SPEC prévoit 60/100 « indicatif ». Ce n'est
        # pas la même chose qu'une garantie déclarée mais pas encore constituée —
        # ici il n'y a rien à constituer.
        score = min(score, plafond)
        commentaire = ("Aucune garantie déclarée — score indicatif plafonné en "
                       "attente de constitution.")
    elif not constituees:
        score = min(score, plafond)
        commentaire = "Garanties déclarées mais non encore constituées — score indicatif."

    return {
        "score": q1(score), "poids": poids, "points": _points(q1(score), poids),
        "details": {
            "ratioCouverture": float(ratio),
            "couvertureRetenue": float(q2(couverture)),
            "montantFinance": float(q2(montant)),
            "constituees": constituees,
            "garanties": detail_lignes,
            "commentaire": commentaire,
        },
    }


# ── Cash-flows prévisionnels ─────────────────────────────────────────────────

def projeter_cash_flows(referentiel: ReferentielFiliere, quantite: Decimal,
                        total_plan: Decimal, duree_mois: int,
                        differe_mois: int, unite: str | None = None,
                        ) -> tuple[list[Decimal], dict]:
    """Cash-flows mensuels disponibles pour le service de la dette.

    HYPOTHÈSE EXPLICITE — la SPEC lit ces flux dans une feuille « Tresorerie »
    du template qui n'existe pas : le classeur ingéré ne porte que les feuilles 4
    et 5. Faute de trésorerie prévisionnelle déclarée, on la PROJETTE :

        revenu brut = rendement_ref.qte_unite × prix_unitaire × quantité
        marge nette du cycle = revenu brut − coûts du plan
        disponible mensuel = marge ÷ nombre de mois d'amortissement

    `qte_unite` est la production attendue PAR UNITÉ DE RÉFÉRENCE (par hectare,
    par ruche, par sujet…) et `quantite` la dimension du dossier dans cette même
    unité — le produit est homogène quelle que soit la filière.

    La marge est portée sur les mois d'AMORTISSEMENT et non sur toute la durée :
    le produit de la vente arrive après la récolte, c'est précisément la raison
    d'être du différé. Cette hypothèse est restituée dans les détails du critère
    DSCR, avec ses trois termes, pour que l'analyste puisse la contester.

    Quand le référentiel n'a pas de rendement, on renvoie des flux nuls et on le
    dit : un DSCR de 0 dû à un référentiel incomplet doit être lisible comme tel.
    """
    rendement = referentiel.rendement_ref or {}
    qte = Decimal(str(rendement.get("qte_unite", 0) or 0))
    prix = Decimal(str(rendement.get("prix_unitaire", 0) or 0))
    quantite = Decimal(str(quantite or 0))
    unite = (unite or getattr(referentiel, "unite_reference", "") or "ha").strip().lower()

    revenu_brut = q2(qte * prix * quantite)
    marge = q2(revenu_brut - Decimal(str(total_plan or 0)))
    n_amort = max(duree_mois - differe_mois, 1)

    hypothese = {
        "origine": "projection_referentiel",
        "referentiel": referentiel.code,
        "referentielSource": referentiel.source,
        "rendementUnitaire": float(qte),
        "prixUnitaire": float(prix),
        "uniteRendement": rendement.get("unite", ""),
        "quantiteReference": float(quantite),
        "uniteReference": unite,
        "superficieHa": float(quantite) if unite == "ha" else None,
        "revenuBrut": float(revenu_brut),
        "chargesPlan": float(q2(total_plan or 0)),
        "margeNetteCycle": float(marge),
        "moisAmortissement": n_amort,
        # CALENDRIER de trésorerie de la filière, désormais chargé depuis la
        # feuille « 16_Tresorerie_Mensuelle » du simulateur (`tresorerie_ref`).
        #
        # Il est SERVI mais pas encore CONSOMMÉ : les cash-flows restent étalés à
        # plat sur les mois d'amortissement. Le brancher changerait le DSCR de
        # chaque dossier — donc les recommandations — et c'est un arbitrage qui
        # revient au comité, pas à un lot de lecture de fichier. En attendant,
        # l'analyste voit côte à côte l'hypothèse du moteur et le calendrier réel
        # de sa filière, et peut mesurer l'écart lui-même : c'est ce que
        # l'ancien message (« la feuille de trésorerie n'existe pas ») lui
        # interdisait de faire.
        "calendrierReference": dict(getattr(referentiel, "tresorerie_ref", None) or {}),
        "commentaire": (
            "Cash-flows ÉTALÉS À PLAT sur les mois d'amortissement : la marge du "
            "cycle est répartie uniformément, faute d'être branchée sur le "
            "calendrier de la filière. Le calendrier réel est fourni à côté "
            "(`calendrierReference`) — les décaissements et la recette n'y "
            "tombent pas aux mêmes mois. Hypothèse à valider avec le client."),
    }

    if qte <= 0 or prix <= 0:
        hypothese["commentaire"] = (
            f"Le référentiel « {referentiel.code} » ne porte pas de rendement de "
            "référence : les cash-flows ne sont pas projetables et le DSCR est "
            "calculé sur des flux nuls. Complétez le référentiel avant de "
            "conclure sur ce critère.")
        return [ZERO] * duree_mois, hypothese

    mensuel = q2(marge / Decimal(n_amort)) if marge > 0 else ZERO
    flux = [ZERO] * differe_mois + [mensuel] * n_amort
    return flux[:duree_mois], hypothese


# ── Barème de décision — 4 niveaux ───────────────────────────────────────────

def recommander(score_global: Decimal, dscr: Decimal, hors_plage: list,
                regles: dict | None = None) -> str:
    """Recommandation du moteur — **jamais une transition** (principe 2).

    Règle de sûreté prioritaire sur le score : un DSCR sous
    `DSCR_PLANCHER_APPROBATION` ne peut donner NI `approbation` NI
    `approbation_cond`, quel que soit le score global. Un dossier qui ne dégage
    pas de quoi payer ses échéances ne s'approuve pas parce qu'il a un bon
    historique — les 30 % du critère comportemental peuvent à eux seuls porter un
    dossier à 60, et c'est exactement le trou que ce garde-fou ferme.
    """
    regles = regles or REGLES_DECISION_DEFAUT
    score = Decimal(str(score_global))
    dscr = Decimal(str(dscr or 0))

    approbation = regles.get("approbation", REGLES_DECISION_DEFAUT["approbation"])
    cond = regles.get("approbation_cond", REGLES_DECISION_DEFAUT["approbation_cond"])
    revue = regles.get("revue", REGLES_DECISION_DEFAUT["revue"])

    if dscr >= DSCR_PLANCHER_APPROBATION:
        if (score >= Decimal(str(approbation.get("score_min", 75)))
                and dscr >= Decimal(str(approbation.get("dscr_min", "1.2")))
                and not (approbation.get("sans_hors_plage", True) and hors_plage)):
            return AnalyseCredit.Recommandation.APPROBATION
        if (score >= Decimal(str(cond.get("score_min", 60)))
                and dscr >= Decimal(str(cond.get("dscr_min", "1.0")))):
            return AnalyseCredit.Recommandation.APPROBATION_COND

    if score >= Decimal(str(revue.get("score_min", 45))):
        return AnalyseCredit.Recommandation.REVUE
    return AnalyseCredit.Recommandation.REFUS


#: Grille lettre de secours — bornes de la SPEC §6.
LETTRES_DEFAUT = [{"lettre": "A", "min": "85"}, {"lettre": "B", "min": "70"},
                  {"lettre": "C", "min": "50"}, {"lettre": "D", "min": "0"}]


def score_lettre(score_global, regles: dict | None = None) -> str:
    """A/B/C/D — la seule note que le client voit (SPEC §6).

    La grille vit en base (`BaremeScore.DECISION.parametres.lettres`, principe 8)
    et **le serveur seul l'applique**. Trois fichiers du front la recopiaient à la
    main : c'était un barème dans le navigateur, que le comité ne pouvait pas
    recalibrer sans un déploiement, et dont la version client (`scoreLetterOf`)
    apprenait au demandeur qu'à 70,1 il passe de C à B — le fragment de barème
    exact que le principe 7 doit retenir. La lettre est donc servie, la grille
    jamais.

    Bornes STRICTES (`>`) : ce sont celles de la SPEC §6, conservées telles quelles
    pour ne pas déplacer silencieusement la frontière d'un dossier à 85,0.
    """
    grille = (regles or {}).get("lettres") or LETTRES_DEFAUT
    score = Decimal(str(score_global))
    for palier in sorted(grille, key=lambda p: Decimal(str(p["min"])), reverse=True):
        borne = Decimal(str(palier["min"]))
        if score > borne or borne == 0:
            return palier["lettre"]
    return "D"


# ── Orchestration ────────────────────────────────────────────────────────────

def resoudre_referentiel(application) -> ReferentielFiliere:
    """Référentiel actif de la filière du dossier.

    Trois clés d'accès, de la plus précise à la plus lâche : code `ValueChain`,
    puis libellé de filière. La SPEC filtre sur `demande.culture`, champ qui
    n'existe pas sur `CreditApplication`.
    """
    chain = application.value_chain
    qs = ReferentielFiliere.objects.filter(actif=True)

    if chain is not None:
        ref = qs.filter(value_chain_code=chain.code).first()
        if ref:
            return ref
        ref = qs.filter(filiere__iexact=chain.label).first()
        if ref:
            return ref

    raise ReferentielAbsent(
        "Aucun référentiel technico-économique actif pour la filière "
        f"« {chain.label if chain else '(non renseignée)'} » de ce dossier. "
        "L'analyse est refusée plutôt que faite contre un référentiel de repli : "
        "un score technique nul imputable à une configuration manquante serait "
        "un refus fabriqué par l'outil."
    )


@transaction.atomic
def executer_analyse(application, *, duree_mois: int, differe_mois: int = 0,
                     taux_annuel=None, mode_differe: str = MODE_INTERETS_SEULS,
                     execute_par: str = "",
                     cash_flows: list[Decimal] | None = None) -> AnalyseCredit:
    """Exécute le moteur et persiste une NOUVELLE `AnalyseCredit`.

    Atomique avec sa journalisation d'audit : une analyse dont l'entrée de journal
    manque ne prouve rien, et un journal qui référence une analyse absente est
    pire encore.

    `cash_flows` permet d'injecter une trésorerie prévisionnelle connue (test de
    référence, ou future feuille de trésorerie du template) ; à défaut, elle est
    projetée depuis le référentiel — cf. `projeter_cash_flows`.
    """
    source = application.needs_source
    if source is None:
        raise SourceBesoinsAbsente(
            "Aucune feuille de besoins ingérée pour ce dossier : il n'y a rien à "
            "scorer. Ce qui est scoré est ce qui est en base (principe 1) — "
            "l'analyse ne lit ni fichier ni formulaire.")

    referentiel = resoudre_referentiel(application)
    baremes = charger_baremes()
    poids = poids_effectifs()
    regles = regles_decision(baremes.get("DECISION"))
    tarifs = regles_taux(baremes.get("TAUX"))
    # Lève `DimensionIncoherente` AVANT tout calcul : mieux vaut refuser tôt que
    # produire un échéancier pour un dossier qu'on ne saura pas scorer.
    quantite, unite_reference = resoudre_quantite_reference(application, referentiel)

    capital = Decimal(str(application.amount_approved or application.amount_requested or 0))
    if capital <= 0:
        raise ParametresInvalides(
            "Le dossier ne porte aucun montant : ni montant approuvé ni montant "
            "demandé. L'échéancier n'est pas constructible.")

    if taux_annuel is None:
        chain = application.value_chain
        taux_annuel = Decimal(str(chain.base_rate)) if chain and chain.base_rate else Decimal("18")
    taux_annuel = Decimal(str(taux_annuel))

    if mode_differe not in MODES:
        raise ParametresInvalides(
            f"Mode de différé « {mode_differe} » inconnu (attendu : {', '.join(MODES)}).")

    try:
        lignes = construire_echeancier(
            capital, taux_annuel, int(duree_mois), int(differe_mois), mode_differe)
    except EcheancierError as exc:
        raise ParametresInvalides(
            exc.message, errors=[{"code": exc.code, "message": exc.message}]) from exc

    # ── Données scorées : DataRecord de la révision courante (principe 1) ─────
    totaux = extract_module_totals(source)
    total_plan = q2(sum(totaux.values(), ZERO))

    # Pondération des écarts par la SIGNATURE DE COÛTS de la filière
    # (`ValueChain.module_weights`, versionnée en maker-checker). Elle est lue ici
    # et non dans `scorer_technique` pour que la fonction de score reste pure et
    # testable sans base — et pour que le simulateur indicatif puisse lui passer
    # exactement la même pondération (principe 6 : un seul calcul, deux appelants).
    c1 = scorer_technique(totaux, referentiel, quantite,
                          baremes["ECART_TECHNIQUE"], poids["technique"],
                          unite_reference,
                          poids_modules=getattr(application.value_chain,
                                                "module_weights", None))

    if cash_flows is None:
        cash_flows, hypothese_cf = projeter_cash_flows(
            referentiel, quantite, total_plan, int(duree_mois), int(differe_mois),
            unite_reference)
    else:
        cash_flows = [Decimal(str(cf)) for cf in cash_flows]
        hypothese_cf = {"origine": "fourni",
                        "commentaire": "Cash-flows fournis à l'appel du moteur."}

    dscr = calculer_dscr(cash_flows, lignes)
    diagnostic = {
        "hypotheseCashFlows": hypothese_cf,
        "moisLePlusTendu": _jsonifier(dscr_mensuel_minimum(cash_flows, lignes)),
        "serviceDette": float(totaux_echeancier(lignes)["service_dette"]),
        "cashFlowTotal": float(q2(sum(cash_flows, ZERO))),
    }
    if differe_mois:
        diagnostic["facteurDominant"] = (
            f"Différé de {differe_mois} mois sur {duree_mois} : le capital "
            f"s'amortit sur {duree_mois - differe_mois} mois, ce qui concentre le "
            f"service de la dette et dégrade mécaniquement le DSCR.")
        levier, alternatives = diagnostiquer_levier(
            capital, taux_annuel, int(duree_mois), int(differe_mois),
            mode_differe, cash_flows)
        diagnostic["levier"] = levier
        diagnostic["alternativesDiffere"] = alternatives
    elif dscr < Decimal("1.0"):
        diagnostic["facteurDominant"] = (
            "Sans différé, l'écart vient du niveau des revenus attendus au regard "
            "du montant demandé, pas du calendrier de remboursement.")
        diagnostic["levier"] = ""

    c2 = scorer_dscr(dscr, baremes["DSCR"], poids["dscr"], diagnostic)
    c3 = scorer_stress(cash_flows, lignes, baremes["DSCR"], poids["stress"],
                       choc_stress(regles))
    c4 = scorer_comportemental(application.client, baremes.get("COMPORTEMENTAL"),
                               poids["comportemental"])
    c5 = scorer_garanties(application, baremes["COUVERTURE_GARANTIES"], poids["garanties"])

    criteres = {"technique": c1, "dscr": c2, "stress": c3,
                "comportemental": c4, "garanties": c5}
    score_global = q1(sum((c["points"] for c in criteres.values()), ZERO))
    hors_plage = c1.get("hors_plage", [])
    reco = recommander(score_global, dscr, hors_plage, regles)

    # ── Tarification — grille UNIQUE, figée avec l'analyse ────────────────────
    # Le taux de base est celui de la filière, pas `taux_annuel` : ce dernier est
    # le taux auquel l'échéancier a été construit (paramètre d'instruction), le
    # premier est l'assiette de la grille. Les confondre ferait dériver le taux à
    # chaque ré-analyse (18 → 20 → 22…), le taux proposé devenant sa propre base.
    chain = application.value_chain
    taux_base = (Decimal(str(chain.base_rate))
                 if chain is not None and chain.base_rate else taux_annuel)
    tarification = proposer_taux(score_global, taux_base, tarifs)

    analyse = AnalyseCredit.objects.create(
        application=application,
        needs_source=source,
        needs_source_revision=source.revision,
        needs_source_sha256=source.sha256 or "",
        referentiel=referentiel,
        duree_mois=int(duree_mois), differe_mois=int(differe_mois),
        mode_differe=mode_differe, taux_annuel=taux_annuel,
        capital=q2(capital), devise=application.currency or "USD",
        criteres=_jsonifier(criteres),
        dscr=dscr, dscr_stress=c3["dscr_stress"],
        score_global=score_global, recommandation=reco,
        taux_propose=tarification["_taux"],
        indicateurs_hors_plage=_jsonifier(hors_plage),
        echeancier=serialiser_echeancier(lignes),
        poids_appliques={k: str(v) for k, v in poids.items()},
        # `_regles` fige la grille de décision ET la grille de lettres appliquées :
        # un recalibrage du comité ne doit pas rendre une analyse passée
        # inexplicable, ni changer rétroactivement la lettre affichée au client.
        baremes_appliques={
            **{c: {"id": b.pk, "version": b.version} for c, b in baremes.items()},
            "_regles": regles,
            # Grille de tarification appliquée, figée elle aussi : sans elle, on
            # ne saurait pas dire deux ans plus tard POURQUOI ce dossier a été
            # tarifé à 20 % — la grille aura pu changer entre-temps.
            "_tarification": {k: v for k, v in tarification.items()
                              if not k.startswith("_")},
        },
        execute_par=execute_par or "",
        version_moteur=VERSION_MOTEUR,
    )

    from audit.services import record as audit_record
    audit_record(
        actor=execute_par or "", action="credits.analyse.execute",
        entity_type="AnalyseCredit", entity_id=str(analyse.pk),
        details={
            "applicationCode": application.code,
            "scoreGlobal": str(score_global),
            "recommandation": reco,
            "tauxPropose": str(tarification["_taux"]),
            "dscr": str(dscr),
            "dscrStress": str(c3["dscr_stress"]),
            "needsSourceId": source.pk,
            "needsSourceRevision": source.revision,
            "needsSourceSha256": source.sha256 or "",
            "referentiel": referentiel.code,
            "versionMoteur": VERSION_MOTEUR,
        },
    )
    return analyse


@transaction.atomic
def justifier_indicateur(analyse: AnalyseCredit, *, indicateur: str,
                         justification: str, agent: str) -> AnalyseCredit:
    """Ajoute une justification d'analyste — append only (principe 3).

    L'indicateur doit figurer parmi les indicateurs hors plage de CETTE analyse :
    justifier un écart que le moteur n'a pas relevé n'aurait aucun destinataire,
    et laisserait croire qu'un point a été traité.
    """
    indicateur = (indicateur or "").strip()
    justification = (justification or "").strip()

    erreurs = []
    if not indicateur:
        erreurs.append({"code": "INDICATEUR_REQUIS",
                        "message": "L'indicateur à justifier est obligatoire."})
    if not justification:
        erreurs.append({"code": "JUSTIFICATION_REQUISE",
                        "message": "La justification ne peut pas être vide : c'est "
                                   "elle qui sera relue par le comité."})
    if erreurs:
        raise AnalyseError("Justification incomplète.", errors=erreurs)

    connus = {i.get("indicateur") for i in (analyse.indicateurs_hors_plage or [])}
    if indicateur not in connus:
        raise AnalyseError(
            f"L'indicateur « {indicateur} » ne fait pas partie des écarts relevés "
            f"par cette analyse ({', '.join(sorted(c for c in connus if c)) or 'aucun'}).",
            errors=[{"code": "INDICATEUR_INCONNU",
                     "message": f"Indicateur « {indicateur} » hors du périmètre de "
                                f"l'analyse #{analyse.pk}."}],
        )

    analyse.justifications = list(analyse.justifications or []) + [{
        "indicateur": indicateur,
        "justification": justification,
        "agent": agent or "",
        "date": timezone.now().isoformat(),
    }]
    analyse.save(update_fields=["justifications"])

    from audit.services import record as audit_record
    audit_record(
        actor=agent or "", action="credits.analyse.justifier",
        entity_type="AnalyseCredit", entity_id=str(analyse.pk),
        details={"applicationCode": analyse.application.code,
                 "indicateur": indicateur, "justification": justification},
    )
    return analyse


def _jsonifier(value):
    """`Decimal` → `float` pour le stockage JSON des blocs de détail.

    Le calcul est terminé quand cette fonction s'exécute : les scores et points
    sont déjà quantizés, aucune arithmétique ne se fait plus dessus. Les montants
    de l'échéancier, eux, restent des CHAÎNES (`serialiser_echeancier`) — ce sont
    les seuls chiffres que quelqu'un pourrait vouloir re-sommer.
    """
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dict):
        return {k: _jsonifier(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonifier(v) for v in value]
    return value


# ── Sérialisation API ────────────────────────────────────────────────────────
#
# Le contrat est FIGÉ dans `src/types/api.ts` (`CreditAnalyse`,
# `CreditAnalyseCritere`, `CreditEcheancierLigne`, `CreditAnalyseResume`) et deux
# agents front sont typés dessus. Clés en camelCase, montants en `number`.

#: Le contrat front type `phase` en `'différé' | 'amortissement' | 'franchise'`
#: alors que `echeancier.py` produit `'differe'` / `'amortissement'` (sans accent,
#: identifiants de stockage). La traduction se fait ICI, à la frontière HTTP :
#: la valeur stockée reste un identifiant stable, l'affichage reste le contrat.
#: Exercé pour les DEUX modes par
#: `test_phase_franchise_traduite_pour_le_contrat_front` : une entrée manquante
#: retomberait en silence sur l'identifiant de stockage, hors union du contrat.
_PHASES_API = {
    ("differe", MODE_INTERETS_SEULS): "différé",
    ("differe", "franchise_totale"): "franchise",
    ("amortissement", MODE_INTERETS_SEULS): "amortissement",
    ("amortissement", "franchise_totale"): "amortissement",
}


def serialiser_echeancier_api(lignes: list[dict], mode_differe: str) -> list[dict]:
    """Échéancier au format `CreditEcheancierLigne` : nombres, phases accentuées."""
    out = []
    for l in lignes:
        phase = _PHASES_API.get((l.get("phase"), mode_differe), l.get("phase"))
        out.append({
            "mois": l["mois"],
            "phase": phase,
            "capital": float(Decimal(str(l["capital"]))),
            "interets": float(Decimal(str(l["interets"]))),
            "interetsCapitalises": float(Decimal(str(l.get("interets_capitalises", 0)))),
            "echeance": float(Decimal(str(l["echeance"]))),
            "crd": float(Decimal(str(l["crd"]))),
        })
    return out


def _totaux_api(lignes: list[dict]) -> dict:
    """Totaux de l'échéancier — calculés en `Decimal`, servis en `number`.

    Pas de `totalCommissions` : la ligne `commission` de la SPEC §A.3 n'est pas
    implémentée (l'écart 25 vs 19,95 du simulateur Excel n'est pas tranché). La
    servir à 0 laisserait croire qu'il n'y a pas de commission, ce qui n'est pas
    établi — l'absence de clé est plus honnête que le zéro.
    """
    if not lignes:
        return {"totalInterets": 0.0, "totalCapital": 0.0, "serviceDette": 0.0,
                "totalInteretsCapitalises": 0.0, "nbEcheances": 0, "crdFinal": 0.0}
    totaux = totaux_echeancier([
        {k: Decimal(str(v)) if k != "mois" and k != "phase" else v for k, v in l.items()}
        for l in lignes
    ])
    return {
        "totalInterets": float(totaux["interets_payes"]),
        "totalInteretsCapitalises": float(totaux["interets_capitalises"]),
        "totalCapital": float(totaux["capital_rembourse"]),
        "serviceDette": float(totaux["service_dette"]),
        "crdFinal": float(totaux["crd_final"]),
        "nbEcheances": totaux["nb_echeances"],
    }


def _tarification_api(analyse: AnalyseCredit) -> dict | None:
    """Bloc de tarification d'une analyse, tel qu'il a été figé à l'exécution.

    `None` pour les analyses antérieures à la grille unique : on ne recalcule pas
    un taux a posteriori avec la grille d'aujourd'hui, ce serait réécrire ce qu'un
    analyste a lu (principe 3). L'écran affiche « non tarifée », l'analyste
    ré-analyse s'il veut un taux.
    """
    if analyse.taux_propose is None:
        return None
    fige = (analyse.baremes_appliques or {}).get("_tarification") or {}
    return {
        "tauxPropose": float(analyse.taux_propose),
        "tauxBase": fige.get("tauxBase"),
        "bandeScoreMin": fige.get("bandeScoreMin"),
        "ajustement": fige.get("ajustement"),
        "plancher": fige.get("plancher"),
        "plancherApplique": fige.get("plancherApplique"),
        "origineGrille": fige.get("origine"),
        "devise": analyse.devise,
    }


def serialiser_analyse_staff(analyse: AnalyseCredit) -> dict:
    """Vue ANALYSTE — expose barèmes, plages et tolérances. Jamais servie au client."""
    return {
        "id": analyse.pk,
        "reference": analyse.application.code,
        "referentiel": analyse.referentiel.code,
        "parametres": {
            "dureeMois": analyse.duree_mois,
            "differeMois": analyse.differe_mois,
            "tauxAnnuel": float(analyse.taux_annuel),
            "modeDiffere": analyse.mode_differe,
            "capital": float(analyse.capital),
            "devise": analyse.devise,
        },
        "scoreGlobal": float(analyse.score_global),
        # Lettre servie par le serveur : le front ne convertit plus un score en
        # lettre avec des bornes recopiées à la main (principe 8).
        "scoreLettre": score_lettre(analyse.score_global,
                                    (analyse.baremes_appliques or {}).get("_regles")),
        "recommandation": analyse.recommandation,
        # Tarification : le taux figé à l'analyse et la bande qui l'explique.
        # Staff UNIQUEMENT — la grille apprendrait au client qu'un point de score
        # de plus lui fait gagner 2 points de taux (principe 7).
        "tarification": _tarification_api(analyse),
        "dscr": float(analyse.dscr) if analyse.dscr is not None else None,
        "dscrStress": float(analyse.dscr_stress) if analyse.dscr_stress is not None else None,
        "criteres": {
            cle: {
                "score": float(bloc.get("score", 0)),
                "poids": float(bloc.get("poids", 0)),
                "points": float(bloc.get("points", 0)),
                "details": bloc.get("details", {}),
            }
            for cle, bloc in (analyse.criteres or {}).items() if cle in CRITERES
        },
        "indicateursHorsPlage": analyse.indicateurs_hors_plage or [],
        "justifications": analyse.justifications or [],
        "echeancier": serialiser_echeancier_api(analyse.echeancier or [],
                                                analyse.mode_differe),
        # Totaux servis par le serveur (SPEC §A.4) : le front ne somme pas une
        # colonne de montants en `float`, il affiche ce que le moteur a calculé
        # en `Decimal`.
        "totaux": _totaux_api(analyse.echeancier or []),
        # Devise de l'analyse. Le contrat `CreditAnalyse` ne la portait pas ; la
        # servir évite un repli sur la devise d'un autre objet, et un agrégat
        # multi-devises muet (CONVENTIONS.md §7.2, « KPI honnêtes »).
        "devise": analyse.devise,
        # Autorité du référentiel : une plage indicative ne vaut pas une plage
        # apprise sur 200 dossiers, et l'écran doit pouvoir le dire (§4.6).
        "referentielInfo": {
            "code": analyse.referentiel.code,
            "filiere": analyse.referentiel.filiere,
            "source": analyse.referentiel.source,
            "estIndicatif": analyse.referentiel.est_indicatif,
            "nCasReels": analyse.referentiel.n_cas_reels,
            "version": analyse.referentiel.version,
        },
        "executeLe": analyse.execute_le.isoformat() if analyse.execute_le else None,
        "versionMoteur": analyse.version_moteur,
        "lignage": {
            "needsSourceId": analyse.needs_source_id,
            "revision": analyse.needs_source_revision,
            "sha256": analyse.needs_source_sha256,
        },
        # Émis en `number`, comme `criteres.<x>.poids` — la même grandeur ne peut
        # pas sortir en chaîne à un endroit et en nombre à l'autre. Le STOCKAGE
        # reste une chaîne (`poids_appliques`) : c'est la trace d'audit, elle ne
        # passe pas par un binaire flottant.
        "poidsAppliques": {k: float(Decimal(str(v)))
                           for k, v in (analyse.poids_appliques or {}).items()},
    }


#: Message client par critère quand il est en dessous du niveau attendu.
#: Formulés en ACTION, jamais en seuil : « constituez une garantie » et non
#: « votre couverture est à 0,42 pour un barème qui donne 75 à 1,0 ».
_PISTES_CLIENT = {
    "technique": ("Détaillez votre feuille de besoins poste par poste : plusieurs "
                  "rubriques s'écartent de ce qui est observé sur des projets "
                  "comparables."),
    "dscr": ("Les revenus attendus couvrent difficilement les remboursements. "
             "Une durée plus longue ou un montant plus mesuré améliorerait cet "
             "équilibre."),
    "stress": ("Votre projet résiste mal à une baisse des revenus. Prévoir une "
               "réserve d'exploitation renforcerait sa solidité."),
    "comportemental": ("Un historique de remboursement au sein d'AGRICAP renforce "
                       "un dossier. Une épargne régulière y contribue aussi."),
    "garanties": ("Constituez vos garanties : une garantie confirmée pèse "
                  "davantage qu'une garantie annoncée."),
}

_FORTS_CLIENT = {
    "technique": "Votre plan de financement est cohérent avec des projets comparables.",
    "dscr": "Les revenus attendus couvrent confortablement vos remboursements.",
    "stress": "Votre projet resterait viable même si les revenus baissaient.",
    "comportemental": "Votre historique avec AGRICAP joue en votre faveur.",
    "garanties": "Vos garanties sont solides et bien constituées.",
}

#: Un critère STRICTEMENT au-dessus est un point fort, strictement en dessous une
#: piste d'amélioration. Ce n'est PAS un seuil de barème : c'est la moitié de
#: l'échelle 0–100, une information que le client déduit déjà de sa propre lettre.
#:
#: Comparaison stricte des deux côtés : un critère à exactement 50 n'est ni un
#: point fort ni une faiblesse, il est NEUTRE — et le seul critère qui vaut
#: exactement 50 est le comportemental sans historique. Avec un `>=`, l'absence
#: d'historique était annoncée au client comme « votre historique joue en votre
#: faveur » : le moteur félicitait un client pour une donnée qu'il n'a pas.
_MEDIANE = Decimal(50)


def serialiser_analyse_resume(analyse: AnalyseCredit) -> dict:
    """Vue CLIENT — anti-gaming (principe 7), volontairement pauvre.

    Ce que ce dictionnaire ne contient JAMAIS, et qu'un test verrouille :
    aucun score chiffré, aucun poids, aucun point, aucun DSCR, aucun barème,
    aucune tolérance, aucune plage du référentiel, aucun code de référentiel,
    aucune recommandation du moteur, aucun montant du plan comparé à une
    référence. Le client voit sa LETTRE et des pistes formulées en actions.

    La recommandation elle-même est retenue : elle est l'entrée d'une décision
    humaine (principe 2), et l'annoncer au client avant la décision créerait une
    attente que l'institution n'a pas prise.
    """
    criteres = analyse.criteres or {}
    forts, ameliorer = [], []
    for cle in CRITERES:
        bloc = criteres.get(cle)
        if not bloc:
            continue
        score = Decimal(str(bloc.get("score", 0)))
        details = bloc.get("details") or {}

        # Absence d'historique : jamais un point fort. C'est une piste — le
        # client peut agir dessus (épargner, rembourser un premier crédit),
        # alors qu'il ne peut rien faire d'une félicitation imméritée.
        if cle == "comportemental" and details.get("historiqueDisponible") is False:
            ameliorer.append(_PISTES_CLIENT[cle])
            continue

        if score > _MEDIANE and cle in _FORTS_CLIENT:
            forts.append(_FORTS_CLIENT[cle])
        elif score < _MEDIANE and cle in _PISTES_CLIENT:
            ameliorer.append(_PISTES_CLIENT[cle])

    return {
        "reference": analyse.application.code,
        "scoreLettre": score_lettre(analyse.score_global,
                                    (analyse.baremes_appliques or {}).get("_regles")),
        "pointsForts": forts,
        "pointsAAmeliorer": ameliorer,
        "analyseLe": analyse.execute_le.isoformat() if analyse.execute_le else None,
    }
