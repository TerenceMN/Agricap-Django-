"""
Ventilation CAPITAL / INTÉRÊTS d'un remboursement — le chaînon manquant entre le
prêt et le grand livre.

Le blocage que ce module lève
-----------------------------
`credits.events.emettre_echeance` attend DEUX montants (capital B2, intérêts B3)
parce que les schémas comptables B2 et B3 ne mouvementent ni les mêmes comptes ni
les mêmes classes : 501 → 413 pour le capital (l'encours diminue), 501 → 701 pour
les intérêts (un produit naît). `portfolio.add_transaction` n'enregistrait qu'un
TOTAL. Aucun producteur ne pouvait donc appeler l'émetteur, et rien de la vie du
prêt n'atteignait la comptabilité après le décaissement.

La ventilation ne se devine pas
-------------------------------
Deviner la répartition — au prorata, à la louche, « x % d'intérêts » —
fabriquerait un produit financier qui n'a jamais existé et fausserait le résultat.
Elle est ici le résultat d'une IMPUTATION déterministe sur l'échéancier réel,
c'est-à-dire sur `services.schedule_for(loan)`, le calendrier que le client
rembourse effectivement :

  1. les échéances sont servies dans l'ordre, la plus ancienne d'abord ;
  2. dans chaque échéance, les INTÉRÊTS d'abord, puis le CAPITAL ;
  3. une échéance peut être partiellement servie, et un versement peut couvrir
     plusieurs échéances.

Une SEULE règle d'imputation, et il en existait déjà une
---------------------------------------------------------
`accounting.provisions.imputer` applique exactement cet ordre pour dater le
premier impayé. Ce module ne l'a pas réécrite par inadvertance : elle est ici
parce que l'imputation est une propriété de l'ÉCHÉANCIER, que `portfolio`
produit — et qu'un module métier ne peut pas dépendre de la comptabilité pour
appliquer sa propre règle (le sens de la dépendance est déjà
`accounting → portfolio`, depuis que `provisions` consomme `schedule_for`).

La copie comptable est donc le doublon à retirer, exactement comme la 4ᵉ
réimplémentation d'échéancier qui vient de l'être. En attendant qu'elle appelle
celle-ci, l'égalité des deux est VERROUILLÉE par un test croisé
(`ImputationUniqueTests`) : tant que les deux coexistent, elles ne peuvent pas
diverger en silence. Deux règles différentes produiraient un encours 413 et un
PAR incohérents entre eux sans que rien ne le signale.

Le versement est ventilé par DIFFÉRENCE entre l'imputation cumulée avant et après
lui. C'est ce qui rend la ventilation correcte quand un versement tombe à cheval
sur deux échéances, ou quand un client règle en plusieurs fois : la quote-part
d'intérêts d'un second versement dépend de ce que le premier a déjà éteint.

Ce que ce module REFUSE
-----------------------
Sans échéancier — prêt sans capital, sans durée, paramétrage refusé — il n'existe
aucune répartition connue. On renvoie `disponible=False` avec son motif et AUCUN
événement n'est émis : la file comptable attend, plutôt que d'inscrire un produit
d'intérêts inventé. C'est la même ligne que `investments.echeancier_retour`
oppose au schéma B12.
"""
from __future__ import annotations

import logging

# Le quantize monétaire et le zéro de référence viennent de l'échéancier, ils ne
# sont pas redéfinis ici : deux règles d'arrondi pour un même prêt, c'est un
# centime d'écart entre ce que le client paie et ce que la comptabilité écrit.
from .schedule import ZERO, q2

logger = logging.getLogger(__name__)


def imputer(lignes: list[dict], total: Decimal) -> dict:
    """Impute `total` sur l'échéancier : plus ancienne d'abord, intérêts puis capital.

    Fonction PUBLIQUE : `accounting.provisions` la consomme pour dater le premier
    impayé, et un consommateur ne doit pas signer pour une rupture sans préavis sur
    le chemin qui décide d'une provision.

    Args:
        lignes: lignes de `services.schedule_for(loan)["schedule"]` — chacune porte
            `principal`, `interest` (`Decimal`) et `date` (chaîne ISO).
        total: montant CUMULÉ réglé, à imputer depuis la première échéance.

    Returns:
        `capital` et `interets` effectivement imputés, `surplus` (reliquat non
        imputable une fois tout l'échéancier soldé), `par_ligne` (détail des seules
        échéances SERVIES) et `premiere_echeance_impayee` — date ISO de la plus
        ancienne échéance non intégralement réglée, `None` si tout est à jour.

    Deux pièges, tous deux tenus ici :

    1. **La boucle ne s'interrompt pas quand le règlement est épuisé.** Elle
       parcourt TOUTES les lignes, parce que la première échéance impayée est
       précisément, le plus souvent, la première que le règlement n'a pas atteinte —
       donc absente de `par_ligne`. Sortir tôt rendrait `None` sur un dossier en
       défaut, c'est-à-dire zéro jour de retard et aucune provision.

    2. **Une échéance à 0/0 est réglée par définition** (`0 >= 0`) : c'est le cas
       des lignes de différé en franchise totale, où rien n'est exigible. Les
       compter impayées reclasserait en PAR90 des clients parfaitement à jour.
    """
    reste = q2(total)
    capital = ZERO
    interets = ZERO
    par_ligne: dict = {}
    premiere_impayee = None
    for ligne in lignes:
        du_interets = ligne.get("interest", ZERO)
        du_capital = ligne.get("principal", ZERO)
        # Une fois `reste` épuisé, ces parts valent 0 — et l'échéance est impayée
        # dès qu'elle devait quelque chose.
        paye_interets = du_interets if du_interets <= reste else reste
        reste = q2(reste - paye_interets)
        paye_capital = du_capital if du_capital <= reste else reste
        reste = q2(reste - paye_capital)
        if paye_interets > ZERO or paye_capital > ZERO:
            interets = q2(interets + paye_interets)
            capital = q2(capital + paye_capital)
            par_ligne[ligne.get("number")] = {
                "numero": ligne.get("number"),
                "date": ligne.get("date"),
                "capital": paye_capital,
                "interets": paye_interets,
            }
        if (paye_interets < du_interets or paye_capital < du_capital) \
                and premiere_impayee is None:
            premiere_impayee = ligne.get("date")
    return {"capital": capital, "interets": interets, "surplus": reste,
            "par_ligne": par_ligne, "premiere_echeance_impayee": premiere_impayee}


def _lignes_du_versement(avant: dict, apres: dict) -> list[dict]:
    """Échéances servies par CE versement — l'écart entre les deux imputations.

    L'imputation cumulée d'`apres` contient aussi les échéances qu'un versement
    ANTÉRIEUR avait déjà éteintes : les servir à l'auditeur comme le détail du
    versement courant lui ferait lire deux fois le même encaissement.
    """
    lignes = []
    for numero, ligne in apres["par_ligne"].items():
        precedent = avant["par_ligne"].get(numero, {})
        capital = q2(ligne["capital"] - precedent.get("capital", ZERO))
        interets = q2(ligne["interets"] - precedent.get("interets", ZERO))
        if capital > ZERO or interets > ZERO:
            lignes.append({"numero": numero, "date": ligne["date"],
                           "capital": capital, "interets": interets})
    return lignes


def ventiler_remboursement(loan, *, montant, deja_regle=ZERO) -> dict:
    """Ventile UN versement entre capital et intérêts, selon l'échéancier du prêt.

    Args:
        loan: le prêt (`portfolio.models.Loan`).
        montant: le versement à ventiler (positif, `Decimal`).
        deja_regle: total des remboursements VALIDÉS antérieurs à ce versement.
            C'est lui qui décale l'imputation : le deuxième versement d'un client
            ne rembourse pas les mêmes intérêts que le premier.

    Returns:
        `disponible` (bool), `capital`, `interets`, `surplus`, `lignes_imputees`,
        `motif`. Quand `disponible` est faux, AUCUN montant n'est proposé et
        `motif` dit pourquoi — l'appelant n'émet alors rien.
    """
    from . import services

    total = q2(montant)
    if total <= ZERO:
        return {
            "disponible": False, "capital": ZERO, "interets": ZERO, "surplus": ZERO,
            "lignes_imputees": [],
            "motif": f"Montant de remboursement non positif ({total}) : rien à ventiler.",
        }

    try:
        lignes = services.schedule_for(loan)["schedule"]
    except Exception as exc:  # noqa: BLE001 — un refus de l'échéancier est une DONNÉE
        return {
            "disponible": False, "capital": ZERO, "interets": ZERO, "surplus": ZERO,
            "lignes_imputees": [],
            "motif": (
                f"Échéancier indisponible ({type(exc).__name__} : {exc}) : la "
                f"ventilation capital / intérêts de ce versement ne peut pas être "
                f"établie et n'est pas devinée. Corrigez le paramétrage du prêt."
            ),
        }

    if not lignes:
        return {
            "disponible": False, "capital": ZERO, "interets": ZERO, "surplus": ZERO,
            "lignes_imputees": [],
            "motif": (
                f"Le prêt {loan.reference} n'a pas d'échéancier (capital ou durée "
                f"nuls) : aucune répartition capital / intérêts n'existe pour ce "
                f"versement. Aucun événement comptable n'est émis."
            ),
        }

    avant = imputer(lignes, q2(deja_regle))
    apres = imputer(lignes, q2(q2(deja_regle) + total))

    capital = q2(apres["capital"] - avant["capital"])
    interets = q2(apres["interets"] - avant["interets"])
    surplus = q2(apres["surplus"] - avant["surplus"])

    # Invariant dur : les deux jambes plus le reliquat valent EXACTEMENT
    # l'encaissement. Une ventilation qui ne boucle pas ne produit rien — mieux
    # vaut un événement en attente qu'une écriture fausse.
    if q2(capital + interets + surplus) != total:
        logger.error(
            "Ventilation incohérente sur %s : capital %s + intérêts %s + surplus %s "
            "≠ versement %s.", loan.reference, capital, interets, surplus, total,
        )
        return {
            "disponible": False, "capital": ZERO, "interets": ZERO, "surplus": ZERO,
            "lignes_imputees": [],
            "motif": (
                f"Ventilation incohérente ({capital} + {interets} + {surplus} ≠ "
                f"{total}) : aucun événement n'est émis."
            ),
        }

    motif = ""
    if surplus > ZERO:
        # Le service de la dette est intégralement imputé : ce qui arrive en plus
        # n'est ni du capital ni un intérêt de cet échéancier. On ne l'attribue
        # pas — un remboursement anticipé, une pénalité ou une erreur de saisie
        # ne s'écrivent pas au même compte, et ce module ne sait pas lequel.
        motif = (
            f"Versement supérieur au solde de l'échéancier : {surplus} "
            f"{loan.currency} ne sont imputés à aucune échéance et ne produisent "
            f"aucun événement comptable. Nature à instruire (remboursement "
            f"anticipé, pénalité, erreur de saisie) avant écriture."
        )
        logger.warning("Prêt %s : %s", loan.reference, motif)

    return {"disponible": True, "capital": capital, "interets": interets,
            "surplus": surplus,
            "lignes_imputees": _lignes_du_versement(avant, apres),
            "motif": motif}
