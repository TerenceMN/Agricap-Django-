"""Statut des connexions API tierces (ApiPartners.jsx)."""
from __future__ import annotations

from django.db import models


class Partner(models.Model):
    class Status(models.TextChoices):
        CONNECTE = "Connecté", "Connecté"
        ACTIF = "Actif", "Actif"
        EN_ATTENTE = "En attente", "En attente"
        DECONNECTE = "Déconnecté", "Déconnecté"

    class CircuitState(models.TextChoices):
        """Disjoncteur simple (pas de file d'attente/retry différé, faute d'infrastructure
        de tâches planifiées dans ce projet) : après `CIRCUIT_FAILURE_THRESHOLD` échecs
        consécutifs, plus aucun appel sortant n'est tenté avant `CIRCUIT_HALF_OPEN_SECONDS`
        — protège un partenaire déjà en panne d'un bombardement de tentatives inutiles."""
        CLOSED = "CLOSED", "Fermé"
        OPEN = "OPEN", "Ouvert"
        HALF_OPEN = "HALF_OPEN", "Semi-ouvert"

    name = models.CharField(max_length=150)
    type = models.CharField(max_length=120, blank=True)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.EN_ATTENTE)
    last_sync = models.DateTimeField(null=True, blank=True)
    # URL de test réel (health check) — laissé vide par défaut : sans identifiants réels
    # d'opérateur mobile money/bancaire, "tester" reste honnête (message explicite) plutôt
    # que de simuler un résultat aléatoire.
    base_url = models.CharField(max_length=255, blank=True)
    consecutive_failures = models.IntegerField(default=0)
    circuit_state = models.CharField(max_length=10, choices=CircuitState.choices, default=CircuitState.CLOSED)
    circuit_opened_at = models.DateTimeField(null=True, blank=True)

    def __str__(self) -> str:
        return self.name


class PartnerHealthCheck(models.Model):
    partner = models.ForeignKey(Partner, on_delete=models.CASCADE, related_name="health_checks")
    checked_at = models.DateTimeField(auto_now_add=True)
    ok = models.BooleanField()
    latency_ms = models.IntegerField(null=True, blank=True)
    http_status = models.IntegerField(null=True, blank=True)
    error_text = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["-checked_at"]

    def __str__(self) -> str:
        return f"{self.partner.name} {'OK' if self.ok else 'FAIL'} {self.checked_at}"


class PartnerSyncLog(models.Model):
    class Status(models.TextChoices):
        SUCCESS = "SUCCESS", "Succès"
        FAILED = "FAILED", "Échec"

    partner = models.ForeignKey(Partner, on_delete=models.CASCADE, related_name="sync_logs")
    status = models.CharField(max_length=10, choices=Status.choices)
    started_at = models.DateTimeField(auto_now_add=True)
    error_text = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["-started_at"]

    def __str__(self) -> str:
        return f"{self.partner.name} {self.status} {self.started_at}"
