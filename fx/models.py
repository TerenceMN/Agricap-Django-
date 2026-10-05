"""Taux de change GOUVERNÉS (HAZINA principe 5) — à 3 paliers (bcc/staff/client), par devise
étrangère, par date d'effet ET PAR USAGE (opérationnel / clôture).

Le CDF est la devise locale implicite (pivot), pas une entrée de `Currency` : chaque taux
exprime « combien de CDF pour 1 unité de `currency` » (`ExchangeRateManager` côté frontend).

Ce que « gouverné » veut dire ici, et que le simple `ExchangeRate` d'origine ne portait pas :

* **un taux par jour ET par usage** — un état financier référence « le taux de clôture »,
  qui n'est pas le taux auquel on a servi les guichets de la journée ;
* **historisé, jamais écrasé** (principe 3) — corriger un taux crée une VERSION suivante ;
  l'ancienne reste lisible avec son statut `REMPLACE`, sa date de remplacement et le lien
  vers la version qui l'a remplacée ;
* **maker ≠ checker au-delà d'un écart de X %** vs le taux de référence — X vit dans
  `InstitutionConfig` (principe 8). Sous le seuil, la saisie est directement `ACTIF` ;
  au-delà, elle naît `EN_ATTENTE` et n'est utilisable par aucune conversion tant qu'un
  second acteur ne l'a pas validée avec motif ;
* **source tracée** (BCC / manuelle / agrégateur) sur chaque ligne.

Invariant central : à un instant donné, il existe AU PLUS UN taux `ACTIF` par
(palier, devise, date d'effet, usage) — contrainte de base, pas convention de code.
"""
from __future__ import annotations

from django.db import models
from django.db.models import F, Q

from common.exceptions import ValidationFailed

#: Champs figés à la création : un taux probant ne se ré-écrit pas, il se re-version.
IMMUTABLE_FIELDS = (
    "tier", "currency", "usage", "buy_rate", "sell_rate", "effective_date",
    "source", "version", "created_by",
)


class ExchangeRate(models.Model):
    class Tier(models.TextChoices):
        BCC = "BCC", "BCC (officiel)"
        STAFF = "STAFF", "Interne (staff)"
        CLIENT = "CLIENT", "Client (commercial)"

    class Currency(models.TextChoices):
        USD = "USD", "USD"
        EUR = "EUR", "EUR"
        GBP = "GBP", "GBP"
        CAD = "CAD", "CAD"
        CHF = "CHF", "CHF"
        CNY = "CNY", "CNY"
        ZAR = "ZAR", "ZAR"

    class Usage(models.TextChoices):
        """Codes alignés sur `accounting.TauxChange.Usage` (principe 6 : une seule
        nomenclature par concept) — les deux modèles doivent converger, cf. rapport."""
        OPERATIONNEL = "OPERATIONNEL", "Opérationnel (guichets, transactions du jour)"
        CLOTURE = "CLOTURE", "Clôture (arrêté comptable, états financiers)"

    class Source(models.TextChoices):
        BCC = "BCC", "BCC (publication officielle)"
        MANUELLE = "MANUELLE", "Saisie manuelle (décision interne)"
        AGREGATEUR = "AGREGATEUR", "Agrégateur / marché constaté"

    class Status(models.TextChoices):
        EN_ATTENTE = "EN_ATTENTE", "En attente de validation (écart > seuil)"
        ACTIF = "ACTIF", "Actif (taux en vigueur)"
        REMPLACE = "REMPLACE", "Remplacé par une version postérieure"
        REJETE = "REJETE", "Rejeté par le valideur"

    tier = models.CharField(max_length=10, choices=Tier.choices)
    currency = models.CharField(max_length=3, choices=Currency.choices)
    usage = models.CharField(max_length=16, choices=Usage.choices,
                             default=Usage.OPERATIONNEL, db_index=True)
    buy_rate = models.DecimalField(max_digits=14, decimal_places=6)
    sell_rate = models.DecimalField(max_digits=14, decimal_places=6)
    effective_date = models.DateField()

    # ── Provenance ────────────────────────────────────────────────────────────
    source = models.CharField(max_length=16, choices=Source.choices, default=Source.MANUELLE)
    #: Référence vérifiable de la source (URL de publication BCC, identifiant d'agrégateur,
    #: n° de note interne) — sans elle, « source = BCC » est une affirmation, pas une preuve.
    source_reference = models.CharField(max_length=255, blank=True)

    # ── Versionnement append-only ─────────────────────────────────────────────
    status = models.CharField(max_length=16, choices=Status.choices,
                              default=Status.ACTIF, db_index=True)
    version = models.PositiveIntegerField(default=1)
    supersedes = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.PROTECT,
        related_name="superseded_by",
        help_text="Version que ce taux remplace (chaîne de correction lisible).",
    )
    superseded_at = models.DateTimeField(null=True, blank=True)

    # ── Maker / checker ───────────────────────────────────────────────────────
    #: Écart mesuré (en %) contre le taux de référence au moment de la saisie, et seuil
    #: appliqué à ce moment-là : le seuil est paramétrable, donc il doit être figé sur la
    #: ligne — sinon un auditeur ne peut plus dire pourquoi CE taux est passé sans checker.
    variation_pct = models.DecimalField(max_digits=9, decimal_places=4, null=True, blank=True)
    threshold_pct = models.DecimalField(max_digits=9, decimal_places=4, null=True, blank=True)
    reference_rate = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.PROTECT,
        related_name="compared_by",
        help_text="Taux contre lequel l'écart a été mesuré (la veille, ou le taux corrigé).",
    )
    reason = models.TextField(blank=True)  # motif du maker (obligatoire au-delà du seuil)

    created_by = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    validated_by = models.CharField(max_length=255, blank=True)
    validated_at = models.DateTimeField(null=True, blank=True)
    validation_reason = models.TextField(blank=True)  # motif du checker (validation OU rejet)

    class Meta:
        ordering = ["-effective_date", "usage", "-version"]
        constraints = [
            # Historique : une seule ligne par (palier, devise, date, usage, version).
            models.UniqueConstraint(
                fields=["tier", "currency", "effective_date", "usage", "version"],
                name="fx_rate_version_unique",
            ),
            # Au plus un taux EN VIGUEUR par (palier, devise, date, usage).
            models.UniqueConstraint(
                fields=["tier", "currency", "effective_date", "usage"],
                condition=Q(status="ACTIF"), name="fx_rate_single_active",
            ),
            # Au plus une correction en attente : deux corrections contradictoires en
            # attente sur le même taux rendraient la validation ambiguë.
            models.UniqueConstraint(
                fields=["tier", "currency", "effective_date", "usage"],
                condition=Q(status="EN_ATTENTE"), name="fx_rate_single_pending",
            ),
            models.CheckConstraint(condition=Q(sell_rate__gt=F("buy_rate")), name="fx_sell_gt_buy"),
            models.CheckConstraint(condition=Q(buy_rate__gt=0), name="fx_buy_strictly_positive"),
        ]
        indexes = [
            models.Index(fields=["tier", "currency", "usage", "status", "-effective_date"],
                         name="fx_rate_lookup_idx"),
        ]

    def __str__(self) -> str:
        return (f"{self.tier}/{self.currency}/{self.usage} {self.effective_date} v{self.version} "
                f"[{self.status}] buy={self.buy_rate} sell={self.sell_rate}")

    # ── Append-only (principe 3) ──────────────────────────────────────────────

    def save(self, *args, **kwargs):
        """Un taux déjà enregistré ne change pas de valeur : on en publie une nouvelle
        version. Seuls le cycle de vie (statut, validation, remplacement) et le motif du
        checker restent modifiables."""
        if self.pk:
            stored = (ExchangeRate.objects.filter(pk=self.pk)
                      .values(*IMMUTABLE_FIELDS).first())
            if stored:
                modifies = [f for f in IMMUTABLE_FIELDS if stored[f] != getattr(self, f)]
                if modifies:
                    raise ValidationFailed(
                        "Un taux enregistré ne se corrige pas sur place ({}) : publiez une "
                        "nouvelle version (fx.services.set_rate).".format(", ".join(modifies))
                    )
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationFailed(
            "DELETE n'existe pas sur un taux de change : il est probant. "
            "Publiez une version corrective (fx.services.set_rate)."
        )

    # ── Lecture ───────────────────────────────────────────────────────────────

    @property
    def is_current(self) -> bool:
        return self.status == self.Status.ACTIF

    @property
    def mid_rate(self):
        """Cours pivot (achat+vente)/2 — le « cours indicatif » au sens BCC."""
        return (self.buy_rate + self.sell_rate) / 2
