"""Contrats client avec signature électronique simple partie (Contracts.jsx) — aucun
besoin de multi-signature/contre-signature identifié pour ce type de document (à la
différence des transactions financières, voir `transactions` app)."""
from __future__ import annotations

from django.db import models


class Contract(models.Model):
    class Status(models.TextChoices):
        EN_ATTENTE = "en_attente", "En attente"
        ACTIF = "actif", "Actif"
        CLOTURE = "cloture", "Clôturé"

    user = models.ForeignKey("accounts.FintechUser", on_delete=models.CASCADE, related_name="contracts")
    title = models.CharField(max_length=200)
    type = models.CharField(max_length=80, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.EN_ATTENTE)
    signature = models.TextField(blank=True)
    signed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.title} ({self.user_id})"
