"""Seule exception au principe « aucun modèle autoritaire » de cette app : un instantané
horodaté du score de conformité, nécessaire pour calculer une variation semaine sur semaine
(le score lui-même reste calculé à la volée depuis les autres apps à chaque appel)."""
from __future__ import annotations

from django.db import models


class ComplianceScoreSnapshot(models.Model):
    computed_at = models.DateTimeField(auto_now_add=True, db_index=True)
    global_score = models.FloatField()
    components = models.JSONField(default=list)

    class Meta:
        ordering = ["-computed_at"]

    def __str__(self) -> str:
        return f"{self.computed_at:%Y-%m-%d %H:%M} score={self.global_score}"
