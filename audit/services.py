"""Point d'entrée unique pour journaliser une action métier. Appelé en fin de chaque
service mutateur des autres apps — participe à la transaction atomique englobante (Django
imbrique via savepoint automatiquement, pas besoin de @transaction.atomic ici)."""
from __future__ import annotations

from .models import AuditEntry


def record(*, actor: str, action: str, entity_type: str, entity_id: str,
           details: dict | None = None, actor_role: str = "", ip: str | None = None) -> AuditEntry:
    return AuditEntry.objects.create(
        actor=actor, actor_role=actor_role, action=action,
        entity_type=entity_type, entity_id=str(entity_id),
        details=details or {}, ip_address=ip,
    )
