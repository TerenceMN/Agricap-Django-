"""
Projection du moteur unifié vers l'ancien format `score_result` — ADAPTATEUR.

Ce module ne score plus rien. Il ne contient plus un seul seuil, plus une seule
courbe, plus une seule grille de taux.

POURQUOI
--------
Deux systèmes de scoring coexistaient sur le même dossier :

  - ce module, avec CINQ critères (historique de remboursement, cohérence de la
    feuille de besoins, ratio d'endettement, ancienneté KYC, risque filière),
    calculés en `float`, dont le résultat écrasait `CreditApplication.score_result` ;
  - `credits.analyse`, avec CINQ AUTRES critères (technique, DSCR, stress,
    comportemental, garanties), en `Decimal`, barèmes en base, résultat persisté
    en `AnalyseCredit` immuable et journalisé.

Un analyste voyait les critères du premier à l'écran et lisait la recommandation
du second : personne ne pouvait dire lequel faisait foi. Pire, les deux
proposaient un TAUX, et pas le même (+2,5 ici, +2,0 dans le simulateur, sur la
même bande de score) — deux prix pour un même client selon l'écran consulté.

L'AUTORITÉ EST `credits.analyse`, pour des raisons vérifiables et non d'ancienneté :
  1. `Decimal` de bout en bout (principe 4) ; ce module calculait en `float` ;
  2. barèmes, poids et seuils en base, éditables par le comité en maker-checker
     avec prévisualisation sur le golden set (principe 8) ; ici, la grille de taux
     était en dur dans une fonction ;
  3. résultat APPEND-ONLY, horodaté, tracé (`needs_source` + révision + SHA-256),
     journalisé en audit (principes 1 et 3) ; ici, un `UPDATE` écrasait le score
     précédent — l'écart entre deux scorings, qui est une donnée, était perdu ;
  4. le contrat front (`CreditAnalyse` dans `src/types/api.ts`) est typé dessus.

CE QUE FAIT CE MODULE
---------------------
Il PROJETTE la dernière `AnalyseCredit` du dossier dans la forme `score_result`
attendue par des consommateurs qui vivent hors du moteur : `credits.disbursement`
(taux du prêt créé), `credits.workflow` (lettre de rejet), `portfolio.services`
et `portfolio.serializers` (score et décision du prêt), `credits.view_context`
(filtre client). Le supprimer aurait cassé ces quatre modules ; le laisser scorer
aurait laissé deux vérités.

Il n'INVENTE jamais de score : sans analyse exécutée, il le dit
(`analyseDisponible: False`) et n'émet ni `score` ni `proposedRate`, pour que les
consommateurs retombent sur leurs valeurs par défaut explicites plutôt que sur un
chiffre fabriqué par une porte dérobée.

Il n'exécute PAS le moteur non plus : `POST /score/` ne porte ni durée, ni
différé, ni mode de différé, et les deviner produirait une analyse aux paramètres
inventés — qui deviendrait de surcroît la dernière analyse du dossier, donc celle
du golden set de calibrage des barèmes. L'analyse s'exécute par
`POST /applications/<code>/reanalyser/`, avec ses paramètres, par un analyste.
"""
from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

from credits.analyse import CRITERES, proposer_taux, regles_taux, score_lettre
from common.money import en_float_optionnel

logger = logging.getLogger(__name__)

#: Libellés des cinq critères du moteur unifié — nomenclature UNIQUE (principe 6).
#: Ce sont les mêmes clés que `credits.analyse.CRITERES` et que le contrat front ;
#: les anciens codes de ce module (`repayment_history`, `kyc_seniority`…) et ceux
#: du simulateur (`fiabilite`, `behavioral`, `guarantees`) ont disparu.
LABELS_CRITERES = {
    "technique": "Fiabilité technique",
    "dscr": "Capacité financière (DSCR)",
    "stress": "Résilience au stress",
    "comportemental": "Historique comportemental",
    "garanties": "Garanties & domiciliation",
}

#: Note de valorisation par recommandation du moteur. Elle suit la RECOMMANDATION
#: et non une bande de score parallèle : une deuxième grille de bandes serait une
#: deuxième vérité, exactement ce que ce lot supprime.
NOTES_RECOMMANDATION = {
    "approbation": "Dossier solide — approbation recommandée par le moteur.",
    "approbation_cond": ("Dossier recevable — approbation sous conditions "
                         "recommandée par le moteur."),
    "revue": "Dossier à revoir — examen approfondi requis avant décision.",
    "refus": "Refus recommandé par le moteur en l'état du dossier.",
}

#: Recommandations qui valent « éligible » pour les consommateurs aval.
#: L'éligibilité n'est plus un `score >= seuil` recalculé ici : c'est le verdict
#: du moteur, seul endroit où DSCR plancher, hors-plage et score sont croisés.
RECOMMANDATIONS_ELIGIBLES = ("approbation", "approbation_cond")


def derniere_analyse(application):
    """Dernière `AnalyseCredit` du dossier, ou `None`.

    `AnalyseCredit.Meta.ordering` trie déjà par `-execute_le, -id` : la première
    ligne est la plus récente, et c'est celle qui fait foi (les précédentes sont
    conservées — on ré-analyse, on ne corrige pas).
    """
    try:
        return application.analyses.select_related("referentiel").first()
    except Exception:  # noqa: BLE001 — un dossier non persisté n'a pas d'analyses
        return None


def _breakdown(analyse) -> list[dict]:
    """Détail par critère, dans l'ordre canonique.

    `points` = points PONDÉRÉS (score × poids / 100) et `maxPoints` = le poids du
    critère : la colonne se lit « 8,5 / 25 » et sa somme tombe exactement sur le
    score global. C'est l'invariant que l'analyste vérifie de tête à l'écran
    (CONVENTIONS.md §5.2) — l'ancien format (score /100 par critère) ne le respectait
    pas, la somme des lignes ne faisait pas le total affiché.
    """
    criteres = analyse.criteres or {}
    lignes = []
    for cle in CRITERES:
        bloc = criteres.get(cle)
        if not bloc:
            continue
        poids = Decimal(str(bloc.get("poids", 0)))
        points = Decimal(str(bloc.get("points", 0)))
        details = bloc.get("details") or {}
        lignes.append({
            "code": cle,
            "label": LABELS_CRITERES.get(cle, cle),
            "points": float(points),
            "maxPoints": float(poids),
            "weight": float(poids),
            "weightedScore": float(points),
            "score": float(Decimal(str(bloc.get("score", 0)))),
            "detail": details.get("commentaire", ""),
        })
    return lignes


def _schedule(analyse) -> list[dict]:
    """Échéancier au format legacy `{month, payment, principal, interest, balance}`.

    Les montants sont ceux calculés en `Decimal` par `credits.echeancier` et
    stockés en chaînes : ils sont convertis ici, à la frontière HTTP, et jamais
    recalculés. Aucune formule d'amortissement ne subsiste dans ce module —
    il en portait une troisième (annuité constante) là où le moteur amortit à
    capital constant, ce qui donnait un échéancier différent de celui de l'analyse
    pour le même dossier.
    """
    out = []
    for ligne in (analyse.echeancier or []):
        out.append({
            "month": ligne.get("mois"),
            "principal": float(Decimal(str(ligne.get("capital", 0)))),
            "interest": float(Decimal(str(ligne.get("interets", 0)))),
            "payment": float(Decimal(str(ligne.get("echeance", 0)))),
            "balance": float(Decimal(str(ligne.get("crd", 0)))),
        })
    return out


def _schedule_totals(schedule: list[dict]) -> dict:
    """Totaux servis par le serveur — le front ne somme jamais une colonne."""
    somme = lambda cle: float(  # noqa: E731
        sum((Decimal(str(r[cle])) for r in schedule), Decimal("0")))
    return {
        "totalPrincipal": round(somme("principal"), 2),
        "totalInterest": round(somme("interest"), 2),
        "totalPayments": round(somme("payment"), 2),
        "count": len(schedule),
    }


def _taux_propose(analyse, application) -> float | None:
    """Taux figé par l'analyse. `None` si elle est antérieure à la grille unique.

    On ne re-tarifie pas une analyse passée avec la grille d'aujourd'hui : le taux
    lu par un analyste ne se réécrit pas rétroactivement (principe 3). L'absence
    de clé fait retomber `credits.disbursement` sur le taux de base de la filière,
    valeur explicite et sans surcote — jamais sur une surcote devinée.
    """
    if analyse.taux_propose is not None:
        return float(analyse.taux_propose)
    logger.warning(
        "Dossier %s : analyse #%s antérieure à la grille de tarification unique — "
        "aucun taux proposé n'est servi (le décaissement retombera sur le taux de "
        "base de la filière). Ré-analysez pour tarifer.",
        getattr(application, "code", "?"), analyse.pk)
    return None


class CreditScoringEngine:
    """Adaptateur `AnalyseCredit` → `score_result`. Ne calcule aucun score.

    Le nom est conservé parce que `credits.views.score_application` l'importe et
    que les vues ne sont pas dans le périmètre de ce lot ; son contenu, lui, est
    entièrement remplacé.
    """

    def __init__(self, application, needs_totals: dict[str, float] | None = None) -> None:
        self.app = application
        self.client = getattr(application, "client", None)
        self.value_chain = getattr(application, "value_chain", None)
        #: Accepté pour compatibilité d'appel (`views.score_application` le passe).
        #: Inutilisé : les totaux par module sont relus par le moteur lui-même dans
        #: les `DataRecord` de la révision courante (principe 1). Les recevoir ici
        #: laisserait croire qu'un appelant peut influencer le score.
        self.needs_totals = needs_totals

    # ── API publique ──────────────────────────────────────────────────────────

    def compute(self) -> dict[str, Any]:
        analyse = derniere_analyse(self.app)
        if analyse is None:
            return self._sans_analyse()
        return self.projeter(analyse)

    def projeter(self, analyse) -> dict[str, Any]:
        schedule = _schedule(analyse)
        taux = _taux_propose(analyse, self.app)
        min_required = (int(self.value_chain.min_score_required)
                        if self.value_chain is not None else 50)

        resultat: dict[str, Any] = {
            "score": float(analyse.score_global),
            "eligible": analyse.recommandation in RECOMMANDATIONS_ELIGIBLES,
            "minScoreRequired": min_required,
            "breakdown": _breakdown(analyse),
            "scheduleDraft": schedule,
            "scheduleTotals": _schedule_totals(schedule),
            "valuationNote": NOTES_RECOMMANDATION.get(
                analyse.recommandation, "Analyse disponible."),
            "analyseDisponible": True,
            # Provenance : quel moteur, quelle analyse, quelle feuille de besoins.
            # Sans elle, `score_result` serait un chiffre sans auteur.
            "analyse": {
                "id": analyse.pk,
                "recommandation": analyse.recommandation,
                "scoreLettre": score_lettre(
                    analyse.score_global,
                    (analyse.baremes_appliques or {}).get("_regles")),
                "dscr": en_float_optionnel(analyse.dscr),
                "dscrStress": en_float_optionnel(analyse.dscr_stress),
                "executeLe": (analyse.execute_le.isoformat()
                              if analyse.execute_le else None),
                "versionMoteur": analyse.version_moteur,
                "needsSourceId": analyse.needs_source_id,
                "needsSourceRevision": analyse.needs_source_revision,
                "needsSourceSha256": analyse.needs_source_sha256,
            },
        }
        if taux is not None:
            resultat["proposedRate"] = taux
        return resultat

    # ── Absence d'analyse — dit, jamais comblé ────────────────────────────────

    def _sans_analyse(self) -> dict[str, Any]:
        """Aucune analyse exécutée : ni score ni taux, et la raison en clair.

        Les clés `score` et `proposedRate` sont ABSENTES (et non nulles) :
        `credits.disbursement` fait `int(score_result.get("score", 0))`, qu'un
        `None` ferait échouer, et retombe sur le taux de base de la filière quand
        `proposedRate` manque. Une clé absente est un contrat ; une clé nulle est
        un piège.
        """
        return {
            "eligible": False,
            "breakdown": [],
            "minScoreRequired": (int(self.value_chain.min_score_required)
                                 if self.value_chain is not None else 50),
            "valuationNote": ("Aucune analyse exécutée sur ce dossier : le score "
                              "est produit par le moteur d'analyse, à partir de la "
                              "feuille de besoins ingérée et des paramètres de "
                              "crédit choisis par l'analyste."),
            "analyseDisponible": False,
            "unavailable": {
                "code": "ANALYSE_REQUISE",
                "message": ("Lancez l'analyse du dossier "
                            "(POST /api/credits/applications/<code>/reanalyser/) "
                            "pour obtenir un score, une recommandation et un taux."),
            },
        }


# ── Tarification hors analyse (simulation indicative) ─────────────────────────

def taux_pour_score(score_global, taux_base) -> dict:
    """Taux proposé par la grille UNIQUE, pour un appelant sans `AnalyseCredit`.

    Point d'entrée unique du simulateur indicatif (`credits.dataio_simulator`) :
    il n'a pas d'analyse persistée, mais il doit annoncer le MÊME taux que celui
    que le dossier obtiendra à l'instruction pour le même score. C'est cette
    fonction qui l'y oblige — il n'existe plus d'autre chemin vers un taux.
    """
    from credits.models import BaremeScore

    bareme = BaremeScore.objects.filter(code="TAUX", actif=True).first()
    return proposer_taux(score_global, taux_base, regles_taux(bareme))
