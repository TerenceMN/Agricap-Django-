"""Exécution du catalogue d'événements (annexe B).

« Les écritures naissent des événements, jamais des doigts. » Le code de ce module ne
contient AUCUN schéma d'écriture : il lit `EventEntryTemplate` en base et l'applique. Pour
changer un schéma, on change le paramétrage — pas ce fichier.
"""
from __future__ import annotations

from datetime import date as date_cls
from decimal import Decimal

from django.db import transaction

from common.exceptions import NotFoundError, ValidationFailed

from . import services
from .models import EventEntryTemplate, EventEntryTemplateLine, PieceComptable, TauxChange


def _devise_de_la_regle(regle: str, contexte: dict) -> str:
    """Traduit `devise_regle` en devise concrète, à partir du contexte de l'événement."""
    if regle == EventEntryTemplateLine.DeviseRegle.OPERATION:
        devise = contexte.get("devise")
        cle = "devise"
    elif regle == EventEntryTemplateLine.DeviseRegle.SOURCE:
        devise = contexte.get("devise_source") or contexte.get("devise")
        cle = "devise_source"
    elif regle == EventEntryTemplateLine.DeviseRegle.CIBLE:
        devise = contexte.get("devise_cible")
        cle = "devise_cible"
    else:
        return regle  # FC ou USD figés par le schéma
    if not devise:
        raise ValidationFailed(f"Contexte incomplet : « {cle} » est requis par ce schéma.")
    return devise


def _compte_de_la_ligne(ligne: EventEntryTemplateLine, contexte: dict) -> str:
    """Résout un placeholder `$…` depuis le contexte, ou renvoie la racine littérale."""
    reference = ligne.compte_racine
    if not reference.startswith("$"):
        return reference
    comptes = contexte.get("comptes") or {}
    resolu = comptes.get(reference)
    if not resolu:
        raise ValidationFailed(
            f"Le schéma exige le compte {reference} : fournissez-le dans "
            f"contexte['comptes']['{reference}'] (l'annexe laisse le choix 501/511/53x)."
        )
    return resolu


def construire_lignes(code_evenement: str, contexte: dict) -> list[dict]:
    """Applique un schéma du catalogue et retourne des lignes prêtes pour le moteur.

    Une ligne dont le montant est nul est OMISE (ex. B12 sans rendement) : une ligne à zéro
    n'a pas de sens comptable et violerait la contrainte `acc_ligne_non_nulle`. L'équilibre
    reste garanti par le contrôle par devise en aval.
    """
    template = EventEntryTemplate.objects.filter(code=code_evenement, actif=True).first()
    if template is None:
        raise NotFoundError(
            f"Schéma d'écriture « {code_evenement} » introuvable ou inactif. "
            "Chargez le catalogue avec « manage.py seed_accounting »."
        )

    montants = contexte.get("montants") or {}
    conditions = set(contexte.get("conditions") or ())

    lignes: list[dict] = []
    for ligne_modele in template.lignes.all():
        if ligne_modele.condition and ligne_modele.condition not in conditions:
            continue

        if ligne_modele.montant_ref not in montants:
            raise ValidationFailed(
                f"Schéma {code_evenement} : montant « {ligne_modele.montant_ref} » absent du "
                "contexte."
            )
        montant = services.q2(montants[ligne_modele.montant_ref])
        if montant < 0:
            raise ValidationFailed(
                f"Schéma {code_evenement} : montant « {ligne_modele.montant_ref} » négatif "
                "— inversez le sens du schéma plutôt que le signe du montant."
            )
        if montant == 0:
            continue

        devise = _devise_de_la_regle(ligne_modele.devise_regle, contexte)
        au_debit = ligne_modele.sens == EventEntryTemplateLine.Sens.DEBIT
        lignes.append({
            "compte": _compte_de_la_ligne(ligne_modele, contexte),
            "devise": devise,
            "debit": montant if au_debit else Decimal("0.00"),
            "credit": Decimal("0.00") if au_debit else montant,
            "libelle": ligne_modele.libelle or template.libelle,
            "ordre": len(lignes) + 1,
        })

    if not lignes:
        raise ValidationFailed(
            f"Schéma {code_evenement} : aucune ligne produite (tous les montants sont nuls "
            "ou aucune condition ne s'applique)."
        )
    return lignes


@transaction.atomic
def executer(
    evenements: list[tuple[str, dict]],
    *,
    reference: str,
    date_operation: date_cls,
    journal: str = "",
    libelle: str = "",
    taux_change: TauxChange | None = None,
    origine_type: str = "",
    origine_id: str = "",
    par: str = "",
) -> PieceComptable:
    """Compose UNE pièce indivisible à partir d'un ou plusieurs schémas.

    Plusieurs schémas dans une même pièce, c'est le cas du change (B14 + B15 + B16) : les
    trois jambes forment UNE opération, donc UNE pièce — jamais trois pièces séparées qui
    pourraient exister à moitié.
    """
    if not evenements:
        raise ValidationFailed("Aucun événement à exécuter.")

    lignes: list[dict] = []
    codes: list[str] = []
    journaux: list[str] = []
    for code_evenement, contexte in evenements:
        codes.append(code_evenement)
        template = EventEntryTemplate.objects.filter(code=code_evenement, actif=True).first()
        if template is not None:
            journaux.append(template.journal)
        for ligne in construire_lignes(code_evenement, contexte):
            ligne["ordre"] = len(lignes) + 1
            lignes.append(ligne)

    return services.enregistrer_piece(
        reference=reference,
        date_operation=date_operation,
        journal=journal or (journaux[0] if journaux else ""),
        libelle=libelle,
        evenement="+".join(codes),
        lignes=lignes,
        taux_change=taux_change,
        origine_type=origine_type,
        origine_id=origine_id,
        par=par,
    )


def executer_evenement(code_evenement: str, contexte: dict, **kwargs) -> PieceComptable:
    """Raccourci pour le cas courant : un événement = une pièce."""
    return executer([(code_evenement, contexte)], **kwargs)
