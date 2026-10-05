"""Fonctions de sérialisation plates — utilisées par `services.py` (snapshot d'idempotence)
et `views.py` (réponse succès) pour garantir qu'un rejeu renvoie EXACTEMENT la même forme."""
from __future__ import annotations

from .models import JournalEntry


def entry_row(e: JournalEntry) -> dict:
    return {
        "id": e.pk, "date": e.date.isoformat(), "pieceRef": e.piece_ref, "code": e.code,
        "description": e.description, "currency": e.currency, "status": e.status,
        "lines": [
            {"account": l.account.code, "debit": float(l.debit), "credit": float(l.credit)}
            for l in e.lines.all()
        ],
    }
