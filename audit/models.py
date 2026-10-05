"""Journal d'audit générique et immuable, partagé par toutes les apps. Remplace la
réinvention ad hoc de l'audit sur chaque page frontend (`createAuditLog`/`logAction`/
`addAuditEntry`/`admin_audit_log`) et le seul précédent backend existant
(`portfolio.LoanConfigHistory`, propre au crédit) par une source unique consultée par
`AuditLog.jsx`."""
from __future__ import annotations

from django.db import models


class AuditEntry(models.Model):
    actor = models.CharField(max_length=255, blank=True, db_index=True)       # sub IdP
    actor_role = models.CharField(max_length=40, blank=True)                   # rôle AU MOMENT de l'action
    action = models.CharField(max_length=120, db_index=True)                   # ex. "agency.suspend"
    entity_type = models.CharField(max_length=80, db_index=True)               # ex. "Agency"
    entity_id = models.CharField(max_length=64, blank=True)
    details = models.JSONField(default=dict, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["entity_type", "entity_id"])]
        verbose_name_plural = "Audit entries"

    def __str__(self) -> str:
        return f"{self.created_at:%Y-%m-%d %H:%M} {self.actor} {self.action}"
