"""Sérialisation de l'API comptable.

Une seule règle, non négociable : **aucun montant ne quitte le backend en `float`**.
Le rendu JSON de DRF convertit silencieusement un `Decimal` en flottant ; sur des soldes de
grand livre, c'est la porte ouverte à un centime qui apparaît puis disparaît selon l'écran.
Tout montant part donc en CHAÎNE (`"1234.56"`), et le front le formate.

Deuxième règle : ces serializers sont STAFF. Il n'existe aucune vue client de la
comptabilité — un membre n'a rien à faire dans le grand livre de l'institution (principe 7
de MKOPO, anti-gaming). Le garde est posé côté vue (`IsStaff` + capacité), pas ici.
"""
from __future__ import annotations

from decimal import Decimal


def montant(valeur) -> str | None:
    """`Decimal` → chaîne exacte. `None` reste `None` (une absence n'est pas un zéro)."""
    if valeur is None:
        return None
    return str(valeur if isinstance(valeur, Decimal) else Decimal(str(valeur)))


def jour(valeur) -> str | None:
    return valeur.isoformat() if valeur else None


def horodatage(valeur) -> str | None:
    return valeur.isoformat() if valeur else None


# ------------------------------------------------------------------ PLAN COMPTABLE

def compte(obj) -> dict:
    return {
        "code": obj.code,
        "racine": obj.racine,
        "intitule": obj.intitule,
        "classe": obj.classe,
        "nature": obj.nature,
        "devise": obj.devise,
        "estTransitoire": obj.est_transitoire,
        "cantonnement": obj.cantonnement,
        "actif": obj.actif,
        "parent": obj.parent_id,
    }


def demande_compte(obj) -> dict:
    return {
        "id": obj.pk,
        "code": obj.code,
        "racine": obj.racine,
        "intitule": obj.intitule,
        "classe": obj.classe,
        "nature": obj.nature,
        "devise": obj.devise,
        "estTransitoire": obj.est_transitoire,
        "cantonnement": obj.cantonnement,
        "parentCode": obj.parent_code,
        "justification": obj.justification,
        "statut": obj.statut,
        "demandePar": obj.demande_par,
        "demandeLe": horodatage(obj.demande_le),
        "decidePar": obj.decide_par,
        "decideLe": horodatage(obj.decide_le),
        "motifDecision": obj.motif_decision,
        "compte": obj.compte.code if obj.compte_id else None,
    }


# ------------------------------------------------------------------------ PIÈCES

def ligne(obj) -> dict:
    return {
        "id": obj.pk,
        "compte": obj.compte.code,
        "intitule": obj.compte.intitule,
        "devise": obj.devise,
        "debit": montant(obj.debit),
        "credit": montant(obj.credit),
        "libelle": obj.libelle,
        "ordre": obj.ordre,
    }


def piece(obj, *, avec_lignes: bool = True) -> dict:
    donnees = {
        "reference": obj.reference,
        "dateOperation": jour(obj.date_operation),
        "journal": obj.journal,
        "libelle": obj.libelle,
        "statut": obj.statut,
        "evenement": obj.evenement,
        "saisieManuelle": obj.saisie_manuelle,
        "motif": obj.motif,
        "origineType": obj.origine_type,
        "origineId": obj.origine_id,
        "creePar": obj.cree_par,
        "creeLe": horodatage(obj.cree_le),
        "validePar": obj.valide_par,
        "valideLe": horodatage(obj.valide_le),
        "pieceContrepassee": (
            obj.piece_contrepassee.reference if obj.piece_contrepassee_id else None
        ),
        "pieceRectifiee": obj.piece_rectifiee.reference if obj.piece_rectifiee_id else None,
        "tauxChange": taux(obj.taux_change) if obj.taux_change_id else None,
    }
    if avec_lignes:
        lignes = list(obj.lignes.all())
        donnees["lignes"] = [ligne(l) for l in lignes]
        totaux: dict[str, dict] = {}
        for l in lignes:
            bucket = totaux.setdefault(l.devise, {"debit": Decimal("0.00"), "credit": Decimal("0.00")})
            bucket["debit"] += l.debit
            bucket["credit"] += l.credit
        donnees["totaux"] = [
            {"devise": d, "debit": montant(t["debit"]), "credit": montant(t["credit"]),
             "equilibre": t["debit"] == t["credit"]}
            for d, t in sorted(totaux.items())
        ]
    return donnees


def taux(obj) -> dict:
    return {
        "id": obj.pk,
        "dateTaux": jour(obj.date_taux),
        "usage": obj.usage,
        "deviseBase": obj.devise_base,
        "deviseContre": obj.devise_contre,
        "taux": montant(obj.taux),
        "source": obj.source,
        "sourceReference": obj.source_reference,
        "saisiPar": obj.saisi_par,
        "validePar": obj.valide_par,
        "creeLe": horodatage(obj.cree_le),
    }


# -------------------------------------------------------------------- CATALOGUE

def schema(obj) -> dict:
    return {
        "code": obj.code,
        "libelle": obj.libelle,
        "journal": obj.journal,
        "description": obj.description,
        "actif": obj.actif,
        "version": obj.version,
        "lignes": [
            {
                "ordre": l.ordre,
                "sens": l.sens,
                "compteRacine": l.compte_racine,
                "deviseRegle": l.devise_regle,
                "montantRef": l.montant_ref,
                "condition": l.condition,
                "libelle": l.libelle,
            }
            for l in obj.lignes.all()
        ],
    }


# ------------------------------------------------------------------ RESTITUTIONS

def ligne_balance(row: dict) -> dict:
    return {
        "code": row["code"],
        "intitule": row["intitule"],
        "nature": row["nature"],
        "devise": row["devise"],
        "debit": montant(row["debit"]),
        "credit": montant(row["credit"]),
        "solde": montant(row["solde"]),
    }


def mouvement_grand_livre(row: dict) -> dict:
    return {
        "date": jour(row["date"]),
        "reference": row["reference"],
        "journal": row["journal"],
        "evenement": row["evenement"],
        "libelle": row["libelle"],
        "debit": montant(row["debit"]),
        "credit": montant(row["credit"]),
        "solde": montant(row["solde"]),
    }


def grand_livre(donnees: dict) -> dict:
    return {
        "compte": donnees["compte"],
        "devise": donnees["devise"],
        "debut": jour(donnees["debut"]),
        "fin": jour(donnees["fin"]),
        "report": montant(donnees["report"]),
        "mouvements": [mouvement_grand_livre(m) for m in donnees["mouvements"]],
        "totalRows": donnees["total_rows"],
        "totalDebit": montant(donnees["total_debit"]),
        "totalCredit": montant(donnees["total_credit"]),
        "solde": montant(donnees["solde"]),
    }


def journal_auxiliaire(row: dict) -> dict:
    return {
        "journal": row["journal"],
        "libelle": row["libelle"],
        "nombrePieces": row["nombre_pieces"],
        "devises": [
            {"devise": d["devise"], "debit": montant(d["debit"]),
             "credit": montant(d["credit"]), "equilibre": d["equilibre"]}
            for d in row["devises"]
        ],
    }


def anomalie_integrite(row: dict) -> dict:
    return {
        "pieceId": row["piece_id"],
        "reference": row["reference"],
        "devise": row["devise"],
        "debit": montant(row["debit"]),
        "credit": montant(row["credit"]),
        "ecart": montant(row["ecart"]),
    }


def anomalie_fx(row: dict) -> dict:
    return {
        "reference": row["reference"],
        "statut": row["statut"],
        "ageHeures": row["age_heures"],
        "residu": montant(row["residu"]),
        "probleme": row["probleme"],
    }


# ------------------------------------------------------------------- PROVISIONS

def classe_risque(obj) -> dict:
    return {
        "code": obj.code,
        "libelle": obj.libelle,
        "joursMin": obj.jours_min,
        "joursMax": obj.jours_max,
        "tauxProvision": montant(obj.taux_provision),
        "enSouffrance": obj.en_souffrance,
        "ordre": obj.ordre,
        "actif": obj.actif,
        "modifiePar": obj.modifie_par,
        "modifieLe": horodatage(obj.modifie_le),
    }


def credit_classe(row: dict) -> dict:
    return {
        "loanId": row["loan_id"],
        "reference": row["loan_reference"],
        "operateur": row["operateur"],
        "statutPortefeuille": row["statut_portefeuille"],
        "devise": row["devise"],
        "decaisse": montant(row["decaisse"]),
        "regle": montant(row["regle"]),
        "capitalRembourse": montant(row["capital_rembourse"]),
        "encours": montant(row["encours"]),
        "joursRetard": row["jours_retard"],
        "premiereEcheanceImpayee": jour(row["premiere_echeance_impayee"]),
        "classe": row["classe"].code,
        "tauxProvision": montant(row["taux_provision"]),
        "provision": montant(row["provision"]),
        "enSouffrance": row["en_souffrance"],
        "anomalies": row["anomalies"],
    }


def synthese_provision(row: dict) -> dict:
    return {
        "devise": row["devise"],
        "nombreCredits": row["nombre_credits"],
        "encoursTotal": montant(row["encours_total"]),
        "provisionRequise": montant(row["provision_requise"]),
        "provisionComptabilisee": montant(row["provision_comptabilisee"]),
        "encoursComptable": montant(row["encours_comptable"]),
        "encoursARisque30j": montant(row["encours_a_risque_30j"]),
        "par30Ratio": montant(row["par30_ratio"]),
        "lignes": [
            {
                "classe": l["classe"],
                "libelle": l["libelle"],
                "tauxProvision": montant(l["taux_provision"]),
                "nombre": l["nombre"],
                "encours": montant(l["encours"]),
                "provision": montant(l["provision"]),
            }
            for l in row["lignes"]
        ],
    }


def classification(donnees: dict) -> dict:
    return {
        "asOf": jour(donnees["as_of"]),
        "grille": [classe_risque(c) for c in donnees["grille"]],
        "credits": [credit_classe(c) for c in donnees["credits"]],
        "totalRows": donnees["total_rows"],
        "synthese": [synthese_provision(s) for s in donnees["synthese"]],
        "anomalies": donnees["anomalies"],
    }


def arrete_provision(obj) -> dict:
    return {
        "id": obj.pk,
        "dateArrete": jour(obj.date_arrete),
        "devise": obj.devise,
        "provisionRequise": montant(obj.provision_requise),
        "provisionAnterieure": montant(obj.provision_anterieure),
        "dotation": montant(obj.dotation),
        "reprise": montant(obj.reprise),
        "encoursPortefeuille": montant(obj.encours_portefeuille),
        "encoursComptable": montant(obj.encours_comptable),
        "nombreCredits": obj.nombre_credits,
        "piece": obj.piece.reference if obj.piece_id else None,
        "creePar": obj.cree_par,
        "creeLe": horodatage(obj.cree_le),
        "lignes": [
            {
                "classe": l.classe.code,
                "nombreCredits": l.nombre_credits,
                "encours": montant(l.encours),
                "tauxApplique": montant(l.taux_applique),
                "provision": montant(l.provision),
            }
            for l in obj.lignes.all()
        ],
    }


def classement_credit(obj) -> dict:
    return {
        "id": obj.pk,
        "dateArrete": jour(obj.date_arrete),
        "loanId": obj.loan_id,
        "reference": obj.loan_reference,
        "classe": obj.classe.code,
        "joursRetard": obj.jours_retard,
        "encours": montant(obj.encours),
        "devise": obj.devise,
        "enSouffrance": obj.en_souffrance,
        "pieceDeclassement": (
            obj.piece_declassement.reference if obj.piece_declassement_id else None
        ),
        "pieceReclassement": (
            obj.piece_reclassement.reference if obj.piece_reclassement_id else None
        ),
        "creePar": obj.cree_par,
        "creeLe": horodatage(obj.cree_le),
    }


def resultat_arrete(donnees: dict) -> dict:
    return {
        "dateArrete": jour(donnees["date_arrete"]),
        "declassements": [
            {
                "reference": d["loan_reference"],
                "devise": d["devise"],
                "encours": montant(d["encours"]),
                "joursRetard": d["jours_retard"],
                "classe": d["classe"],
                "piece": d["piece"],
            }
            for d in donnees["declassements"]
        ],
        "reclassements": [
            {
                "reference": r["loan_reference"],
                "devise": r["devise"],
                "encoursReclasse": montant(r["encours_reclasse"]),
                "encoursCourant": montant(r["encours_courant"]),
                "joursRetard": r["jours_retard"],
                "classe": r["classe"],
                "piece": r["piece"],
            }
            for r in donnees.get("reclassements", [])
        ],
        "arretes": [
            {
                "devise": a["devise"],
                "provisionRequise": montant(a["provision_requise"]),
                "provisionAnterieure": montant(a["provision_anterieure"]),
                "dotation": montant(a["dotation"]),
                "reprise": montant(a["reprise"]),
                "piece": a["piece"],
                "encoursPortefeuille": montant(a["encours_portefeuille"]),
                "encoursComptable": montant(a["encours_comptable"]),
                "ecartEncours": montant(a["ecart_encours"]),
            }
            for a in donnees["arretes"]
        ],
        "anomalies": donnees["anomalies"],
    }


# ------------------------------------------------------------ ÉTATS FINANCIERS

def _poste(row: dict) -> dict:
    return {
        "code": row["code"],
        "intitule": row["intitule"],
        "nature": row["nature"],
        "debit": montant(row["debit"]),
        "credit": montant(row["credit"]),
        "soldeSigne": montant(row["solde_signe"]),
        "montant": montant(row["montant"]),
    }


def bilan(donnees: dict) -> dict:
    return {
        "devise": donnees["devise"],
        "asOf": jour(donnees["as_of"]),
        "actif": [_poste(p) for p in donnees["actif"]],
        "passif": [_poste(p) for p in donnees["passif"]],
        "totalActif": montant(donnees["total_actif"]),
        "totalPassif": montant(donnees["total_passif"]),
        "resultatExercice": montant(donnees["resultat_exercice"]),
        "totalPassifEtResultat": montant(donnees["total_passif_et_resultat"]),
        "ecartBouclage": montant(donnees["ecart_bouclage"]),
        "boucle": donnees["boucle"],
        "avertissements": donnees["avertissements"],
    }


def compte_de_resultat(donnees: dict) -> dict:
    return {
        "devise": donnees["devise"],
        "asOf": jour(donnees["as_of"]),
        "charges": [_poste(p) for p in donnees["charges"]],
        "produits": [_poste(p) for p in donnees["produits"]],
        "totalCharges": montant(donnees["total_charges"]),
        "totalProduits": montant(donnees["total_produits"]),
        "resultat": montant(donnees["resultat"]),
    }


def etats_consolides(donnees: dict) -> dict:
    taux_cloture = donnees["taux_cloture"]
    return {
        "asOf": jour(donnees["as_of"]),
        "tauxCloture": {
            "id": taux_cloture["id"],
            "dateTaux": jour(taux_cloture["date_taux"]),
            "usage": taux_cloture["usage"],
            "deviseBase": taux_cloture["devise_base"],
            "deviseContre": taux_cloture["devise_contre"],
            "taux": montant(taux_cloture["taux"]),
            "source": taux_cloture["source"],
            "sourceReference": taux_cloture["source_reference"],
            "provenance": taux_cloture["provenance"],
        },
        "parDevise": {
            devise: {
                "bilan": bilan(etats["bilan"]),
                "resultat": compte_de_resultat(etats["resultat"]),
            }
            for devise, etats in donnees["par_devise"].items()
        },
        "consolide": {
            "devisePivot": donnees["consolide"]["devise_pivot"],
            "totalActif": montant(donnees["consolide"]["total_actif"]),
            "totalPassif": montant(donnees["consolide"]["total_passif"]),
            "totalCharges": montant(donnees["consolide"]["total_charges"]),
            "totalProduits": montant(donnees["consolide"]["total_produits"]),
            "resultat": montant(donnees["consolide"]["resultat"]),
            "totalPassifEtResultat": montant(donnees["consolide"]["total_passif_et_resultat"]),
            "ecartBouclage": montant(donnees["consolide"]["ecart_bouclage"]),
            "boucle": donnees["consolide"]["boucle"],
        },
        "avertissements": donnees["avertissements"],
    }
