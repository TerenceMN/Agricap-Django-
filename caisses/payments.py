"""Ordres de paiement Makuta — le chaînon entre un portefeuille AGRICAP et l'argent réel.

`caisses/services.py` sait créditer un portefeuille. `common/makuta.py` sait parler à la
plateforme de paiement. Rien ne reliait les deux : un « dépôt mobile money » enregistrait un
mouvement sans qu'un seul franc ne bouge. Ce module est ce lien, et il est construit autour
d'une seule idée difficile.

── L'état indéterminé ───────────────────────────────────────────────────────────
Entre « la requête est partie » et « l'issue est connue », il existe un troisième état.
Quand le réseau coupe après l'envoi, l'opération a **pu aboutir** chez Makuta. Un
`MakutaTransportError` ne signifie donc jamais « échec » : il signifie « issue inconnue ».

  * un ordre en échec **connu** (`REFUSED`) se rejoue en toute sécurité ;
  * un ordre **indéterminé** se RÉCONCILIE — on relit le statut chez le fournisseur —
    et ne se rejoue JAMAIS à l'aveugle.

Un système qui confond les deux paie deux fois. C'est la raison d'être de `INDETERMINATE`,
et la raison pour laquelle l'appel réseau est délibérément sorti de la transaction : le
passage à `SENT` est **committé avant** que le premier octet ne parte. Un ordre dont la
trace d'envoi disparaît avec le rollback est un paiement fantôme.

── Ce que ce module NE fait PAS ─────────────────────────────────────────────────
Il n'invente aucun endpoint métier Makuta. La documentation fournisseur décrit
l'authentification, rien d'autre : ni catalogue d'opérations, ni schéma de requête, ni
schéma de réponse, ni codes d'erreur, ni devises acceptées, ni format de rappel entrant.
Chemins, corps, champs de statut et valeurs de statut sont donc **paramétrés** par
`settings.MAKUTA` (clés listées dans `MISSING_PROVIDER_CONTRACT` ci-dessous) et, tant qu'ils
ne sont pas fournis, le module refuse franchement plutôt que de deviner. Une réponse 2xx
dont on ne sait pas lire le statut ne vaut PAS confirmation : l'ordre reste
`AWAITING_CONFIRMATION` et attend une réconciliation. Aucun crédit n'en découle.
"""
from __future__ import annotations

import logging
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from django.conf import settings
from django.db import transaction
from django.db.models import F
from django.utils import timezone

from audit.services import record as audit_record
from common import idempotency, makuta
from common.exceptions import ConflictError, InsufficientFundsError, NotFoundError, ValidationFailed
from common.makuta import MakutaConfigurationError, MakutaRefused, MakutaTransportError
from common.parsing import to_decimal

from . import channels, serializers
from .models import ClientWallet, PaymentOrder, PaymentOrderEvent, TreasuryAccount, WalletMovement

logger = logging.getLogger("agricap.payments")

Status = PaymentOrder.Status
Direction = PaymentOrder.Direction
EventKind = PaymentOrderEvent.Kind
EventSource = PaymentOrderEvent.Source


# ═══════════════════════════════════════════════════════════ MACHINE À ÉTATS

#: Transitions autorisées. Tout le reste est refusé par `_transition` — y compris
#: `CONFIRMED → REFUSED` : une confirmation ne se dédit pas, elle se contre-passe par un
#: ordre inverse motivé.
TRANSITIONS: dict[str, frozenset[str]] = {
    Status.PENDING: frozenset({Status.SENT, Status.CANCELLED}),
    Status.SENT: frozenset({Status.AWAITING_CONFIRMATION, Status.CONFIRMED,
                            Status.REFUSED, Status.INDETERMINATE}),
    Status.AWAITING_CONFIRMATION: frozenset({Status.CONFIRMED, Status.REFUSED, Status.INDETERMINATE}),
    Status.INDETERMINATE: frozenset({Status.CONFIRMED, Status.REFUSED, Status.AWAITING_CONFIRMATION}),
    Status.CONFIRMED: frozenset(),
    Status.REFUSED: frozenset(),
    Status.CANCELLED: frozenset(),
}


class Outcome:
    """Issue telle que la RAPPORTE le fournisseur, après classification de sa réponse."""

    CONFIRMED = "CONFIRMED"
    REFUSED = "REFUSED"
    PENDING = "PENDING"      # le fournisseur dit explicitement « en cours »
    UNKNOWN = "UNKNOWN"      # on ne sait pas lire sa réponse — surtout ne rien en déduire


#: Ce qu'il faut obtenir de Wolf Technologies avant que ce module ne fonctionne en réel.
#: Cette liste n'est pas décorative : chaque entrée manquante fait échouer une fonction
#: précise, avec un message qui la nomme.
MISSING_PROVIDER_CONTRACT = {
    "OPERATIONS": "Catalogue des opérations : chemin, méthode, gabarit de corps, chemin de "
                  "relecture de statut — par opération métier (collecte MM, décaissement MM…).",
    "STATUS_FIELD": "Chemin du champ portant le statut dans la réponse (ex. \"data.status\").",
    "STATUS_CONFIRMED": "Valeurs de ce champ signifiant « l'argent a bougé ».",
    "STATUS_REFUSED": "Valeurs signifiant « refusé définitivement ».",
    "STATUS_PENDING": "Valeurs signifiant « en cours » (facultatif mais recommandé).",
    "PROVIDER_REFERENCE_FIELD": "Chemin du champ portant LEUR identifiant de transaction.",
    "CALLBACK_PUBLIC_KEY_PEM": "Clé publique Makuta, pour authentifier un rappel entrant.",
    "CALLBACK_REFERENCE_FIELD": "Chemin du champ où le rappel renvoie NOTRE référence.",
    "CALLBACK_SIGNATURE_HEADER": "Nom de l'en-tête portant la signature du rappel.",
}


# ═══════════════════════════════════════════════════ CONFIGURATION FOURNISSEUR

#: Quantum des montants (principe 4). Quantizer À LA CRÉATION, pas à l'affichage : la
#: référence, l'empreinte d'idempotence et le corps signé doivent tous porter le MÊME
#: « 120.00 », qu'on ait saisi « 120 », « 120,0 » ou « 120.004 ».
_CENT = Decimal("0.01")


def _quantize(value) -> Decimal:
    return to_decimal(value).quantize(_CENT, rounding=ROUND_HALF_UP)


def _config() -> dict:
    config = getattr(settings, "MAKUTA", None) or {}
    return config if isinstance(config, dict) else {}


def operation_config(operation: str) -> dict:
    """Configuration d'une opération métier. Absente = on refuse, on ne devine pas."""
    operations = _config().get("OPERATIONS") or {}
    conf = operations.get(operation)
    if not isinstance(conf, dict) or not conf.get("path"):
        raise MakutaConfigurationError(
            f"Opération Makuta « {operation} » non configurée (settings.MAKUTA[\"OPERATIONS\"]). "
            f"{MISSING_PROVIDER_CONTRACT['OPERATIONS']}"
        )
    return conf


def _dig(payload: Any, dotted_path: str) -> Any:
    """Lecture d'un champ par chemin pointé (« data.status »). Rien de trouvé = None."""
    current = payload
    for part in str(dotted_path).split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return None
    return current


def _normalized(values) -> set[str]:
    if isinstance(values, str):
        values = [values]
    return {str(v).strip().upper() for v in (values or [])}


def classify_provider_status(payload: Any) -> tuple[str, str]:
    """Traduit une réponse fournisseur en `Outcome`, **uniquement** d'après la configuration.

    Renvoie `(outcome, explication)`. Aucune heuristique, aucun « ça ressemble à un succès » :
    tant que le vocabulaire de statuts de Makuta n'est pas configuré, tout est `UNKNOWN` et
    aucun portefeuille ne bouge. C'est volontairement gênant — c'est ce qui empêche un
    déploiement de créditer sur une réponse qu'il ne comprend pas.
    """
    config = _config()
    field = config.get("STATUS_FIELD")
    if not field:
        return Outcome.UNKNOWN, ("Champ de statut fournisseur non configuré "
                                 "(settings.MAKUTA[\"STATUS_FIELD\"]).")
    raw = _dig(payload, field)
    if raw is None:
        return Outcome.UNKNOWN, f"Champ « {field} » absent de la réponse fournisseur."
    value = str(raw).strip().upper()
    if value in _normalized(config.get("STATUS_CONFIRMED")):
        return Outcome.CONFIRMED, f"Statut fournisseur « {raw} » = confirmé."
    if value in _normalized(config.get("STATUS_REFUSED")):
        return Outcome.REFUSED, f"Statut fournisseur « {raw} » = refusé."
    if value in _normalized(config.get("STATUS_PENDING")):
        return Outcome.PENDING, f"Statut fournisseur « {raw} » = en cours."
    return Outcome.UNKNOWN, (f"Statut fournisseur « {raw} » inconnu du paramétrage "
                             f"(STATUS_CONFIRMED / STATUS_REFUSED / STATUS_PENDING).")


def extract_provider_reference(payload: Any) -> str:
    field = _config().get("PROVIDER_REFERENCE_FIELD")
    if not field:
        return ""
    value = _dig(payload, field)
    return str(value)[:128] if value not in (None, "") else ""


def payment_payload(order: PaymentOrder) -> dict:
    """Corps à envoyer, produit à partir du **gabarit** configuré pour l'opération.

    Référencé par l'en-tête de `common/makuta.py` : c'est ici que notre référence propre
    entre dans le contenu signé. Les emplacements disponibles sont fixes et documentés —
    aucun nom de champ Makuta n'est écrit dans ce code, seulement dans la configuration.

      `{reference}` `{amount}` `{currency}` `{counterparty}` `{direction}` `{operation}`
      `{meta.<clé>}` (puisé dans `order.metadata`)

    `{amount}` est rendu par `str(Decimal)` — jamais un float : un montant qui traverse un
    `float` en JSON peut arriver en `10.199999999999999` chez le destinataire.
    """
    template = operation_config(order.operation).get("body")
    if not isinstance(template, dict):
        raise MakutaConfigurationError(
            f"Gabarit de corps absent pour l'opération « {order.operation} » "
            f"(settings.MAKUTA[\"OPERATIONS\"][\"{order.operation}\"][\"body\"]). "
            "Le schéma de requête n'est pas dans la documentation fournisseur."
        )
    return _render(template, _placeholders(order))


def _placeholders(order: PaymentOrder) -> dict[str, str]:
    values = {
        "reference": order.reference,
        "amount": str(order.amount),
        "currency": order.currency,
        "counterparty": order.counterparty,
        "direction": order.direction,
        "operation": order.operation,
        "provider_reference": order.provider_reference,
    }
    for key, value in (order.metadata or {}).items():
        values[f"meta.{key}"] = "" if value is None else str(value)
    return values


def _render(node, values: dict[str, str]):
    """Substitution récursive sur les chaînes du gabarit. Une chaîne entièrement composée
    d'un seul emplacement garde son type d'origine (une chaîne) ; on ne convertit jamais un
    montant en nombre JSON de notre propre initiative."""
    if isinstance(node, dict):
        return {k: _render(v, values) for k, v in node.items()}
    if isinstance(node, list):
        return [_render(v, values) for v in node]
    if isinstance(node, str):
        rendered = node
        for key, value in values.items():
            rendered = rendered.replace("{" + key + "}", value)
        return rendered
    return node


def _status_path(order: PaymentOrder) -> str:
    """Chemin de relecture de statut (GET signé). Sans lui, aucune réconciliation possible."""
    conf = operation_config(order.operation)
    template = conf.get("status_path")
    if not template:
        raise MakutaConfigurationError(
            f"Chemin de relecture de statut absent pour « {order.operation} » "
            f"(settings.MAKUTA[\"OPERATIONS\"][\"{order.operation}\"][\"status_path\"]). "
            "Sans ce chemin, un ordre indéterminé ne peut pas être réconcilié — c'est le "
            "premier contrat à obtenir de Wolf Technologies."
        )
    path = _render(template, _placeholders(order))
    if not path.startswith("/"):
        raise MakutaConfigurationError("Le chemin de relecture de statut doit commencer par « / ».")
    return path


# ═══════════════════════════════════════════════════════════ JOURNAL & ÉTATS

def _log_event(order: PaymentOrder, *, kind: str, source: str = EventSource.SYSTEM,
               from_status: str = "", to_status: str = "", actor: str = "", motive: str = "",
               payload: dict | None = None) -> PaymentOrderEvent:
    return PaymentOrderEvent.objects.create(
        order=order, kind=kind, source=source, from_status=from_status, to_status=to_status,
        actor=actor, motive=motive, payload=payload or {},
    )


def _transition(order: PaymentOrder, target: str) -> str:
    """Applique une transition ou refuse. Retourne le statut d'origine (pour le journal)."""
    origin = order.status
    if target == origin:
        return origin
    if target not in TRANSITIONS.get(origin, frozenset()):
        raise ConflictError(
            f"Transition interdite sur l'ordre {order.reference} : {origin} → {target}."
        )
    order.status = target
    if target in PaymentOrder.SETTLED_STATUSES:
        order.settled_at = timezone.now()
    return origin


def _safe_response(payload: Any) -> Any:
    """Réponse fournisseur stockée telle quelle si elle est sérialisable, sinon résumée —
    un `JSONField` qui explose à l'écriture ferait perdre la trace de la réponse."""
    if isinstance(payload, (dict, list, str, int, bool)) or payload is None:
        return payload
    return {"repr": str(payload)[:2000]}


# ═══════════════════════════════════════════════════════════ CRÉATION

#: Longueur maximale de `PaymentOrder.counterparty` en base. On la contrôle en amont plutôt
#: que de laisser la base trancher : PostgreSQL lèverait une `DataError` (500 opaque) et
#: SQLite tronquerait EN SILENCE — un numéro Mobile Money tronqué, c'est un versement à
#: quelqu'un d'autre.
_COUNTERPARTY_MAX = 64


def _validate_counterparty(counterparty: str) -> None:
    if not counterparty:
        raise ValidationFailed("La contrepartie (numéro Mobile Money / compte) est requise.")
    if len(counterparty) > _COUNTERPARTY_MAX:
        raise ValidationFailed(
            f"La contrepartie est trop longue ({len(counterparty)} caractères, "
            f"maximum {_COUNTERPARTY_MAX})."
        )


def _validate_metadata(metadata: dict | None) -> dict:
    """Les métadonnées alimentent les emplacements `{meta.<clé>}` du gabarit de corps : une
    valeur qui n'est pas un objet ferait échouer le rendu au moment de l'ENVOI, c'est-à-dire
    sur un ordre déjà créé et déjà comptabilisé. On refuse à la création."""
    if metadata in (None, ""):
        return {}
    if not isinstance(metadata, dict):
        raise ValidationFailed("Les métadonnées de l'ordre de paiement doivent être un objet.")
    return metadata


def _resolve_treasury_account(*, code: str, wallet: ClientWallet) -> TreasuryAccount | None:
    """Compte de trésorerie de traçabilité. Un code inconnu est une ERREUR, pas un silence :
    laisser passer `None` ferait perdre le rattachement du flux sans que personne ne le voie."""
    if not code:
        return None
    account = TreasuryAccount.objects.filter(code=code).first()
    if not account:
        raise NotFoundError(f"Compte de trésorerie « {code} » introuvable.")
    if account.currency != wallet.currency:
        raise ValidationFailed(
            "Le compte de trésorerie et le portefeuille doivent partager la même devise "
            "(aucune conversion implicite ici — voir fx.convert)."
        )
    return account


@transaction.atomic
def create_payment_order(*, wallet_id: int, direction: str, operation: str,
                         amount: Decimal | str, counterparty: str = "",
                         treasury_account_code: str = "", metadata: dict | None = None,
                         idempotency_key: str, by: str = "") -> PaymentOrder:
    """Crée l'ordre — et **n'envoie rien**. L'envoi est un acte séparé (`dispatch`).

    Séparer les deux n'est pas de la cérémonie : la création est transactionnelle et
    réversible, l'envoi ne l'est pas. Les mélanger, c'est mettre un appel réseau à
    l'intérieur d'une transaction, donc accepter qu'un rollback efface la trace d'un
    paiement peut-être déjà parti.
    """
    amount = _quantize(amount)
    if amount <= 0:
        raise ValidationFailed("Le montant de l'ordre de paiement doit être strictement positif.")
    if direction not in Direction.values:
        raise ValidationFailed(f"Sens de paiement inconnu : {direction}.")
    # Le sens et l'opération doivent raconter la même histoire : un « encaissement » posté sur
    # le chemin de décaissement enverrait de l'argent en croyant en recevoir. Hors catalogue
    # AGRICAP, on ne présume rien (cf. `channels.direction_for_operation`).
    expected_direction = channels.direction_for_operation(operation)
    if expected_direction and expected_direction != direction:
        raise ValidationFailed(
            f"L'opération « {operation} » ne s'exécute qu'en sens {expected_direction} "
            f"(sens demandé : {direction})."
        )
    _validate_counterparty(counterparty)
    metadata = _validate_metadata(metadata)
    # Échoue AVANT toute écriture si l'opération n'est pas configurée : créer un ordre
    # qu'on ne saura jamais envoyer, c'est fabriquer de la dette de réconciliation.
    operation_config(operation)

    rec = idempotency.begin(
        scope="caisses.payment_order", key=idempotency_key,
        params={"wallet": wallet_id, "direction": direction, "operation": operation,
                "amount": str(amount), "counterparty": counterparty}, by=by,
    )

    wallet = ClientWallet.objects.select_for_update().filter(pk=wallet_id).first()
    if not wallet:
        raise NotFoundError("Portefeuille introuvable.")
    if wallet.status != ClientWallet.Status.ACTIF:
        raise ConflictError("Ce portefeuille est bloqué — aucun ordre de paiement n'est autorisé.")

    treasury_account = _resolve_treasury_account(code=treasury_account_code, wallet=wallet)

    order = PaymentOrder(
        wallet=wallet, treasury_account=treasury_account, direction=direction, operation=operation,
        counterparty=counterparty, amount=amount, currency=wallet.currency,
        metadata=metadata, idempotency_key=idempotency_key, created_by=by,
    )

    if direction == Direction.COLLECTION:
        _check_kyc_headroom(wallet=wallet, amount=amount)
        order.save()
    else:
        # Décaissement : les fonds sont RÉSERVÉS immédiatement (débit réel). On n'ordonne
        # pas à un fournisseur de payer un montant qu'on ne détient pas encore ; et un
        # solde qui reste disponible entre l'ordre et sa confirmation permet de le dépenser
        # deux fois.
        if wallet.balance < amount:
            raise InsufficientFundsError(account_id=wallet.pk)
        order.save()
        # `_contexte_reference` : l'ordre pointera vers ce mouvement JUSTE APRÈS,
        # donc le lien inverse n'existe pas encore. Sans ce contexte, la référence
        # naîtrait en « HQ-NA » et il faudrait la réécrire — or une référence émise
        # ne se réécrit pas (principe 3).
        movement = WalletMovement(
            wallet=wallet, kind=WalletMovement.Kind.WITHDRAW, amount=amount,
        )
        movement._contexte_reference = order
        movement.save()
        ClientWallet.objects.filter(pk=wallet.pk).update(balance=F("balance") - amount)
        order.movement = movement
        order.save(update_fields=["movement", "updated_at"])
        _log_event(order, kind=EventKind.WALLET_POSTED, actor=by,
                   motive="Réservation des fonds à la création de l'ordre de décaissement.",
                   payload={"movementId": movement.pk, "amount": str(amount)})

    _log_event(order, kind=EventKind.CREATED, actor=by, to_status=order.status,
               payload={"amount": str(amount), "currency": order.currency,
                        "operation": operation, "direction": direction})
    audit_record(actor=by, action="caisses.payment_order.create", entity_type="PaymentOrder",
                 entity_id=order.reference,
                 details={"amount": str(amount), "currency": order.currency,
                          "direction": direction, "operation": operation})

    response = serializers.payment_order_row(order)
    idempotency.complete(rec, response=response, entity_type="PaymentOrder", entity_id=order.reference)
    return order


@transaction.atomic
def create_settlement_payout(*, withdrawal_request, operation: str, counterparty: str,
                            treasury_account_code: str = "", metadata: dict | None = None,
                            by: str = "") -> PaymentOrder:
    """Ordre de DÉCAISSEMENT adossé à un retrait DÉJÀ réglé — la jambe externe de la « seule
    porte » : le portefeuille a été débité par `withdrawal_tiers._post_withdrawal`, cet ordre
    ne fait que porter l'argent jusqu'à la contrepartie via Makuta.

    Différence NON négociable avec `create_payment_order(direction=PAYOUT)` : ici on ne
    RÉSERVE aucun fonds. Le débit a déjà eu lieu (c'est le mouvement du retrait) ; le refaire
    paierait deux fois. L'ordre RÉUTILISE le mouvement de retrait — de sorte qu'un refus
    définitif du fournisseur déclenche la contre-passation standard (`_reverse_payout_
    reservation`) et rend les fonds au portefeuille, ce qui est correct : le versement
    externe n'a pas eu lieu.

    Ne dispatche rien (l'appel réseau est un acte séparé, hors transaction — cf. `dispatch_
    payment_order`). Échoue AVANT toute écriture si l'opération n'est pas configurée, pour ne
    pas régler un retrait externe qu'on ne saura jamais verser.
    """
    _validate_counterparty(counterparty)
    metadata = _validate_metadata(metadata)
    if withdrawal_request.movement_id is None:
        raise ConflictError("Le retrait n'est pas encore réglé : aucun mouvement à décaisser.")
    expected_direction = channels.direction_for_operation(operation)
    if expected_direction and expected_direction != Direction.PAYOUT:
        raise ValidationFailed(
            f"L'opération « {operation} » n'est pas un décaissement : elle ne peut pas porter "
            "un versement externe."
        )
    amount = _quantize(withdrawal_request.amount)
    if amount <= 0:
        raise ValidationFailed("Le montant du versement externe doit être strictement positif.")
    operation_config(operation)  # échoue tôt si le catalogue fournisseur manque

    wallet = ClientWallet.objects.select_for_update().get(pk=withdrawal_request.wallet_id)
    order = PaymentOrder.objects.create(
        wallet=wallet, direction=Direction.PAYOUT, operation=operation, counterparty=counterparty,
        amount=amount, currency=wallet.currency,
        treasury_account=_resolve_treasury_account(code=treasury_account_code, wallet=wallet),
        metadata={**metadata, "withdrawalRequestId": withdrawal_request.pk},
        movement_id=withdrawal_request.movement_id, created_by=by,
    )
    _log_event(order, kind=EventKind.CREATED, actor=by, to_status=order.status,
               motive="Décaissement adossé à un retrait déjà réglé (aucune réservation : le "
                      "portefeuille est déjà débité).",
               payload={"amount": str(order.amount), "currency": order.currency,
                        "operation": operation, "withdrawalRequestId": withdrawal_request.pk,
                        "movementId": withdrawal_request.movement_id})
    audit_record(actor=by, action="caisses.payment_order.create_settlement_payout",
                 entity_type="PaymentOrder", entity_id=order.reference,
                 details={"amount": str(order.amount), "currency": order.currency,
                          "withdrawalRequestId": withdrawal_request.pk})
    return order


def _check_kyc_headroom(*, wallet: ClientWallet, amount: Decimal) -> None:
    """Le plafond KYC se vérifie AVANT d'ordonner un encaissement — refuser un dépôt déjà
    encaissé chez l'opérateur ne le fait pas revenir."""
    from compliance.kyc_levels import balance_cap_for

    cap = balance_cap_for(user=wallet.user)
    if wallet.balance + amount > cap:
        raise ValidationFailed(
            f"Cet encaissement dépasserait le plafond de solde de votre palier KYC "
            f"({cap} {wallet.currency})."
        )


@transaction.atomic
def cancel_payment_order(*, reference: str, motive: str, by: str = "") -> PaymentOrder:
    """Annulation — possible UNIQUEMENT tant que rien n'est parti."""
    if not motive:
        raise ValidationFailed("Un motif est obligatoire pour annuler un ordre de paiement.")
    order = _lock(reference)
    if order.status != Status.PENDING:
        raise ConflictError(
            f"L'ordre {reference} est en statut {order.status} : une requête est peut-être "
            "déjà partie, il se réconcilie, il ne s'annule pas."
        )
    origin = _transition(order, Status.CANCELLED)
    if order.direction == Direction.PAYOUT and order.movement_id and not order.reversal_movement_id:
        _reverse_payout_reservation(order=order, actor=by,
                                    motive=f"Annulation avant envoi : {motive}")
    order.save(update_fields=["status", "settled_at", "updated_at"])
    _log_event(order, kind=EventKind.CANCELLED, from_status=origin, to_status=order.status,
               actor=by, motive=motive)
    audit_record(actor=by, action="caisses.payment_order.cancel", entity_type="PaymentOrder",
                 entity_id=order.reference, details={"motive": motive})
    return order


def _lock(reference: str) -> PaymentOrder:
    order = PaymentOrder.objects.select_for_update().filter(reference=reference).first()
    if not order:
        raise NotFoundError("Ordre de paiement introuvable.")
    return order


# ═══════════════════════════════════════════════════════════ ENVOI

def dispatch_payment_order(*, reference: str, by: str = "") -> PaymentOrder:
    """Envoie l'ordre au fournisseur. **Hors transaction, volontairement.**

    Déroulé, dans cet ordre non négociable :

      1. `PENDING → SENT` en base, **committé** (transaction courte, refermée) ;
      2. l'appel réseau ;
      3. l'enregistrement de l'issue, dans une seconde transaction.

    Si le processus meurt entre 2 et 3, l'ordre reste `SENT` : la file de réconciliation le
    voit, personne ne le rejoue à l'aveugle. Si l'étape 1 était dans la même transaction que
    l'appel, un rollback effacerait la trace d'un paiement peut-être exécuté.
    """
    order = _mark_sent(reference=reference, by=by)
    path = order.request_path
    body = order.request_body

    try:
        response = makuta.post(path, body)
    except MakutaTransportError as exc:
        return _record_indeterminate(reference=reference, detail=str(exc), by=by)
    except MakutaRefused as exc:
        return _record_refusal(reference=reference, detail=str(exc), by=by,
                               source=EventSource.PROVIDER_RESPONSE)
    except MakutaConfigurationError:
        # Défaut de déploiement (clé/URL) : la requête n'est PAS partie. On revient à un
        # état honnête plutôt que de laisser un ordre « envoyé » qui ne l'a jamais été.
        _record_not_sent(reference=reference, by=by)
        raise

    return _record_response(reference=reference, payload=response, by=by,
                            source=EventSource.PROVIDER_RESPONSE)


@transaction.atomic
def _mark_sent(*, reference: str, by: str) -> PaymentOrder:
    order = _lock(reference)
    if order.status != Status.PENDING:
        raise ConflictError(
            f"L'ordre {reference} a déjà été envoyé (statut {order.status}) — un second envoi "
            "paierait deux fois. Utilisez la réconciliation pour en connaître l'issue."
        )
    order.request_path = operation_config(order.operation)["path"]
    order.request_body = payment_payload(order)
    origin = _transition(order, Status.SENT)
    order.sent_at = timezone.now()
    order.save(update_fields=["status", "request_path", "request_body", "sent_at", "updated_at"])
    _log_event(order, kind=EventKind.SENT, from_status=origin, to_status=order.status, actor=by,
               payload={"path": order.request_path, "body": order.request_body})
    audit_record(actor=by, action="caisses.payment_order.send", entity_type="PaymentOrder",
                 entity_id=order.reference, details={"path": order.request_path})
    return order


@transaction.atomic
def _record_not_sent(*, reference: str, by: str) -> PaymentOrder:
    """Retour à `PENDING` — réservé au cas où l'on a la CERTITUDE que rien n'est parti
    (configuration absente : `common.makuta` échoue avant l'appel HTTP)."""
    order = _lock(reference)
    if order.status != Status.SENT:
        return order
    order.status = Status.PENDING
    order.sent_at = None
    order.save(update_fields=["status", "sent_at", "updated_at"])
    _log_event(order, kind=EventKind.TRANSPORT_ERROR, from_status=Status.SENT, to_status=Status.PENDING,
               actor=by, motive="Intégration Makuta non configurée : aucune requête n'est partie.")
    return order


@transaction.atomic
def _record_indeterminate(*, reference: str, detail: str, by: str) -> PaymentOrder:
    order = _lock(reference)
    origin = _transition(order, Status.INDETERMINATE)
    order.failure_detail = detail[:2000]
    order.save(update_fields=["status", "failure_detail", "settled_at", "updated_at"])
    _log_event(order, kind=EventKind.TRANSPORT_ERROR, from_status=origin, to_status=order.status,
               actor=by, motive=detail,
               payload={"consigne": "Issue INCONNUE — réconcilier, ne jamais rejouer."})
    audit_record(actor=by, action="caisses.payment_order.indeterminate", entity_type="PaymentOrder",
                 entity_id=order.reference, details={"detail": detail[:500]})
    logger.error("[PAIEMENT] Ordre %s INDÉTERMINÉ : %s", order.reference, detail)
    return order


@transaction.atomic
def _record_refusal(*, reference: str, detail: str, by: str, source: str,
                    payload: Any = None) -> PaymentOrder:
    order = _lock(reference)
    origin = _transition(order, Status.REFUSED)
    order.failure_detail = detail[:2000]
    if payload is not None:
        order.last_response = _safe_response(payload)
    order.save(update_fields=["status", "failure_detail", "last_response", "settled_at", "updated_at"])
    _log_event(order, kind=EventKind.REFUSED, source=source, from_status=origin,
               to_status=order.status, actor=by, motive=detail,
               payload={"response": _safe_response(payload)} if payload is not None else {})
    if order.direction == Direction.PAYOUT and order.movement_id and not order.reversal_movement_id:
        _reverse_payout_reservation(order=order, actor=by,
                                    motive="Décaissement refusé par le fournisseur : "
                                           "les fonds réservés sont rendus au portefeuille.")
    audit_record(actor=by, action="caisses.payment_order.refused", entity_type="PaymentOrder",
                 entity_id=order.reference, details={"detail": detail[:500], "source": source})
    return order


def _record_response(*, reference: str, payload: Any, by: str, source: str,
                     motive: str = "") -> PaymentOrder:
    """Point d'entrée unique de toute issue rapportée par le fournisseur — réponse synchrone,
    réconciliation ou rappel entrant passent tous par ici, donc par la même classification et
    la même règle : **aucun crédit sans `Outcome.CONFIRMED`**."""
    outcome, explanation = classify_provider_status(payload)
    if outcome == Outcome.REFUSED:
        return _record_refusal(reference=reference, detail=explanation, by=by, source=source,
                               payload=payload)
    if outcome == Outcome.CONFIRMED:
        return _record_confirmation(reference=reference, payload=payload, by=by, source=source,
                                    motive=motive or explanation)
    return _record_unsettled(reference=reference, payload=payload, by=by, source=source,
                             explanation=explanation, outcome=outcome)


@transaction.atomic
def _record_unsettled(*, reference: str, payload: Any, by: str, source: str,
                      explanation: str, outcome: str) -> PaymentOrder:
    """Le fournisseur a répondu, mais son statut est « en cours » ou illisible pour nous.
    L'ordre attend — il n'est ni confirmé, ni refusé, et surtout il ne crédite rien."""
    order = _lock(reference)
    origin = order.status
    if order.status in PaymentOrder.SETTLED_STATUSES:
        return order
    # Un ordre INDÉTERMINÉ ne perd son alarme que si le fournisseur dit EXPLICITEMENT
    # « en cours ». Une réponse illisible ne lève aucun doute : elle le confirme.
    promote = origin == Status.SENT or outcome == Outcome.PENDING
    if promote and Status.AWAITING_CONFIRMATION in TRANSITIONS.get(order.status, frozenset()):
        _transition(order, Status.AWAITING_CONFIRMATION)
    order.last_response = _safe_response(payload)
    order.provider_reference = extract_provider_reference(payload) or order.provider_reference
    order.save(update_fields=["status", "last_response", "provider_reference", "updated_at"])
    kind = EventKind.RESPONSE if outcome == Outcome.PENDING else EventKind.UNCLASSIFIED
    _log_event(order, kind=kind, source=source, from_status=origin, to_status=order.status,
               actor=by, motive=explanation, payload={"response": _safe_response(payload)})
    if outcome == Outcome.UNKNOWN:
        logger.warning("[PAIEMENT] Ordre %s : réponse fournisseur non classable — %s",
                       order.reference, explanation)
    return order


@transaction.atomic
def _record_confirmation(*, reference: str, payload: Any, by: str, source: str,
                         motive: str = "") -> PaymentOrder:
    """La seule porte par laquelle un portefeuille peut être crédité."""
    order = _lock(reference)
    if order.status == Status.CONFIRMED:
        return order  # rejeu d'un rappel/réconciliation — no-op, surtout pas un second crédit
    origin = _transition(order, Status.CONFIRMED)
    order.last_response = _safe_response(payload)
    order.provider_reference = extract_provider_reference(payload) or order.provider_reference
    order.save(update_fields=["status", "last_response", "provider_reference", "settled_at", "updated_at"])
    _log_event(order, kind=EventKind.CONFIRMED, source=source, from_status=origin,
               to_status=order.status, actor=by, motive=motive,
               payload={"response": _safe_response(payload)})

    if order.direction == Direction.COLLECTION and not order.movement_id:
        _post_collection(order=order, actor=by, source=source)

    audit_record(actor=by, action="caisses.payment_order.confirm", entity_type="PaymentOrder",
                 entity_id=order.reference,
                 details={"amount": str(order.amount), "currency": order.currency,
                          "source": source, "providerReference": order.provider_reference})
    return order


def _post_collection(*, order: PaymentOrder, actor: str, source: str) -> None:
    """Crédite le portefeuille — appelé UNIQUEMENT depuis `_record_confirmation`."""
    from compliance.kyc_levels import balance_cap_for

    wallet = ClientWallet.objects.select_for_update().get(pk=order.wallet_id)
    cap = balance_cap_for(user=wallet.user)
    if wallet.balance + order.amount > cap:
        # L'argent est réellement arrivé chez l'opérateur : le refuser ici créerait un écart
        # de trésorerie au lieu de le prévenir (le contrôle utile a lieu à la création).
        # On crédite et on signale — un dépassement de plafond KYC est un sujet de
        # conformité, pas un motif de faire disparaître des fonds reçus.
        logger.warning("[PAIEMENT] Ordre %s : crédit au-delà du plafond KYC (%s > %s) — à traiter "
                       "en conformité.", order.reference, wallet.balance + order.amount, cap)
        audit_record(actor=actor, action="caisses.payment_order.kyc_cap_exceeded",
                     entity_type="PaymentOrder", entity_id=order.reference,
                     details={"cap": str(cap), "balanceAfter": str(wallet.balance + order.amount)})

    movement = WalletMovement(
        wallet=wallet, kind=WalletMovement.Kind.DEPOSIT, amount=order.amount,
    )
    movement._contexte_reference = order
    movement.save()
    ClientWallet.objects.filter(pk=wallet.pk).update(balance=F("balance") + order.amount)
    order.movement = movement
    order.save(update_fields=["movement", "updated_at"])
    _log_event(order, kind=EventKind.WALLET_POSTED, source=source, actor=actor,
               motive="Encaissement confirmé par le fournisseur : crédit du portefeuille.",
               payload={"movementId": movement.pk, "amount": str(order.amount)})


def _reverse_payout_reservation(*, order: PaymentOrder, actor: str, motive: str) -> None:
    """Rend au portefeuille les fonds réservés pour un décaissement qui n'aura pas lieu.
    Contre-passation (P3) : on n'efface pas le mouvement d'origine, on en écrit un second."""
    wallet = ClientWallet.objects.select_for_update().get(pk=order.wallet_id)
    reversal = WalletMovement(
        wallet=wallet, kind=WalletMovement.Kind.REVERSAL, amount=order.amount,
    )
    reversal._contexte_reference = order
    reversal.save()
    ClientWallet.objects.filter(pk=wallet.pk).update(balance=F("balance") + order.amount)
    order.reversal_movement = reversal
    order.save(update_fields=["reversal_movement", "updated_at"])
    _log_event(order, kind=EventKind.WALLET_REVERSED, actor=actor, motive=motive,
               payload={"movementId": reversal.pk, "amount": str(order.amount)})
    audit_record(actor=actor, action="caisses.payment_order.reverse", entity_type="PaymentOrder",
                 entity_id=order.reference, details={"amount": str(order.amount), "motive": motive})


# ═══════════════════════════════════════════════════════════ RÉCONCILIATION

def open_orders(*, status: str | None = None, direction: str | None = None, limit: int = 500):
    qs = PaymentOrder.objects.select_related("wallet").all()
    if status:
        qs = qs.filter(status=status)
    if direction:
        qs = qs.filter(direction=direction)
    return qs[:limit]


def indeterminate_orders(*, limit: int = 500):
    """La file d'attente de la réconciliation : tout ce dont l'issue n'est pas connue.

    `SENT` en fait partie — un ordre qui n'a jamais reçu de réponse enregistrée est aussi
    indéterminé qu'un ordre coupé en plein vol, même s'il n'a pas eu droit à une exception."""
    return (PaymentOrder.objects.select_related("wallet")
            .filter(status__in=PaymentOrder.OPEN_STATUSES).order_by("created_at")[:limit])


def reconcile_payment_order(*, reference: str, motive: str, by: str = "") -> PaymentOrder:
    """Relit le statut de l'ordre CHEZ le fournisseur, puis applique l'issue lue.

    Motif obligatoire, journalisé (P3). Cette fonction ne réémet JAMAIS le paiement — elle
    interroge, elle n'ordonne pas. C'est toute la différence entre réconcilier et rejouer.

    Sur le principe 2, la ligne de partage n'est pas « manuel contre automatique » mais
    **« qui fait autorité »** : la RELECTURE peut être outillée et répétée (c'est ce que fait
    `reconcile_open_orders`), parce qu'elle ne décide de rien — elle transcrit ce que le
    fournisseur affirme. La DÉCISION, elle, reste humaine : ce que le paramétrage ne sait pas
    lire demeure `INDETERMINATE` et ne se clôt que par `force_settle`, sur preuve externe,
    motif circonstancié et acteur nommé.
    """
    if not motive:
        raise ValidationFailed(
            "Un motif est obligatoire pour réconcilier un ordre de paiement "
            "(qui demande, pourquoi maintenant)."
        )
    order = PaymentOrder.objects.filter(reference=reference).first()
    if not order:
        raise NotFoundError("Ordre de paiement introuvable.")
    if order.status == Status.PENDING:
        raise ConflictError("Rien n'a été envoyé pour cet ordre : il n'y a rien à réconcilier.")
    if order.status in PaymentOrder.SETTLED_STATUSES:
        raise ConflictError(f"L'ordre {reference} est déjà résolu ({order.status}).")

    path = _status_path(order)
    audit_record(actor=by, action="caisses.payment_order.reconcile.read", entity_type="PaymentOrder",
                 entity_id=order.reference, details={"path": path, "motive": motive})
    try:
        payload = makuta.get(path)
    except (MakutaTransportError, MakutaRefused) as exc:
        with transaction.atomic():
            locked = _lock(reference)
            _log_event(locked, kind=EventKind.TRANSPORT_ERROR, source=EventSource.RECONCILIATION,
                       from_status=locked.status, to_status=locked.status, actor=by, motive=motive,
                       payload={"erreur": str(exc), "path": path})
        raise

    return _record_response(reference=reference, payload=payload, by=by,
                            source=EventSource.RECONCILIATION, motive=motive)


# ────────────────────────────────────────────── Réconciliation en lot (outillage)

class ReconciliationResult:
    """Issue d'UNE tentative de réconciliation, du point de vue de l'outil.

    À ne pas confondre avec `Outcome`, qui décrit ce que dit le fournisseur : ici on décrit
    ce que la relecture a pu FAIRE. `UNREADABLE_STATUS` et `CONTRACT_MISSING` sont deux
    échecs distincts et il importe de ne pas les confondre — le premier veut dire « Makuta a
    répondu quelque chose qu'on ne sait pas lire », le second « on ne sait même pas où lire ».
    """

    CONFIRMED = "CONFIRMED"                  # le fournisseur confirme : issue appliquée
    REFUSED = "REFUSED"                      # le fournisseur refuse : issue appliquée
    AWAITING = "AWAITING"                    # le fournisseur dit explicitement « en cours »
    UNREADABLE_STATUS = "UNREADABLE_STATUS"  # réponse non classable → reste pour l'humain
    CONTRACT_MISSING = "CONTRACT_MISSING"    # pas de chemin de relecture configuré
    UNREACHABLE = "UNREACHABLE"              # fournisseur injoignable / refus de lecture
    NOT_OPEN = "NOT_OPEN"                    # ordre déjà résolu, ou rien n'est parti
    NOT_FOUND = "NOT_FOUND"                  # référence demandée inexistante
    ERROR = "ERROR"                          # anomalie métier inattendue
    WOULD_READ = "WOULD_READ"                # `--dry-run` : ce qui SERAIT relu


#: Motif par défaut inscrit au journal (P3). Il dit ce que la commande est — une RELECTURE —
#: et ce qu'elle n'est pas : une décision. Le fournisseur fait autorité ; ce qui reste
#: illisible reste illisible et attend un humain (P2).
DEFAULT_RECONCILIATION_MOTIVE = (
    "Réconciliation outillée (manage.py reconcile_payment_orders) : relecture du statut chez "
    "le fournisseur, qui fait autorité. Aucune issue n'est décidée ici."
)

_OUTCOME_TO_RESULT = {
    Outcome.CONFIRMED: ReconciliationResult.CONFIRMED,
    Outcome.REFUSED: ReconciliationResult.REFUSED,
    Outcome.PENDING: ReconciliationResult.AWAITING,
    Outcome.UNKNOWN: ReconciliationResult.UNREADABLE_STATUS,
}


def reconcile_open_orders(*, limit: int = 500, status: str = "", references: list[str] | None = None,
                          motive: str = "", by: str = "", dry_run: bool = False) -> dict:
    """Relit chez le fournisseur le statut de TOUS les ordres dont l'issue est inconnue.

    C'est la version en lot de `reconcile_payment_order`, et elle en garde toutes les règles :
    elle interroge et n'ordonne jamais (aucun `POST` n'est émis), elle n'écrit que ce que le
    fournisseur affirme, elle laisse `INDETERMINATE` tout ce qui reste illisible. Elle est
    **idempotente** : un ordre résolu sort de la file et un second passage ne le revoit pas ;
    un ordre confirmé deux fois n'est crédité qu'une (verrou + garde dans
    `_record_confirmation`).

    Makuta non configuré (cas actuel : ni URL ni clé) : **rien n'est touché**, et le rapport
    porte le motif de la dégradation — un outil de réconciliation qui échoue bruyamment sur
    une intégration non branchée n'apprend rien à personne.
    """
    report: dict[str, Any] = {
        "configured": makuta.is_configured(), "dryRun": bool(dry_run),
        "motive": (motive or DEFAULT_RECONCILIATION_MOTIVE), "actor": by,
        "scanned": 0, "results": [], "totals": {}, "degradation": "",
    }

    if references:
        found = {o.reference: o for o in
                 PaymentOrder.objects.select_related("wallet").filter(reference__in=references)}
        orders = [found[ref] for ref in references if ref in found]
        missing = [ref for ref in references if ref not in found]
    else:
        qs = (PaymentOrder.objects.select_related("wallet")
              .filter(status__in=PaymentOrder.OPEN_STATUSES).order_by("created_at"))
        if status:
            qs = qs.filter(status=status)
        orders, missing = list(qs[:limit]), []

    report["scanned"] = len(orders) + len(missing)
    for reference in missing:
        _append_result(report, reference=reference, before="", result=ReconciliationResult.NOT_FOUND,
                       detail="Aucun ordre ne porte cette référence.")

    if not report["configured"]:
        report["degradation"] = (
            "Intégration Makuta non configurée (URL et/ou clé privée absentes) : aucune "
            "relecture de statut n'est possible, RIEN n'a été modifié. Lancez "
            "« manage.py check_makuta » pour la liste exacte de ce qui manque, puis "
            "traitez les ordres en attente par preuve externe (force_settle)."
        )
        for order in orders:
            _append_result(report, reference=order.reference, before=order.status,
                           result=ReconciliationResult.CONTRACT_MISSING,
                           detail="Non relu : intégration Makuta non configurée.")
        _tally(report)
        return report

    for order in orders:
        _reconcile_one(report, order=order, motive=report["motive"], by=by, dry_run=dry_run)
    _tally(report)
    return report


def _reconcile_one(report: dict, *, order: PaymentOrder, motive: str, by: str, dry_run: bool) -> None:
    reference, before = order.reference, order.status
    if order.status not in PaymentOrder.OPEN_STATUSES:
        _append_result(report, reference=reference, before=before,
                       result=ReconciliationResult.NOT_OPEN,
                       detail=f"L'ordre est en statut {before} : il n'attend aucune issue.")
        return
    try:
        path = _status_path(order)
    except MakutaConfigurationError as exc:
        _append_result(report, reference=reference, before=before,
                       result=ReconciliationResult.CONTRACT_MISSING, detail=str(exc))
        return
    if dry_run:
        _append_result(report, reference=reference, before=before,
                       result=ReconciliationResult.WOULD_READ,
                       detail=f"Relecture prévue : GET {path}")
        return

    try:
        settled = reconcile_payment_order(reference=reference, motive=motive, by=by)
    except (MakutaTransportError, MakutaRefused) as exc:
        # Journalisé par `reconcile_payment_order` avant la remontée. Le lot continue : une
        # coupure sur un ordre ne doit pas priver les suivants de leur relecture.
        _append_result(report, reference=reference, before=before,
                       result=ReconciliationResult.UNREACHABLE, detail=str(exc))
        return
    except ConflictError as exc:
        _append_result(report, reference=reference, before=before,
                       result=ReconciliationResult.NOT_OPEN, detail=str(exc))
        return
    except (ValidationFailed, NotFoundError) as exc:  # pragma: no cover - garde-fou
        _append_result(report, reference=reference, before=before,
                       result=ReconciliationResult.ERROR, detail=str(exc))
        return

    outcome, explanation = classify_provider_status(settled.last_response)
    _append_result(report, reference=reference, before=before, after=settled.status,
                   result=_OUTCOME_TO_RESULT.get(outcome, ReconciliationResult.ERROR),
                   detail=explanation, path=path)


def _append_result(report: dict, *, reference: str, before: str, result: str, detail: str,
                   after: str = "", path: str = "") -> None:
    report["results"].append({"reference": reference, "before": before, "after": after or before,
                              "result": result, "detail": detail, "path": path})


def _tally(report: dict) -> None:
    totals: dict[str, int] = {}
    for row in report["results"]:
        totals[row["result"]] = totals.get(row["result"], 0) + 1
    report["totals"] = totals


@transaction.atomic
def force_settle(*, reference: str, outcome: str, motive: str, by: str) -> PaymentOrder:
    """Clôture manuelle d'un ordre indéterminé, sur preuve EXTERNE (relevé de l'opérateur,
    confirmation écrite du fournisseur). Dernier recours, et donc le plus encadré :
    motif obligatoire, acteur nommé, journalisé comme décision humaine explicite.

    Ce n'est pas un raccourci de la réconciliation : c'est ce qu'on fait quand la
    réconciliation elle-même n'est pas disponible (endpoint de statut inexistant, par
    exemple — situation exacte tant que Wolf Technologies n'a pas fourni son catalogue)."""
    if outcome not in (Outcome.CONFIRMED, Outcome.REFUSED):
        raise ValidationFailed("L'issue forcée doit être CONFIRMED ou REFUSED.")
    if not motive or len(motive.strip()) < 10:
        raise ValidationFailed(
            "Un motif circonstancié est obligatoire (référence du relevé ou de la "
            "confirmation externe qui fonde cette décision)."
        )
    if not by:
        raise ValidationFailed("Une clôture manuelle exige un acteur identifié.")
    order = PaymentOrder.objects.filter(reference=reference).first()
    if not order:
        raise NotFoundError("Ordre de paiement introuvable.")
    if order.status not in PaymentOrder.OPEN_STATUSES:
        raise ConflictError(f"L'ordre {reference} n'est pas en attente d'issue ({order.status}).")

    proof = {"decision": "manuelle", "motive": motive, "actor": by}
    if outcome == Outcome.CONFIRMED:
        return _record_confirmation(reference=reference, payload=proof, by=by,
                                    source=EventSource.RECONCILIATION, motive=motive)
    return _record_refusal(reference=reference, detail=motive, by=by,
                           source=EventSource.RECONCILIATION, payload=proof)


# ═══════════════════════════════════════════════════════════ RAPPEL ENTRANT

class CallbackRejected(Exception):
    """Rappel refusé — jamais silencieusement ignoré, jamais cru sur parole."""

    def __init__(self, message: str, *, http_status: int = 401) -> None:
        super().__init__(message)
        self.message = message
        self.http_status = http_status


def callback_signature_header() -> str:
    config = _config()
    return (config.get("CALLBACK_SIGNATURE_HEADER") or config.get("SIGNATURE_HEADER")
            or makuta.DEFAULT_SIGNATURE_HEADER)


def handle_callback(*, raw_body: bytes, signature: str, ip: str = "") -> PaymentOrder:
    """Traite un rappel Makuta — **après** l'avoir authentifié, jamais avant.

    Deux verrous, dans cet ordre :

    1. **Sans clé publique fournisseur configurée, tout rappel est refusé.** Nous n'avons
       pas cette clé aujourd'hui : la documentation ne couvre que le sens partenaire →
       Makuta. Un endpoint qui accepterait un rappel non authentifié laisserait n'importe
       qui sur Internet se déclarer payé — c'est-à-dire créditer un portefeuille AGRICAP
       avec une requête HTTP.
    2. **La signature porte les octets bruts du corps**, pas le dictionnaire re-sérialisé :
       re-sérialiser avant de vérifier, c'est vérifier autre chose que ce qui a été signé.

    Passé ces verrous, le rappel n'est toujours qu'une SOURCE d'issue de plus : il traverse
    la même classification que la réconciliation et ne peut rien confirmer que le
    paramétrage ne sache lire.
    """
    import json

    config = _config()
    public_key = config.get("CALLBACK_PUBLIC_KEY_PEM") or ""
    if not public_key:
        raise CallbackRejected(
            "Rappel Makuta refusé : aucune clé publique fournisseur n'est configurée "
            "(MAKUTA_CALLBACK_PUBLIC_KEY_PEM). Un rappel non authentifiable n'est pas une "
            "information, c'est une porte ouverte.",
            http_status=503,
        )
    if not signature:
        raise CallbackRejected(f"Rappel Makuta refusé : en-tête {callback_signature_header()} absent.")
    if not makuta.verify(raw_body, signature, public_key):
        raise CallbackRejected("Rappel Makuta refusé : signature invalide.")

    reference_field = config.get("CALLBACK_REFERENCE_FIELD")
    if not reference_field:
        raise CallbackRejected(
            "Rappel Makuta signé mais inexploitable : le champ portant NOTRE référence n'est "
            "pas configuré (MAKUTA[\"CALLBACK_REFERENCE_FIELD\"]) — format du rappel non "
            "documenté par le fournisseur.",
            http_status=503,
        )
    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise CallbackRejected(f"Rappel Makuta refusé : corps illisible ({exc.__class__.__name__}).",
                               http_status=400) from exc

    reference = _dig(payload, reference_field)
    order = PaymentOrder.objects.filter(reference=str(reference or "")).first()
    if not order:
        raise CallbackRejected("Rappel Makuta refusé : référence inconnue.", http_status=404)

    audit_record(actor="makuta-callback", action="caisses.payment_order.callback",
                 entity_type="PaymentOrder", entity_id=order.reference,
                 details={"ip": ip}, ip=ip or None)
    return _record_response(reference=order.reference, payload=payload, by="makuta-callback",
                            source=EventSource.CALLBACK, motive="Rappel entrant authentifié.")


def log_rejected_callback(*, reason: str, ip: str = "") -> None:
    """Un rappel refusé laisse une trace côté audit même sans ordre identifié : une rafale
    de rappels mal signés est un signal de tentative, pas du bruit."""
    logger.warning("[PAIEMENT] Rappel Makuta REFUSÉ (ip=%s) : %s", ip or "?", reason)
    audit_record(actor="anonyme", action="caisses.payment_order.callback_rejected",
                 entity_type="PaymentOrder", entity_id="-", details={"reason": reason, "ip": ip},
                 ip=ip or None)
