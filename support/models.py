"""CRM support (Support.jsx) + conversations investisseur/gestionnaire (chat de
Holdings.jsx)."""
from __future__ import annotations

import re

from django.db import models


class Ticket(models.Model):
    class Category(models.TextChoices):
        CREDIT = "credit", "Crédit"
        EPARGNE = "epargne", "Épargne"
        MOBILE_MONEY = "mobile-money", "Mobile Money"
        COMPTABILITE = "comptabilite", "Comptabilité"
        FX = "fx", "Change (FX)"
        TECHNIQUE = "technique", "Technique"

    class Priority(models.TextChoices):
        FAIBLE = "faible", "Faible"
        NORMAL = "normal", "Normal"
        URGENT = "urgent", "Urgent"
        CRITIQUE = "critique", "Critique"

    class Status(models.TextChoices):
        OUVERT = "ouvert", "Ouvert"
        EN_TRAITEMENT = "en-traitement", "En traitement"
        ESCALADE = "escalade", "Escaladé"
        EN_ATTENTE_CLIENT = "en-attente-client", "En attente client"
        RESOLU = "resolu", "Résolu"
        REJETE = "rejete", "Rejeté"

    class Level(models.TextChoices):
        L1 = "L1", "Niveau 1"
        L2 = "L2", "Niveau 2"
        L3 = "L3", "Niveau 3"

    class WaitingOn(models.TextChoices):
        AGENT = "agent", "Agent"
        CLIENT = "client", "Client"

    class RejectType(models.TextChoices):
        DOUBLON = "doublon", "Doublon"
        HORS_PERIMETRE = "hors_perimetre", "Hors périmètre"
        INFO_INSUFFISANTES = "informations_insuffisantes", "Informations insuffisantes"
        FRAUDE = "fraude_suspectee", "Fraude suspectée"

    # Catégories financières — la vérification MM et le force-credit ne s'appliquent qu'à celles-ci.
    FINANCIAL_CATEGORIES = {Category.CREDIT, Category.MOBILE_MONEY, Category.COMPTABILITE, Category.FX}

    # Équipes cibles selon la catégorie (utilisé lors de l'escalade)
    ESCALATION_TEAMS = {
        Category.CREDIT: "Back-Office Finance",
        Category.COMPTABILITE: "Back-Office Finance",
        Category.FX: "Back-Office Finance",
        Category.MOBILE_MONEY: "Équipe Intégrations",
        Category.TECHNIQUE: "Équipe IT",
        Category.EPARGNE: "Back-Office Finance",
    }

    user = models.ForeignKey("accounts.FintechUser", on_delete=models.CASCADE, related_name="tickets")
    category = models.CharField(max_length=14, choices=Category.choices, default=Category.TECHNIQUE)
    priority = models.CharField(max_length=10, choices=Priority.choices, default=Priority.NORMAL)
    status = models.CharField(max_length=18, choices=Status.choices, default=Status.OUVERT)
    level = models.CharField(max_length=2, choices=Level.choices, default=Level.L1)
    assigned_to_sub = models.CharField(max_length=255, blank=True)
    assigned_team = models.CharField(max_length=100, blank=True)
    subject = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    reject_type = models.CharField(max_length=30, choices=RejectType.choices, blank=True)
    original_ticket = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL, related_name="duplicates",
    )

    # SLA (§6.2 annexe Support & Client) — horloge de résolution suspendue tant que
    # `waiting_on='client'` (le temps d'attente d'une réponse client ne doit pas compter
    # contre l'agent).
    waiting_on = models.CharField(max_length=10, choices=WaitingOn.choices, default=WaitingOn.AGENT)
    sla_first_response_due = models.DateTimeField(null=True, blank=True)
    sla_resolution_due = models.DateTimeField(null=True, blank=True)
    sla_paused_at = models.DateTimeField(null=True, blank=True)
    sla_breached_first_response = models.BooleanField(default=False)
    sla_breached_resolution = models.BooleanField(default=False)
    first_response_at = models.DateTimeField(null=True, blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    rejected_reason = models.TextField(blank=True)
    reopened_count = models.IntegerField(default=0)
    satisfaction_rating = models.IntegerField(null=True, blank=True)
    satisfaction_comment = models.TextField(blank=True)

    # ── En attente client ─────────────────────────────────────────────────────
    awaiting_since = models.DateTimeField(null=True, blank=True)
    await_client_question = models.TextField(blank=True)
    sla_accumulated_pause_seconds = models.IntegerField(default=0)
    await_task_j2_id = models.CharField(max_length=200, blank=True)
    await_task_j5_id = models.CharField(max_length=200, blank=True)
    await_task_j7_id = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.subject} [{self.status}]"

    @property
    def public_id(self) -> str:
        """Format lisible : TKT-YYYYMMDD-{pk:03d}."""
        return f"TKT-{self.created_at.strftime('%Y%m%d')}-{self.pk:03d}"

    # ── Priorité intelligente à la création ───────────────────────────────────
    @classmethod
    def compute_auto_priority(cls, description: str, base_priority: str,
                               user_role: str = "") -> str:
        text = (description or "").lower()
        priority_order = [cls.Priority.FAIBLE, cls.Priority.NORMAL,
                          cls.Priority.URGENT, cls.Priority.CRITIQUE]
        idx = priority_order.index(base_priority) if base_priority in priority_order else 1

        # Montants > 500 USD ou > 1 000 000 FC → urgent minimum
        usd_amounts = re.findall(r'(\d[\d\s,.]*)\s*(?:\$|usd)', text)
        cdf_amounts = re.findall(r'(\d[\d\s,.]*)\s*(?:fc|cdf|congolais|francs?)', text)
        for raw in usd_amounts:
            try:
                if float(raw.replace(" ", "").replace(",", "")) > 500:
                    idx = max(idx, 2)
            except ValueError:
                pass
        for raw in cdf_amounts:
            try:
                if float(raw.replace(" ", "").replace(",", "")) > 1_000_000:
                    idx = max(idx, 2)
            except ValueError:
                pass

        # Rôle VIP / Corporate → +1 niveau
        if user_role in ("corporate", "vip"):
            idx = min(idx + 1, 3)

        # Mots-clés critiques → urgent minimum
        if any(k in text for k in ("bloqué", "bloque", "fraude", "urgent")):
            idx = max(idx, 2)

        return priority_order[idx]

    # ── Détection de doublons ─────────────────────────────────────────────────
    @classmethod
    def find_duplicate(cls, user_sub: str, category: str, description: str):
        """Retourne un ticket ouvert de même catégorie dont la description partage une
        référence de transaction (format AG-\d+, MP\d+, OM\d+, REF\d+)."""
        refs = re.findall(r'\b(AG-\d+|MP\d+|OM\d+|REF[\w\d]+|TRANS[\w\d]+)\b',
                          (description or "").upper())
        if not refs:
            return None
        open_statuses = (cls.Status.OUVERT, cls.Status.EN_TRAITEMENT, cls.Status.ESCALADE)
        existing = cls.objects.filter(user_id=user_sub, category=category,
                                      status__in=open_statuses)
        for t in existing:
            existing_refs = re.findall(
                r'\b(AG-\d+|MP\d+|OM\d+|REF[\w\d]+|TRANS[\w\d]+)\b',
                (t.description or "").upper(),
            )
            if set(refs) & set(existing_refs):
                return t
        return None


class TicketMessage(models.Model):
    ticket = models.ForeignKey(Ticket, on_delete=models.CASCADE, related_name="messages")
    author_sub = models.CharField(max_length=255)
    author_role = models.CharField(max_length=40, blank=True)
    text = models.TextField()
    is_internal = models.BooleanField(default=False)
    action_source = models.CharField(max_length=100, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]

    def __str__(self) -> str:
        return f"Msg({self.ticket_id}) {self.author_sub}"


class TicketAuditLog(models.Model):
    """Traçabilité bancaire immuable — jamais modifiable ni supprimable après création."""
    ticket = models.ForeignKey(Ticket, on_delete=models.CASCADE, related_name="audit_logs")
    actor = models.CharField(max_length=255)
    action = models.CharField(max_length=100)
    payload = models.JSONField(default=dict)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValueError("TicketAuditLog entries are immutable.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError("TicketAuditLog entries cannot be deleted.")

    def __str__(self) -> str:
        return f"[{self.action}] ticket={self.ticket_id} by={self.actor}"


class MobileMoneyVerification(models.Model):
    class Operator(models.TextChoices):
        AIRTEL = "airtel", "Airtel Money"
        ORANGE = "orange", "Orange Money"
        MPESA = "mpesa", "M-Pesa"

    class Status(models.TextChoices):
        PENDING = "pending", "En attente"
        FOUND = "found_operator_side", "Trouvée chez l'opérateur"
        NOT_FOUND = "not_found", "Introuvable"
        CREDITED = "already_credited", "Déjà créditée"
        FAILED = "failed", "Échec API"

    ticket = models.ForeignKey(Ticket, on_delete=models.CASCADE, related_name="mm_verifications")
    operator = models.CharField(max_length=10, choices=Operator.choices)
    transaction_ref = models.CharField(max_length=100)
    amount = models.DecimalField(max_digits=15, decimal_places=2, null=True, blank=True)
    currency = models.CharField(max_length=3, default="CDF")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    raw_response = models.JSONField(default=dict)
    verified_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"MM({self.operator} {self.transaction_ref}) [{self.status}]"


class PendingFinancialAction(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending_approval", "En attente d'approbation"
        APPROVED = "approved", "Approuvée"
        REJECTED = "rejected", "Rejetée"

    ticket = models.ForeignKey(Ticket, on_delete=models.CASCADE, related_name="financial_actions")
    action_type = models.CharField(max_length=50, default="force_credit")
    amount = models.DecimalField(max_digits=15, decimal_places=2)
    currency = models.CharField(max_length=3)
    initiated_by = models.CharField(max_length=255)
    approved_by = models.CharField(max_length=255, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    idempotency_key = models.CharField(max_length=100, unique=True)
    accounting_ref = models.CharField(max_length=100, blank=True)
    rejection_note = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    decided_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"FinancialAction({self.action_type} {self.amount} {self.currency}) [{self.status}]"


class Conversation(models.Model):
    investor_sub = models.CharField(max_length=255)
    manager_sub = models.CharField(max_length=255)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["investor_sub", "manager_sub"], name="unique_conversation")]


class Message(models.Model):
    conversation = models.ForeignKey(Conversation, on_delete=models.CASCADE, related_name="messages")
    sender_sub = models.CharField(max_length=255)
    text = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]
