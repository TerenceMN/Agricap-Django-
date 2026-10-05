"""
Cycle de l'argent d'une levée : réservation → encaissement → clôture → décaissement
→ retours → distributions.

Le principe qui structure tout le fichier : **la souscription RÉSERVE, elle n'encaisse
pas.** Réserver, c'est prendre un engagement ; encaisser, c'est recevoir de l'argent.
Ce sont deux événements distincts, à deux dates distinctes, et un seul des deux
produit une écriture comptable (B10). Il en découle mécaniquement :

- `Offer.reserved_amount` (engagements) ≠ `Offer.funded_amount` (argent reçu) ;
- aucun décaissement avant clôture de souscription (garde P07→P08) ;
- aucune distribution sans encaissement de retour préalable (B12 avant B13) ;
- le XIRR de l'investisseur est daté sur les ENCAISSEMENTS, jamais sur les réservations.

Ce module ne passe **aucune écriture comptable** : il produit des `InvestmentEvent`
que le moteur d'écritures (`accounting`, autre agent) consomme pour appliquer le
catalogue B10→B13. Il ne détient pas non plus le solde des caisses : le mouvement
réel reste délégué à `transactions`/`caisses` et n'est référencé qu'en FK sur
`Movement.transaction`.
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from django.db import transaction
from django.db.models import F, Sum
from django.utils import timezone

from audit.services import record as audit_record
from common import idempotency
from common.exceptions import BusinessError, ConflictError, InsufficientFundsError, NotFoundError, \
    ValidationFailed
from common.parsing import to_decimal

from . import echeancier_retour, workflow
from .models import (
    Distribution, DistributionLine, InvestmentConfig, InvestmentEvent, Investor, Movement,
    Offer, Project, Subscription,
)

CENT = Decimal("0.01")


def _q(value) -> Decimal:
    return Decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


def segregation_account(offer: Offer) -> str:
    """Sous-compte de cantonnement attendu par la comptabilité (Annexe A : `419-OFF-xxxx`).

    Le cantonnement est l'invariant : on doit pouvoir prouver que l'argent de l'offre X
    n'a pas financé le projet Y. Ce module ne crée pas le compte — il nomme celui que
    la comptabilité doit ouvrir.
    """
    return f"419-OFF-{offer.code}"


# ── Débit du portefeuille client : délégué à `caisses`, jamais réimplémenté ────
#
# Décision du fondateur — « une seule porte » : le wallet est le SEUL point de contact
# avec l'extérieur ; tout autre flux d'argent est INTERNE et débite le WALLET du client.
# Un encaissement de souscription n'est donc pas une entrée externe qu'on « constate » :
# c'est du cash qui quitte réellement le portefeuille du souscripteur. Le débit passe par
# l'UNIQUE service de débit du système (`caisses.services.withdraw`), le même qu'empruntent
# déjà les obligations — pas de second chemin de débit (principe 6).

#: Devise des flux monétaires du module (voir `Movement.currency`, `settle`).
WALLET_CURRENCY = "USD"


class WalletDebitError(BusinessError):
    """Refus d'un débit de portefeuille dans un flux d'investissement.

    Même contrat `{code, message}` que `credits.workflow.WorkflowError` /
    `obligations.ObligationError` : le `code` est consommé par le front, le message
    explique sans se parser. `as_errors()` produit la liste attendue par les écrans.
    """

    code = "WALLET_DEBIT_ERROR"
    http_status = 422

    def as_errors(self) -> list[dict]:
        return [{"code": self.code, "message": self.message}]


class WalletMissingError(WalletDebitError):
    """Aucun portefeuille dans la devise du flux : il n'y a pas de cash à débiter."""

    code = "WALLET_MISSING"


class InsufficientWalletError(WalletDebitError):
    """Solde de portefeuille insuffisant — refus AVANT toute écriture, jamais partiel."""

    code = "WALLET_INSUFFICIENT_FUNDS"


def debit_investor_wallet(*, investor: Investor, amount, idempotency_key: str, by: str = ""):
    """Débite le portefeuille de l'investisseur via le SEUL service de débit du système.

    `caisses.services.withdraw` pose le verrou de ligne (`select_for_update`), refuse un
    solde insuffisant, journalise le `WalletMovement` et l'audit. Ce module n'écrit jamais
    dans les modèles de `caisses` — il appelle son service. C'est l'unique chemin de débit
    de l'app investissement : l'encaissement de souscription l'emprunte, exactement comme
    les obligations (principe 6, pas de second chemin de débit)."""
    from caisses.models import ClientWallet
    from caisses.services import withdraw as caisses_withdraw

    montant = _q(amount)
    wallet = ClientWallet.objects.filter(user=investor.user, currency=WALLET_CURRENCY).first()
    if wallet is None:
        raise WalletMissingError(
            f"Aucun portefeuille {WALLET_CURRENCY} sur ce compte : il n'y a pas de cash "
            "à engager. Alimentez d'abord le portefeuille."
        )
    try:
        return caisses_withdraw(wallet_id=wallet.pk, amount=montant,
                                 idempotency_key=idempotency_key, by=by)
    except InsufficientFundsError as exc:
        raise InsufficientWalletError(
            f"Solde insuffisant : {_q(wallet.balance)} {WALLET_CURRENCY} disponibles "
            f"pour un encaissement de {montant} {WALLET_CURRENCY}. Aucun encaissement "
            "n'a été effectué."
        ) from exc


def _emit(event_type: str, *, project: Project | None = None, offer: Offer | None = None,
          subscription: Subscription | None = None, investor: Investor | None = None,
          amount: Decimal = Decimal("0"), currency: str = "USD", occurred_at=None,
          actor_sub: str = "", **payload) -> InvestmentEvent:
    """Produit l'événement métier que la comptabilité consommera. Append-only."""
    return InvestmentEvent.objects.create(
        event_type=event_type, project=project, offer=offer, subscription=subscription,
        investor=investor, amount=_q(amount), currency=currency,
        segregation_account=segregation_account(offer) if offer else "",
        occurred_at=occurred_at or timezone.now(), actor_sub=actor_sub or "",
        payload=payload,
    )


# ── Rattachement d'une souscription à une poche ───────────────────────────────


def _poche_de_l_investisseur(*, investor: Investor, sub_portfolio):
    """Résout une poche et vérifie qu'elle appartient bien à cet investisseur.

    Accepte un identifiant ou une instance. `None`/vide = souscription non classée,
    ce qui reste un état légitime : ranger n'est pas obligatoire.
    """
    from .models import SubPortfolio

    if sub_portfolio in (None, "", 0):
        return None
    if isinstance(sub_portfolio, SubPortfolio):
        poche = sub_portfolio
    else:
        try:
            poche = SubPortfolio.objects.filter(pk=int(sub_portfolio)).first()
        except (TypeError, ValueError):
            raise ValidationFailed("Identifiant de sous-portefeuille illisible.",
                                    code="SUB_PORTFOLIO_INVALID") from None
    if poche is None or poche.investor_id != investor.pk:
        raise ValidationFailed(
            "Ce sous-portefeuille n'existe pas dans votre espace : une souscription ne "
            "se range que dans une de vos propres poches.",
            code="SUB_PORTFOLIO_NOT_YOURS",
        )
    return poche


@transaction.atomic
def assign_sub_portfolio(*, subscription: Subscription, sub_portfolio, by: str = "",
                         reason: str = "") -> Subscription:
    """Réaffecte une souscription à une autre poche — journalisé, avec l'ancienne valeur.

    Le classement d'une souscription n'engage aucun franc : il ne change ni le
    montant, ni le statut, ni l'offre. Il change en revanche la répartition affichée
    et l'écart à la cible d'allocation — donc la lecture sur laquelle un
    investisseur décide. La trace porte donc l'AVANT et l'APRÈS : « d'où vient cette
    position ? » doit rester répondable.
    """
    sub = Subscription.objects.select_for_update().select_related("investor").get(
        pk=subscription.pk)
    poche = _poche_de_l_investisseur(investor=sub.investor, sub_portfolio=sub_portfolio)
    ancienne = sub.sub_portfolio_id
    if ancienne == (poche.pk if poche else None):
        return sub
    sub.sub_portfolio = poche
    sub.save(update_fields=["sub_portfolio", "updated_at"])
    audit_record(
        actor=by, action="investments.subscription.assign_sub_portfolio",
        entity_type="Subscription", entity_id=str(sub.pk),
        details={"from": ancienne, "to": poche.pk if poche else None,
                 "toName": poche.name if poche else "", "reason": (reason or "").strip()},
    )
    return sub


# ── Réservation ───────────────────────────────────────────────────────────────

@transaction.atomic
def reserve(*, investor: Investor, offer_id: int, bonds: int, idempotency_key: str,
            by: str = "", sub_portfolio=None) -> Subscription:
    """Réserve `bonds` titres sur une offre. AUCUN argent n'est encaissé ici.

    Sursouscription (politique portée par l'offre, principe 8) :

    - `REJECT`  : la réservation qui ferait dépasser l'objectif est refusée ;
    - `QUEUE`   : elle est acceptée en liste d'attente, avec son rang ;
    - `PRORATA` : elle est acceptée en entier ; l'allocation réelle est calculée à la
      clôture, et l'encaissement est refusé tant qu'elle n'est pas connue.

    `sub_portfolio` range la souscription dans une poche de l'investisseur dès sa
    naissance. La poche est vérifiée comme APPARTENANT au souscripteur : sans ce
    contrôle, un investisseur classerait sa souscription dans le portefeuille d'un
    autre, et la répartition par poche cesserait d'être la sienne.
    """
    bonds = int(bonds or 0)
    if bonds <= 0:
        raise ValidationFailed("Le nombre d'obligations souscrites doit être positif.")
    if investor.status != Investor.Status.ACTIVE:
        raise ConflictError("Ce profil investisseur est suspendu : aucune souscription possible.")

    poche = _poche_de_l_investisseur(investor=investor, sub_portfolio=sub_portfolio)

    rec = idempotency.begin(
        scope="investments.subscribe", key=idempotency_key,
        params={"investor": investor.pk, "offer": offer_id, "bonds": bonds,
                "subPortfolio": poche.pk if poche else None}, by=by,
    )

    offer = Offer.objects.select_for_update().select_related("project").filter(pk=offer_id).first()
    if not offer:
        raise NotFoundError("Offre introuvable.")
    if offer.project.status != Project.Status.P06:
        raise ConflictError("Ce projet n'est pas ouvert à la souscription.")
    if offer.status != Offer.Status.OUVERT:
        raise ConflictError(f"L'offre {offer.code} n'est pas ouverte (statut « {offer.status} »).")
    if offer.subscription_deadline and offer.subscription_deadline < timezone.localdate():
        raise ConflictError(
            f"La période de souscription de l'offre {offer.code} s'est achevée le "
            f"{offer.subscription_deadline.isoformat()}."
        )
    if bonds < offer.min_bonds:
        raise ValidationFailed(f"Minimum {offer.min_bonds} titre(s) par souscription.")
    if bonds > offer.available_bonds:
        raise ValidationFailed(
            f"Nombre d'obligations hors bornes (min={offer.min_bonds}, "
            f"disponible={offer.available_bonds})."
        )

    amount = _q(offer.bond_unit_value * bonds)
    if offer.min_ticket and amount < offer.min_ticket:
        raise ValidationFailed(f"Ticket minimum de l'offre : {offer.min_ticket}.")

    goal = Decimal(offer.funding_goal)
    depasse = bool(goal) and (Decimal(offer.reserved_amount) + amount) > goal
    policy = offer.oversubscription_policy or InvestmentConfig.active().default_oversubscription_policy

    status = Subscription.Status.RESERVED
    queue_rank = 0
    allocated = amount
    if depasse:
        if policy == Offer.Oversubscription.REJECT:
            reste = (goal - Decimal(offer.reserved_amount)).quantize(CENT)
            raise ConflictError(
                f"Offre sursouscrite : il ne reste que {reste} sur l'objectif de {goal}."
            )
        if policy == Offer.Oversubscription.QUEUE:
            status = Subscription.Status.WAITLISTED
            queue_rank = offer.subscriptions.filter(
                status=Subscription.Status.WAITLISTED).count() + 1
            allocated = Decimal("0")
    if policy == Offer.Oversubscription.PRORATA:
        # L'allocation n'est connue qu'à la clôture : on ne promet rien d'avance.
        allocated = Decimal("0")

    subscription = Subscription.objects.create(
        investor=investor, offer=offer, amount=amount, allocated_amount=_q(allocated),
        bonds=bonds, status=status, queue_rank=queue_rank, sub_portfolio=poche,
        coupon_rate_snapshot=offer.coupon_rate, reserved_at=timezone.now(), created_by=by,
    )
    updates = {"available_bonds": F("available_bonds") - bonds}
    if status == Subscription.Status.RESERVED:
        updates["reserved_amount"] = F("reserved_amount") + amount
    Offer.objects.filter(pk=offer.pk).update(**updates)

    # Trace applicative de la réservation — statut `draft` : rien n'est comptabilisé.
    Movement.objects.create(
        type=Movement.Type.SUBSCRIPTION, investor=investor, project=offer.project,
        subscription=subscription, assigned_manager_sub=offer.project.manager_sub,
        amount=amount, currency="USD", status="draft",
    )

    audit_record(actor=by, action="investments.subscription.reserve", entity_type="Subscription",
                 entity_id=str(subscription.pk),
                 details={"offer": offer.code, "bonds": bonds, "amount": str(amount),
                          "status": status, "policy": policy, "queueRank": queue_rank,
                          "subPortfolio": poche.pk if poche else None})

    from . import serializers
    idempotency.complete(rec, response=serializers.subscription_row(subscription),
                          entity_type="Subscription", entity_id=str(subscription.pk))
    return subscription


@transaction.atomic
def cancel_reservation(*, subscription: Subscription, by: str = "", reason: str = "") -> Subscription:
    """Annule une réservation NON encaissée et libère les titres.

    Une souscription encaissée ne s'annule pas : elle se rembourse (`refund`), parce
    qu'un encaissement comptabilisé se contrepasse, il ne s'efface pas.
    """
    # Relue et verrouillée en base : le statut porté par l'instance de l'appelant peut
    # être périmé (encaissement intervenu entre-temps), et annuler une souscription
    # déjà encaissée rendrait des titres tout en gardant l'argent.
    sub = Subscription.objects.select_for_update().get(pk=subscription.pk)
    if sub.status not in (Subscription.Status.RESERVED, Subscription.Status.WAITLISTED):
        raise ConflictError(
            "Seule une réservation non encaissée s'annule ; une souscription encaissée "
            "se rembourse (contrepassation de l'encaissement)."
        )
    updates = {"available_bonds": F("available_bonds") + sub.bonds}
    if sub.status == Subscription.Status.RESERVED:
        updates["reserved_amount"] = F("reserved_amount") - sub.amount
    Offer.objects.filter(pk=sub.offer_id).update(**updates)

    sub.status = Subscription.Status.CANCELLED
    sub.allocated_amount = Decimal("0")
    sub.save(update_fields=["status", "allocated_amount", "updated_at"])
    audit_record(actor=by, action="investments.subscription.cancel", entity_type="Subscription",
                 entity_id=str(sub.pk), details={"reason": reason})
    return sub


# ── Encaissement (B10) ────────────────────────────────────────────────────────

@transaction.atomic
def settle(*, subscription: Subscription, idempotency_key: str, by: str = "",
           amount=None, value_date=None) -> Subscription:
    """Encaisse une souscription réservée — l'événement comptable B10.

    C'est ici, et nulle part ailleurs, que `funded_amount` bouge. La date
    d'encaissement (`settled_at`) est la date de flux du XIRR : elle est réelle,
    jamais reconstruite depuis la date de réservation.
    """
    sub = Subscription.objects.select_for_update().select_related("offer__project").get(pk=subscription.pk)
    if sub.status != Subscription.Status.RESERVED:
        raise ConflictError(
            f"Seule une souscription réservée s'encaisse (statut courant : « {sub.status} »)."
        )
    project = sub.offer.project
    if project.status not in (Project.Status.P06, Project.Status.P07):
        raise ConflictError(
            "L'encaissement n'est possible que pendant la levée (P06) ou entre la "
            f"clôture et le décaissement (P07) — statut courant : « {project.status} »."
        )
    if sub.allocated_amount <= Decimal("0"):
        raise ConflictError(
            "L'allocation de cette souscription n'est pas connue : sur une offre au "
            "prorata, l'encaissement n'a lieu qu'après la clôture de la souscription."
        )

    montant = _q(to_decimal(amount)) if amount is not None else _q(sub.allocated_amount)
    if montant <= Decimal("0"):
        raise ValidationFailed("Le montant encaissé doit être positif.")
    if montant > _q(sub.allocated_amount):
        raise ValidationFailed(
            f"Encaissement de {montant} supérieur au montant alloué ({sub.allocated_amount})."
        )

    rec = idempotency.begin(
        scope="investments.settle", key=idempotency_key,
        params={"subscription": sub.pk, "amount": str(montant)}, by=by,
    )

    now = timezone.now()
    sub.status = Subscription.Status.SETTLED
    sub.settled_amount = montant
    sub.settled_at = now
    sub.payment_status = Subscription.PaymentStatus.PAID
    sub.save(update_fields=["status", "settled_amount", "settled_at", "payment_status", "updated_at"])

    Offer.objects.filter(pk=sub.offer_id).update(funded_amount=F("funded_amount") + montant)
    Project.objects.filter(pk=project.pk).update(funded_amount=F("funded_amount") + montant)

    movement = Movement.objects.create(
        type=Movement.Type.SETTLEMENT, investor=sub.investor, project=project, subscription=sub,
        assigned_manager_sub=project.manager_sub, amount=montant, currency="USD", status="posted",
    )
    _emit(InvestmentEvent.Type.SUBSCRIPTION_SETTLED, project=project, offer=sub.offer,
          subscription=sub, investor=sub.investor, amount=montant, occurred_at=now,
          actor_sub=by, movementId=movement.pk, offerCode=sub.offer.code,
          projectCode=project.code)

    audit_record(actor=by, action="investments.subscription.settle", entity_type="Subscription",
                 entity_id=str(sub.pk),
                 details={"amount": str(montant), "offer": sub.offer.code,
                          "valueDate": str(value_date) if value_date else now.date().isoformat()})

    from . import serializers
    idempotency.complete(rec, response=serializers.subscription_row(sub),
                          entity_type="Subscription", entity_id=str(sub.pk))
    project.refresh_from_db()
    return sub


@transaction.atomic
def settle_from_wallet(*, subscription: Subscription, idempotency_key: str, by: str = "",
                       amount=None, value_date=None) -> Subscription:
    """Encaisse une souscription EN DÉBITANT le portefeuille du souscripteur (flux interne).

    Décision du fondateur — « une seule porte » : l'argent d'une souscription vient du
    WALLET du client, jamais d'une entrée externe. L'encaissement comptable (`settle`,
    B10) et le débit réel du portefeuille vivent dans UNE transaction : si le solde est
    insuffisant, `settle` est annulé et la souscription reste réservée. Il n'existe donc
    jamais d'encaissement inscrit (ni de `funded_amount` qui bouge) sans un débit du même
    montant — le piège de l'« argent créé de rien » est fermé.

    `settle` porte déjà toutes les gardes de statut (souscription réservée, projet en
    P06/P07, allocation connue) et calcule le montant encaissé : on débite exactement ce
    qu'il a encaissé (`settled_amount`). Le débit passe par `debit_investor_wallet` — le
    même chemin que les obligations (principe 6), qui empruntent `settle` de leur côté
    après avoir débité eux-mêmes ; il n'y a jamais double débit.
    """
    rec = idempotency.begin(
        scope="investments.settle_from_wallet", key=idempotency_key,
        params={"subscription": subscription.pk,
                "amount": str(amount) if amount is not None else ""}, by=by,
    )
    sub = settle(subscription=subscription, idempotency_key=f"settle-b10:{idempotency_key}",
                 by=by, amount=amount, value_date=value_date)
    debit_investor_wallet(investor=sub.investor, amount=sub.settled_amount,
                          idempotency_key=f"settle-debit:{idempotency_key}", by=by)

    from . import serializers
    idempotency.complete(rec, response=serializers.subscription_row(sub),
                          entity_type="Subscription", entity_id=str(sub.pk))
    return sub


# ── Clôture de la souscription (P06 → P07 ou P13) ────────────────────────────

def _allocate_prorata(offer: Offer) -> None:
    """Réduit chaque réservation au prorata de l'objectif ; le reliquat va à la
    dernière ligne pour que Σ allocations = objectif au centime près."""
    subs = list(offer.subscriptions.filter(status=Subscription.Status.RESERVED).order_by("created_at", "pk"))
    total = sum((Decimal(s.amount) for s in subs), Decimal("0"))
    goal = Decimal(offer.funding_goal)
    if not subs:
        return
    if not goal or total <= goal:
        for s in subs:
            s.allocated_amount = _q(s.amount)
            s.save(update_fields=["allocated_amount", "updated_at"])
        return
    cumul = Decimal("0")
    for s in subs[:-1]:
        part = _q(Decimal(s.amount) * goal / total)
        s.allocated_amount = part
        s.save(update_fields=["allocated_amount", "updated_at"])
        cumul += part
    dernier = subs[-1]
    dernier.allocated_amount = _q(goal - cumul)
    dernier.save(update_fields=["allocated_amount", "updated_at"])


@transaction.atomic
def close_offer(*, offer: Offer, by: str = "") -> Offer:
    """Ferme une offre : alloue (prorata) et purge la liste d'attente non servie."""
    offer = Offer.objects.select_for_update().get(pk=offer.pk)
    if offer.status == Offer.Status.CLOTURE:
        return offer
    if offer.oversubscription_policy == Offer.Oversubscription.PRORATA:
        _allocate_prorata(offer)
    for waitlisted in offer.subscriptions.filter(status=Subscription.Status.WAITLISTED):
        cancel_reservation(subscription=waitlisted, by=by,
                            reason="Liste d'attente non servie à la clôture de la souscription.")
    offer.status = Offer.Status.CLOTURE
    offer.closed_at = timezone.now()
    offer.reserved_amount = _q(
        offer.subscriptions.filter(status__in=Subscription.LIVE_STATUSES)
        .aggregate(t=Sum("allocated_amount"))["t"] or Decimal("0")
    )
    offer.save(update_fields=["status", "closed_at", "reserved_amount", "updated_at"])
    audit_record(actor=by, action="investments.offer.close", entity_type="Offer",
                 entity_id=offer.code, details={"reservedAmount": str(offer.reserved_amount)})
    return offer


@transaction.atomic
def close_fundraising(*, project: Project, by: str = "", reason: str = "") -> Project:
    """Clôture la levée : P06 → P07 si le min-funding est atteint, P06 → … → P13 sinon.

    « En deçà du seuil à l'échéance, les souscripteurs sont remboursés » : la branche
    d'échec rembourse d'abord (contrepassation de B10) et annule ensuite — l'ordre
    compte, la garde de P13 refuse une annulation qui laisserait de l'argent encaissé.
    """
    if project.status != Project.Status.P06:
        raise ConflictError(
            f"Seule une levée active se clôture (statut courant : « {project.status} »)."
        )
    reason = (reason or "").strip() or "Clôture de la période de souscription."

    for offer in project.offers.all():
        close_offer(offer=offer, by=by)
    project.refresh_from_db()

    plancher = workflow.min_funding_floor(project)
    engage = workflow.committed_amount(project)
    if engage < plancher:
        motif = (
            f"Min-funding non atteint à l'échéance : {engage} engagé pour un plancher "
            f"de {plancher}. Souscripteurs remboursés."
        )
        refund_project(project=project, by=by, reason=motif)
        project.refresh_from_db()
        return workflow.transition(project, to_status=Project.Status.P13, actor_sub=by,
                                    reason=motif,
                                    details={"committed": str(engage), "floor": str(plancher)})

    return workflow.transition(project, to_status=Project.Status.P07, actor_sub=by, reason=reason,
                                details={"committed": str(engage), "floor": str(plancher),
                                         "settled": str(project.funded_amount)})


# ── Remboursement (contrepassation B10) ───────────────────────────────────────

@transaction.atomic
def refund_project(*, project: Project, by: str = "", reason: str = "") -> Decimal:
    """Rembourse toutes les souscriptions encaissées d'un projet et libère les réservations.

    Refusé après décaissement : l'argent n'est plus dans le cantonnement, il est chez
    le promoteur — le chemin est alors le défaut (P12) et le recouvrement, pas le
    remboursement.
    """
    if project.status in (Project.Status.P08, Project.Status.P09, Project.Status.P10,
                          Project.Status.P11, Project.Status.P12):
        raise ConflictError(
            "Les fonds ont déjà été décaissés : ils ne peuvent plus être remboursés "
            "aux souscripteurs (voir le défaut P12 et le plan de recouvrement)."
        )
    reason = (reason or "").strip() or "Remboursement des souscriptions."
    total = Decimal("0")
    now = timezone.now()

    for sub in Subscription.objects.select_for_update().filter(
        offer__project=project, status__in=Subscription.FUNDED_STATUSES,
    ).select_related("offer"):
        montant = _q(sub.settled_amount)
        if montant <= Decimal("0"):
            continue
        sub.status = Subscription.Status.REFUNDED
        sub.refunded_amount = montant
        sub.refunded_at = now
        sub.save(update_fields=["status", "refunded_amount", "refunded_at", "updated_at"])
        Offer.objects.filter(pk=sub.offer_id).update(funded_amount=F("funded_amount") - montant)
        Movement.objects.create(
            type=Movement.Type.REFUND, investor=sub.investor, project=project, subscription=sub,
            amount=montant, currency="USD", status="reversed",
        )
        _emit(InvestmentEvent.Type.SUBSCRIPTION_REFUNDED, project=project, offer=sub.offer,
              subscription=sub, investor=sub.investor, amount=montant, occurred_at=now,
              actor_sub=by, reason=reason, projectCode=project.code)
        total += montant

    for sub in Subscription.objects.select_for_update().filter(
        offer__project=project,
        status__in=(Subscription.Status.RESERVED, Subscription.Status.WAITLISTED),
    ):
        cancel_reservation(subscription=sub, by=by, reason=reason)

    if total:
        Project.objects.filter(pk=project.pk).update(funded_amount=F("funded_amount") - total)
        project.refresh_from_db()
    audit_record(actor=by, action="investments.project.refund", entity_type="Project",
                 entity_id=project.code, details={"total": str(_q(total)), "reason": reason})
    return _q(total)


@transaction.atomic
def cancel_project(*, project: Project, by: str = "", reason: str = "") -> Project:
    """Annule un projet (P13) — avant P08 uniquement, souscriptions remboursées d'abord."""
    reason = (reason or "").strip()
    if not reason:
        raise ValidationFailed("L'annulation d'un projet exige un motif.")
    refund_project(project=project, by=by, reason=reason)
    project.refresh_from_db()
    return workflow.transition(project, to_status=Project.Status.P13, actor_sub=by, reason=reason)


# ── Décaissement (B11) ────────────────────────────────────────────────────────

@transaction.atomic
def disburse(*, project: Project, amount, idempotency_key: str, by: str = "",
             reason: str = "") -> Project:
    """Décaisse vers le promoteur — B11 — et fait passer le projet en P08.

    Impossible avant clôture de souscription : la machine à états n'offre P08 que
    depuis P07. Impossible au-delà du cantonnement disponible : on ne décaisse jamais
    plus que ce qui a été encaissé pour CE projet (ségrégation des fonds).

    **Le décaissement produit l'échéancier de retour** (`echeancier_retour`), dans
    cette transaction : l'obligation de rendre naît le jour où l'argent part, et
    c'est cet échéancier qui rend P10 atteignable, « prochain paiement » calculable
    et chaque encaissement B12 ventilable. Des termes d'offre inexploitables font
    donc échouer le décaissement, plutôt que de laisser un projet décaissé sans
    calendrier de retour.
    """
    if project.status != Project.Status.P07:
        raise ConflictError(
            "Le décaissement suppose une souscription clôturée (P07) — statut courant : "
            f"« {project.status} ». Aucun décaissement avant clôture."
        )
    montant = _q(to_decimal(amount))
    if montant <= Decimal("0"):
        raise ValidationFailed("Le montant décaissé doit être positif.")
    disponible = workflow.segregated_balance(project)
    if montant > disponible:
        raise ValidationFailed(
            f"Décaissement de {montant} supérieur au cantonnement disponible du projet "
            f"({disponible}) : l'argent d'un autre projet ne finance pas celui-ci."
        )

    rec = idempotency.begin(scope="investments.disburse", key=idempotency_key,
                             params={"project": project.pk, "amount": str(montant)}, by=by)

    now = timezone.now()
    Project.objects.filter(pk=project.pk).update(disbursed_amount=F("disbursed_amount") + montant)
    project.refresh_from_db()
    offer = project.offers.order_by("pk").first()
    Movement.objects.create(type=Movement.Type.DISBURSEMENT, project=project, amount=montant,
                             currency="USD", status="posted",
                             assigned_manager_sub=project.manager_sub)
    _emit(InvestmentEvent.Type.PROJECT_DISBURSED, project=project, offer=offer, amount=montant,
          occurred_at=now, actor_sub=by, projectCode=project.code, reason=reason)
    echeancier_retour.generer_pour_projet(
        project=project, base_total=montant, date_depart=timezone.localdate(), by=by,
    )

    project = workflow.transition(
        project, to_status=Project.Status.P08, actor_sub=by,
        reason=reason or f"Décaissement de {montant} au promoteur.",
        details={"amount": str(montant)}, skip_guard=False,
    )
    Subscription.objects.filter(offer__project=project, status=Subscription.Status.SETTLED).update(
        status=Subscription.Status.ACTIVE, updated_at=now,
    )
    from . import serializers
    idempotency.complete(rec, response=serializers.project_row(project),
                          entity_type="Project", entity_id=project.code)
    return project


# ── Retours du projet (B12) ───────────────────────────────────────────────────

@transaction.atomic
def record_return(*, project: Project, amount, idempotency_key: str, by: str = "",
                  value_date=None, reason: str = "") -> Project:
    """Encaisse un retour du projet — B12. Aucune distribution n'est possible avant.

    **L'événement porte sa ventilation.** Le schéma B12 ventile l'encaissement entre
    le cantonnement 419-OFF (capital rendu) et le compte 719 (rendement) « selon
    l'échéancier » : cette répartition ne se déduit pas d'un total, et la deviner
    fabriquerait un produit financier qui n'a jamais existé — elle fausserait
    directement le rendement affiché aux investisseurs. Elle est donc calculée ici,
    par imputation déterministe sur l'échéancier de retour du projet
    (`echeancier_retour.ventiler_retour`), et publiée dans le payload de
    l'événement sous les clés attendues par le catalogue : `retour_total`,
    `capital_rembourse`, `rendement` — avec l'invariant
    `capital_rembourse + rendement == amount` au centime.

    Un projet décaissé AVANT l'existence du producteur d'échéanciers n'en porte
    aucun : l'événement est alors émis SANS ventilation, avec le motif. Il reste en
    file côté comptabilité au lieu d'y entrer faux.
    """
    if project.status not in (Project.Status.P09, Project.Status.P10, Project.Status.P12):
        raise ConflictError(
            "Un retour ne s'encaisse que sur un projet en cours, en remboursement ou en "
            f"défaut (statut courant : « {project.status} »)."
        )
    montant = _q(to_decimal(amount))
    if montant <= Decimal("0"):
        raise ValidationFailed("Le montant encaissé doit être positif.")

    rec = idempotency.begin(scope="investments.return", key=idempotency_key,
                             params={"project": project.pk, "amount": str(montant),
                                     "valueDate": str(value_date or "")}, by=by)

    now = timezone.now()
    Project.objects.filter(pk=project.pk).update(returned_amount=F("returned_amount") + montant)
    project.refresh_from_db()
    offer = project.offers.order_by("pk").first()
    mouvement = Movement.objects.create(type=Movement.Type.PROJECT_RETURN, project=project,
                                         amount=montant, currency="USD", status="posted")
    ventilation = echeancier_retour.ventiler_retour(project=project, montant=montant,
                                                     movement=mouvement)
    detail_ventilation = {}
    if ventilation["disponible"]:
        detail_ventilation = {
            "retour_total": str(ventilation["retour_total"]),
            "capital_rembourse": str(ventilation["capital_rembourse"]),
            "rendement": str(ventilation["rendement"]),
        }
        if ventilation["surplus"] > Decimal("0"):
            # Un encaissement au-delà de tout l'échéancier n'est pas du capital :
            # le principal dû est déjà intégralement imputé. Il est signalé pour
            # que le comptable le voie, jamais fondu dans le remboursement.
            detail_ventilation["surplusEnRendement"] = str(ventilation["surplus"])
    else:
        detail_ventilation = {"ventilationIndisponible": ventilation["motif"]}

    _emit(InvestmentEvent.Type.PROJECT_RETURN_RECEIVED, project=project, offer=offer,
          amount=montant, occurred_at=now, actor_sub=by, projectCode=project.code,
          valueDate=str(value_date) if value_date else now.date().isoformat(), reason=reason,
          **detail_ventilation)
    audit_record(actor=by, action="investments.project.return", entity_type="Project",
                 entity_id=project.code,
                 details={"amount": str(montant),
                          "echeancesImputees": len(ventilation["lignes_imputees"]),
                          **detail_ventilation})

    from . import serializers
    idempotency.complete(rec, response=serializers.project_row(project),
                          entity_type="Project", entity_id=project.code)
    return project


# ── Distribution au prorata (B13) ─────────────────────────────────────────────

@transaction.atomic
def distribute(*, offer: Offer, amount, kind: str = Distribution.Kind.COUPON,
               idempotency_key: str, by: str = "", value_date=None) -> Distribution:
    """Distribue `amount` aux souscripteurs ENCAISSÉS de l'offre, au prorata.

    Deux invariants :

    - **pas de distribution sans encaissement** : le cumul distribué ne peut dépasser
      le cumul des retours encaissés du projet (B12) ;
    - **le prorata se calcule sur les montants encaissés**, jamais sur les montants
      réservés — servir quelqu'un au prorata de ce qu'il avait promis serait payer une
      intention.
    """
    project = offer.project
    montant = _q(to_decimal(amount))
    if montant <= Decimal("0"):
        raise ValidationFailed("Le montant distribué doit être positif.")
    if kind not in Distribution.Kind.values:
        # La NATURE de la distribution n'est pas décorative : `metrics` amortit le
        # capital restant dû sur les seules distributions CAPITAL et nette les intérêts
        # courus des seules distributions COUPON. Un `kind` fantaisiste accepté ici
        # fausserait durablement la valorisation du portefeuille.
        raise ValidationFailed(
            f"Nature de distribution inconnue : « {kind} ». Valeurs admises : "
            f"{', '.join(Distribution.Kind.values)}."
        )
    if project.status not in (Project.Status.P10, Project.Status.P09):
        raise ConflictError(
            "Les distributions ont lieu pendant la phase de remboursement du projet "
            f"(statut courant : « {project.status} »)."
        )
    encaisse = Decimal(project.returned_amount) - Decimal(project.distributed_amount)
    if montant > encaisse:
        raise ConflictError(
            f"Distribution de {montant} pour seulement {_q(encaisse)} encaissé et non "
            "encore distribué : pas de distribution sans encaissement."
        )

    subs = list(offer.subscriptions.filter(status__in=Subscription.FUNDED_STATUSES,
                                            settled_amount__gt=Decimal("0")).order_by("pk"))
    base = sum((Decimal(s.settled_amount) for s in subs), Decimal("0"))
    if not subs or base <= Decimal("0"):
        raise ConflictError("Aucune souscription encaissée sur cette offre : rien à distribuer.")

    rec = idempotency.begin(scope="investments.distribute", key=idempotency_key,
                             params={"offer": offer.pk, "amount": str(montant), "kind": kind}, by=by)

    now = timezone.now()
    distribution = Distribution.objects.create(
        offer=offer, kind=kind, total_amount=montant, currency="USD",
        value_date=value_date or now.date(), executed_by=by,
    )
    cumul = Decimal("0")
    for sub in subs[:-1]:
        part = _q(montant * Decimal(sub.settled_amount) / base)
        _distribution_line(distribution, sub, part, base, now, by, project, offer)
        cumul += part
    dernier = subs[-1]
    _distribution_line(distribution, dernier, _q(montant - cumul), base, now, by, project, offer)

    Project.objects.filter(pk=project.pk).update(distributed_amount=F("distributed_amount") + montant)
    audit_record(actor=by, action="investments.distribution.execute", entity_type="Distribution",
                 entity_id=str(distribution.pk),
                 details={"offer": offer.code, "amount": str(montant), "kind": kind,
                          "beneficiaries": len(subs)})

    idempotency.complete(rec, response={"id": distribution.pk, "totalAmount": float(montant)},
                          entity_type="Distribution", entity_id=str(distribution.pk))
    return distribution


def _distribution_line(distribution, sub, part, base, now, by, project, offer) -> None:
    share = (Decimal(sub.settled_amount) / base).quantize(Decimal("0.00000001"))
    DistributionLine.objects.create(distribution=distribution, subscription=sub,
                                     investor=sub.investor, share=share, amount=part)
    Subscription.objects.filter(pk=sub.pk).update(total_received=F("total_received") + part)
    Movement.objects.create(
        type=Movement.Type.DISTRIBUTION, investor=sub.investor, project=project,
        subscription=sub, amount=part, currency="USD", status="posted",
    )
    _emit(InvestmentEvent.Type.DISTRIBUTION_PAID, project=project, offer=offer, subscription=sub,
          investor=sub.investor, amount=part, occurred_at=now, actor_sub=by,
          distributionId=distribution.pk, kind=distribution.kind, share=str(share))
