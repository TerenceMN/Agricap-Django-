"""Moteur d'alertes configurable (Supervision.jsx) — remplace `agencies.AgencyAlert`/
`transactions.SpecialCase` en tant que source des KPI « Alertes Critiques » : ces deux
modèles existent mais n'ont AUCUN appelant automatique nulle part dans le code (vérifié) —
un rôle strictement manuel qui les laisse en pratique toujours vides. Ce module calcule et
matérialise de vraies alertes à partir de métriques réelles, évaluées à la demande (pas de
planificateur dans ce projet)."""
from __future__ import annotations

from django.db import models
from django.db.models import Q


class AlertRule(models.Model):
    class Metric(models.TextChoices):
        AGENCY_SUSPENDED = "AGENCY_SUSPENDED", "Agence suspendue depuis"
        RECONCILIATION_OVERDUE = "RECONCILIATION_OVERDUE", "Rapprochement ouvert depuis"
        COMPLIANCE_SCORE_LOW = "COMPLIANCE_SCORE_LOW", "Score de conformité global"
        TRANSACTION_OVERDUE = "TRANSACTION_OVERDUE", "Transactions en attente en retard"
        PARTNER_FAILURES = "PARTNER_FAILURES", "Échecs consécutifs d'un partenaire"
        TICKET_SLA_BREACHED = "TICKET_SLA_BREACHED", "Tickets en dépassement de SLA"

    class Operator(models.TextChoices):
        GT = ">", ">"
        GTE = ">=", ">="
        LT = "<", "<"
        LTE = "<=", "<="

    class Severity(models.TextChoices):
        INFO = "INFO", "Info"
        WARNING = "WARNING", "Avertissement"
        CRITICAL = "CRITICAL", "Critique"

    code = models.CharField(max_length=60, unique=True, db_index=True)
    name = models.CharField(max_length=120)
    description = models.TextField(blank=True)
    metric = models.CharField(max_length=30, choices=Metric.choices)
    operator = models.CharField(max_length=2, choices=Operator.choices)
    # Unité dépend de la métrique : heures pour AGENCY_SUSPENDED/RECONCILIATION_OVERDUE,
    # points de score pour COMPLIANCE_SCORE_LOW, nombre pour TRANSACTION_OVERDUE/PARTNER_FAILURES.
    #
    # `Decimal` et non `float` (principe 4) : un seuil est une frontière, et une
    # frontière n'a d'intérêt que si elle est franchie au bon endroit. En binaire,
    # un seuil saisi à 79,9 valait 79,900000000000005684…, et la comparaison à
    # `<` ou `>=` basculait du mauvais côté sur la valeur d'égalité exacte — c'est
    # le même raisonnement que pour les seuils du moteur de scoring
    # (`referentiel.InstitutionConfig`), auxquels ces règles font écho.
    threshold = models.DecimalField(max_digits=12, decimal_places=2)
    severity = models.CharField(max_length=10, choices=Severity.choices)
    # Numéro notifié par SMS à chaque nouvelle alerte matérialisée pour cette règle (vide =
    # pas de notification SMS, comportement historique).
    notify_phone = models.CharField(max_length=20, blank=True)
    enabled = models.BooleanField(default=True)
    created_by = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["code"]

    def __str__(self) -> str:
        return f"{self.code} ({self.metric} {self.operator} {self.threshold})"


class Alert(models.Model):
    class Status(models.TextChoices):
        ACTIVE = "ACTIVE", "Active"
        ACKNOWLEDGED = "ACKNOWLEDGED", "Acquittée"
        RESOLVED = "RESOLVED", "Résolue"

    rule = models.ForeignKey(AlertRule, null=True, blank=True, on_delete=models.SET_NULL, related_name="alerts")
    severity = models.CharField(max_length=10, choices=AlertRule.Severity.choices)
    title = models.CharField(max_length=200)
    body = models.TextField(blank=True)
    source_type = models.CharField(max_length=40, blank=True)
    source_id = models.CharField(max_length=64, blank=True)
    # Une seule alerte ACTIVE par (règle, source) — recréer la même alerte tant qu'elle n'a
    # pas été résolue serait du bruit, pas un signal.
    dedup_key = models.CharField(max_length=160)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.ACTIVE)
    triggered_at = models.DateTimeField(auto_now_add=True)
    acknowledged_by = models.CharField(max_length=255, blank=True)
    acknowledged_at = models.DateTimeField(null=True, blank=True)
    resolved_by = models.CharField(max_length=255, blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    resolution_note = models.TextField(blank=True)

    class Meta:
        ordering = ["-triggered_at"]
        constraints = [
            models.UniqueConstraint(fields=["dedup_key"], condition=Q(status="ACTIVE"),
                                     name="unique_active_alert_dedup_key"),
        ]

    def __str__(self) -> str:
        return f"[{self.severity}] {self.title} ({self.status})"
