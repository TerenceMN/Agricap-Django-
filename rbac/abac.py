"""Vérifications ABAC (attribut + contexte), au-delà du RBAC pur — ex. « un Agent ne peut
valider QUE SI Agence = X ET Heure < 18h » (exemple du document de conception, Scénario 9).
`profile.assignment_id` n'existe qu'à partir de la Phase 1 (FK ajoutée une fois `agencies`
créée) — avant ça, `getattr(..., None)` renvoie toujours False sans lever d'erreur."""
from __future__ import annotations

from datetime import datetime

from django.utils import timezone


def can_validate_for_agency(user, agency_id: int, *, now: datetime | None = None) -> bool:
    profile = getattr(user, "staff_profile", None)
    assignment_id = getattr(profile, "assignment_id", None)
    if not assignment_id or assignment_id != agency_id:
        return False
    current = now or timezone.localtime()
    return current.hour < 18
