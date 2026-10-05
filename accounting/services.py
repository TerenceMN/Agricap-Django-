"""Moteur d'écritures — SEUL chemin d'écriture comptable autorisé.

Toute opération métier passe par `enregistrer_piece` (directement, ou via
`catalogue.executer` qui applique un schéma de l'annexe B). Aucune app ne construit de
`LigneEcriture` à la main.

L'invariant central est vérifié ici, AVANT persistance : Σ débits = Σ crédits **par devise**.
Une pièce déséquilibrée lève `ValidationFailed` à l'intérieur d'un `transaction.atomic`,
donc rollback — jamais un warning.
"""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from datetime import date as date_cls

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from common.exceptions import NotFoundError, ValidationFailed
from common.parsing import to_decimal

from .models import (
    CompteComptable,
    Devise,
    LigneEcriture,
    PieceComptable,
    TauxChange,
)

CENTIME = Decimal("0.01")


def q2(valeur) -> Decimal:
    """Quantize monétaire du projet : 2 décimales, ROUND_HALF_UP (principe 4)."""
    return to_decimal(valeur).quantize(CENTIME, rounding=ROUND_HALF_UP)


# --------------------------------------------------------------------------- COMPTES

def resoudre_compte(reference: str, devise: str) -> CompteComptable:
    """Résout une référence de compte (code complet OU racine de l'annexe) pour une devise.

    « 413 » + FC → 413FC ; « 613FX » + FC → 613FX (compte non dédoublé) ; « 501USD » → tel quel.
    """
    if not reference:
        raise ValidationFailed("Référence de compte vide.")
    if reference.startswith("$"):
        raise ValidationFailed(
            f"Placeholder {reference} non résolu : l'appelant doit fournir le compte réel."
        )

    candidats = [f"{reference}{devise}", reference]
    for code in candidats:
        compte = CompteComptable.objects.filter(code=code).first()
        if compte is not None:
            if not compte.actif:
                raise ValidationFailed(f"Le compte {compte.code} est désactivé : écriture refusée.")
            if not compte.accepte_devise(devise):
                raise ValidationFailed(
                    f"Le compte {compte.code} n'accepte pas la devise {devise} "
                    f"(devise du compte : {compte.devise})."
                )
            return compte
    raise NotFoundError(
        f"Compte introuvable dans le plan comptable pour « {reference} » en {devise} "
        f"(codes essayés : {', '.join(candidats)})."
    )


def creer_sous_compte_cantonnement(*, offre_ref: str, devises=("FC", "USD")) -> list[CompteComptable]:
    """Crée (idempotent) les sous-comptes de cantonnement 419-OFF-<offre> d'une offre.

    Principe 9 : les fonds levés pour un projet sont cantonnés — on doit pouvoir prouver que
    l'argent du projet X n'a pas financé le projet Y.
    """
    if not offre_ref:
        raise ValidationFailed("Référence d'offre obligatoire pour un compte de cantonnement.")
    racine = f"419-OFF-{offre_ref}"
    parents = {c.devise: c for c in CompteComptable.objects.filter(racine="419")}
    comptes = []
    for devise in devises:
        compte, _ = CompteComptable.objects.get_or_create(
            code=f"{racine}{devise}",
            defaults={
                "racine": racine,
                "intitule": f"Souscriptions investisseurs — offre {offre_ref}",
                "classe": 4,
                "nature": "PASSIF",
                "devise": devise,
                "cantonnement": offre_ref,
                "parent": parents.get(devise),
            },
        )
        comptes.append(compte)
    return comptes


# --------------------------------------------------------------------------- INVARIANT

def equilibre_par_devise(lignes: list[dict]) -> dict[str, dict[str, Decimal]]:
    """Totaux débit/crédit ventilés PAR DEVISE. Aucune conversion : deux devises ne
    s'additionnent jamais."""
    totaux: dict[str, dict[str, Decimal]] = {}
    for ligne in lignes:
        devise = ligne["devise"]
        bucket = totaux.setdefault(devise, {"debit": Decimal("0.00"), "credit": Decimal("0.00")})
        bucket["debit"] += q2(ligne.get("debit"))
        bucket["credit"] += q2(ligne.get("credit"))
    return totaux


def verifier_equilibre(lignes: list[dict]) -> dict[str, dict[str, Decimal]]:
    """INVARIANT MACHINE. Lève `ValidationFailed` en listant CHAQUE devise déséquilibrée.

    Note : un total global équilibré ne suffit pas. 100 FC au débit contre 100 USD au crédit
    est un déséquilibre — c'est précisément ce que le contrôle par devise attrape.
    """
    if not lignes:
        raise ValidationFailed("Une pièce comptable doit comporter au moins une ligne.")

    totaux = equilibre_par_devise(lignes)
    ecarts = [
        f"{devise} : débit {t['debit']} ≠ crédit {t['credit']} (écart {t['debit'] - t['credit']})"
        for devise, t in sorted(totaux.items())
        if t["debit"] != t["credit"]
    ]
    if ecarts:
        raise ValidationFailed("Pièce déséquilibrée — " + " ; ".join(ecarts) + ".")

    if all(t["debit"] == 0 for t in totaux.values()):
        raise ValidationFailed("Une pièce ne peut pas être intégralement nulle.")
    return totaux


# --------------------------------------------------------------------------- ÉCRITURES

def _normaliser_lignes(lignes: list[dict]) -> list[dict]:
    """Valide la forme de chaque ligne et résout les comptes. Retourne des lignes prêtes."""
    normalisees = []
    for index, brute in enumerate(lignes, start=1):
        devise = (brute.get("devise") or "").upper()
        if devise not in Devise.values:
            raise ValidationFailed(
                f"Ligne {index} : devise « {brute.get('devise')} » inconnue "
                f"(attendu : {', '.join(Devise.values)})."
            )
        debit = q2(brute.get("debit"))
        credit = q2(brute.get("credit"))
        if debit < 0 or credit < 0:
            raise ValidationFailed(f"Ligne {index} : montants négatifs interdits.")
        if debit > 0 and credit > 0:
            raise ValidationFailed(f"Ligne {index} : une ligne est au débit OU au crédit, pas les deux.")
        if debit == 0 and credit == 0:
            raise ValidationFailed(f"Ligne {index} : une ligne d'écriture ne peut pas être nulle.")

        compte = brute.get("compte_obj") or resoudre_compte(brute["compte"], devise)
        normalisees.append({
            "compte_obj": compte,
            "devise": devise,
            "debit": debit,
            "credit": credit,
            "libelle": brute.get("libelle", ""),
            "ordre": brute.get("ordre", index),
        })
    return normalisees


@transaction.atomic
def enregistrer_piece(
    *,
    reference: str,
    date_operation: date_cls,
    journal: str,
    lignes: list[dict],
    libelle: str = "",
    evenement: str = "",
    taux_change: TauxChange | None = None,
    origine_type: str = "",
    origine_id: str = "",
    par: str = "",
    valider: bool = True,
    piece_contrepassee: PieceComptable | None = None,
    piece_rectifiee: PieceComptable | None = None,
    motif: str = "",
    saisie_manuelle: bool = False,
) -> PieceComptable:
    """Crée UNE pièce et ses n lignes, de façon indivisible.

    L'équilibre par devise est vérifié AVANT toute persistance ; l'ensemble est dans un
    `transaction.atomic`, donc toute erreur annule la pièce entière.

    `saisie_manuelle=True` marque une OD : elle naît OBLIGATOIREMENT en brouillon et sa
    validation exigera un checker distinct du maker.
    """
    if saisie_manuelle:
        if valider:
            raise ValidationFailed(
                "Une OD ne s'auto-valide pas : elle naît en brouillon et un checker "
                "distinct du maker la valide (services.valider_piece)."
            )
        if not par:
            raise ValidationFailed(
                "Une OD sans auteur identifié est ingérable : le maker doit être connu "
                "pour qu'un checker puisse lui être opposé."
            )
    normalisees = _normaliser_lignes(lignes)
    totaux = verifier_equilibre(normalisees)

    if len(totaux) > 1 and taux_change is None:
        raise ValidationFailed(
            "Pièce multi-devises sans taux de change journalisé : "
            "aucune opération mixte FC/USD ne peut être enregistrée sans son taux "
            "(principes 4 et 5)."
        )

    if PieceComptable.objects.filter(reference=reference).exists():
        raise ValidationFailed(f"La pièce « {reference} » existe déjà.")

    piece = PieceComptable.objects.create(
        reference=reference,
        date_operation=date_operation,
        journal=journal,
        libelle=libelle,
        evenement=evenement,
        taux_change=taux_change,
        origine_type=origine_type,
        origine_id=origine_id,
        cree_par=par,
        piece_contrepassee=piece_contrepassee,
        piece_rectifiee=piece_rectifiee,
        motif=motif,
        saisie_manuelle=saisie_manuelle,
        statut=PieceComptable.Statut.BROUILLON,
    )
    LigneEcriture.objects.bulk_create([
        LigneEcriture(
            piece=piece,
            compte=ligne["compte_obj"],
            devise=ligne["devise"],
            debit=ligne["debit"],
            credit=ligne["credit"],
            libelle=ligne["libelle"],
            ordre=ligne["ordre"],
        )
        for ligne in normalisees
    ])

    if valider:
        valider_piece(piece, par=par)
    else:
        _journaliser(piece, action="accounting.piece_brouillon", par=par, totaux=totaux)
    return piece


@transaction.atomic
def valider_piece(piece: PieceComptable, *, par: str = "") -> PieceComptable:
    """`BROUILLON → VALIDEE`. Re-vérifie l'équilibre sur les lignes RÉELLEMENT en base.

    Sur une OD (`saisie_manuelle`), le checker doit être DIFFÉRENT du maker : c'est le
    seul contrôle qui empêche une écriture manuelle d'entrer au grand livre par la seule
    volonté de son auteur.
    """
    if piece.statut == PieceComptable.Statut.VALIDEE:
        raise ValidationFailed(f"La pièce {piece.reference} est déjà validée.")

    if piece.saisie_manuelle:
        if not par:
            raise ValidationFailed(
                "Validation anonyme refusée : une OD se valide sous une identité "
                "opposable au maker."
            )
        if par == piece.cree_par:
            raise ValidationFailed(
                f"Maker ≠ checker : « {par} » a saisi la pièce {piece.reference}, "
                "il ne peut pas la valider lui-même."
            )

    lignes = [
        {"devise": l.devise, "debit": l.debit, "credit": l.credit}
        for l in piece.lignes.all()
    ]
    totaux = verifier_equilibre(lignes)

    piece.statut = PieceComptable.Statut.VALIDEE
    piece.valide_par = par
    piece.valide_le = timezone.now()
    piece.save(update_fields=["statut", "valide_par", "valide_le"])
    _journaliser(piece, action="accounting.piece_validee", par=par, totaux=totaux)
    return piece


@transaction.atomic
def contrepasser_piece(
    piece: PieceComptable,
    *,
    motif: str,
    par: str = "",
    lignes_rectificatives: list[dict] | None = None,
    reference_contrepassation: str = "",
    reference_rectification: str = "",
    date_operation: date_cls | None = None,
) -> tuple[PieceComptable, PieceComptable | None]:
    """On ne modifie JAMAIS une écriture validée : on contrepasse.

    Produit la pièce INVERSE (débits et crédits permutés, devises conservées) et, si des
    lignes rectificatives sont fournies, la pièce CORRIGÉE. Les trois pièces restent liées :
    inverse.piece_contrepassee = origine, corrigée.piece_rectifiee = origine.
    """
    if not motif or not motif.strip():
        raise ValidationFailed("Un motif est obligatoire pour contrepasser une pièce.")
    if piece.statut != PieceComptable.Statut.VALIDEE:
        raise ValidationFailed(
            "Seule une pièce validée se contrepasse ; une pièce en brouillon se corrige "
            "avant validation."
        )
    if piece.contrepassations.exists():
        raise ValidationFailed(f"La pièce {piece.reference} a déjà été contrepassée.")

    date_op = date_operation or piece.date_operation
    ref_cp = reference_contrepassation or f"{piece.reference}-CP"

    inverse = enregistrer_piece(
        reference=ref_cp,
        date_operation=date_op,
        journal=piece.journal,
        libelle=f"Contrepassation de {piece.reference}",
        evenement=piece.evenement,
        taux_change=piece.taux_change,
        origine_type=piece.origine_type,
        origine_id=piece.origine_id,
        par=par,
        motif=motif,
        piece_contrepassee=piece,
        lignes=[
            {
                "compte_obj": l.compte,
                "compte": l.compte.code,
                "devise": l.devise,
                "debit": l.credit,
                "credit": l.debit,
                "libelle": l.libelle,
                "ordre": l.ordre,
            }
            for l in piece.lignes.all()
        ],
    )

    rectification = None
    if lignes_rectificatives:
        rectification = enregistrer_piece(
            reference=reference_rectification or f"{piece.reference}-RECT",
            date_operation=date_op,
            journal=piece.journal,
            libelle=f"Rectification de {piece.reference}",
            evenement=piece.evenement,
            taux_change=piece.taux_change,
            origine_type=piece.origine_type,
            origine_id=piece.origine_id,
            par=par,
            motif=motif,
            piece_rectifiee=piece,
            lignes=lignes_rectificatives,
        )
    return inverse, rectification


# --------------------------------------------------------------------------- LECTURES

def _lignes_validees(*, as_of: date_cls | None = None):
    qs = LigneEcriture.objects.filter(
        piece__statut=PieceComptable.Statut.VALIDEE
    ).select_related("compte", "piece")
    if as_of:
        qs = qs.filter(piece__date_operation__lte=as_of)
    return qs


def solde_compte(reference: str, *, devise: str, as_of: date_cls | None = None) -> Decimal:
    """Solde SIGNÉ (débit − crédit) d'un compte dans UNE devise. Jamais d'agrégation
    multi-devises silencieuse : la devise est un argument obligatoire."""
    total = Decimal("0.00")
    for ligne in _lignes_validees(as_of=as_of).filter(
        compte__code__in=[f"{reference}{devise}", reference], devise=devise
    ):
        total += ligne.debit - ligne.credit
    return q2(total)


def balance_par_devise(*, devise: str, as_of: date_cls | None = None) -> list[dict]:
    """Balance générale d'UNE devise. `Decimal` conservé de bout en bout."""
    totaux: dict[str, dict] = {}
    for ligne in _lignes_validees(as_of=as_of).filter(devise=devise):
        row = totaux.setdefault(ligne.compte.code, {
            "code": ligne.compte.code,
            "intitule": ligne.compte.intitule,
            "nature": ligne.compte.nature,
            "devise": devise,
            "debit": Decimal("0.00"),
            "credit": Decimal("0.00"),
        })
        row["debit"] += ligne.debit
        row["credit"] += ligne.credit
    rows = []
    for row in sorted(totaux.values(), key=lambda r: r["code"]):
        row["solde"] = q2(row["debit"] - row["credit"])
        rows.append(row)
    return rows


def grand_livre(
    *,
    reference_compte: str,
    devise: str,
    debut: date_cls | None = None,
    fin: date_cls | None = None,
) -> dict:
    """Grand livre d'un compte dans UNE devise, avec report à nouveau et solde progressif.

    Le « report » n'est pas décoratif : sans lui, un extrait de période ne se rapproche
    d'aucun solde et n'a aucune valeur probante.
    """
    if devise not in Devise.values:
        raise ValidationFailed(f"Devise « {devise} » inconnue.")
    codes = [f"{reference_compte}{devise}", reference_compte]
    compte = CompteComptable.objects.filter(code__in=codes).order_by("-code").first()
    if compte is None:
        raise NotFoundError(f"Compte « {reference_compte} » introuvable en {devise}.")

    report = Decimal("0.00")
    if debut:
        for ligne in _lignes_validees().filter(
            compte=compte, devise=devise, piece__date_operation__lt=debut
        ):
            report += ligne.debit - ligne.credit
    report = q2(report)

    qs = _lignes_validees(as_of=fin).filter(compte=compte, devise=devise)
    if debut:
        qs = qs.filter(piece__date_operation__gte=debut)
    qs = qs.order_by("piece__date_operation", "piece_id", "ordre", "id")

    solde = report
    mouvements = []
    total_debit = total_credit = Decimal("0.00")
    for ligne in qs:
        solde += ligne.debit - ligne.credit
        total_debit += ligne.debit
        total_credit += ligne.credit
        mouvements.append({
            "date": ligne.piece.date_operation,
            "reference": ligne.piece.reference,
            "journal": ligne.piece.journal,
            "evenement": ligne.piece.evenement,
            "libelle": ligne.libelle or ligne.piece.libelle,
            "debit": ligne.debit,
            "credit": ligne.credit,
            "solde": q2(solde),
        })
    return {
        "compte": {"code": compte.code, "intitule": compte.intitule,
                   "nature": compte.nature, "racine": compte.racine},
        "devise": devise,
        "debut": debut,
        "fin": fin,
        "report": report,
        "mouvements": mouvements,
        "total_rows": len(mouvements),
        "total_debit": q2(total_debit),
        "total_credit": q2(total_credit),
        "solde": q2(solde),
    }


def journaux_auxiliaires(
    *, debut: date_cls | None = None, fin: date_cls | None = None,
) -> list[dict]:
    """Un journal auxiliaire par code, avec ses totaux PAR DEVISE.

    Invariant vérifiable : Σ des journaux auxiliaires = grand livre (test dédié).
    """
    from .models import Journal

    totaux: dict[str, dict] = {
        code: {
            "journal": code,
            "libelle": libelle,
            "nombre_pieces": 0,
            "devises": {},
        }
        for code, libelle in Journal.choices
    }
    qs = _lignes_validees(as_of=fin)
    if debut:
        qs = qs.filter(piece__date_operation__gte=debut)

    pieces_vues: dict[str, set] = {}
    for ligne in qs:
        journal = ligne.piece.journal
        bucket = totaux.setdefault(
            journal, {"journal": journal, "libelle": journal, "nombre_pieces": 0, "devises": {}},
        )
        pieces_vues.setdefault(journal, set()).add(ligne.piece_id)
        devise = bucket["devises"].setdefault(
            ligne.devise, {"devise": ligne.devise,
                           "debit": Decimal("0.00"), "credit": Decimal("0.00")},
        )
        devise["debit"] += ligne.debit
        devise["credit"] += ligne.credit

    resultat = []
    for code, bucket in totaux.items():
        bucket["nombre_pieces"] = len(pieces_vues.get(code, ()))
        bucket["devises"] = [
            {**d, "debit": q2(d["debit"]), "credit": q2(d["credit"]),
             "equilibre": q2(d["debit"]) == q2(d["credit"])}
            for d in sorted(bucket["devises"].values(), key=lambda d: d["devise"])
        ]
        resultat.append(bucket)
    return sorted(resultat, key=lambda b: b["journal"])


def rechercher_pieces(
    *,
    debut: date_cls | None = None,
    fin: date_cls | None = None,
    journal: str = "",
    statut: str = "",
    evenement: str = "",
    reference: str = "",
    compte: str = "",
    devise: str = "",
    origine_type: str = "",
    origine_id: str = "",
):
    """Requête filtrée sur les pièces. Retourne un queryset (la vue pagine et compte)."""
    qs = PieceComptable.objects.all()
    if debut:
        qs = qs.filter(date_operation__gte=debut)
    if fin:
        qs = qs.filter(date_operation__lte=fin)
    if journal:
        qs = qs.filter(journal=journal)
    if statut:
        qs = qs.filter(statut=statut)
    if evenement:
        qs = qs.filter(evenement__icontains=evenement)
    if reference:
        qs = qs.filter(reference__icontains=reference)
    if compte:
        # « 413 » (racine) ou « 413USD » (code exact) doivent tous deux fonctionner :
        # l'utilisateur qui cherche « 413 » veut les deux devises.
        qs = qs.filter(Q(lignes__compte__code=compte) | Q(lignes__compte__racine=compte))
    if devise:
        qs = qs.filter(lignes__devise=devise)
    if origine_type:
        qs = qs.filter(origine_type=origine_type)
    if origine_id:
        qs = qs.filter(origine_id=origine_id)
    return qs.distinct()


def controler_integrite(*, as_of: date_cls | None = None) -> list[dict]:
    """Contrôle global rejouable : liste TOUTE pièce validée déséquilibrée sur une devise.

    C'est le filet qui compense l'impossibilité d'exprimer un invariant agrégé cross-lignes
    en `CheckConstraint` SQL. Doit toujours retourner une liste vide.
    """
    par_piece: dict[int, list[dict]] = {}
    references: dict[int, str] = {}
    for ligne in _lignes_validees(as_of=as_of):
        par_piece.setdefault(ligne.piece_id, []).append(
            {"devise": ligne.devise, "debit": ligne.debit, "credit": ligne.credit}
        )
        references[ligne.piece_id] = ligne.piece.reference

    anomalies = []
    for piece_id, lignes in par_piece.items():
        for devise, totaux in equilibre_par_devise(lignes).items():
            if totaux["debit"] != totaux["credit"]:
                anomalies.append({
                    "piece_id": piece_id,
                    "reference": references[piece_id],
                    "devise": devise,
                    "debit": totaux["debit"],
                    "credit": totaux["credit"],
                    "ecart": totaux["debit"] - totaux["credit"],
                })
    return anomalies


# --------------------------------------------------------------------------- AUDIT

def _journaliser(piece: PieceComptable, *, action: str, par: str, totaux: dict) -> None:
    """Journalisation append-only via l'app `audit` (import différé : l'audit ne doit jamais
    faire échouer une écriture comptable correcte)."""
    try:
        from audit.services import record as audit_record
    except Exception:  # pragma: no cover - l'app audit est toujours installée en pratique
        return
    audit_record(
        actor=par,
        action=action,
        entity_type="PieceComptable",
        entity_id=str(piece.pk),
        details={
            "reference": piece.reference,
            "journal": piece.journal,
            "evenement": piece.evenement,
            "totaux": {d: str(t["debit"]) for d, t in totaux.items()},
        },
    )
