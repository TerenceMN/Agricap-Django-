"""Émission des événements comptables de l'épargne — le point de contact unique entre
`savings` et le grand livre.

Constat à l'origine de ce module : la comptabilité a été branchée aux événements
d'investissement (`accounting/consommation.py`), et l'agent qui l'a construite a relevé
que « `savings` n'émet rien non plus : B8/B9 (dépôt/retrait d'épargne) sont orphelins ».
Un dépôt d'épargne existait donc en base sans jamais atteindre le grand livre.

    savings.plan_deposit / plan_withdraw
              │  (MÊME transaction que le mouvement de wallet et l'inscription au plan)
              ▼
        SavingsEvent  (append-only, consumed_at = NULL)
              │
              ▼
    accounting.consommation  ──►  annexe B : B8 (dépôt) / B9 (retrait)

Trois règles de conduite :

1. **L'événement naît avec l'acte, ou ne naît pas.** `emettre` n'ouvre pas de transaction :
   il est appelé DANS celle de l'acte métier. Un `rollback` emporte les deux ensemble.
   C'est la seule garantie que « ce qui est en base = ce qui est comptabilisé ».

2. **On ne décrit que ce qu'on sait.** L'événement porte le fait (type, montant, devise,
   date, références) ; il ne choisit AUCUN compte. Le compte de trésorerie de B8/B9
   (`$TRESORERIE`) est un arbitrage comptable qui vit en base côté `accounting`
   (`RegleConsommation.compte_tresorerie`) — ce module ne le renseigne délibérément pas
   (cf. `NOTE_FLUX_INTERNE`).

3. **Le sens vient du type, jamais du signe.** Un montant est toujours strictement
   positif ; c'est `event_type` qui dit si l'argent entre ou sort. Un montant négatif est
   refusé plutôt que réinterprété.
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from django.utils import timezone

from common.exceptions import ValidationFailed

from .models import SavingsEvent, SavingsPlan

#: Identifiant de la source d'événements, à la forme de `accounting.definitions
#: .SOURCE_INVESTISSEMENT`. C'est la clé que portera `RegleConsommation.source` pour
#: mapper les types ci-dessous sur les schémas B8/B9 du catalogue.
SOURCE_EPARGNE = "savings.SavingsEvent"

#: ARBITRAGE REMONTÉ AU FONDATEUR — inscrit dans le payload de chaque événement pour que
#: le comptable qui lit une pièce sache d'où vient l'argent.
#:
#: L'annexe B écrit littéralement, pour B8 : « Débit 501/53x — Crédit 412 », c'est-à-dire
#: une ENTRÉE DE CAISSE. Or, depuis la décision « une seule porte » (le wallet est le seul
#: point de contact avec l'extérieur), un dépôt d'épargne DÉBITE le portefeuille du client :
#: le cash est déjà entré dans l'institution au moment de l'alimentation du wallet. Le
#: mouvement est donc INTERNE, et sa contrepartie économique est l'extinction d'une dette
#: de portefeuille envers le client, pas un encaissement.
#:
#: Comptabiliser B8 au débit de 501/511 compterait le même franc deux fois en trésorerie.
#: C'est le MÊME arbitrage que celui déjà remonté pour les souscriptions
#: (`accounting.definitions.COMPTE_TRESORERIE_DEFAUT`). Ce module ne le tranche pas : il
#: le signale, et laisse le compte de contrepartie au paramétrage comptable.
NOTE_FLUX_INTERNE = (
    "Flux INTERNE : la contrepartie réelle est le portefeuille du client (wallet), "
    "pas une entrée de caisse. Compte de contrepartie à arbitrer côté accounting."
)


def q2(valeur) -> Decimal:
    """Quantize monétaire du module (0.01, ROUND_HALF_UP) — P4 : `Decimal` partout."""
    return Decimal(str(valeur)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def emettre(event_type: str, *, plan: SavingsPlan, amount, actor_sub: str = "",
            occurred_at=None, **payload) -> SavingsEvent:
    """Produit l'événement que la comptabilité consommera. Append-only, jamais modifié.

    À appeler DANS la transaction de l'acte métier — jamais après son commit, jamais dans
    une transaction à part : un événement qui survivrait à un acte annulé ferait entrer au
    grand livre un dépôt qui n'a pas eu lieu.

    Le payload est libre mais toujours enrichi de `flux` / `note` : le consommateur
    comptable n'a pas à deviner qu'un dépôt d'épargne est un mouvement interne.
    """
    if event_type not in SavingsEvent.Type.values:
        raise ValidationFailed(f"Type d'événement d'épargne inconnu : {event_type}.")
    montant = q2(amount)
    if montant <= 0:
        raise ValidationFailed(
            f"Montant non exploitable ({montant}) : un événement comptable ne naît pas "
            "d'un montant nul ou négatif (le sens se porte par le type, jamais par le signe)."
        )
    return SavingsEvent.objects.create(
        event_type=event_type,
        plan=plan,
        amount=montant,
        currency=plan.currency,
        occurred_at=occurred_at or timezone.now(),
        actor_sub=actor_sub or "",
        payload={
            "planId": plan.pk,
            "planName": plan.name,
            "holderSub": plan.user_id,
            "flux": "INTERNE",
            "contrepartieReelle": "WALLET_CLIENT",
            "note": NOTE_FLUX_INTERNE,
            **payload,
        },
    )
