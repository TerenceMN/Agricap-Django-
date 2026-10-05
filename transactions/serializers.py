"""Fonctions de sérialisation plates — utilisées par `services.py` (snapshot d'idempotence)
et `views.py` (réponse succès) pour garantir qu'un rejeu renvoie EXACTEMENT la même forme."""
from __future__ import annotations

from .models import Transaction


def tx_row(t: Transaction) -> dict:
    return {
        "id": t.pk, "date": t.created_at.isoformat(), "description": t.description,
        "agencyId": t.agency_id, "type": t.kind, "amount": float(t.amount), "currency": t.currency,
        "operationType": t.operation_type, "emitter": t.emitter, "receiver": t.receiver,
        "status": t.status, "autoValidated": t.auto_validated,
        "approvals": [
            {"approver": a.approver_sub, "role": a.approver_role, "decision": a.decision}
            for a in t.approvals.all()
        ],
    }
