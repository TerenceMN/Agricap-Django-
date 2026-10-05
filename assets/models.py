"""Registre des actifs gageables.

Historique — ce modèle était un CRUD léger pour `AssetsInventory.jsx` :
`status ∈ {free, pledged}`, valeur purement déclarative, et le client pouvait
écrire `status` lui-même via PATCH. Un actif « libre » n'avait donc jamais été
vu par personne : il suffisait de le déclarer pour qu'il serve de garantie.

Principe 9 — « toute garantie est opposable ou n'est pas » : un actif doit
exister, avoir été **vérifié par un agent**, appartenir au client et être libre
de gage. D'où le cycle de vie à cinq états ci-dessous et la distinction entre
`value` (déclarée par le client) et `valeur_retenue` (fixée par l'agent après
décote institutionnelle) — c'est `valeur_retenue`, et elle seule, qui compte
dans la couverture d'un crédit.

`image` stocke un chemin/URL (pas d'ImageField/Pillow, cohérent avec l'ethos
« projet isolé et léger » du backend).
"""
from __future__ import annotations

from decimal import Decimal

from django.db import models


class Asset(models.Model):
    class Type(models.TextChoices):
        """Catégories alignées sur les types de garantie canoniques.

        `materiel` et `vehicule` et `stock` donnent une garantie `materiel` ;
        `foncier` donne une garantie `foncier` ; `autre` n'est jamais gageable.
        """
        MATERIEL = "materiel", "Matériel / Équipement"
        FONCIER = "foncier", "Foncier / Immobilier"
        VEHICULE = "vehicule", "Véhicule"
        STOCK = "stock", "Stock / Récolte"
        AUTRE = "autre", "Autre (non gageable)"

    class Status(models.TextChoices):
        DECLARE = "declare", "Déclaré"      # saisi par le client, non vérifié
        VERIFIE = "verifie", "Vérifié"      # contrôlé par un agent terrain
        REJETE = "rejete", "Rejeté"
        GAGE = "gage", "Gagé"               # nanti sur un dossier de crédit
        LIBERE = "libere", "Libéré"         # gage levé — repasse vérifié

    user = models.ForeignKey(
        "accounts.FintechUser", on_delete=models.CASCADE, related_name="assets",
    )
    name = models.CharField(max_length=150)
    type = models.CharField(max_length=12, choices=Type.choices, default=Type.AUTRE)

    # Valeur déclarée par le client — jamais utilisée pour la couverture
    value = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0"))
    # Valeur retenue par l'agent après vérification et décote — fait foi
    valeur_retenue = models.DecimalField(
        max_digits=14, decimal_places=2, null=True, blank=True,
    )
    currency = models.CharField(max_length=3, default="USD")

    description = models.CharField(max_length=255, blank=True)
    localisation = models.CharField(max_length=200, blank=True)
    # Références des preuves : titre foncier, facture, photos…
    documents = models.JSONField(default=list, blank=True)

    status = models.CharField(max_length=10, choices=Status.choices, default=Status.DECLARE)
    image = models.CharField(max_length=500, blank=True)

    # ── Vérification terrain ─────────────────────────────────────────────────
    verifie_par_sub = models.CharField(max_length=255, blank=True)
    verifie_le = models.DateTimeField(null=True, blank=True)
    motif_rejet = models.CharField(max_length=255, blank=True)

    # ── Gage ─────────────────────────────────────────────────────────────────
    gage_application = models.ForeignKey(
        "credits.CreditApplication", null=True, blank=True,
        on_delete=models.PROTECT, related_name="assets_gages",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["user", "status"])]

    def __str__(self) -> str:
        return f"{self.name} ({self.user_id}) [{self.status}]"

    # ── Invariants métier ────────────────────────────────────────────────────

    @property
    def is_pledgeable(self) -> bool:
        """Un actif n'est mobilisable en garantie que vérifié, libre et valorisé."""
        return (
            self.status in (self.Status.VERIFIE, self.Status.LIBERE)
            and self.gage_application_id is None
            and self.valeur_retenue is not None
            and self.valeur_retenue > 0
        )

    @property
    def guarantee_type(self) -> str:
        """Type de garantie canonique correspondant à la catégorie de l'actif."""
        if self.type == self.Type.FONCIER:
            return "foncier"
        if self.type in (self.Type.MATERIEL, self.Type.VEHICULE, self.Type.STOCK):
            return "materiel"
        return ""  # `autre` — non gageable
