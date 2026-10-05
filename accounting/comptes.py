"""Gouvernance du plan comptable : ouverture d'un compte en maker-checker, désactivation,
et refus définitif de la suppression d'un compte mouvementé (annexe A).

Pourquoi un maker-checker sur un simple compte ? Parce qu'un plan comptable est une grille
de LECTURE : ouvrir « 6185 — frais divers » à côté de « 611 — services extérieurs », c'est
créer l'endroit où les charges non expliquées iront se ranger. Le compte se décide donc à
deux, comme un décaissement.
"""
from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from common.exceptions import ConflictError, NotFoundError, ValidationFailed

from .models import CompteComptable, DemandeCompteComptable, Devise, Nature

CLASSES_AUTORISEES = (1, 2, 3, 4, 5, 6, 7, 8)


def _journaliser(action: str, *, par: str, demande: DemandeCompteComptable) -> None:
    try:
        from audit.services import record as audit_record
    except Exception:  # pragma: no cover - l'app audit est toujours installée
        return
    audit_record(
        actor=par, action=action,
        entity_type="DemandeCompteComptable", entity_id=str(demande.pk),
        details={
            "code": demande.code, "intitule": demande.intitule,
            "classe": demande.classe, "nature": demande.nature,
            "devise": demande.devise, "statut": demande.statut,
            "demande_par": demande.demande_par, "decide_par": demande.decide_par,
        },
    )


@transaction.atomic
def demander_ouverture(
    *,
    code: str,
    intitule: str,
    classe: int,
    nature: str,
    racine: str = "",
    devise: str = "",
    est_transitoire: bool = False,
    cantonnement: str = "",
    parent_code: str = "",
    justification: str,
    par: str,
) -> DemandeCompteComptable:
    """MAKER. Décrit le compte souhaité ; ne crée RIEN au plan comptable."""
    code = (code or "").strip()
    if not code:
        raise ValidationFailed("Le code du compte est obligatoire.")
    if not (justification or "").strip():
        raise ValidationFailed(
            "Justification obligatoire : un compte s'ouvre pour une raison écrite, "
            "opposable au checker."
        )
    if not par:
        raise ValidationFailed("Auteur de la demande inconnu : maker-checker impossible.")
    if nature not in Nature.values:
        raise ValidationFailed(
            f"Nature « {nature} » inconnue (attendu : {', '.join(Nature.values)})."
        )
    if devise and devise not in Devise.values:
        raise ValidationFailed(f"Devise « {devise} » inconnue.")
    if int(classe) not in CLASSES_AUTORISEES:
        raise ValidationFailed(f"Classe « {classe} » hors plan comptable (1 à 8).")
    if CompteComptable.objects.filter(code=code).exists():
        raise ConflictError(f"Le compte {code} existe déjà au plan comptable.")
    if DemandeCompteComptable.objects.filter(
        code=code, statut=DemandeCompteComptable.Statut.EN_ATTENTE
    ).exists():
        raise ConflictError(f"Une demande d'ouverture de {code} est déjà en attente.")
    if parent_code and not CompteComptable.objects.filter(code=parent_code).exists():
        raise NotFoundError(f"Compte parent {parent_code} introuvable.")

    demande = DemandeCompteComptable.objects.create(
        code=code,
        racine=(racine or code).strip(),
        intitule=(intitule or "").strip(),
        classe=int(classe),
        nature=nature,
        devise=devise,
        est_transitoire=bool(est_transitoire),
        cantonnement=(cantonnement or "").strip(),
        parent_code=(parent_code or "").strip(),
        justification=justification.strip(),
        demande_par=par,
    )
    _journaliser("accounting.compte_demande", par=par, demande=demande)
    return demande


@transaction.atomic
def decider_ouverture(
    demande: DemandeCompteComptable, *, approuver: bool, par: str, motif: str = "",
) -> DemandeCompteComptable:
    """CHECKER. Approuver crée le compte ; rejeter laisse une trace motivée."""
    if demande.statut != DemandeCompteComptable.Statut.EN_ATTENTE:
        raise ConflictError(
            f"La demande {demande.code} est déjà {demande.get_statut_display().lower()}."
        )
    if not par:
        raise ValidationFailed("Validation anonyme refusée.")
    if par == demande.demande_par:
        raise ValidationFailed(
            f"Maker ≠ checker : « {par} » a demandé l'ouverture de {demande.code}, "
            "il ne peut pas l'approuver lui-même."
        )
    if not approuver and not (motif or "").strip():
        raise ValidationFailed("Un rejet exige un motif.")

    demande.decide_par = par
    demande.decide_le = timezone.now()
    demande.motif_decision = (motif or "").strip()

    if approuver:
        if CompteComptable.objects.filter(code=demande.code).exists():
            raise ConflictError(
                f"Le compte {demande.code} a été créé entre-temps : demande caduque."
            )
        parent = (
            CompteComptable.objects.filter(code=demande.parent_code).first()
            if demande.parent_code else None
        )
        compte = CompteComptable.objects.create(
            code=demande.code,
            racine=demande.racine,
            intitule=demande.intitule,
            classe=demande.classe,
            nature=demande.nature,
            devise=demande.devise,
            est_transitoire=demande.est_transitoire,
            cantonnement=demande.cantonnement,
            parent=parent,
        )
        demande.compte = compte
        demande.statut = DemandeCompteComptable.Statut.APPROUVEE
    else:
        demande.statut = DemandeCompteComptable.Statut.REJETEE

    demande.save(update_fields=["statut", "decide_par", "decide_le", "motif_decision", "compte"])
    _journaliser(
        "accounting.compte_approuve" if approuver else "accounting.compte_rejete",
        par=par, demande=demande,
    )
    return demande


@transaction.atomic
def basculer_activation(code: str, *, actif: bool, par: str, motif: str) -> CompteComptable:
    """Désactiver un compte = interdire de nouvelles écritures dessus, sans jamais toucher
    aux écritures passées (`services.resoudre_compte` refuse un compte inactif).

    C'est la SEULE façon de « retirer » un compte : la suppression d'un compte mouvementé
    est refusée par le modèle, et la réactivation est tracée comme la désactivation.
    """
    if not (motif or "").strip():
        raise ValidationFailed("Un changement d'activation de compte exige un motif.")
    compte = CompteComptable.objects.filter(code=code).first()
    if compte is None:
        raise NotFoundError(f"Compte {code} introuvable.")
    if compte.actif == actif:
        raise ConflictError(
            f"Le compte {code} est déjà {'actif' if actif else 'désactivé'}."
        )
    compte.actif = actif
    compte.save(update_fields=["actif"])
    try:
        from audit.services import record as audit_record

        audit_record(
            actor=par,
            action="accounting.compte_active" if actif else "accounting.compte_desactive",
            entity_type="CompteComptable", entity_id=compte.code,
            details={"motif": motif.strip(), "mouvemente": compte.lignes.exists()},
        )
    except Exception:  # pragma: no cover
        pass
    return compte
