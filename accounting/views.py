"""API REST du socle comptable AGRICAP.

Le moteur d'écritures existait (`services`, `catalogue`, `fx`) et était couvert par des
tests, mais il était INJOIGNABLE : aucune vue ne l'exposait. Ce module est la porte — et
uniquement la porte : il ne contient aucune règle comptable, il valide des entrées, appelle
les services et sérialise. Toute logique métier qui apparaîtrait ici serait à déplacer.

Permissions
-----------
Deux gardes cumulés sur CHAQUE vue, sans exception :

* `IsStaff` — la comptabilité n'a aucune vue client. Attention : `HasCapability("read")`
  seul ne suffit PAS, car les rôles clients (`client`, `agri_op`, `invest`) portent
  `read=True` dans le registre RBAC ; sans `IsStaff`, un investisseur lirait le grand livre
  de l'institution (principe 7 de MKOPO).
* une capacité explicite : `read` (consultation), `create` (maker), `validate` (checker /
  actes comptables), `config` (paramétrage : plan comptable, grille PAR).

Maker ≠ checker
---------------
Il est appliqué DANS LES SERVICES (`services.valider_piece`,
`comptes.decider_ouverture`), pas ici : une vue peut être contournée, un service non.
"""
from __future__ import annotations

from datetime import date as date_cls
from decimal import Decimal

from django.db.models import Q
from django.utils import timezone
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response

from accounts.permissions import IsStaff
from common.exceptions import ConflictError, NotFoundError, ValidationFailed
from common.parsing import to_date, to_int

from . import comptes as comptes_service
from . import etats, fx, provisions, serializers, services
from .models import (
    ArreteProvision,
    CompteComptable,
    DemandeCompteComptable,
    Devise,
    EventEntryTemplate,
    Journal,
    PieceComptable,
    TauxChange,
)
from .permissions import LIRE, PARAMETRER, SAISIR, VALIDER, CapaciteSelonMethode

#: Une saisie manuelle est cantonnée au journal des opérations diverses. Autoriser une OD
#: sur JCR ou JEP reviendrait à permettre d'écrire un décaissement de crédit à la main —
#: exactement ce que le catalogue d'événements existe pour empêcher (principe 1 de HAZINA).
JOURNAL_DES_OD = Journal.JOD

MAX_PAGE = 200


def _acteur(request) -> str:
    return getattr(request.user, "sub", "") or ""


def _pagination(request) -> tuple[int, int]:
    limite = min(to_int(request.GET.get("limit"), 50) or 50, MAX_PAGE)
    depuis = max(to_int(request.GET.get("offset"), 0), 0)
    return limite, depuis


def _devise_requise(request, *, defaut: str = "") -> str:
    devise = (request.GET.get("devise") or defaut).upper()
    if devise not in Devise.values:
        raise ValidationFailed(
            f"Paramètre `devise` obligatoire et valide ({', '.join(Devise.values)}) : "
            "un montant sans devise n'existe pas."
        )
    return devise


# ============================================================== PLAN COMPTABLE

@api_view(["GET"])
@permission_classes(LIRE)
def comptes(request):
    qs = CompteComptable.objects.all()
    if request.GET.get("classe"):
        qs = qs.filter(classe=to_int(request.GET["classe"]))
    if request.GET.get("devise"):
        qs = qs.filter(devise=request.GET["devise"].upper())
    if request.GET.get("nature"):
        qs = qs.filter(nature=request.GET["nature"].upper())
    if request.GET.get("actif") in ("true", "false"):
        qs = qs.filter(actif=request.GET["actif"] == "true")
    if request.GET.get("cantonnement"):
        qs = qs.filter(cantonnement=request.GET["cantonnement"])
    recherche = (request.GET.get("q") or "").strip()
    if recherche:
        qs = qs.filter(Q(code__icontains=recherche) | Q(intitule__icontains=recherche))
    total = qs.count()
    limite, depuis = _pagination(request)
    return Response({
        "results": [serializers.compte(c) for c in qs[depuis:depuis + limite]],
        "total_rows": total,
        "limit": limite,
        "offset": depuis,
    })


@api_view(["GET"])
@permission_classes(LIRE)
def compte_detail(request, code):
    compte = CompteComptable.objects.filter(code=code).first()
    if compte is None:
        raise NotFoundError(f"Compte {code} introuvable.")
    donnees = serializers.compte(compte)
    donnees["mouvemente"] = compte.lignes.exists()
    donnees["soldes"] = [
        {"devise": devise,
         "solde": serializers.montant(services.solde_compte(compte.code, devise=devise))}
        for devise in ([compte.devise] if compte.devise else list(Devise.values))
    ]
    return Response(donnees)


@api_view(["DELETE"])
@permission_classes(PARAMETRER)
def compte_suppression(request, code):
    """Endpoint volontairement présent pour répondre 409 : la suppression d'un compte
    n'existe pas en comptabilité. Sans cette vue, un client HTTP recevrait un 404
    ambigu — ici, il reçoit la règle."""
    raise ConflictError(
        f"La suppression du compte {code} n'existe pas : la comptabilité est append-only. "
        "Désactivez-le (POST comptes/<code>/activation) — il restera consultable et "
        "auditable, mais ne sera plus postable."
    )


@api_view(["POST"])
@permission_classes(PARAMETRER)
def compte_activation(request, code):
    data = request.data or {}
    compte = comptes_service.basculer_activation(
        code, actif=bool(data.get("actif")), par=_acteur(request),
        motif=data.get("motif", ""),
    )
    return Response(serializers.compte(compte))


@api_view(["GET", "POST"])
@permission_classes([IsStaff, CapaciteSelonMethode(GET="read", POST="create")])
def demandes_compte(request):
    """GET : la file des demandes (`read`). POST : le MAKER décrit le compte (`create`) —
    c'est le CHECKER (`config`) qui le crée réellement, sur `demande_compte_decision`."""
    if request.method == "GET":
        qs = DemandeCompteComptable.objects.select_related("compte")
        if request.GET.get("statut"):
            qs = qs.filter(statut=request.GET["statut"].upper())
        total = qs.count()
        limite, depuis = _pagination(request)
        return Response({
            "results": [serializers.demande_compte(d) for d in qs[depuis:depuis + limite]],
            "total_rows": total, "limit": limite, "offset": depuis,
        })

    data = request.data or {}
    demande = comptes_service.demander_ouverture(
        code=data.get("code", ""),
        racine=data.get("racine", ""),
        intitule=data.get("intitule", ""),
        classe=to_int(data.get("classe"), 0),
        nature=(data.get("nature") or "").upper(),
        devise=(data.get("devise") or "").upper(),
        est_transitoire=bool(data.get("estTransitoire")),
        cantonnement=data.get("cantonnement", ""),
        parent_code=data.get("parentCode", ""),
        justification=data.get("justification", ""),
        par=_acteur(request),
    )
    return Response(serializers.demande_compte(demande), status=201)


@api_view(["POST"])
@permission_classes(PARAMETRER)
def demande_compte_decision(request, demande_id):
    demande = DemandeCompteComptable.objects.filter(pk=demande_id).first()
    if demande is None:
        raise NotFoundError("Demande d'ouverture introuvable.")
    data = request.data or {}
    if "approuver" not in data:
        raise ValidationFailed("Champ `approuver` (booléen) obligatoire.")
    demande = comptes_service.decider_ouverture(
        demande, approuver=bool(data["approuver"]), par=_acteur(request),
        motif=data.get("motif", ""),
    )
    return Response(serializers.demande_compte(demande))


# =================================================================== CATALOGUE

@api_view(["GET"])
@permission_classes(LIRE)
def catalogue_schemas(request):
    qs = EventEntryTemplate.objects.prefetch_related("lignes").all()
    if request.GET.get("actif") in ("true", "false"):
        qs = qs.filter(actif=request.GET["actif"] == "true")
    return Response({
        "results": [serializers.schema(t) for t in qs],
        "total_rows": qs.count(),
    })


# ====================================================================== PIÈCES

@api_view(["GET"])
@permission_classes(LIRE)
def pieces(request):
    qs = services.rechercher_pieces(
        debut=to_date(request.GET.get("debut")),
        fin=to_date(request.GET.get("fin")),
        journal=(request.GET.get("journal") or "").upper(),
        statut=(request.GET.get("statut") or "").upper(),
        evenement=request.GET.get("evenement", ""),
        reference=request.GET.get("reference", ""),
        compte=request.GET.get("compte", ""),
        devise=(request.GET.get("devise") or "").upper(),
        origine_type=request.GET.get("origineType", ""),
        origine_id=request.GET.get("origineId", ""),
    ).select_related("taux_change", "piece_contrepassee", "piece_rectifiee")
    total = qs.count()
    limite, depuis = _pagination(request)
    avec_lignes = request.GET.get("lignes") == "true"
    page = qs[depuis:depuis + limite]
    if avec_lignes:
        page = page.prefetch_related("lignes__compte")
    return Response({
        "results": [serializers.piece(p, avec_lignes=avec_lignes) for p in page],
        "total_rows": total, "limit": limite, "offset": depuis,
    })


def _piece_ou_404(reference: str) -> PieceComptable:
    piece = (
        PieceComptable.objects
        .select_related("taux_change", "piece_contrepassee", "piece_rectifiee")
        .prefetch_related("lignes__compte")
        .filter(reference=reference).first()
    )
    if piece is None:
        raise NotFoundError(f"Pièce « {reference} » introuvable.")
    return piece


@api_view(["GET"])
@permission_classes(LIRE)
def piece_detail(request, reference):
    piece = _piece_ou_404(reference)
    donnees = serializers.piece(piece)
    donnees["contrepassations"] = [
        p.reference for p in piece.contrepassations.all()
    ]
    donnees["rectifications"] = [p.reference for p in piece.rectifications.all()]
    if piece.lignes.filter(compte__racine="588").exists():
        try:
            donnees["residuFx"] = serializers.montant(fx.residu_transitoire_fx(piece))
        except ValidationFailed as exc:
            donnees["residuFx"] = None
            donnees["residuFxProbleme"] = str(exc)
    return Response(donnees)


@api_view(["POST"])
@permission_classes(SAISIR)
def piece_od(request):
    """MAKER — crée une opération diverse en BROUILLON. Elle n'entre au grand livre
    qu'après validation par un CHECKER distinct."""
    data = request.data or {}
    journal = (data.get("journal") or JOURNAL_DES_OD).upper()
    if journal != JOURNAL_DES_OD:
        raise ValidationFailed(
            f"La saisie manuelle est cantonnée au journal {JOURNAL_DES_OD} (opérations "
            f"diverses). Une écriture de {journal} naît d'un événement métier et passe "
            "par le catalogue (B1…B16) — la saisir à la main contournerait le moteur."
        )
    lignes = data.get("lignes") or []
    if not isinstance(lignes, list) or not lignes:
        raise ValidationFailed("Au moins une ligne d'écriture est requise.")
    if not (data.get("libelle") or "").strip():
        raise ValidationFailed("Le libellé d'une OD est obligatoire : une écriture sans "
                               "explication n'est pas justifiable devant un auditeur.")

    date_operation = to_date(data.get("dateOperation")) or timezone.localdate()

    # Le taux ne se CHOISIT pas dans le formulaire : si l'OD porte plusieurs devises, c'est
    # le taux gouverné du jour (app `fx`) qui s'applique — et s'il n'est pas publié,
    # l'écriture est refusée plutôt que passée à un cours arbitraire (principe 5).
    taux = None
    if len({(l.get("devise") or "").upper() for l in lignes}) > 1:
        taux = fx.taux_du_jour(date_taux=date_operation)
    reference = (data.get("reference") or "").strip() or _reference_od(date_operation)
    piece = services.enregistrer_piece(
        reference=reference,
        date_operation=date_operation,
        journal=journal,
        libelle=data.get("libelle", "").strip(),
        lignes=[
            {
                "compte": (l.get("compte") or "").strip(),
                "devise": (l.get("devise") or "").upper(),
                "debit": l.get("debit") or "0",
                "credit": l.get("credit") or "0",
                "libelle": l.get("libelle", ""),
            }
            for l in lignes
        ],
        taux_change=taux,
        motif=data.get("motif", ""),
        origine_type=data.get("origineType", ""),
        origine_id=data.get("origineId", ""),
        par=_acteur(request),
        valider=False,
        saisie_manuelle=True,
    )
    return Response(serializers.piece(piece), status=201)


def _reference_od(jour: date_cls) -> str:
    prefixe = f"OD-{jour:%Y%m%d}"
    rang = PieceComptable.objects.filter(reference__startswith=prefixe).count() + 1
    while PieceComptable.objects.filter(reference=f"{prefixe}-{rang:03d}").exists():
        rang += 1
    return f"{prefixe}-{rang:03d}"


@api_view(["POST"])
@permission_classes(VALIDER)
def piece_validation(request, reference):
    """CHECKER — `services.valider_piece` refuse que le maker se valide lui-même."""
    piece = _piece_ou_404(reference)
    piece = services.valider_piece(piece, par=_acteur(request))
    return Response(serializers.piece(_piece_ou_404(piece.reference)))


@api_view(["POST"])
@permission_classes(VALIDER)
def piece_contrepassation(request, reference):
    piece = _piece_ou_404(reference)
    data = request.data or {}
    lignes = data.get("lignesRectificatives") or None
    if lignes is not None:
        lignes = [
            {
                "compte": (l.get("compte") or "").strip(),
                "devise": (l.get("devise") or "").upper(),
                "debit": l.get("debit") or "0",
                "credit": l.get("credit") or "0",
                "libelle": l.get("libelle", ""),
            }
            for l in lignes
        ]
    inverse, rectification = services.contrepasser_piece(
        piece,
        motif=data.get("motif", ""),
        par=_acteur(request),
        lignes_rectificatives=lignes,
        reference_contrepassation=data.get("referenceContrepassation", ""),
        reference_rectification=data.get("referenceRectification", ""),
        date_operation=to_date(data.get("dateOperation")),
    )
    return Response({
        "origine": piece.reference,
        "contrepassation": serializers.piece(_piece_ou_404(inverse.reference)),
        "rectification": (
            serializers.piece(_piece_ou_404(rectification.reference)) if rectification else None
        ),
    }, status=201)


# ================================================================== JOURNAUX

@api_view(["GET"])
@permission_classes(LIRE)
def journaux(request):
    donnees = services.journaux_auxiliaires(
        debut=to_date(request.GET.get("debut")), fin=to_date(request.GET.get("fin")),
    )
    return Response({
        "results": [serializers.journal_auxiliaire(j) for j in donnees],
        "total_rows": len(donnees),
        "debut": request.GET.get("debut"),
        "fin": request.GET.get("fin"),
    })


# ============================================================== RESTITUTIONS

@api_view(["GET"])
@permission_classes(LIRE)
def balance(request):
    devise = _devise_requise(request)
    as_of = to_date(request.GET.get("as_of"))
    lignes = services.balance_par_devise(devise=devise, as_of=as_of)
    total_debit = sum((l["debit"] for l in lignes), Decimal("0.00"))
    total_credit = sum((l["credit"] for l in lignes), Decimal("0.00"))
    return Response({
        "devise": devise,
        "asOf": serializers.jour(as_of),
        "results": [serializers.ligne_balance(l) for l in lignes],
        "total_rows": len(lignes),
        "totalDebit": serializers.montant(services.q2(total_debit)),
        "totalCredit": serializers.montant(services.q2(total_credit)),
        "equilibree": services.q2(total_debit) == services.q2(total_credit),
    })


@api_view(["GET"])
@permission_classes(LIRE)
def grand_livre(request):
    reference = (request.GET.get("compte") or "").strip()
    if not reference:
        raise ValidationFailed("Paramètre `compte` obligatoire (code ou racine).")
    donnees = services.grand_livre(
        reference_compte=reference,
        devise=_devise_requise(request),
        debut=to_date(request.GET.get("debut")),
        fin=to_date(request.GET.get("fin")),
    )
    return Response(serializers.grand_livre(donnees))


@api_view(["GET"])
@permission_classes(LIRE)
def controle_integrite(request):
    anomalies = services.controler_integrite(as_of=to_date(request.GET.get("as_of")))
    return Response({
        "results": [serializers.anomalie_integrite(a) for a in anomalies],
        "total_rows": len(anomalies),
        "conforme": not anomalies,
    })


@api_view(["GET"])
@permission_classes(LIRE)
def controle_fx(request):
    """Rapprochement 588FX : pièces non dénouées, avec leur ÂGE (annexe E, étape 4)."""
    age = to_int(request.GET.get("ageHeures"), 48)
    anomalies = fx.pieces_fx_non_denouees(age_heures=age)
    soldes = [
        {"devise": devise,
         "solde": serializers.montant(fx.solde_global_transitoire_fx(devise=devise))}
        for devise in Devise.values
    ]
    # La contre-valeur se calcule au dernier taux de CLÔTURE connu — et la réponse dit
    # LEQUEL : une position exprimée sans son taux n'est pas rapprochable.
    position = None
    taux = TauxChange.objects.filter(usage=TauxChange.Usage.CLOTURE).first()
    if taux is not None:
        total = Decimal("0.00")
        for devise in Devise.values:
            total += fx.convertir(
                fx.solde_global_transitoire_fx(devise=devise),
                de=devise, vers=fx.DEVISE_PIVOT, taux=taux,
            )
        position = serializers.montant(services.q2(total))
    return Response({
        "ageHeures": age,
        "results": [serializers.anomalie_fx(a) for a in anomalies],
        "total_rows": len(anomalies),
        "soldesTransitoire": soldes,
        "positionContreValeur": position,
        "tauxUtilise": fx.provenance(taux) if taux is not None else None,
        "devisePivot": fx.DEVISE_PIVOT,
        "note": "Le contrôle de dénouement porte sur la CONTRE-VALEUR du 588FX, pas sur "
                "son solde par devise : après un règlement dénoué, 588FC et 588USD sont "
                "chacun non nuls et se compensent au taux.",
    })


# ================================================================ TAUX DE CHANGE

@api_view(["GET"])
@permission_classes(LIRE)
def taux_change(request):
    """LECTURE SEULE. La saisie et la validation des taux vivent dans `/api/fx/` :
    `accounting` ne gouverne plus le taux de change (versionnement, maker-checker au-delà
    du seuil `InstitutionConfig`, source tracée — tout cela est porté par `fx`).

    Ce que cette vue expose, c'est la PROJECTION effectivement appliquée aux écritures :
    ce que les pièces référencent, avec la provenance remontée jusqu'au taux `fx` d'origine.
    """
    qs = TauxChange.objects.all()
    if request.GET.get("usage"):
        qs = qs.filter(usage=request.GET["usage"].upper())
    if request.GET.get("debut"):
        qs = qs.filter(date_taux__gte=to_date(request.GET["debut"]))
    if request.GET.get("fin"):
        qs = qs.filter(date_taux__lte=to_date(request.GET["fin"]))
    total = qs.count()
    limite, depuis = _pagination(request)
    return Response({
        "results": [
            {**serializers.taux(t), "provenance": fx.provenance(t)}
            for t in qs[depuis:depuis + limite]
        ],
        "total_rows": total, "limit": limite, "offset": depuis,
        "saisie": "POST /api/fx/rates — la comptabilité consomme les taux, elle ne les "
                  "gouverne pas.",
    })


@api_view(["POST", "PUT", "PATCH", "DELETE"])
@permission_classes(LIRE)
def taux_saisie_interdite(request):
    """Endpoint volontairement présent pour renvoyer la règle plutôt qu'un 404 ambigu."""
    raise ConflictError(
        "La saisie d'un taux de change ne se fait plus ici : `fx.ExchangeRate` est la "
        "source de vérité unique (POST /api/fx/rates). `accounting.TauxChange` n'est plus "
        "qu'une projection en lecture seule de ce que les écritures ont appliqué."
    )


# =================================================================== PROVISIONS

@api_view(["GET"])
@permission_classes(LIRE)
def classes_risque(request):
    grille = provisions.grille(actives_seulement=request.GET.get("actives") != "false")
    couverture_ok, message = True, ""
    try:
        provisions.verifier_couverture()
    except ValidationFailed as exc:
        couverture_ok, message = False, str(exc)
    return Response({
        "results": [serializers.classe_risque(c) for c in grille],
        "total_rows": len(grille),
        "couvertureValide": couverture_ok,
        "couvertureProbleme": message,
    })


@api_view(["PATCH"])
@permission_classes(PARAMETRER)
def classe_risque_detail(request, code):
    data = request.data or {}
    champs = {}
    correspondance = {
        "libelle": "libelle", "joursMin": "jours_min", "joursMax": "jours_max",
        "tauxProvision": "taux_provision", "enSouffrance": "en_souffrance",
        "ordre": "ordre", "actif": "actif",
    }
    for cle_api, cle_modele in correspondance.items():
        if cle_api in data:
            valeur = data[cle_api]
            if cle_modele in ("jours_min", "ordre"):
                valeur = to_int(valeur)
            elif cle_modele == "jours_max":
                valeur = None if valeur in (None, "") else to_int(valeur)
            champs[cle_modele] = valeur
    if not champs:
        raise ValidationFailed("Aucun champ modifiable fourni.")
    classe = provisions.modifier_classe(code, par=_acteur(request), **champs)
    return Response(serializers.classe_risque(classe))


@api_view(["GET"])
@permission_classes(LIRE)
def classification(request):
    """Simulation LECTURE SEULE : ce que coûterait l'arrêté, sans écrire une ligne."""
    as_of = to_date(request.GET.get("as_of")) or timezone.localdate()
    donnees = provisions.analyser_portefeuille(as_of=as_of)
    return Response(serializers.classification(donnees))


@api_view(["GET", "POST"])
@permission_classes([IsStaff, CapaciteSelonMethode(GET="read", POST="validate")])
def arretes(request):
    if request.method == "GET":
        historique = provisions.historique(
            devise=(request.GET.get("devise") or "").upper(),
            limite=min(to_int(request.GET.get("limit"), 50) or 50, MAX_PAGE),
        )
        return Response({
            "results": [serializers.arrete_provision(a) for a in historique],
            "total_rows": ArreteProvision.objects.count(),
        })

    data = request.data or {}
    date_arrete = to_date(data.get("dateArrete")) or timezone.localdate()
    resultat = provisions.arreter(
        date_arrete=date_arrete,
        par=_acteur(request),
        prefixe_reference=(data.get("prefixe") or "PROV").strip(),
    )
    return Response(serializers.resultat_arrete(resultat), status=201)


@api_view(["GET"])
@permission_classes(LIRE)
def classements(request):
    lignes = provisions.classements(
        date_arrete=to_date(request.GET.get("dateArrete")),
        loan_reference=request.GET.get("reference", ""),
        limite=min(to_int(request.GET.get("limit"), 200) or 200, 1000),
    )
    return Response({
        "results": [serializers.classement_credit(c) for c in lignes],
        "total_rows": len(lignes),
    })


# ============================================================ ÉTATS FINANCIERS

@api_view(["GET"])
@permission_classes(LIRE)
def etat_bilan(request):
    donnees = etats.bilan(
        devise=_devise_requise(request), as_of=to_date(request.GET.get("as_of")),
    )
    return Response(serializers.bilan(donnees))


@api_view(["GET"])
@permission_classes(LIRE)
def etat_resultat(request):
    donnees = etats.compte_de_resultat(
        devise=_devise_requise(request), as_of=to_date(request.GET.get("as_of")),
    )
    return Response(serializers.compte_de_resultat(donnees))


@api_view(["GET"])
@permission_classes(LIRE)
def etat_consolide(request):
    as_of = to_date(request.GET.get("as_of"))
    if as_of is None:
        raise ValidationFailed(
            "`as_of` obligatoire : un état consolidé se rattache à un taux de clôture daté."
        )
    return Response(serializers.etats_consolides(etats.etats_consolides(as_of=as_of)))
