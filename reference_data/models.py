"""
Référentiel des chaînes de valeur agricoles.
Upload Excel + validation + activation maker-checker + versionnage.
"""
from __future__ import annotations

from django.db import models

MODULE_WEIGHT_KEYS = [
    "semences", "mecanisation", "maindoeuvre", "equipements",
    "postrecolte", "logistique", "commercialisation", "reserve",
]


class ReferenceFileUpload(models.Model):
    class FileType(models.TextChoices):
        VALUE_CHAINS = "value_chains", "Chaînes de valeur"
        SUPPLIERS = "suppliers", "Fournisseurs agréés"
        RATES = "rates", "Grille de taux"

    class Status(models.TextChoices):
        PENDING_VALIDATION = "pending_validation", "En attente d'activation"
        ACTIVE = "active", "Active"
        ARCHIVED = "archived", "Archivée"
        REJECTED = "rejected", "Rejetée"

    file = models.FileField(upload_to="reference_data/")
    file_type = models.CharField(max_length=30, choices=FileType.choices)
    version = models.CharField(max_length=50, blank=True)
    uploaded_by = models.CharField(max_length=255)
    uploaded_at = models.DateTimeField(auto_now_add=True)
    activated_by = models.CharField(max_length=255, blank=True)
    activated_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(
        max_length=25, choices=Status.choices, default=Status.PENDING_VALIDATION,
    )
    validation_report = models.JSONField(default=dict)
    diff_summary = models.JSONField(default=dict)
    row_count = models.IntegerField(default=0)

    class Meta:
        ordering = ["-uploaded_at"]

    def __str__(self) -> str:
        return f"{self.file_type} v{self.version} [{self.status}]"


class ValueChain(models.Model):
    """Une chaîne de valeur agricole active, issue d'un ReferenceFileUpload activé."""

    code = models.CharField(max_length=50, db_index=True)
    label = models.CharField(max_length=100)
    active = models.BooleanField(default=True)
    source_file = models.ForeignKey(
        ReferenceFileUpload, on_delete=models.PROTECT, related_name="value_chains",
    )

    cycle_months = models.IntegerField()
    cost_per_hectare_usd = models.DecimalField(max_digits=12, decimal_places=2)
    cost_per_hectare_cdf = models.DecimalField(max_digits=14, decimal_places=2)
    module_weights = models.JSONField()  # {"semences": 15, "mecanisation": 10, ...}
    risk_factor = models.DecimalField(max_digits=5, decimal_places=3)
    min_score_required = models.IntegerField()
    base_rate = models.DecimalField(max_digits=5, decimal_places=2)
    harvest_months = models.JSONField(default=list)   # [3, 4]
    eligible_guarantees = models.JSONField(default=list)  # ["epargne", "morale"]

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["label"]

    def __str__(self) -> str:
        return f"{self.label} ({self.code})"
