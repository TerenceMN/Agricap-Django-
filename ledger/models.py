"""Comptabilité en partie double (Accounting.jsx) — plan comptable, écritures, lignes.
L'équilibre Σdébit=Σcrédit par écriture est un invariant CROSS-LIGNES, vérifié en service
(`services.post_journal_entry` lève `ValidationFailed` si déséquilibré) — un
`CheckConstraint` DB ne peut pas exprimer un invariant agrégé sur plusieurs lignes ; limite
assumée et documentée."""
from __future__ import annotations

from decimal import Decimal

from django.db import models
from django.db.models import Q


class ChartAccount(models.Model):
    class Nature(models.TextChoices):
        """Sens normal du solde — nécessaire car SYSCOHADA mélange actif et passif DANS une
        même classe (ex. classe 4 : 411 Clients est actif, 401 Fournisseurs est passif) ; le
        seul `class_no` ne suffit pas à répartir Bilan/Compte de résultat correctement."""
        ACTIF = "ACTIF", "Actif"
        PASSIF = "PASSIF", "Passif"
        CHARGE = "CHARGE", "Charge"
        PRODUIT = "PRODUIT", "Produit"

    code = models.CharField(max_length=16, unique=True, db_index=True)
    name = models.CharField(max_length=200)
    class_no = models.IntegerField()  # 1-8 SYSCOHADA révisé (classe 9 facultative, non utilisée)
    nature = models.CharField(max_length=8, choices=Nature.choices, default=Nature.ACTIF)
    # False pour les comptes présents uniquement pour la conformité structurelle SYSCOHADA
    # (classes 2/3/8, comptes de négoce/production 601-603/701-705/72/73) qu'AGRICAP FINTECH
    # ne mouvemente jamais en pratique (prêteur, pas une entreprise commerciale/industrielle)
    # — distingue dans le plan comptable ce qui relève de l'activité réelle vs de la
    # complétude légale. N'empêche aucune écriture : un compte non-core reste postable.
    is_core_activity = models.BooleanField(default=True)
    currencies = models.JSONField(default=list, blank=True)  # ex. ["FC", "USD"]
    parent = models.ForeignKey("self", null=True, blank=True, on_delete=models.SET_NULL, related_name="children")

    class Meta:
        ordering = ["code"]

    def __str__(self) -> str:
        return f"{self.code} — {self.name}"


class JournalEntry(models.Model):
    class Code(models.TextChoices):
        JCR = "JCR", "Journal crédit"
        JEP = "JEP", "Journal épargne"
        JCA = "JCA", "Journal caisse"
        JMM = "JMM", "Journal mobile money"
        JFX = "JFX", "Journal change"

    class Status(models.TextChoices):
        VALIDE = "VALIDE", "Validé"
        EN_ATTENTE = "EN_ATTENTE", "En attente"
        REJETE = "REJETE", "Rejeté"

    date = models.DateField()
    piece_ref = models.CharField(max_length=64)
    code = models.CharField(max_length=3, choices=Code.choices)
    description = models.CharField(max_length=255, blank=True)
    currency = models.CharField(max_length=3, default="USD")
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.VALIDE)
    created_by = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-date", "-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["date", "piece_ref"], name="journalentry_date_pieceref_unique"),
        ]

    def __str__(self) -> str:
        return f"{self.piece_ref} [{self.code}] {self.date}"


class JournalLine(models.Model):
    entry = models.ForeignKey(JournalEntry, on_delete=models.CASCADE, related_name="lines")
    account = models.ForeignKey(ChartAccount, on_delete=models.PROTECT, related_name="lines")
    debit = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal("0"))
    credit = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal("0"))
    user_sub = models.CharField(max_length=255, blank=True)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=Q(debit__gte=0), name="line_debit_nonneg"),
            models.CheckConstraint(condition=Q(credit__gte=0), name="line_credit_nonneg"),
            models.CheckConstraint(
                condition=~(Q(debit__gt=0) & Q(credit__gt=0)),
                name="line_not_both_debit_credit",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.entry_id} {self.account.code} D:{self.debit} C:{self.credit}"
