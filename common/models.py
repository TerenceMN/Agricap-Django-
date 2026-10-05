"""Modèles partagés — pour l'instant uniquement la clé d'idempotence (voir
`common/idempotency.py`)."""
from __future__ import annotations

from django.db import models


class IdempotencyKey(models.Model):
    class Status(models.TextChoices):
        PENDING = "PENDING", "En cours"
        COMPLETED = "COMPLETED", "Terminé"
        FAILED = "FAILED", "Échoué"

    scope = models.CharField(max_length=80)           # ex. "caisses.transfer"
    key = models.CharField(max_length=128)             # clé fournie par le client
    fingerprint = models.CharField(max_length=64)      # sha256 des paramètres canonicalisés
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    entity_type = models.CharField(max_length=80, blank=True)
    entity_id = models.CharField(max_length=64, blank=True)
    response_snapshot = models.JSONField(null=True, blank=True)
    created_by = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["scope", "key"], name="idempotency_scope_key_unique"),
        ]

    def __str__(self) -> str:
        return f"{self.scope}:{self.key} [{self.status}]"
