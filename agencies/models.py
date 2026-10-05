"""Cycle de vie des agences (Agencies.jsx) : création → (Actif ⇄ Suspendu) → Fermée.
Aucun champ de solde stocké ici — `balanceUSD` est un agrégat live via
`caisses.services.agency_balance()` (une seule source de vérité pour l'argent)."""
from __future__ import annotations

import uuid

from django.db import models


def _generate_challenge_id() -> str:
    return uuid.uuid4().hex


class Agency(models.Model):
    class Type(models.TextChoices):
        SIEGE = "SIEGE", "Siège"
        RURALE = "RURALE", "Rurale"
        URBAINE = "URBAINE", "Urbaine"
        POINT_SERVICE = "POINT_SERVICE", "Point de service"

    class Status(models.TextChoices):
        ACTIF = "ACTIF", "Actif"
        SUSPENDU = "SUSPENDU", "Suspendu"
        FERMEE = "FERMEE", "Fermée"

    code = models.CharField(max_length=32, unique=True, db_index=True)
    name = models.CharField(max_length=200)
    type = models.CharField(max_length=20, choices=Type.choices, default=Type.URBAINE)
    city = models.CharField(max_length=120, blank=True)
    province = models.CharField(max_length=120, blank=True)
    manager_sub = models.CharField(max_length=255, blank=True)
    compliance_score = models.FloatField(default=0)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.ACTIF)
    suspended_reason = models.CharField(max_length=255, blank=True)
    closed_reason = models.CharField(max_length=255, blank=True)
    created_by = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]
        indexes = [models.Index(fields=["status"])]

    def __str__(self) -> str:
        return f"{self.code} — {self.name}"


class AgencyComplianceSnapshot(models.Model):
    """Historique du score de conformité PAR AGENCE (`agencies.compliance`) — distinct de
    `analytics.ComplianceScoreSnapshot` (réseau entier). Persiste aussi la valeur courante
    sur `Agency.compliance_score` (champ existant, jusqu'ici modifiable seulement à la main
    via PATCH, jamais calculé)."""
    agency = models.ForeignKey(Agency, on_delete=models.CASCADE, related_name="compliance_snapshots")
    score = models.FloatField()
    components = models.JSONField(default=list)
    computed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-computed_at"]

    def __str__(self) -> str:
        return f"{self.agency_id} {self.score}% @ {self.computed_at}"


class EvolutionPlan(models.Model):
    """Plan de montée/descente en catégorie réseau (Point de service -> Rurale -> Urbaine ->
    Siège, ou l'inverse) — remplace le changement instantané `services.evolve_type` par une
    checklist de prérequis à cocher avant que le type réel de l'agence ne change. `evolve_type`
    reste utilisable tel quel (compatibilité ascendante, déjà câblé sur Agencies.jsx) ; ce
    workflow est la voie recommandée pour tout nouveau câblage."""
    class Status(models.TextChoices):
        IN_PROGRESS = "IN_PROGRESS", "En cours"
        COMPLETED = "COMPLETED", "Terminé"
        CANCELLED = "CANCELLED", "Annulé"

    agency = models.ForeignKey(Agency, on_delete=models.CASCADE, related_name="evolution_plans")
    from_type = models.CharField(max_length=20, choices=Agency.Type.choices)
    to_type = models.CharField(max_length=20, choices=Agency.Type.choices)
    reason = models.TextField(blank=True)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.IN_PROGRESS)
    created_by = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.agency.code} {self.from_type}->{self.to_type} [{self.status}]"


class EvolutionPlanItem(models.Model):
    plan = models.ForeignKey(EvolutionPlan, on_delete=models.CASCADE, related_name="items")
    label = models.CharField(max_length=200)
    order = models.IntegerField(default=0)
    is_done = models.BooleanField(default=False)
    done_by = models.CharField(max_length=255, blank=True)
    done_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["order", "id"]

    def __str__(self) -> str:
        return f"{self.plan_id} — {self.label} [{'fait' if self.is_done else 'à faire'}]"


class AgencyAlert(models.Model):
    class Level(models.TextChoices):
        INFO = "INFO", "Info"
        WARNING = "WARNING", "Avertissement"
        DANGER = "DANGER", "Danger"

    agency = models.ForeignKey(Agency, on_delete=models.CASCADE, related_name="alerts")
    level = models.CharField(max_length=10, choices=Level.choices, default=Level.INFO)
    message = models.CharField(max_length=255)
    resolved_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.agency.code} [{self.level}] {self.message}"


class AgencyReactivation(models.Model):
    """Justificatif écrit + pièce à l'appui d'une réactivation (déverrouillage après
    suspension, ou réouverture après fermeture) — l'institution a délibérément suspendu ou
    fermé l'agence, donc y revenir exige une preuve tracée, pas seulement un motif texte
    (contrairement à `suspend`/`close` qui n'exigent qu'une raison)."""
    class Kind(models.TextChoices):
        UNLOCK = "UNLOCK", "Déverrouillage (après suspension)"
        REOPEN = "REOPEN", "Réouverture (après fermeture)"

    agency = models.ForeignKey(Agency, on_delete=models.CASCADE, related_name="reactivations")
    kind = models.CharField(max_length=10, choices=Kind.choices)
    reason = models.TextField()
    document = models.FileField(upload_to="agency_reactivations/")
    created_by = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.agency.code} [{self.kind}] {self.created_at}"


class AgencyReconciliation(models.Model):
    """Rapprochement structuré d'une agence sur une période donnée — workflow réel
    (ouvert → assigné → terminé avec écart constaté), distinct du simple rapport de balance
    en lecture seule (`agencies.services.reconciliation_report`, qui reste utile mais ne
    trace aucun suivi/assignation)."""
    class Status(models.TextChoices):
        PENDING = "PENDING", "En attente"
        IN_PROGRESS = "IN_PROGRESS", "En cours"
        COMPLETED = "COMPLETED", "Terminé"

    agency = models.ForeignKey(Agency, on_delete=models.CASCADE, related_name="reconciliations")
    period_start = models.DateField()
    period_end = models.DateField()
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING)
    delta_amount = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    currency = models.CharField(max_length=3, blank=True)
    assigned_to = models.CharField(max_length=255, blank=True)
    notes = models.TextField(blank=True)
    is_final_closure = models.BooleanField(default=False)
    opened_at = models.DateTimeField(auto_now_add=True)
    closed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-opened_at"]

    def __str__(self) -> str:
        return f"{self.agency.code} {self.period_start}..{self.period_end} [{self.status}]"


class AgencyActionRequest(models.Model):
    """Demande maker-checker pour une action sensible de cycle de vie d'agence — le
    demandeur (maker) crée la demande, un second approbateur DISTINCT (checker), muni d'un
    code de vérification à usage unique, l'exécute. Remplace l'exécution directe à un seul
    acteur pour les 4 actions les plus sensibles (`suspend`/`close`/`unlock_temporary`/
    `reopen`) — `evolve_type` reste à acteur unique (changement de catégorie réseau, pas
    une décision de gel/réactivation)."""
    class ActionType(models.TextChoices):
        SUSPEND = "SUSPEND", "Suspendre"
        CLOSE = "CLOSE", "Fermer"
        UNLOCK_TEMPORARY = "UNLOCK_TEMPORARY", "Déverrouillage temporaire"
        REOPEN = "REOPEN", "Réouverture"

    class Status(models.TextChoices):
        PENDING_APPROVAL = "PENDING_APPROVAL", "En attente d'approbation"
        REJECTED = "REJECTED", "Rejetée"
        EXECUTED = "EXECUTED", "Exécutée"

    agency = models.ForeignKey(Agency, on_delete=models.CASCADE, related_name="action_requests")
    action_type = models.CharField(max_length=20, choices=ActionType.choices)
    reason = models.TextField()
    # Requis uniquement pour UNLOCK_TEMPORARY/REOPEN (même exigence que
    # `AgencyReactivation`) — vide pour SUSPEND/CLOSE.
    document = models.FileField(upload_to="agency_action_requests/", null=True, blank=True)
    requested_by = models.CharField(max_length=255)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING_APPROVAL)
    approved_by = models.CharField(max_length=255, blank=True)
    decided_at = models.DateTimeField(null=True, blank=True)
    rejection_note = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.agency.code} [{self.action_type}] {self.status}"


class AgencyActionChallenge(models.Model):
    """Code de vérification à usage unique envoyé au checker avant approbation — même
    mécanisme (hash salé, expiration, tentatives limitées) que `transactions.OtpChallenge`
    pour les approbations de transaction, dupliqué ici plutôt que généralisé : la FK de
    `OtpChallenge` vers `Transaction` est fixe, la généraliser aurait touché un modèle déjà
    testé en production pour un gain minime."""
    id = models.CharField(max_length=36, primary_key=True, default=_generate_challenge_id)
    request = models.ForeignKey(AgencyActionRequest, on_delete=models.CASCADE, related_name="challenges")
    approver_sub = models.CharField(max_length=255)
    code_hash = models.CharField(max_length=128)
    attempts = models.IntegerField(default=0)
    max_attempts = models.IntegerField(default=5)
    expires_at = models.DateTimeField()
    verified_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return f"Challenge {self.id[:8]} request={self.request_id} approver={self.approver_sub}"


class ActionApproverConfig(models.Model):
    """Désignation explicite des approbateurs par type d'action — quand au moins un
    approbateur est configuré pour un (scope, action_type), seuls ces utilisateurs
    peuvent déclencher le code de vérification et approuver l'action."""

    class Scope(models.TextChoices):
        AGENCY = "agency", "Action Agence"
        TRANSACTION = "transaction", "Transaction"
        CAISSE_REG = "caisse_regularization", "Régularisation Caisse"
        CAISSE_WD = "caisse_withdrawal", "Retrait Caisse"

    scope = models.CharField(max_length=30, choices=Scope.choices, default=Scope.AGENCY)
    action_type = models.CharField(max_length=50)
    approver_sub = models.CharField(max_length=255)
    approver_name = models.CharField(max_length=200, blank=True)
    approver_role = models.CharField(max_length=50, blank=True)
    approver_phone = models.CharField(max_length=30, blank=True)
    assigned_by = models.CharField(max_length=255, blank=True)
    assigned_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [("scope", "action_type", "approver_sub")]
        ordering = ["scope", "action_type"]

    def __str__(self) -> str:
        return f"{self.scope}/{self.action_type} → {self.approver_name or self.approver_sub}"
