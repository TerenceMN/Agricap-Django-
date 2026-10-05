"""Émission de la file d'événements comptables du crédit (annexe B, B1→B4).

Pourquoi ce module existe
-------------------------
La comptabilité (`accounting.consommation`) sait consommer une file d'événements
métier depuis que le module d'investissement en produit une (B10→B13). Le crédit
— le métier PRINCIPAL de l'institution — n'en produisait aucune : aucun
décaissement, aucun remboursement, aucun intérêt n'atteignait le grand livre. Le
compte 413 restait vide, le provisionnement refusait de déclasser un encours
qu'il ne voyait pas, et l'encours comptable divergeait de `portfolio` sans que
rien ne le signale.

Ce module est le producteur manquant. Il n'écrit AUCUNE écriture : il déclare des
faits. La comptabilité les lit à l'heure qu'elle choisit, avec le mapping qu'elle
paramètre en base (`RegleConsommation`), et marque ce qu'elle a consommé.

    credits (ici)                    accounting (là-bas)
    ─────────────                    ───────────────────
    confirm_disbursement ──► CreditEvent(CREDIT_DISBURSED) ──► B1 : 413 / trésorerie
    encaissement capital ──► CreditEvent(CREDIT_PRINCIPAL_REPAID) ──► B2 : trésorerie / 413
    encaissement intérêts ─► CreditEvent(CREDIT_INTEREST_COLLECTED) ─► B3 : trésorerie / 701
    commission ───────────► CreditEvent(CREDIT_COMMISSION_COLLECTED) ► B4 : trésorerie / 702

Quatre règles de conduite
-------------------------
1. **Dans la transaction de l'acte métier.** Un décaissement qui existerait sans
   son événement serait un franc sorti sans trace comptable — la pire des
   divergences, parce qu'elle est silencieuse. Les émetteurs sont donc appelés à
   l'intérieur du `transaction.atomic()` de l'acte, et signalent (warning) toute
   émission faite hors transaction.

2. **Un fait, un montant.** Une échéance encaissée produit DEUX événements
   (capital B2 et intérêts B3), jamais un « remboursement » global : ils ne
   mouvementent ni les mêmes comptes ni les mêmes classes, et la ventilation ne
   se déduit pas d'un total. Le consommateur refuse — à juste titre — de la
   deviner : `emettre_echeance` la fait donc en amont, là où elle est connue.

3. **Un montant nul ne produit rien.** Une échéance sans intérêts (prêt bloqué à
   taux 0) n'émet pas d'événement d'intérêts : une écriture de zéro n'est pas une
   écriture, elle encombre la file et le journal. Un montant négatif, lui, est
   refusé : le SENS d'une écriture vient de son schéma, jamais du signe du
   montant.

4. **`Decimal` partout** (principe 4), quantize `0.01`, devise obligatoire et
   contrôlée. Aucun `float` n'entre dans la file.
"""
from __future__ import annotations

import logging
from decimal import Decimal, ROUND_HALF_UP

from django.db import transaction
from django.utils import timezone

logger = logging.getLogger(__name__)

#: Identifiant de la source, tel que la comptabilité le nomme dans ses règles de
#: consommation (`RegleConsommation.source`). Publié ici pour que le contrat soit
#: lisible d'un seul côté : `accounting` ne devine pas ce nom, il le lit.
SOURCE_CREDIT = "credits.CreditEvent"

#: Devises acceptées dans la file. « CDF » et « FC » désignent le même franc
#: congolais : `portfolio` dit CDF, l'annexe A dit FC ; la traduction est faite
#: côté comptable (`DEVISE_EVENEMENT`), on accepte donc les deux graphies plutôt
#: que d'en imposer une à l'appelant.
DEVISES = ("USD", "CDF", "FC")

CENTIME = Decimal("0.01")


class CreditEventError(Exception):
    """Refus d'émission — l'événement décrirait un fait que la compta ne peut pas
    écrire (devise inconnue, montant négatif)."""

    code = "CREDIT_EVENT_INVALID"


def q2(valeur) -> Decimal:
    """Montant comptable : `Decimal`, deux décimales, `ROUND_HALF_UP` (principe 4)."""
    return Decimal(str(valeur)).quantize(CENTIME, rounding=ROUND_HALF_UP)


def _devise(currency: str) -> str:
    code = (currency or "").strip().upper()[:3]
    if code not in DEVISES:
        raise CreditEventError(
            f"Devise « {currency} » inconnue de la file comptable "
            f"(attendu : {', '.join(DEVISES)}). Aucun événement n'est émis sur "
            "une devise qu'on ne sait pas nommer."
        )
    return code


def emettre(
    event_type: str,
    *,
    amount,
    currency: str,
    reference: str = "",
    application=None,
    loan_id: int | None = None,
    loan_reference: str = "",
    occurred_at=None,
    actor_sub: str = "",
    **payload,
):
    """Produit UN événement de la file — ou `None` si le montant est nul.

    Idempotent par `(event_type, reference)` : rejouer l'acte métier ne crée pas
    un second événement, donc pas une seconde écriture. C'est la même garantie
    que la référence déterministe de pièce côté comptable, posée un cran plus
    tôt — là où l'acte est connu.
    """
    from credits.models import CreditEvent

    montant = q2(amount)
    if montant < 0:
        raise CreditEventError(
            f"Montant négatif ({montant}) pour « {event_type} » : le sens d'une "
            "écriture vient de son schéma (annexe B), jamais du signe du montant. "
            "Émettez l'événement de la nature correspondante."
        )
    if montant == 0:
        # Pas une erreur : une échéance sans intérêts, une commission nulle. Une
        # écriture de zéro n'existe pas — on ne l'inscrit pas dans la file.
        logger.debug("Événement « %s » non émis : montant nul (%s).", event_type, reference)
        return None

    devise = _devise(currency)

    if not transaction.get_connection().in_atomic_block:
        # Non bloquant — mais un acte métier dont l'événement vit hors de sa
        # transaction peut exister sans lui. Le signaler vaut mieux que de le
        # laisser passer inaperçu.
        logger.warning(
            "Événement crédit « %s » (%s) émis HORS transaction : l'acte métier "
            "et son événement doivent être indivisibles.", event_type, reference,
        )

    valeurs = {
        "application": application,
        "loan_id": loan_id,
        "loan_reference": (loan_reference or "")[:64],
        "amount": montant,
        "currency": devise,
        "occurred_at": occurred_at or timezone.now(),
        "actor_sub": (actor_sub or "")[:255],
        "payload": payload,
    }

    if not reference:
        return CreditEvent.objects.create(event_type=event_type, reference="", **valeurs)

    evenement, cree = CreditEvent.objects.get_or_create(
        event_type=event_type, reference=reference[:64], defaults=valeurs,
    )
    if not cree:
        logger.info(
            "Événement crédit « %s » déjà émis pour l'acte « %s » (#%s) : "
            "aucun doublon créé.", event_type, reference, evenement.pk,
        )
    return evenement


# ── Émetteurs par nature (annexe B) ───────────────────────────────────────────

def emettre_decaissement(application, *, amount, currency: str, reference: str,
                         loan_id: int | None = None, loan_reference: str = "",
                         occurred_at=None, actor_sub: str = "", **payload):
    """B1 — mise à disposition du capital : l'encours sain naît au grand livre."""
    from credits.models import CreditEvent

    return emettre(
        CreditEvent.Type.DISBURSED, application=application, amount=amount,
        currency=currency, reference=reference, loan_id=loan_id,
        loan_reference=loan_reference, occurred_at=occurred_at,
        actor_sub=actor_sub, **payload,
    )


def emettre_remboursement_capital(*, amount, currency: str, reference: str,
                                  application=None, loan_id: int | None = None,
                                  loan_reference: str = "", occurred_at=None,
                                  actor_sub: str = "", **payload):
    """B2 — quote-part CAPITAL d'une échéance encaissée (l'encours diminue)."""
    from credits.models import CreditEvent

    return emettre(
        CreditEvent.Type.PRINCIPAL_REPAID, application=application, amount=amount,
        currency=currency, reference=reference, loan_id=loan_id,
        loan_reference=loan_reference, occurred_at=occurred_at,
        actor_sub=actor_sub, **payload,
    )


def emettre_remboursement_interets(*, amount, currency: str, reference: str,
                                   application=None, loan_id: int | None = None,
                                   loan_reference: str = "", occurred_at=None,
                                   actor_sub: str = "", **payload):
    """B3 — quote-part INTÉRÊTS d'une échéance encaissée (produit 701)."""
    from credits.models import CreditEvent

    return emettre(
        CreditEvent.Type.INTEREST_COLLECTED, application=application, amount=amount,
        currency=currency, reference=reference, loan_id=loan_id,
        loan_reference=loan_reference, occurred_at=occurred_at,
        actor_sub=actor_sub, **payload,
    )


def emettre_commission(*, amount, currency: str, reference: str,
                       application=None, loan_id: int | None = None,
                       loan_reference: str = "", occurred_at=None,
                       actor_sub: str = "", **payload):
    """B4 — commission de dossier ou de service encaissée (produit 702)."""
    from credits.models import CreditEvent

    return emettre(
        CreditEvent.Type.COMMISSION_COLLECTED, application=application, amount=amount,
        currency=currency, reference=reference, loan_id=loan_id,
        loan_reference=loan_reference, occurred_at=occurred_at,
        actor_sub=actor_sub, **payload,
    )


def emettre_echeance(*, capital, interets, currency: str, reference: str,
                     application=None, loan_id: int | None = None,
                     loan_reference: str = "", occurred_at=None,
                     actor_sub: str = "", commission=0, **payload) -> list:
    """Encaissement d'une échéance → B2 + B3 (+ B4), un événement par nature.

    C'est le point d'entrée que `portfolio` doit appeler quand il enregistre un
    remboursement : la ventilation capital / intérêts est connue de l'échéancier,
    elle se perd si on n'émet qu'un total. Les références dérivent de celle de
    l'acte (`…/CAP`, `…/INT`, `…/COM`) pour que chaque nature garde son
    idempotence propre.

    Retourne la liste des événements RÉELLEMENT émis (les jambes nulles n'en
    produisent aucun).
    """
    emis = []
    for suffixe, montant, emetteur in (
        ("CAP", capital, emettre_remboursement_capital),
        ("INT", interets, emettre_remboursement_interets),
        ("COM", commission, emettre_commission),
    ):
        evenement = emetteur(
            amount=montant, currency=currency,
            reference=f"{reference}/{suffixe}" if reference else "",
            application=application, loan_id=loan_id, loan_reference=loan_reference,
            occurred_at=occurred_at, actor_sub=actor_sub, **payload,
        )
        if evenement is not None:
            emis.append(evenement)
    return emis


# ── Lecture (supervision, jamais consommation) ────────────────────────────────

def file_en_attente(*, types: list[str] | None = None):
    """Événements crédit pas encore entrés au grand livre.

    Sert à SURVEILLER la file (« combien de décaissements attendent une
    écriture ? »), jamais à la consommer : la consommation appartient à
    `accounting`, qui seule sait poser `consumed_at` et la pièce dans la même
    transaction.
    """
    from credits.models import CreditEvent

    qs = CreditEvent.objects.filter(consumed_at__isnull=True)
    if types:
        qs = qs.filter(event_type__in=types)
    return qs.order_by("occurred_at", "id")
