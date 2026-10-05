"""
Unité des taux du portefeuille — le garde-fou du **facteur 12**.

Le problème que ce module ferme
-------------------------------
`portfolio.Loan.rate` est un taux **MENSUEL** en points de pourcentage ; le moteur
d'analyse (`credits.echeancier`, `credits.models.AnalyseCredit.taux_annuel`) raisonne
en taux **ANNUEL**. Un gestionnaire qui lit « 18 % » sur un dossier scoré et le reporte
tel quel dans le champ taux crée un prêt à **18 %/mois = 216 %/an** — usuraire, et rien
ne le signalait : ni le modèle, ni l'API, ni l'échéancier, qui produisait sans broncher
un tableau d'amortissement douze fois trop cher.

Ce que ce module impose
-----------------------
1. **L'unité est nommée dans le schéma.** `Loan.rate` (mensuel) a désormais un pendant
   `Loan.annual_rate` (annuel), et les deux sont maintenus rigoureusement cohérents à
   chaque écriture : il n'existe pas d'état où les deux se contredisent.
2. **Un taux mensuel implausible est REFUSÉ**, pas corrigé en silence. Le plafond est
   un paramètre de gouvernance (principe 8) ; le message d'erreur annualise la valeur
   saisie et donne l'équivalent mensuel du taux annuel visé — l'opérateur n'a pas à
   deviner la conversion.
3. **Le facteur 12 exact est détecté.** Quand le dossier porte une analyse, on connaît
   son taux ANNUEL : une saisie mensuelle rigoureusement égale à ce taux annuel est
   l'erreur de report, pas un choix (un taux mensuel égal au taux annuel du dossier
   multiplierait le coût du crédit par douze). Ce contrôle attrape aussi les petits
   taux — `7 %/an` reporté en `7 %/mois` = 84 %/an passerait sous n'importe quel
   plafond de plausibilité.
4. **La conversion annuel → mensuel n'est jamais approximée dans le calcul.**
   `mensuel_exact()` rend `annuel / 12` en `Decimal` non quantizé : c'est ce que
   `credits.echeancier` fait (`taux_annuel / 100 / 12`), donc l'échéancier PAYÉ tombe
   au centime sur l'échéancier SCORÉ, y compris pour un taux annuel dont le douzième
   n'est pas décimal fini (22,6 %/an → 1,88333…%/mois).
   La colonne `rate` en base n'en est que la projection d'affichage (6 décimales).

Principe 4 : `Decimal` partout. Les entrées d'API sont tolérées en chaîne
(« 1,5 », « 1.5 ») mais converties par `Decimal(str(...))`, jamais par `float()`.
"""
from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from common.exceptions import ValidationFailed

logger = logging.getLogger(__name__)

MOIS_PAR_AN = Decimal("12")
MILLIEME = Decimal("0.001")
MILLIONIEME = Decimal("0.000001")

#: Plafond de plausibilité du taux MENSUEL, en points de % par mois (valeur de
#: SECOURS). 10 %/mois = 120 %/an en intérêt simple : très au-dessus de toute
#: tarification AGRICAP (référentiel : 8 à 25 %/an), et très en-dessous de ce que
#: produit une confusion d'unité sur un taux annuel réaliste (18 %/an reporté en
#: mensuel = 18 %/mois). La fenêtre est donc large côté métier et fermée côté erreur.
#:
#: Principe 8 : ce seuil appartient au comité, pas au code. Il est lu sur
#: `referentiel.InstitutionConfig.taux_mensuel_max` dès que ce champ existe ; tant
#: qu'il n'existe pas, la valeur de secours s'applique AVEC un warning loggé (c'est
#: l'exception prévue par le principe 8, pas un contournement).
TAUX_MENSUEL_MAX_DEFAUT = Decimal("10")

#: Nom du champ de gouvernance attendu sur `InstitutionConfig`.
CHAMP_PLAFOND = "taux_mensuel_max"

_plafond_manquant_signale = False


class TauxInvalide(ValidationFailed):
    """La valeur fournie n'est pas un taux exploitable."""

    code = "TAUX_INVALIDE"


class TauxMensuelImplausible(ValidationFailed):
    """Taux mensuel au-delà du plafond de plausibilité — refus, jamais de correction."""

    code = "TAUX_MENSUEL_IMPLAUSIBLE"


class TauxAnnuelSaisiCommeMensuel(TauxMensuelImplausible):
    """Le taux ANNUEL du dossier a été reporté tel quel dans le champ MENSUEL."""

    code = "TAUX_ANNUEL_SAISI_COMME_MENSUEL"


class TauxAnnuelImplausible(ValidationFailed):
    """Taux annuel au-delà du plafond de plausibilité (12 × le plafond mensuel)."""

    code = "TAUX_ANNUEL_IMPLAUSIBLE"


# --- Conversions -------------------------------------------------------------

def q3(valeur) -> Decimal:
    """Quantize des taux ANNUELS : 3 décimales, comme `AnalyseCredit.taux_annuel`."""
    return Decimal(valeur).quantize(MILLIEME, rounding=ROUND_HALF_UP)


def q6(valeur) -> Decimal:
    """Quantize des taux MENSUELS stockés : 6 décimales.

    Trois décimales ne suffisaient pas : 22,6 %/an ÷ 12 = 1,883333…%/mois, arrondi
    à 1,883, faisait diverger l'échéancier payé de l'échéancier scoré. Le calcul,
    lui, n'utilise jamais cette valeur arrondie (cf. `mensuel_exact`).
    """
    return Decimal(valeur).quantize(MILLIONIEME, rounding=ROUND_HALF_UP)


def to_taux(valeur, *, champ: str = "taux") -> Decimal:
    """Conversion tolérante vers `Decimal` — jamais via `float`."""
    if valeur in (None, ""):
        return Decimal("0")
    if isinstance(valeur, Decimal):
        return valeur
    try:
        return Decimal(str(valeur).replace(",", ".").replace(" ", "").replace("%", ""))
    except (InvalidOperation, ValueError, ArithmeticError) as exc:
        raise TauxInvalide(
            f"« {champ} » n'est pas un taux exploitable : {valeur!r}."
        ) from exc


def mensuel_exact(taux_annuel) -> Decimal:
    """Taux mensuel = taux annuel ÷ 12, **non quantizé**.

    C'est la valeur de CALCUL : `credits.echeancier` fait `taux_annuel / 100 / 12`
    en pleine précision `Decimal`. Quantizer ici rouvrirait un écart de centimes
    entre le calendrier scoré et le calendrier payé.
    """
    return to_taux(taux_annuel, champ="taux annuel") / MOIS_PAR_AN


def mensuel_stocke(taux_annuel) -> Decimal:
    """Projection d'affichage du taux mensuel (colonne `Loan.rate`)."""
    return q6(mensuel_exact(taux_annuel))


def annuel_depuis_mensuel(taux_mensuel) -> Decimal:
    """Taux annuel nominal = taux mensuel × 12."""
    return q3(to_taux(taux_mensuel, champ="taux mensuel") * MOIS_PAR_AN)


def _fmt(valeur: Decimal) -> str:
    """Affichage compact d'un taux dans un message d'erreur (« 1.5 », pas « 1.500000 »)."""
    normalise = Decimal(valeur).normalize()
    texte = format(normalise, "f")
    return texte


# --- Plafond de gouvernance ---------------------------------------------------

def plafond_mensuel() -> Decimal:
    """Plafond de plausibilité du taux mensuel, lu en base si le comité l'y a posé."""
    global _plafond_manquant_signale
    try:
        from referentiel.models import InstitutionConfig

        valeur = getattr(InstitutionConfig.active(), CHAMP_PLAFOND, None)
        if valeur not in (None, ""):
            return to_taux(valeur, champ=CHAMP_PLAFOND)
    except Exception:  # noqa: BLE001 — référentiel indisponible : on ne bloque pas
        pass
    if not _plafond_manquant_signale:
        _plafond_manquant_signale = True
        logger.warning(
            "Plafond de taux mensuel non paramétré (InstitutionConfig.%s absent) : "
            "valeur de secours %s %%/mois appliquée. Principe 8 — ce seuil doit "
            "rejoindre le référentiel de gouvernance.",
            CHAMP_PLAFOND, TAUX_MENSUEL_MAX_DEFAUT,
        )
    return TAUX_MENSUEL_MAX_DEFAUT


# --- Validation ---------------------------------------------------------------

def valider_taux_mensuel(valeur, *, taux_annuel_dossier=None, champ: str = "rate",
                         plafond: Decimal | None = None) -> Decimal:
    """Valide un taux **MENSUEL** et le rend quantizé à 6 décimales.

    Args:
        valeur: le taux mensuel saisi (Decimal, chaîne ou entier).
        taux_annuel_dossier: taux ANNUEL de l'analyse qui a scoré le dossier, quand
            il est connu — sert à détecter le report tel quel du taux annuel.
        champ: nom du champ pour le message d'erreur.
        plafond: plafond mensuel explicite (sinon celui de la gouvernance).

    Raises:
        TauxInvalide: valeur non numérique ou négative.
        TauxAnnuelSaisiCommeMensuel: la saisie est exactement le taux annuel du dossier.
        TauxMensuelImplausible: la saisie dépasse le plafond.
    """
    taux = to_taux(valeur, champ=champ)
    if taux < 0:
        raise TauxInvalide(
            f"Le taux ne peut pas être négatif (« {champ} » = {_fmt(taux)})."
        )

    if taux_annuel_dossier not in (None, ""):
        annuel_dossier = to_taux(taux_annuel_dossier, champ="taux annuel du dossier")
        if annuel_dossier > 0 and taux == annuel_dossier:
            raise TauxAnnuelSaisiCommeMensuel(
                f"« {champ} » est un taux MENSUEL, et {_fmt(taux)} est exactement le "
                f"taux ANNUEL du dossier analysé : le saisir ici applique "
                f"{_fmt(annuel_depuis_mensuel(taux))} %/an, soit douze fois le coût "
                f"sur lequel le dossier a été scoré. Le taux mensuel correspondant "
                f"est {_fmt(mensuel_stocke(annuel_dossier))} %/mois — ou renseignez "
                f"directement « annualRate » = {_fmt(annuel_dossier)}."
            )

    limite = plafond if plafond is not None else plafond_mensuel()
    if taux > limite:
        raise TauxMensuelImplausible(
            f"« {champ} » est un taux MENSUEL : {_fmt(taux)} %/mois = "
            f"{_fmt(annuel_depuis_mensuel(taux))} %/an, au-delà du plafond de "
            f"plausibilité de {_fmt(limite)} %/mois "
            f"({_fmt(annuel_depuis_mensuel(limite))} %/an). "
            f"S'il s'agit d'un taux annuel, renseignez « annualRate » "
            f"({_fmt(taux)} %/an = {_fmt(mensuel_stocke(taux))} %/mois)."
        )
    return q6(taux)


def valider_taux_annuel(valeur, *, champ: str = "annualRate",
                        plafond: Decimal | None = None) -> Decimal:
    """Valide un taux **ANNUEL** et le rend quantizé à 3 décimales."""
    taux = to_taux(valeur, champ=champ)
    if taux < 0:
        raise TauxInvalide(
            f"Le taux annuel ne peut pas être négatif (« {champ} » = {_fmt(taux)})."
        )
    limite_mensuelle = plafond if plafond is not None else plafond_mensuel()
    limite = limite_mensuelle * MOIS_PAR_AN
    if taux > limite:
        raise TauxAnnuelImplausible(
            f"« {champ} » est un taux ANNUEL : {_fmt(taux)} %/an dépasse le plafond "
            f"de plausibilité de {_fmt(limite)} %/an "
            f"({_fmt(limite_mensuelle)} %/mois)."
        )
    return q3(taux)
