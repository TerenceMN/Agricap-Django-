"""Helpers de conversion tolérants, partagés par les apps monétaires — mêmes règles que
`portfolio/services.py` (`_dec/_int/_date`), relocalisées ici pour éviter que chaque
nouvelle app monétaire les réimplémente à sa façon."""
from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation


def to_decimal(value, default: str = "0") -> Decimal:
    if value in (None, ""):
        return Decimal(default)
    try:
        return Decimal(str(value).replace(",", ".").replace(" ", ""))
    except (InvalidOperation, ValueError):
        return Decimal(default)


def to_int(value, default: int = 0) -> int:
    try:
        return int(float(str(value).replace(",", ".")))
    except (TypeError, ValueError):
        return default


def to_date(value):
    if not value:
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None
