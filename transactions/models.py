"""Workflow de validation adaptative des transactions (Transactions.jsx,
ValidationJournal.jsx, SpecialCases.jsx, Supervision.jsx) — cœur du multi-signature +
step-up MFA (Scénario 7 du document de conception)."""
from __future__ import annotations

import uuid

from django.db import models
from django.db.models import Q

from common.choices import FlowStatus


def _generate_otp_id() -> str:
    return uuid.uuid4().hex


class Transaction(models.Model):
    class Kind(models.TextChoices):
        CREDIT = "credit", "Crédit"
        DEBIT = "debit", "Débit"

    class OperationType(models.TextChoices):
        PAYMENT = "PAYMENT", "Paiement"
        REIMBURSEMENT = "REIMBURSEMENT", "Remboursement"
        TRANSFER = "TRANSFER", "Transfert"

    agency = models.ForeignKey("agencies.Agency", null=True, blank=True, on_delete=models.SET_NULL,
                                related_name="transactions")
    kind = models.CharField(max_length=10, choices=Kind.choices)
    amount = models.DecimalField(max_digits=18, decimal_places=2)
    currency = models.CharField(max_length=3, default="USD")
    description = models.CharField(max_length=255, blank=True)
    emitter = models.CharField(max_length=150, blank=True)
    receiver = models.CharField(max_length=150, blank=True)
    operation_type = models.CharField(max_length=16, choices=OperationType.choices, default=OperationType.PAYMENT)
    status = models.CharField(max_length=20, choices=FlowStatus.choices, default=FlowStatus.PENDING_VALIDATION)
    auto_validated = models.BooleanField(default=False)
    created_by = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.CheckConstraint(condition=Q(amount__gt=0), name="transaction_amount_positive"),
        ]
        indexes = [models.Index(fields=["status"]), models.Index(fields=["operation_type"])]

    def __str__(self) -> str:
        return f"TX-{self.pk} {self.amount} {self.currency} [{self.status}]"


class ValidationThreshold(models.Model):
    operation_type = models.CharField(max_length=16, choices=Transaction.OperationType.choices, unique=True)
    auto_limit = models.DecimalField(max_digits=18, decimal_places=2)
    manager_limit = models.DecimalField(max_digits=18, decimal_places=2)
    manual_timeout_hours = models.IntegerField(default=24)

    def __str__(self) -> str:
        return f"{self.operation_type} auto<{self.auto_limit} manager<{self.manager_limit}"


class OtpChallenge(models.Model):
    id = models.CharField(max_length=36, primary_key=True, default=_generate_otp_id)
    transaction = models.ForeignKey(Transaction, on_delete=models.CASCADE, related_name="otp_challenges")
    approver_sub = models.CharField(max_length=255)
    code_hash = models.CharField(max_length=128)
    channel = models.CharField(max_length=10, choices=[("EMAIL", "Email")], default="EMAIL")
    attempts = models.IntegerField(default=0)
    max_attempts = models.IntegerField(default=5)
    expires_at = models.DateTimeField()
    verified_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return f"OTP {self.id[:8]} tx={self.transaction_id} approver={self.approver_sub}"


class TransactionApproval(models.Model):
    class Decision(models.TextChoices):
        APPROVED = "APPROVED", "Approuvé"
        REJECTED = "REJECTED", "Rejeté"

    transaction = models.ForeignKey(Transaction, on_delete=models.CASCADE, related_name="approvals")
    approver_sub = models.CharField(max_length=255)
    approver_role = models.CharField(max_length=40)
    decision = models.CharField(max_length=10, choices=Decision.choices)
    otp_verified_at = models.DateTimeField(null=True, blank=True)
    decided_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["transaction", "approver_sub"], name="one_decision_per_approver"),
        ]
        ordering = ["decided_at"]

    def __str__(self) -> str:
        return f"tx={self.transaction_id} {self.approver_sub} -> {self.decision}"


class SpecialCase(models.Model):
    class AlertLevel(models.TextChoices):
        MOYEN = "MOYEN", "Moyen"
        ELEVE = "ELEVE", "Élevé"
        CRITIQUE = "CRITIQUE", "Critique"

    class Status(models.TextChoices):
        EN_TRANSIT = "EN_TRANSIT", "En transit"
        EN_OBSERVATION = "EN_OBSERVATION", "En observation"
        BLOQUE = "BLOQUE", "Bloqué"

    transaction = models.ForeignKey(Transaction, on_delete=models.CASCADE, related_name="special_cases")
    alert_level = models.CharField(max_length=10, choices=AlertLevel.choices, default=AlertLevel.MOYEN)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.EN_TRANSIT)
    recommendation = models.TextField(blank=True)
    escalated_to_sub = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"SpecialCase tx={self.transaction_id} [{self.alert_level}]"
