"""Produit obligataire client — « créer ou convertir du **cash** en obligation ».

Ce module répare un défaut d'origine : `POST /investments/obligations` créait une
`ObligationPosition` ACTIVE à partir du seul corps de la requête. Le montant venait du
client, aucun portefeuille n'était débité, aucun mouvement n'était journalisé, aucune
offre n'était consultée, et les termes (250 / 9 % / 24 mois) tombaient des valeurs par
défaut du modèle. N'importe quel utilisateur authentifié pouvait donc s'attribuer une
obligation d'un million de dollars sans avoir déposé un centime, puis en demander le
retrait.

Le principe qui structure ce fichier : **une position obligataire est un encaissement,
pas une déclaration.** Il en découle mécaniquement :

- l'argent vient du **portefeuille client** (`caisses.services.withdraw`, seul service
  de débit existant — ce module n'écrit jamais dans les modèles de `caisses`) ;
- solde insuffisant → refus 422 structuré, jamais de position partielle ;
- le verrou de ligne sur le portefeuille (`select_for_update`, posé par `caisses`)
  interdit le double débit concurrent ; débit + souscription + encaissement + position
  vivent dans UNE transaction ;
- **les termes viennent d'une `Offer`**, jamais de valeurs par défaut : sans offre
  applicable, la création est refusée avec son code. Refuser est conforme, inventer ne
  l'est pas.

Conséquence : une position obligataire n'est plus un objet parallèle au cycle des
offres, c'est une **souscription encaissée** (B10) doublée d'une vue produit. Le montant
n'est plus libre : il vaut `nombre de titres × valeur unitaire` de l'offre, exactement
comme une souscription — deux façons d'acheter le même titre ne peuvent pas obéir à
deux règles de prix.
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from django.db import transaction

from audit.services import record as audit_record
from common import idempotency
from common.exceptions import BusinessError, InsufficientFundsError

from . import funding
from .models import Investor, Offer, ObligationPosition, Subscription

CENT = Decimal("0.01")

#: Devise du module (voir `Movement.currency`, `InvestmentEvent.currency`). Les
#: positions obligataires n'ont jamais porté de devise ; elles suivent celle du module
#: plutôt que d'en inventer une seconde.
DEVISE = "USD"

#: Nombre de coupons par an, par fréquence de paiement de l'offre. `BULLET` (in fine)
#: n'y figure pas : il n'a pas de périodicité, il a une échéance.
PERIODES_PAR_AN = {
    Offer.Frequency.MONTHLY: 12,
    Offer.Frequency.QUARTERLY: 4,
    Offer.Frequency.ANNUAL: 1,
}


def _q(value) -> Decimal:
    return Decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


# ── Exceptions (convention `credits.committee.CommitteeError`) ────────────────

class ObligationError(BusinessError):
    """Refus de création d'une position obligataire.

    `code` porté par la classe et consommé par le front ; le message explique, il ne
    se parse pas. `as_errors()` produit la liste `{code, message}` attendue par les
    écrans (même contrat que `credits.workflow.WorkflowError`).
    """

    code = "OBLIGATION_ERROR"
    http_status = 422

    def as_errors(self) -> list[dict]:
        return [{"code": self.code, "message": self.message}]


class OfferRequired(ObligationError):
    """Aucune offre désignée, ou offre introuvable : les termes ne s'inventent pas."""

    code = "OBLIGATION_OFFER_REQUIRED"


class OfferNotABond(ObligationError):
    """L'offre désignée n'émet pas d'obligations (action, part sociale)."""

    code = "OBLIGATION_OFFER_NOT_A_BOND"


class OfferNotServed(ObligationError):
    """La souscription n'a pas été servie immédiatement (liste d'attente, prorata).

    Un titre non servi n'est pas une position : on refuse plutôt que d'encaisser de
    l'argent pour une allocation qui n'est pas encore connue.
    """

    code = "OBLIGATION_OFFER_NOT_SERVED"


class WalletMissing(ObligationError):
    """Aucun portefeuille dans la devise du module : il n'y a pas de cash à convertir."""

    code = "OBLIGATION_WALLET_MISSING"


class InsufficientWallet(ObligationError):
    """Solde de portefeuille insuffisant — refus AVANT toute écriture."""

    code = "OBLIGATION_INSUFFICIENT_FUNDS"


class InvalidQuantity(ObligationError):
    code = "OBLIGATION_INVALID_QUANTITY"


# ── Termes : dérivés de l'offre, jamais fabriqués ─────────────────────────────

def coupon_periodique(*, montant: Decimal, taux_annuel: Decimal, frequence: str,
                      maturite_mois: int) -> Decimal:
    """Coupon en VALEUR d'une période, dérivé des termes de l'offre.

    Intérêt simple sur le nominal, convention du produit obligataire du module
    (`Offer.coupon_rate` est en points de pourcentage, cf. `serializers.UNIT_PERCENT`) :

        coupon annuel = nominal × taux / 100
        coupon d'une période = coupon annuel / (périodes par an)
        in fine (BULLET) = coupon annuel × maturité / 12, versé une seule fois

    Ce n'est pas un terme inventé : c'est la conséquence arithmétique du taux, de la
    fréquence et de la maturité que l'offre porte. Cas chiffré : 1 000 USD à 9 %,
    trimestriel → annuel 90,00 ; trimestriel 22,50. Le même titre en in fine sur
    24 mois → 180,00 versés à l'échéance.
    """
    annuel = _q(Decimal(montant) * Decimal(taux_annuel) / Decimal("100"))
    if frequence == Offer.Frequency.BULLET:
        return _q(annuel * Decimal(maturite_mois) / Decimal("12"))
    periodes = PERIODES_PAR_AN.get(frequence)
    if not periodes:
        # Fréquence inconnue : on ne devine pas une périodicité (principe 8 —
        # le mécanisme est ici, la règle est dans l'offre).
        raise ObligationError(
            f"Fréquence de paiement inconnue sur l'offre : « {frequence} ». "
            f"Valeurs admises : {', '.join(Offer.Frequency.values)}."
        )
    return _q(annuel / Decimal(periodes))


# ── Débit du portefeuille : délégué à `caisses`, jamais réimplémenté ──────────

def _debiter_portefeuille(*, investor: Investor, montant: Decimal, idempotency_key: str,
                          by: str = ""):
    """Débite le portefeuille client via le service de `caisses`.

    `caisses.services.withdraw` est le SEUL service de débit de portefeuille du
    système : il pose le verrou de ligne (`select_for_update`), refuse un solde
    insuffisant (`InsufficientFundsError`), journalise un `WalletMovement` et un
    enregistrement d'audit. Ce module ne touche aucun modèle de `caisses` — il l'appelle.

    Le `WalletMovement` produit porte le genre `WITHDRAW` : `caisses.WalletMovement.Kind`
    n'a pas de genre « placement », et créer un genre reviendrait à migrer une app dont
    ce module n'est pas propriétaire. Dette croisée signalée, pas contournée.
    """
    from caisses.models import ClientWallet
    from caisses.services import withdraw as caisses_withdraw

    wallet = ClientWallet.objects.filter(user=investor.user, currency=DEVISE).first()
    if wallet is None:
        raise WalletMissing(
            f"Aucun portefeuille {DEVISE} sur ce compte : il n'y a pas de cash à "
            "convertir en obligation. Alimentez d'abord le portefeuille."
        )
    try:
        return caisses_withdraw(wallet_id=wallet.pk, amount=montant,
                                 idempotency_key=idempotency_key, by=by)
    except InsufficientFundsError as exc:
        raise InsufficientWallet(
            f"Solde insuffisant : {_q(wallet.balance)} {DEVISE} disponibles pour une "
            f"souscription de {_q(montant)} {DEVISE}. Aucune position n'a été créée."
        ) from exc


# ── Création d'une position ───────────────────────────────────────────────────

@transaction.atomic
def souscrire(*, investor: Investor, offer_id, bonds, idempotency_key: str, by: str = "",
              name: str = "") -> ObligationPosition:
    """Convertit du cash en position obligataire. TOUT ou RIEN.

    Enchaînement, dans une seule transaction :

    1. l'offre est résolue et vérifiée (obligation, ouverte — `funding.reserve` porte
       déjà toutes les gardes de l'offre : statut, échéance, bornes, ticket minimum,
       politique de sursouscription) ;
    2. la souscription RÉSERVE les titres (aucun argent ne bouge) ;
    3. le portefeuille client est DÉBITÉ (verrou de ligne, refus si solde insuffisant) ;
    4. la souscription est ENCAISSÉE (B10 : `Movement` de type `SETTLEMENT`,
       `InvestmentEvent.SUBSCRIPTION_SETTLED`, `funded_amount` du projet et de l'offre) ;
    5. la position est créée avec les termes de l'offre.

    Un échec à n'importe quelle étape annule les précédentes : il n'existe pas de
    position sans encaissement, ni d'encaissement sans débit.
    """
    try:
        titres = int(bonds or 0)
    except (TypeError, ValueError):
        titres = 0
    if titres <= 0:
        raise InvalidQuantity(
            "Le nombre de titres à souscrire doit être un entier positif : une "
            "position obligataire s'exprime en titres de l'offre, pas en montant libre."
        )

    if not offer_id:
        raise OfferRequired(
            "Aucune offre désignée : une position obligataire tire ses conditions "
            "(taux, maturité, fréquence de coupon) d'une offre publiée. Sans offre, "
            "il n'y a pas de termes à appliquer."
        )
    offer = Offer.objects.select_related("project").filter(pk=offer_id).first()
    if offer is None:
        raise OfferRequired(
            f"Offre introuvable ({offer_id}) : aucune condition applicable, la "
            "position n'est pas créée."
        )
    if offer.type_of_title != Offer.TypeOfTitle.OBLIGATION:
        raise OfferNotABond(
            f"L'offre {offer.code} émet des titres « {offer.type_of_title} » : elle ne "
            "peut pas produire de position obligataire."
        )

    rec = idempotency.begin(
        scope="investments.obligation", key=idempotency_key,
        params={"investor": investor.pk, "offer": offer.pk, "bonds": titres}, by=by,
    )

    # 2. Réservation — porte toutes les gardes de l'offre (statut du projet, échéance,
    #    bornes, ticket minimum, sursouscription). Ses refus remontent tels quels :
    #    `ConflictError`/`ValidationFailed` sont déjà des erreurs métier structurées.
    subscription = funding.reserve(
        investor=investor, offer_id=offer.pk, bonds=titres,
        idempotency_key=f"obligation-reserve:{idempotency_key}", by=by,
    )
    if subscription.status != Subscription.Status.RESERVED or subscription.allocated_amount <= 0:
        raise OfferNotServed(
            f"L'offre {offer.code} ne sert pas cette souscription immédiatement "
            f"(statut « {subscription.status} », politique "
            f"« {offer.oversubscription_policy} ») : aucun montant n'est encaissé et "
            "aucune position n'est créée tant que l'allocation n'est pas connue."
        )

    montant = _q(subscription.allocated_amount)

    # 3. Débit réel du portefeuille — avant tout encaissement.
    _debiter_portefeuille(investor=investor, montant=montant,
                           idempotency_key=f"obligation-debit:{idempotency_key}", by=by)

    # 4. Encaissement (B10) : c'est `funding.settle` qui journalise le `Movement`
    #    (`Movement.Type.SETTLEMENT`, type EXISTANT — aucun nouveau type créé) et qui
    #    produit l'événement comptable. Rien n'est réécrit ici.
    subscription = funding.settle(
        subscription=subscription, idempotency_key=f"obligation-settle:{idempotency_key}",
        by=by,
    )

    # 5. Position — termes LUS dans l'offre.
    position = ObligationPosition.objects.create(
        investor=investor, offer=offer, subscription=subscription,
        name=(name or "").strip() or f"{offer.code} — {offer.project.title}",
        invested_amount=_q(subscription.settled_amount),
        rate=offer.coupon_rate,
        term_months=offer.maturity_months,
        coupon_amount=coupon_periodique(
            montant=subscription.settled_amount, taux_annuel=offer.coupon_rate,
            frequence=offer.payment_frequency, maturite_mois=offer.maturity_months,
        ),
        status=ObligationPosition.Status.ACTIF,
    )

    audit_record(
        actor=by, action="investments.obligation.subscribe", entity_type="ObligationPosition",
        entity_id=str(position.pk),
        details={"offer": offer.code, "project": offer.project.code, "bonds": titres,
                 "amount": str(montant), "rate": str(position.rate),
                 "termMonths": position.term_months,
                 "couponAmount": str(position.coupon_amount),
                 "subscription": subscription.pk},
    )

    from . import serializers
    idempotency.complete(rec, response=serializers.obligation_row(position),
                          entity_type="ObligationPosition", entity_id=str(position.pk))
    return position
