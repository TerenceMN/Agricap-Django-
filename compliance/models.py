"""KYC/AML (Compliance.jsx) + centre de documents partagé client/investisseur
(ClientDocuments.jsx / InvestorDocuments.jsx)."""
from __future__ import annotations

from decimal import Decimal

from django.db import models

from common.choices import FlowStatus


class KycProfile(models.Model):
    class Status(models.TextChoices):
        VALIDE = "Validé", "Validé"
        EN_ATTENTE = "En attente", "En attente"

    class RiskScore(models.TextChoices):
        BAS = "Bas", "Bas"
        MOYEN = "Moyen", "Moyen"
        ELEVE = "Élevé", "Élevé"

    user = models.OneToOneField("accounts.FintechUser", on_delete=models.CASCADE, related_name="kyc_profile")
    kyc_status = models.CharField(max_length=12, choices=Status.choices, default=Status.EN_ATTENTE)
    risk_score = models.CharField(max_length=10, choices=RiskScore.choices, default=RiskScore.MOYEN)
    kyc_level = models.CharField(max_length=4, default="T1")
    monthly_limit = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("50000"))
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return f"KYC({self.user_id}) {self.kyc_status}"


class Document(models.Model):
    class Type(models.TextChoices):
        ID_CARD = "id_card", "Pièce d'identité"
        PROOF_ADDRESS = "proof_address", "Justificatif domicile"
        FINANCIAL = "financial", "Document financier"
        OTHER = "other", "Autre"

    user = models.ForeignKey("accounts.FintechUser", on_delete=models.CASCADE, related_name="documents")
    type = models.CharField(max_length=20, choices=Type.choices, default=Type.OTHER)
    name = models.CharField(max_length=200)
    file_url = models.CharField(max_length=500, blank=True)
    status = models.CharField(max_length=20, choices=FlowStatus.choices, default=FlowStatus.SUBMITTED)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.name} ({self.user_id})"


class KycTierLimit(models.Model):
    """Plafonds d'un palier KYC — en BASE, pas dans le code (principe 8).

    `LEVEL_LIMITS` et `BALANCE_CAPS` vivaient dans `kyc_levels.py` : changer le plafond
    de dépôt d'un client exigeait un redéploiement. Ce sont pourtant des seuils métier,
    exactement ce que le principe 8 veut voir en table paramétrique.

    Les constantes du code restent comme valeurs de SECOURS : une table vide ne bloque
    pas l'institution, elle logue un avertissement quand le repli s'applique.
    """

    tier = models.CharField(max_length=2, unique=True)
    #: Plafond de SOLDE — c'est lui qui refuse un dépôt qui le dépasserait.
    balance_cap = models.DecimalField(max_digits=18, decimal_places=2)
    #: Plafond de RETRAIT cumulé sur le mois civil.
    monthly_limit = models.DecimalField(max_digits=18, decimal_places=2)
    label = models.CharField(max_length=80, blank=True)
    updated_by = models.CharField(max_length=255, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["tier"]

    def __str__(self) -> str:
        return f"{self.tier} solde<{self.balance_cap} mois<{self.monthly_limit}"
