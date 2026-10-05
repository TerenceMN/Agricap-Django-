"""Mécanisme d'idempotence partagé : une clé rejouée avec les MÊMES paramètres retourne la
réponse déjà produite (sans double effet) ; rejouée avec des paramètres différents est
refusée (409 IdempotencyConflictError), jamais exécutée avec les nouveaux paramètres.

Utilisation dans un service monétaire (à l'intérieur du bloc @transaction.atomic) :

    @transaction.atomic
    def transfer_funds(*, from_account_id, to_account_id, amount, idempotency_key, by=""):
        rec = idempotency.begin(scope="caisses.transfer", key=idempotency_key,
                                 params={"from": from_account_id, "to": to_account_id,
                                         "amount": str(amount)}, by=by)
        ... logique métier ...
        idempotency.complete(rec, response=result_dict, entity_type="FundTransfer",
                              entity_id=str(transfer.pk))
        return transfer

La vue appelante attrape `IdempotentReplay` et renvoie `exc.record.response_snapshot` tel
quel (statut 200), sans ré-exécuter le service.
"""
from __future__ import annotations

import hashlib
import json

from django.utils import timezone

from .exceptions import ConflictError, IdempotencyConflictError
from .models import IdempotencyKey


class IdempotentReplay(Exception):
    """Clé déjà COMPLETED rejouée à l'identique — pas une erreur, un signal de replay."""

    def __init__(self, record: IdempotencyKey) -> None:
        super().__init__(f"Rejeu idempotent de {record.scope}:{record.key}")
        self.record = record


def _fingerprint(params: dict) -> str:
    canonical = json.dumps(params, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def begin(*, scope: str, key: str, params: dict, by: str = "") -> IdempotencyKey:
    fingerprint = _fingerprint(params)
    record, created = IdempotencyKey.objects.get_or_create(
        scope=scope, key=key,
        defaults={"fingerprint": fingerprint, "created_by": by},
    )
    if created:
        return record
    if record.fingerprint != fingerprint:
        raise IdempotencyConflictError(
            "Cette clé d'idempotence a déjà été utilisée avec des paramètres différents."
        )
    if record.status == IdempotencyKey.Status.COMPLETED:
        raise IdempotentReplay(record)
    raise ConflictError("Une requête avec cette clé est déjà en cours de traitement.")


def replay_response(exc: IdempotentReplay):
    """Construit la Response DRF de rejeu à partir d'un `IdempotentReplay` attrapé en vue."""
    from rest_framework.response import Response
    return Response(exc.record.response_snapshot, status=200)


def complete(record: IdempotencyKey, *, response: dict, entity_type: str = "", entity_id: str = "") -> None:
    record.status = IdempotencyKey.Status.COMPLETED
    record.response_snapshot = response
    record.entity_type = entity_type
    record.entity_id = entity_id
    record.completed_at = timezone.now()
    record.save(update_fields=["status", "response_snapshot", "entity_type", "entity_id", "completed_at"])
