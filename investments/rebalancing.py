"""
Rééquilibrage d'un sous-portefeuille — de l'écart mesuré à l'ordre exécuté.

Le principe qui tient tout le fichier : **rééquilibrer, ce n'est pas déplacer un
solde d'une colonne à l'autre.** Il n'existe aucune opération « transférer 1 200 USD
des liquidités vers les obligations » : renforcer une poche, c'est SOUSCRIRE ;
l'alléger, c'est CÉDER. Les deux ont déjà leur chemin, leurs contrôles et leur
journal — `funding.reserve` / `funding.settle` d'un côté, le marché secondaire de
l'autre. Ce module n'en écrit aucun second : il mesure l'écart, refuse l'ordre qui
n'a pas de sens, puis appelle le chemin existant (principe 6).

Ce qui est contrôlé AVANT toute réservation, dans cet ordre :

1. une cible existe — sans cible, il n'y a pas d'écart, donc rien à corriger ;
2. la poche visée est réellement en DÉFICIT — on ne renforce pas ce qui déborde ;
3. le montant ne dépasse pas l'écart (à une unité de titre près, granularité
   incompressible de l'offre) — un ordre qui dépasse l'écart recrée un écart de
   sens inverse ;
4. le portefeuille du souscripteur porte le solde — refus avant réservation plutôt
   qu'échec à l'encaissement.

L'ordre lui-même est append-only : il fige l'écart et la version de cible qui l'ont
motivé. Son exécution se lit sur la souscription liée, jamais sur une copie de
statut ici.
"""
from __future__ import annotations

from decimal import Decimal

from django.db import transaction

from audit.services import record as audit_record
from common.exceptions import ConflictError, ValidationFailed
from common.money import arrondir

from . import funding, services
from .models import Offer, RebalancingOrder, SubPortfolio, Subscription

#: Poche d'une offre, déduite du type de titre — la même règle que l'allocation.
def poche_de_l_offre(offer: Offer) -> str:
    return "stocks" if offer.type_of_title in services.TITRES_DE_CAPITAL else "bonds"


def _cash_disponible(sub_portfolio: SubPortfolio) -> Decimal:
    return services._solde_disponible(sub_portfolio.investor)


@transaction.atomic
def passer_ordre(*, sub_portfolio: SubPortfolio, offer_id, bonds: int,
                 idempotency_key: str, by: str = "", reason: str = "") -> dict:
    """Instruit un ordre de renforcement : contrôles, puis RÉSERVATION.

    Rend `{order, subscription, allocation}` : l'ordre enregistré, la souscription
    créée par le chemin normal, et la répartition recalculée — pour que l'écran
    n'ait à recomposer aucun chiffre.

    L'ordre s'arrête à la réservation, exactement comme une souscription ordinaire :
    l'encaissement reste l'acte distinct constaté par l'institution
    (`POST /subscriptions/<id>/settle`), avec débit réel du portefeuille. Un ordre
    qui encaisserait lui-même serait le « déplacement en un clic » que ce module
    refuse.
    """
    repartition = services.sub_portfolio_allocation(sub_portfolio=sub_portfolio)
    cible = repartition["target"]
    if not cible:
        raise ConflictError(
            "Aucune allocation cible n'est fixée sur ce sous-portefeuille : sans "
            "cible, aucun écart n'est mesurable et aucun ordre n'a de sens."
        )

    offer = Offer.objects.select_related("project").filter(pk=offer_id).first()
    if offer is None:
        raise ValidationFailed("Offre introuvable.", code="OFFER_NOT_FOUND")

    poche = poche_de_l_offre(offer)
    ligne = repartition["buckets"][poche]
    ecart_montant = Decimal(str(ligne["gapAmount"] or 0))
    if ecart_montant <= Decimal("0"):
        raise ConflictError(
            f"La poche « {ligne['label']} » n'est pas en déficit par rapport à votre "
            f"cible ({ligne['actualPercent']} % pour une cible de "
            f"{ligne['targetPercent']} %) : la renforcer creuserait l'écart au lieu "
            "de le combler."
        )

    quantite = int(bonds or 0)
    if quantite <= 0:
        raise ValidationFailed("Le nombre de titres de l'ordre doit être positif.",
                                code="ORDER_BONDS_INVALID")
    montant = arrondir(Decimal(offer.bond_unit_value) * quantite)[0]

    #: Une unité de titre de dépassement est tolérée : l'offre se souscrit par
    #: titres entiers, et exiger le montant exact de l'écart rendrait la plupart
    #: des ordres impossibles. Au-delà, l'ordre est refusé avec le nombre de titres
    #: qui tient dans l'écart — on répond à la question posée.
    plafond = ecart_montant + Decimal(offer.bond_unit_value)
    if montant > plafond:
        tenables = int(ecart_montant / Decimal(offer.bond_unit_value)) if offer.bond_unit_value else 0
        raise ValidationFailed(
            f"Ordre de {montant} USD pour un écart de {ecart_montant} USD sur la poche "
            f"« {ligne['label']} » : il dépasserait la cible et recréerait un écart en "
            f"sens inverse. {tenables} titre(s) au plus tiennent dans l'écart.",
            code="ORDER_EXCEEDS_GAP",
        )

    disponible = _cash_disponible(sub_portfolio)
    if montant > disponible:
        raise ConflictError(
            f"Solde insuffisant : {disponible} USD disponibles pour un ordre de "
            f"{montant} USD. Aucune réservation n'a été enregistrée."
        )

    souscription = funding.reserve(
        investor=sub_portfolio.investor, offer_id=offer.pk, bonds=quantite,
        idempotency_key=idempotency_key, by=by, sub_portfolio=sub_portfolio,
    )

    ordre = RebalancingOrder.objects.create(
        sub_portfolio=sub_portfolio, target_version=cible["version"], bucket=poche,
        direction=RebalancingOrder.Direction.RENFORCER,
        gap_points=Decimal(str(ligne["gapPercent"] or 0)), gap_amount=ecart_montant,
        amount=Decimal(souscription.amount), subscription=souscription,
        reason=(reason or "").strip()[:255], created_by=by or "",
    )
    audit_record(
        actor=by, action="investments.rebalancing.order", entity_type="SubPortfolio",
        entity_id=str(sub_portfolio.pk),
        details={"orderId": ordre.pk, "bucket": poche, "offer": offer.code,
                 "bonds": quantite, "amount": str(ordre.amount),
                 "gapAmount": str(ecart_montant), "targetVersion": cible["version"],
                 "subscription": souscription.pk},
    )

    from . import serializers
    return {
        "order": ordre_row(ordre),
        "subscription": serializers.subscription_row(souscription),
        "allocation": services.sub_portfolio_allocation(sub_portfolio=sub_portfolio),
    }


def ordre_row(ordre: RebalancingOrder) -> dict:
    """Un ordre servi avec l'ÉTAT RÉEL de sa souscription — jamais une copie de statut."""
    sub = ordre.subscription
    return {
        "id": ordre.pk,
        "subPortfolioId": ordre.sub_portfolio_id,
        "bucket": ordre.bucket,
        "bucketLabel": services.LIBELLES_POCHES.get(ordre.bucket, ordre.bucket),
        "direction": ordre.direction,
        "targetVersion": ordre.target_version,
        "gapPercent": float(ordre.gap_points),
        "gapAmount": float(ordre.gap_amount),
        "amount": float(ordre.amount),
        "reason": ordre.reason,
        "createdBy": ordre.created_by,
        "createdAt": ordre.created_at.isoformat(),
        "subscriptionId": ordre.subscription_id,
        "offerCode": sub.offer.code if sub else "",
        #: L'état d'exécution est LU sur la souscription : réservée tant que
        #: l'institution n'a pas constaté l'encaissement, encaissée ensuite.
        "subscriptionStatus": sub.status if sub else None,
        "settledAmount": float(sub.settled_amount) if sub else 0.0,
        "executed": bool(sub and sub.status in Subscription.FUNDED_STATUSES),
    }


def ordres(*, sub_portfolio: SubPortfolio) -> list[dict]:
    qs = (RebalancingOrder.objects.filter(sub_portfolio=sub_portfolio)
          .select_related("subscription__offer"))
    return [ordre_row(o) for o in qs]
