"""États financiers construits DEPUIS LA BALANCE RÉELLE (annexe F, dette n° 4).

Aucun chiffre n'est saisi, aucun n'est figé : bilan et compte de résultat sont des
regroupements de `services.balance_par_devise`, elle-même issue des seules pièces validées.
Si une écriture manque, l'état le montre — il ne le compense pas.

Bi-monnaie
----------
Un état est produit PAR DEVISE (FC et USD séparés, jamais additionnés en douce), et un état
CONSOLIDÉ est proposé en plus, converti au **taux de clôture** du jour d'arrêté. Le
consolidé porte toujours l'identité du taux utilisé (date, usage, source, valeur) : sans
cette référence, un total consolidé n'est pas auditable et ne doit pas être affiché.
"""
from __future__ import annotations

from datetime import date as date_cls
from decimal import Decimal

from common.exceptions import ValidationFailed

from . import fx, services
from .models import Devise, Nature, TauxChange

#: L'équation du bilan telle qu'elle se vérifie ici (les signes sont ceux de la balance :
#: solde = débit − crédit) :  Σ ACTIF = Σ PASSIF(présenté) + RÉSULTAT.
CLASSES_BILAN = {Nature.ACTIF, Nature.PASSIF}
CLASSES_RESULTAT = {Nature.CHARGE, Nature.PRODUIT}


def _lignes_par_nature(devise: str, as_of: date_cls | None) -> dict[str, list[dict]]:
    groupes: dict[str, list[dict]] = {n: [] for n in Nature.values}
    for ligne in services.balance_par_devise(devise=devise, as_of=as_of):
        if ligne["solde"] == 0 and ligne["debit"] == 0 and ligne["credit"] == 0:
            continue
        groupes[ligne["nature"]].append(ligne)
    return groupes


def _presenter(ligne: dict) -> dict:
    """Un poste se présente dans son SENS NORMAL : un passif de 5 000 s'affiche 5 000,
    pas −5 000. Le solde signé reste exposé à côté pour la traçabilité."""
    sens_credit = ligne["nature"] in (Nature.PASSIF, Nature.PRODUIT)
    montant = -ligne["solde"] if sens_credit else ligne["solde"]
    return {
        "code": ligne["code"],
        "intitule": ligne["intitule"],
        "nature": ligne["nature"],
        "debit": ligne["debit"],
        "credit": ligne["credit"],
        "solde_signe": ligne["solde"],
        "montant": services.q2(montant),
    }


def compte_de_resultat(*, devise: str, as_of: date_cls | None = None) -> dict:
    """Charges, produits, résultat — dans UNE devise."""
    if devise not in Devise.values:
        raise ValidationFailed(f"Devise « {devise} » inconnue.")
    groupes = _lignes_par_nature(devise, as_of)
    charges = [_presenter(l) for l in groupes[Nature.CHARGE]]
    produits = [_presenter(l) for l in groupes[Nature.PRODUIT]]
    total_charges = services.q2(sum((l["montant"] for l in charges), Decimal("0.00")))
    total_produits = services.q2(sum((l["montant"] for l in produits), Decimal("0.00")))
    return {
        "devise": devise,
        "as_of": as_of,
        "charges": charges,
        "produits": produits,
        "total_charges": total_charges,
        "total_produits": total_produits,
        "resultat": services.q2(total_produits - total_charges),
    }


def bilan(*, devise: str, as_of: date_cls | None = None) -> dict:
    """Actif / passif, avec le résultat de la période porté au passif.

    Le résultat n'est PAS un poste saisi : c'est produits − charges de la même balance.
    C'est ce qui fait boucler le bilan sans écriture d'ajustement.
    """
    if devise not in Devise.values:
        raise ValidationFailed(f"Devise « {devise} » inconnue.")
    groupes = _lignes_par_nature(devise, as_of)
    actif = [_presenter(l) for l in groupes[Nature.ACTIF]]
    passif = [_presenter(l) for l in groupes[Nature.PASSIF]]
    total_actif = services.q2(sum((l["montant"] for l in actif), Decimal("0.00")))
    total_passif = services.q2(sum((l["montant"] for l in passif), Decimal("0.00")))

    resultat_dict = compte_de_resultat(devise=devise, as_of=as_of)
    resultat = resultat_dict["resultat"]
    total_passif_et_resultat = services.q2(total_passif + resultat)

    return {
        "devise": devise,
        "as_of": as_of,
        "actif": actif,
        "passif": passif,
        "total_actif": total_actif,
        "total_passif": total_passif,
        "resultat_exercice": resultat,
        "total_passif_et_resultat": total_passif_et_resultat,
        "ecart_bouclage": services.q2(total_actif - total_passif_et_resultat),
        "boucle": services.q2(total_actif - total_passif_et_resultat) == 0,
        # Un bilan qui BOUCLE n'est pas pour autant COMPLET : il peut boucler parfaitement
        # en ignorant des faits qui n'ont pas encore de schéma. Les deux informations
        # voyagent donc ensemble, sans quoi « boucle: true » se lirait comme un quitus.
        "avertissements": _avertissements(as_of),
    }


def _taux_cloture(as_of: date_cls | None) -> TauxChange:
    """Le taux de clôture vient de l'app `fx` (source de vérité) via la projection
    comptable — jamais d'une saisie propre aux états financiers."""
    if as_of is None:
        raise ValidationFailed(
            "Un état consolidé exige une date d'arrêté : le taux de clôture s'y rattache."
        )
    return fx.taux_de_cloture(date_arrete=as_of)


def etats_consolides(*, as_of: date_cls, taux: TauxChange | None = None) -> dict:
    """Bilan et résultat par devise + agrégat converti au taux de CLÔTURE, référencé.

    Le pivot est le FC (devise de tenue de l'annexe A). Aucun agrégat n'est produit si le
    taux de clôture du jour n'est pas saisi : c'est un refus, pas une valeur par défaut
    (principe 5 de HAZINA).
    """
    taux = taux or _taux_cloture(as_of)

    par_devise = {}
    for devise in Devise.values:
        par_devise[devise] = {
            "bilan": bilan(devise=devise, as_of=as_of),
            "resultat": compte_de_resultat(devise=devise, as_of=as_of),
        }

    # Conversion à PLEINE PRÉCISION puis quantize une seule fois, à la fin : quantizer
    # chaque conversion ferait dériver le bouclage d'un centime par devise et donnerait un
    # bilan consolidé qui « ne tombe pas juste » — pour une raison purement technique.
    def _en_pivot(montant: Decimal, devise: str) -> Decimal:
        if devise == fx.DEVISE_PIVOT:
            return montant
        if devise == taux.devise_base and fx.DEVISE_PIVOT == taux.devise_contre:
            return montant * taux.taux
        if devise == taux.devise_contre and fx.DEVISE_PIVOT == taux.devise_base:
            return montant / taux.taux
        raise ValidationFailed(
            f"Le taux {taux.devise_base}/{taux.devise_contre} ne permet pas de convertir "
            f"{devise} → {fx.DEVISE_PIVOT}."
        )

    brut = {"actif": Decimal("0"), "passif": Decimal("0"),
            "charges": Decimal("0"), "produits": Decimal("0")}
    for devise, etats in par_devise.items():
        brut["actif"] += _en_pivot(etats["bilan"]["total_actif"], devise)
        brut["passif"] += _en_pivot(etats["bilan"]["total_passif"], devise)
        brut["charges"] += _en_pivot(etats["resultat"]["total_charges"], devise)
        brut["produits"] += _en_pivot(etats["resultat"]["total_produits"], devise)
    resultat_brut = brut["produits"] - brut["charges"]

    consolide = {
        "devise_pivot": fx.DEVISE_PIVOT,
        "total_actif": services.q2(brut["actif"]),
        "total_passif": services.q2(brut["passif"]),
        "total_charges": services.q2(brut["charges"]),
        "total_produits": services.q2(brut["produits"]),
        "resultat": services.q2(resultat_brut),
        "total_passif_et_resultat": services.q2(brut["passif"] + resultat_brut),
        "ecart_bouclage": services.q2(brut["actif"] - brut["passif"] - resultat_brut),
    }
    consolide["boucle"] = consolide["ecart_bouclage"] == 0

    return {
        "as_of": as_of,
        "taux_cloture": {
            "id": taux.pk,
            "date_taux": taux.date_taux,
            "usage": taux.usage,
            "devise_base": taux.devise_base,
            "devise_contre": taux.devise_contre,
            "taux": taux.taux,
            "source": taux.source,
            "source_reference": taux.source_reference,
            "provenance": fx.provenance(taux),
        },
        "par_devise": par_devise,
        "consolide": consolide,
        "avertissements": _avertissements(as_of, taux=taux),
    }


def evenements_non_comptabilises(as_of: date_cls | None = None) -> dict[str, Decimal]:
    """Montants de faits monétaires SURVENUS mais pas encore au grand livre, par devise.

    Un état financier qui ignore cette somme se présente comme complet alors qu'il ne l'est
    pas. Le cas vivant : les schémas B8/B9 de l'épargne attendent l'ouverture d'un compte de
    dette de portefeuille à l'annexe A ; tant qu'il n'existe pas, la dette d'épargne des
    membres n'apparaît nulle part au passif, et le montant exact de cette absence est la
    somme des événements en attente. On l'affiche plutôt que de la taire — une omission
    déclarée se corrige, une omission silencieuse se découvre à l'audit.

    L'import est différé et l'échec absorbé : un contrôle qui informe un état ne doit jamais
    empêcher cet état de s'afficher.
    """
    try:
        from . import consommation

        return consommation.montants_en_attente(jusqu_au=as_of)
    except Exception:  # noqa: BLE001 - pragma: no cover
        return {}


def evenements_en_devise_inconnue(as_of: date_cls | None = None) -> dict[str, Decimal]:
    """Faits monétaires en attente dont la devise n'est pas au plan comptable.

    Ils ne peuvent pas être chiffrés DANS l'écart (dans quelle colonne les additionner ?),
    mais les taire les rendrait invisibles deux fois. Et contrairement au reste de la file,
    ceux-là n'attendent pas un arbitrage : ils attendent une CORRECTION à la source, sans
    quoi l'écart ne redescendra jamais complètement.
    """
    try:
        from . import consommation

        return consommation.montants_en_devise_inconnue(jusqu_au=as_of)
    except Exception:  # noqa: BLE001 - pragma: no cover
        return {}


def _avertissements(as_of: date_cls | None, *, taux: TauxChange | None = None) -> list[str]:
    """Ce qu'un état ne doit jamais taire."""
    messages = []
    for devise, montant in evenements_non_comptabilises(as_of).items():
        if montant:
            messages.append(
                f"{montant} {devise} de faits monétaires survenus ne sont PAS au grand "
                "livre : leur file d'événements attend un schéma ou un compte du plan "
                "comptable (détail dans le rapport de consommation). Les états ci-dessus "
                "sont incomplets de ce montant — l'écart est connu et chiffré, pas estimé."
            )
    for devise, montant in evenements_en_devise_inconnue(as_of).items():
        if montant:
            messages.append(
                f"{montant} en devise « {devise} », inconnue du plan comptable : ces faits "
                "ne peuvent même pas être chiffrés dans l'écart ci-dessus, et leur file ne "
                "se videra JAMAIS d'elle-même. La devise est à corriger à la source."
            )
    anomalies = services.controler_integrite(as_of=as_of)
    if anomalies:
        messages.append(
            f"{len(anomalies)} pièce(s) validée(s) déséquilibrée(s) : les états ci-dessus "
            "ne sont pas fiables tant que le contrôle d'intégrité n'est pas vert."
        )
    if taux is not None:
        # Le contrôle du 588FX est une CONTRE-VALEUR, pas un solde par devise : après un
        # règlement dénoué, 588FC et 588USD sont chacun non nuls et se compensent au taux
        # (cf. l'en-tête de `accounting/fx.py`).
        position = Decimal("0.00")
        for devise in Devise.values:
            solde = fx.solde_global_transitoire_fx(devise=devise, as_of=as_of)
            position += fx.convertir(solde, de=devise, vers=fx.DEVISE_PIVOT, taux=taux)
        position = services.q2(position)
        if position != 0:
            messages.append(
                f"Transitoire 588FX non dénoué : position résiduelle de {position} "
                f"{fx.DEVISE_PIVOT} au taux de clôture — des opérations de change sont "
                "restées ouvertes."
            )
    return messages
