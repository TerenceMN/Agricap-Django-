"""Mécanisme 588FX et rattachement au taux de change gouverné (annexe E, principes 4 et 5).

« La dollarisation se trace, ne se subit pas. » Toute opération mixte FC/USD transite par le
transitoire 588FX en deux jambes, et l'écart se constate explicitement en 712FX (gain) ou
613FX (perte) — jamais noyé dans un compte de résultat générique.

Source de vérité du taux : l'app `fx`
-------------------------------------
`accounting` ne gouverne PLUS le taux de change. `fx.ExchangeRate` porte ce que
`accounting.TauxChange` n'a jamais porté : versionnement append-only (une correction publie
une version, elle n'écrase pas un taux probant), maker-checker déclenché au-delà d'un écart
lu dans `InstitutionConfig`, source tracée, et refus explicite de retomber en silence sur la
veille. Deux modèles pour la même grandeur, c'était une violation du principe 6 et un
incident de données en puissance (principe 11) : deux écrans auraient affiché deux taux de
clôture pour la même date.

`accounting.TauxChange` survit en **projection locale, en lecture seule** : c'est la ligne
que `PieceComptable.taux_change` référence, pour qu'une pièce reste auto-portante (le taux
appliqué est lisible sur la pièce, sans jointure inter-app). Elle n'est plus alimentée par
aucune saisie humaine — seul `_projeter` la fabrique, à partir d'un taux `fx` ACTIF, et elle
porte l'identité de ce taux dans `source_reference`. Cible à terme : remplacer la clé
étrangère par un pointeur direct vers `fx.ExchangeRate` (migration à coordonner, cf. rapport).

Le cours retenu par la comptabilité est le **cours pivot** `(achat + vente) / 2` de `fx`,
et non l'une des deux jambes : les livres se tiennent à un cours de référence unique ; l'écart
entre ce cours et le prix réellement pratiqué au guichet EST le gain ou la perte de change,
et il doit apparaître en 712FX/613FX — c'est précisément le mécanisme de l'annexe E, pas
quelque chose à dissoudre dans le choix d'une jambe.

Contrôle de dénouement
----------------------
L'annexe E écrit « solde 588FX nul par devise sur cette pièce ». Pris au pied de la lettre
c'est inexact et ce module ne l'implémente donc pas ainsi : sur l'exemple chiffré, 588FX
reste créditeur de 280 000 FC et débiteur de 100 USD. Ce qui est réellement nul — et qui EST
le contrôle de dénouement — c'est la position 588FX exprimée en CONTRE-VALEUR au taux de
l'opération : −280 000 FC + (100 USD × 2 800) = 0. C'est cette grandeur que
`residu_transitoire_fx` calcule et que les tests verrouillent. (Point signalé au fondateur :
la formulation de l'annexe mérite d'être corrigée.)
"""
from __future__ import annotations

from datetime import date as date_cls, timedelta
from decimal import Decimal, ROUND_HALF_UP

from django.db import transaction
from django.utils import timezone

from common.exceptions import ConflictError, ValidationFailed

from . import catalogue, services
from .definitions import CONDITION_GAIN, CONDITION_PERTE, RACINE_TRANSITOIRE_FX
from .models import Devise, PieceComptable, TauxChange

DEVISE_PIVOT = Devise.FC

#: `fx` nomme le franc congolais « CDF » (pivot implicite), l'annexe A le nomme « FC ».
DEVISE_FX = {Devise.FC: "CDF", Devise.USD: "USD"}

#: Correspondance des nomenclatures de source (principe 6 : une seule nomenclature, mais
#: deux tables historiques à réconcilier le temps de la bascule).
SOURCE_FX_VERS_COMPTA = {
    "BCC": TauxChange.Source.BCC,
    "MANUELLE": TauxChange.Source.INTERNE,
    "AGREGATEUR": TauxChange.Source.MARCHE,
}

#: Précision de stockage du taux projeté (`TauxChange.taux` : 6 décimales).
PRECISION_TAUX = Decimal("0.000001")


# --------------------------------------------------------------- GOUVERNANCE DU TAUX

def _projeter(rate) -> TauxChange:
    """Projette un `fx.ExchangeRate` ACTIF en ligne `accounting.TauxChange`.

    Idempotent, et **jamais destructif** : si une projection existe déjà pour cette date et
    cet usage avec une AUTRE valeur, c'est que `fx` a re-versionné un taux déjà utilisé par
    des écritures. On refuse — bruyamment. Réécrire la projection changerait rétroactivement
    le taux de pièces déjà validées ; la correction d'une écriture passée au mauvais taux
    est une CONTREPASSATION, pas une mise à jour de référentiel.
    """
    valeur = Decimal(rate.mid_rate).quantize(PRECISION_TAUX, rounding=ROUND_HALF_UP)
    identite = f"fx.ExchangeRate#{rate.pk} v{rate.version} ({rate.tier}/{rate.currency})"
    projection = TauxChange.objects.filter(
        date_taux=rate.effective_date, usage=rate.usage,
        devise_base=Devise.USD, devise_contre=Devise.FC,
    ).first()
    if projection is not None:
        if projection.taux != valeur:
            raise ConflictError(
                f"Le taux {rate.usage} du {rate.effective_date} vaut {valeur} dans `fx` "
                f"({identite}) mais {projection.taux} dans la projection comptable déjà "
                "utilisée par des écritures. Un taux appliqué ne se réécrit pas : "
                "contrepassez les pièces concernées, puis republiez-les au taux corrigé."
            )
        return projection
    return TauxChange.objects.create(
        date_taux=rate.effective_date,
        usage=rate.usage,
        devise_base=Devise.USD,
        devise_contre=Devise.FC,
        taux=valeur,
        source=SOURCE_FX_VERS_COMPTA.get(rate.source, TauxChange.Source.INTERNE),
        source_reference=" — ".join(filter(None, [identite, rate.source_reference])),
        saisi_par=rate.created_by,
        valide_par=rate.validated_by,
    )


def taux_du_jour(
    *,
    date_taux: date_cls,
    usage: str = TauxChange.Usage.OPERATIONNEL,
    devise_base: str = Devise.USD,
    devise_contre: str = Devise.FC,
) -> TauxChange:
    """Taux applicable à une date et à un usage — DÉLÉGUÉ à `fx.services.taux_du_jour`.

    Sémantique inchangée et volontairement stricte : aucune retombée sur la veille. `fx`
    applique la même règle (et signale en plus qu'un taux existe mais reste EN ATTENTE de
    son second acteur — un message qu'`accounting` ne savait pas produire).
    """
    from fx.services import taux_du_jour as taux_fx

    if devise_contre != Devise.FC or devise_base != Devise.USD:
        raise ValidationFailed(
            f"`fx` cote les devises étrangères CONTRE le CDF (pivot implicite) : le couple "
            f"{devise_base}/{devise_contre} n'y est pas représentable."
        )
    rate = taux_fx(date_taux=date_taux, usage=usage, currency=DEVISE_FX[devise_base])
    return _projeter(rate)


def taux_de_cloture(*, date_arrete: date_cls) -> TauxChange:
    """Taux de CLÔTURE d'une date — celui que tout état consolidé doit référencer."""
    return taux_du_jour(date_taux=date_arrete, usage=TauxChange.Usage.CLOTURE)


def provenance(taux: TauxChange) -> dict:
    """Provenance journalisable du taux appliqué (principe 4 : « toute conversion journalise
    le taux utilisé et sa date »). Remonte jusqu'au taux `fx` d'origine quand la projection
    en porte l'identité."""
    origine = None
    reference = taux.source_reference or ""
    if reference.startswith("fx.ExchangeRate#"):
        try:
            identifiant = int(reference.split("#", 1)[1].split(" ", 1)[0])
        except (IndexError, ValueError):  # pragma: no cover - référence malformée
            identifiant = None
        if identifiant is not None:
            from fx.models import ExchangeRate
            from fx.services import rate_provenance

            rate = ExchangeRate.objects.filter(pk=identifiant).first()
            if rate is not None:
                origine = rate_provenance(rate, asked_for=taux.date_taux)
    return {
        "tauxId": taux.pk,
        "dateTaux": taux.date_taux.isoformat(),
        "usage": taux.usage,
        "deviseBase": taux.devise_base,
        "deviseContre": taux.devise_contre,
        "taux": str(taux.taux),
        "source": taux.source,
        "sourceReference": taux.source_reference,
        "origineFx": origine,
    }


def convertir(montant, *, de: str, vers: str, taux: TauxChange) -> Decimal:
    """Conversion explicite au taux journalisé. Aucune conversion n'existe hors de cette
    fonction : c'est le seul endroit où deux devises se rencontrent."""
    montant = services.q2(montant)
    if de == vers:
        return montant
    if de == taux.devise_base and vers == taux.devise_contre:
        return services.q2(montant * taux.taux)
    if de == taux.devise_contre and vers == taux.devise_base:
        return services.q2(montant / taux.taux)
    raise ValidationFailed(
        f"Le taux {taux.devise_base}/{taux.devise_contre} ne permet pas de convertir "
        f"{de} → {vers}."
    )


# ------------------------------------------------------------------ CONTRÔLE 588FX

def _lignes_transitoire_fx(piece: PieceComptable):
    return piece.lignes.filter(compte__racine=RACINE_TRANSITOIRE_FX)


def position_transitoire_fx(piece: PieceComptable) -> dict[str, Decimal]:
    """Position nette (débit − crédit) du transitoire 588FX, PAR DEVISE, sur une pièce."""
    position: dict[str, Decimal] = {}
    for ligne in _lignes_transitoire_fx(piece):
        position[ligne.devise] = position.get(ligne.devise, Decimal("0.00")) + (
            ligne.debit - ligne.credit
        )
    return {devise: services.q2(v) for devise, v in position.items()}


def residu_transitoire_fx(piece: PieceComptable, *, taux: TauxChange | None = None) -> Decimal:
    """Résidu de dénouement du 588FX, exprimé en contre-valeur FC.

    Zéro = l'opération de change est dénouée : tout ce qui est entré dans le transitoire en
    est ressorti, à l'écart de change près, lequel a été constaté en 712FX/613FX.
    """
    taux = taux or piece.taux_change
    position = position_transitoire_fx(piece)
    if not position:
        return Decimal("0.00")
    if taux is None:
        raise ValidationFailed(
            f"La pièce {piece.reference} mouvemente 588FX sans taux journalisé : "
            "son dénouement n'est pas contrôlable."
        )
    total = Decimal("0.00")
    for devise, montant in position.items():
        total += convertir(montant, de=devise, vers=DEVISE_PIVOT, taux=taux)
    return services.q2(total)


def pieces_fx_non_denouees(*, age_heures: int = 48) -> list[dict]:
    """Job quotidien (annexe E, étape 4) : toute pièce FX non dénouée sous 48 h, avec son âge.

    Le moteur refuse déjà de créer une opération de change non dénouée ; ce contrôle attrape
    les pièces construites autrement (OD manuelle, reprise de données, contrepassation
    partielle) et les pièces laissées en brouillon.
    """
    limite = timezone.now() - timedelta(hours=age_heures)
    anomalies = []
    pieces = (
        PieceComptable.objects
        .filter(lignes__compte__racine=RACINE_TRANSITOIRE_FX, cree_le__lte=limite)
        .select_related("taux_change")
        .distinct()
    )
    for piece in pieces:
        try:
            residu = residu_transitoire_fx(piece)
        except ValidationFailed as exc:
            anomalies.append({
                "reference": piece.reference, "statut": piece.statut,
                "age_heures": int((timezone.now() - piece.cree_le).total_seconds() // 3600),
                "residu": None, "probleme": str(exc),
            })
            continue
        if residu != 0 or piece.statut != PieceComptable.Statut.VALIDEE:
            anomalies.append({
                "reference": piece.reference, "statut": piece.statut,
                "age_heures": int((timezone.now() - piece.cree_le).total_seconds() // 3600),
                "residu": residu,
                "probleme": "Transitoire FX non dénoué" if residu != 0 else "Pièce FX restée en brouillon",
            })
    return anomalies


def solde_global_transitoire_fx(*, devise: str, as_of: date_cls | None = None) -> Decimal:
    """Solde du 588FX sur l'ensemble du grand livre, dans une devise — doit tendre vers zéro."""
    return services.solde_compte(RACINE_TRANSITOIRE_FX, devise=devise, as_of=as_of)


# --------------------------------------------------- OPÉRATION DE CHANGE (B14+B15+B16)

@transaction.atomic
def enregistrer_reglement_fx(
    *,
    reference: str,
    date_operation: date_cls,
    montant_source,
    devise_source: str,
    montant_cible,
    devise_cible: str,
    compte_tresorerie_source: str,
    compte_contrepartie_cible: str,
    taux: TauxChange | None = None,
    libelle: str = "",
    origine_type: str = "",
    origine_id: str = "",
    par: str = "",
) -> PieceComptable:
    """Règlement dans une devise d'une obligation libellée dans une autre (annexe E).

    Exemple de l'annexe : un client apporte 285 000 FC pour une échéance de 100 USD au taux
    2 800 → jambe 1 (B14), jambe 2 (B15), constat du gain de 5 000 FC en 712FX (B16), le tout
    dans UNE pièce indivisible.

    `montant_cible` est l'obligation réellement éteinte (ce que le client DOIT) ;
    `montant_source` est ce qu'il APPORTE. L'écart entre la contre-valeur apportée et la
    contre-valeur due est le gain (ou la perte) de change de l'institution.
    """
    if devise_source == devise_cible:
        raise ValidationFailed(
            "Un règlement FX suppose deux devises différentes ; utilisez le catalogue "
            "standard (B2/B3…) pour une opération mono-devise."
        )

    taux = taux or taux_du_jour(date_taux=date_operation)
    montant_source = services.q2(montant_source)
    montant_cible = services.q2(montant_cible)
    if montant_source <= 0 or montant_cible <= 0:
        raise ValidationFailed("Les montants d'un règlement FX doivent être strictement positifs.")

    # Contre-valeur, dans la devise APPORTÉE, de l'obligation éteinte.
    contre_valeur_due = convertir(montant_cible, de=devise_cible, vers=devise_source, taux=taux)
    ecart = services.q2(montant_source - contre_valeur_due)

    evenements = [
        ("B14", {
            "devise_source": devise_source,
            "montants": {"montant_source": montant_source},
            "comptes": {"$TRESORERIE_SOURCE": compte_tresorerie_source},
        }),
        ("B15", {
            "devise_cible": devise_cible,
            "montants": {"montant_cible": montant_cible},
            "comptes": {"$CONTREPARTIE_CIBLE": compte_contrepartie_cible},
        }),
    ]
    if ecart != 0:
        evenements.append(("B16", {
            "devise_source": devise_source,
            "montants": {"ecart": abs(ecart)},
            "conditions": {CONDITION_GAIN if ecart > 0 else CONDITION_PERTE},
        }))

    piece = catalogue.executer(
        evenements,
        reference=reference,
        date_operation=date_operation,
        journal="JFX",
        libelle=libelle or f"Règlement {montant_source} {devise_source} "
                           f"pour {montant_cible} {devise_cible} au taux {taux.taux}",
        taux_change=taux,
        origine_type=origine_type,
        origine_id=origine_id,
        par=par,
    )

    # Verrou : une opération de change qui ne dénoue pas son transitoire est un bug de
    # conception — rollback plutôt que laisser un 588FX qui traîne.
    residu = residu_transitoire_fx(piece, taux=taux)
    if residu != 0:
        raise ValidationFailed(
            f"Transitoire 588FX non dénoué (résidu {residu} {DEVISE_PIVOT}) : "
            "opération annulée."
        )
    return piece


def verifier_transitoire_solde(piece: PieceComptable) -> None:
    """Assertion utilitaire réutilisable par les jobs de contrôle et les tests."""
    residu = residu_transitoire_fx(piece)
    if residu != 0:
        raise ValidationFailed(
            f"Pièce {piece.reference} : transitoire 588FX non dénoué (résidu {residu})."
        )
