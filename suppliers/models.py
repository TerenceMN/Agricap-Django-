"""Annuaire fournisseurs (Suppliers.jsx) — CRUD léger, action blacklist."""
from __future__ import annotations

from django.db import models


class Supplier(models.Model):
    name = models.CharField(max_length=200)
    category = models.CharField(max_length=120, blank=True)
    rating = models.FloatField(default=0)  # 0-5
    compliance_status = models.CharField(max_length=80, blank=True)
    blacklisted = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name
