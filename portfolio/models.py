"""
Portefeuille de crédits — CYCLE DE VIE des dossiers (distinct du moteur d'analyse
`credit`). Alimente le « Module Crédits Agricoles » côté admin : liste des dossiers,
statuts, taux & maturité (avec historique/audit), échéancier d'amortissement, journal
des mouvements financiers, notes et alertes.

Un dossier peut être relié (optionnel) à un dossier d'analyse du moteur (`credit`),
mais reste autonome : un dossier peut être saisi manuellement (« Ajouter »).
"""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

from django.db import models
from django.utils import timezone

from . import schedule


class Loan(models.Model):
    """Un dossier de crédit géré (un « crédit » de la table admin)."""

    class Status(models.TextChoices):
        EN_TRAITEMENT = "EN_TRAITEMENT", "En traitement"
        APPROUVE = "APPROUVE", "Approuvé"
        EN_COURS = "EN_COURS", "En cours"
        DEFAUT = "DEFAUT", "Défaut"
        CLOTURE = "CLOTURE", "Clôturé"
        REJETE = "REJETE", "Rejeté"
        SUSPENDU = "SUSPENDU", "Suspendu"
        BLOQUE = "BLOQUE", "Bloqué"

    class Frequency(models.TextChoices):
        MONTHLY = "monthly", "Mensuel"
        QUARTERLY = "quarterly", "Trimestriel"
        ANNUAL = "annual", "Annuel"
        BULLET = "bullet", "In fine (à terme)"

    class Currency(models.TextChoices):
        USD = "USD", "USD"
        CDF = "CDF", "CDF"

    class DeferralMode(models.TextChoices):
        """Codes RIGOUREUSEMENT ceux de `credits.echeancier` (principe 6)."""

        INTERETS_SEULS = schedule.MODE_INTERETS_SEULS, "Intérêts seuls"
        FRANCHISE_TOTALE = schedule.MODE_FRANCHISE_TOTALE, "Franchise totale"

    reference = models.CharField(max_length=32, unique=True, db_index=True)  # CRD-AAAA-NNN
    date = models.DateField(default=timezone.localdate)                      # date de la demande
    operator = models.CharField(max_length=200)                             # bénéficiaire / opérateur
    category = models.CharField(max_length=120, blank=True)                 # catégorie / filière

    amount_requested = models.DecimalField(max_digits=16, decimal_places=2, default=Decimal("0"))
    amount_approved = models.DecimalField(max_digits=16, decimal_places=2, default=Decimal("0"))
    currency = models.CharField(max_length=3, choices=Currency.choices, default=Currency.USD)

    duration_months = models.IntegerField(default=12)

    # ── Taux : DEUX champs, UNE vérité ────────────────────────────────────────
    # `rate` est MENSUEL, `annual_rate` est ANNUEL, et `_accorder_les_taux()` les
    # maintient rigoureusement cohérents à chaque écriture. L'unité est nommée
    # dans le schéma parce qu'elle ne l'était nulle part ailleurs : un « 18 % »
    # de dossier scoré (annuel) reporté dans `rate` créait un prêt à 216 %/an.
    rate = models.DecimalField(
        max_digits=9, decimal_places=6, default=Decimal("0"),
        verbose_name="Taux MENSUEL (%/mois)",
        help_text="Taux MENSUEL en points de % (1.5 = 1,5 %/mois = 18 %/an). "
                  "Pour saisir un taux annuel, utilisez « Taux annuel ».",
    )
    #: Taux nominal ANNUEL en % — même unité et même précision que
    #: `credits.AnalyseCredit.taux_annuel`, dont il est la recopie au décaissement.
    #: Source canonique du calcul quand il est renseigné (cf. `monthly_rate_pct`) :
    #: la division par 12 se fait en pleine précision, pas sur `rate` arrondi.
    annual_rate = models.DecimalField(
        max_digits=6, decimal_places=3, null=True, blank=True,
        verbose_name="Taux ANNUEL (%/an)",
        help_text="Taux nominal ANNUEL en points de % (18 = 18 %/an). "
                  "Recopié de l'analyse au rattachement du dossier.",
    )
    frequency = models.CharField(max_length=12, choices=Frequency.choices, default=Frequency.MONTHLY)

    # ── Différé ───────────────────────────────────────────────────────────────
    # Le prévisionnel qui SCORE le dossier gère un différé ; le prêt qui le
    # REMBOURSE n'en avait aucun. Un dossier scoré avec 5 mois de différé — donc
    # dont le DSCR est calculé sur ce différé, après récolte — était remboursé dès
    # le premier mois. Le calendrier cultural n'était pas honoré (CONVENTIONS.md §4.2).
    deferral_months = models.PositiveIntegerField(
        default=0, verbose_name="Différé (mois)",
        help_text="Nombre de mois de différé, INCLUS dans la durée totale. "
                  "Périodicité mensuelle uniquement.",
    )
    deferral_mode = models.CharField(
        max_length=20, choices=DeferralMode.choices, default=DeferralMode.INTERETS_SEULS,
        verbose_name="Mode de différé",
        help_text="Intérêts seuls : le client paie les intérêts, le capital reste "
                  "intact. Franchise totale : rien n'est payé, les intérêts sont "
                  "capitalisés et grossissent le capital à amortir.",
    )

    start_date = models.DateField(null=True, blank=True)
    due_date = models.DateField(null=True, blank=True)

    manager = models.CharField(max_length=150, blank=True)      # gestionnaire
    investor = models.CharField(max_length=150, blank=True)
    source = models.CharField(max_length=120, blank=True)       # ex. « App », « Terrain », « IdP »

    status = models.CharField(max_length=16, choices=Status.choices, default=Status.EN_TRAITEMENT)
    score = models.IntegerField(default=0)
    guarantee = models.CharField(max_length=200, blank=True)

    # Lien optionnel vers un dossier de crédit (traçabilité).
    application = models.ForeignKey(
        "credits.CreditApplication", null=True, blank=True,
        on_delete=models.SET_NULL, related_name="loans",
    )

    # Client TITULAIRE du dossier (Credits.jsx, espace client) — distinct de `created_by`
    # (qui peut être un membre du staff ayant saisi le dossier manuellement). Renseigné
    # automatiquement quand le client soumet lui-même sa demande (auto-service).
    borrower_sub = models.CharField(max_length=255, blank=True, db_index=True)

    created_by = models.CharField(max_length=255, blank=True)   # sub IdP
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.reference} — {self.operator} [{self.get_status_display()}]"

    # --- Taux : cohérence mensuel / annuel, imposée à l'écriture --------------
    #: Champs dont la cohérence est maintenue par `_accorder_les_taux()`.
    RATE_FIELDS = ("rate", "annual_rate")

    @classmethod
    def from_db(cls, db, field_names, values):
        """Mémorise les taux CHARGÉS pour savoir, au `save()`, lesquels ont bougé.

        Sans cette photo, on ne peut pas distinguer « on écrit un taux » de « on
        sauvegarde une ligne dont le taux n'a pas changé » — et le garde-fou de
        plausibilité bloquerait la clôture d'un dossier hérité au lieu de bloquer
        la saisie fautive.
        """
        instance = super().from_db(db, field_names, values)
        instance._photographier_les_taux(
            [nom for nom in cls.RATE_FIELDS if nom in field_names])
        return instance

    def _photographier_les_taux(self, champs=None) -> None:
        """Fige l'état PERSISTÉ des taux — la référence du prochain `save()`."""
        self._taux_charges = {
            nom: getattr(self, nom) for nom in (champs or self.RATE_FIELDS)
        }

    def _accorder_les_taux(self) -> set[str]:
        """Valide le taux ÉCRIT et aligne son pendant. Renvoie les champs touchés.

        Règles :
          - le taux ANNUEL prime quand il est (re)saisi : c'est la valeur que porte
            l'analyse, donc la seule qui se compare à un dossier scoré ;
          - sinon le taux MENSUEL saisi est validé (plafond de plausibilité) et
            projeté en annuel ;
          - une ligne héritée (créée avant `annual_rate`) est complétée sans être
            jugée : on ne requalifie pas rétroactivement un taux déjà appliqué,
            on le rend seulement lisible.
        """
        from . import rates

        charges = getattr(self, "_taux_charges", None) or {}
        # Aucune photo = objet jamais persisté : les deux champs sont « écrits »,
        # et l'ANNUEL prime (c'est l'unité de l'analyse, donc du dossier scoré).
        neuf = not charges
        annuel_modifie = neuf or (
            "annual_rate" in charges and self.annual_rate != charges["annual_rate"])
        mensuel_modifie = neuf or (
            "rate" in charges and self.rate != charges["rate"])

        if self.annual_rate is not None and annuel_modifie:
            self.annual_rate = rates.valider_taux_annuel(self.annual_rate)
            self.rate = rates.mensuel_stocke(self.annual_rate)
            return set(self.RATE_FIELDS)
        if mensuel_modifie:
            self.rate = rates.valider_taux_mensuel(self.rate)
            self.annual_rate = rates.annuel_depuis_mensuel(self.rate)
            return set(self.RATE_FIELDS)
        if self.annual_rate is None and self.rate is not None and "rate" in charges:
            self.annual_rate = rates.annuel_depuis_mensuel(self.rate)
            return {"annual_rate"}
        return set()

    def clean(self):
        """Contrôle de l'admin Django (qui appelle `full_clean`) — même règle."""
        super().clean()
        self._accorder_les_taux()

    def save(self, *args, **kwargs):
        touches = self._accorder_les_taux()
        update_fields = kwargs.get("update_fields")
        ecrit = update_fields is None
        if touches and update_fields is not None:
            champs = set(update_fields)
            # Un `save(update_fields=["rate"])` doit persister l'annuel recalculé,
            # sinon les deux champs se contrediraient en base.
            if champs & touches:
                kwargs["update_fields"] = sorted(champs | touches)
                ecrit = True
        resultat = super().save(*args, **kwargs)
        if ecrit:
            # La photo suit ce qui est RÉELLEMENT en base : sans elle, une seconde
            # écriture sur l'objet en mémoire ne saurait plus quel champ a bougé et
            # écraserait silencieusement une saisie mensuelle par l'ancien annuel.
            self._photographier_les_taux()
        return resultat

    @property
    def monthly_rate_pct(self) -> Decimal:
        """Taux MENSUEL de CALCUL — `annual_rate / 12` en pleine précision.

        `rate` n'est que la projection arrondie à 6 décimales : amortir sur elle
        ferait diverger l'échéancier payé de l'échéancier scoré dès que le douzième
        du taux annuel n'est pas décimal fini (22,6 %/an → 1,88333…%/mois).
        """
        from . import rates

        if self.annual_rate is not None:
            return rates.mensuel_exact(self.annual_rate)
        return Decimal(self.rate or 0)

    # --- Agrégats dérivés du journal ------------------------------------------
    @property
    def disbursed(self) -> Decimal:
        """Total décaissé = somme des décaissements validés/en attente."""
        total = sum((t.amount for t in self.transactions.all()
                     if t.kind == LoanTransaction.Kind.DISBURSEMENT and t.amount), Decimal("0"))
        return total

    @property
    def disbursed_validated(self) -> Decimal:
        """Décaissé effectivement SORTI = décaissements au statut « Validé ».

        Distinct de `disbursed`, qui additionne aussi les mouvements « en attente ».
        C'est cette base-là qui s'amortit : on ne fait pas rembourser un capital
        dont on n'est pas sûr qu'il ait quitté la caisse. Même définition que
        `accounting.provisions._flux_du_credit`, pour que l'encours comptable et
        l'échéancier client parlent du même argent.
        """
        return sum(
            (t.amount for t in self.transactions.all()
             if t.kind == LoanTransaction.Kind.DISBURSEMENT
             and t.status == LoanTransaction.Status.VALIDE and t.amount),
            Decimal("0"),
        )

    @property
    def first_disbursement_date(self):
        """Date du premier décaissement validé — le jour où l'argent est sorti."""
        dates = [t.date for t in self.transactions.all()
                 if t.kind == LoanTransaction.Kind.DISBURSEMENT
                 and t.status == LoanTransaction.Status.VALIDE and t.amount and t.date]
        return min(dates) if dates else None

    @property
    def repaid(self) -> Decimal:
        """Total remboursé (montant positif) = somme des remboursements."""
        total = sum(((-t.amount) for t in self.transactions.all()
                     if t.kind == LoanTransaction.Kind.REPAYMENT and t.amount), Decimal("0"))
        return total

    @property
    def outstanding(self) -> Decimal:
        """Solde restant dû = décaissé − remboursé."""
        return self.disbursed - self.repaid

    @property
    def progress(self) -> int:
        """Progression de remboursement (%) sur le montant approuvé.

        En `Decimal` avec `ROUND_HALF_UP` (principe 4) : `round()` sur un `float`
        faisait de l'arrondi bancaire — 50,5 % s'affichait 50 % et 51,5 % s'affichait
        52 %, deux dossiers à mi-parcours ne se lisant pas de la même façon.
        """
        base = self.amount_approved or self.amount_requested
        if not base:
            return 0
        pourcent = (self.repaid / base * Decimal("100")).quantize(
            Decimal("1"), rounding=ROUND_HALF_UP)
        return max(0, min(100, int(pourcent)))


class LoanTransaction(models.Model):
    """Un mouvement financier du dossier (journal / sous-tableau)."""

    class Kind(models.TextChoices):
        DISBURSEMENT = "DISBURSEMENT", "Décaissement"
        REPAYMENT = "REPAYMENT", "Remboursement"
        FEE = "FEE", "Frais"
        REMINDER = "REMINDER", "Relance"
        ADJUSTMENT = "ADJUSTMENT", "Ajustement"
        OTHER = "OTHER", "Autre"

    class Status(models.TextChoices):
        VALIDE = "VALIDE", "Validé"
        EN_ATTENTE = "EN_ATTENTE", "En attente"
        NON_APPLICABLE = "NON_APPLICABLE", "Non applicable"

    loan = models.ForeignKey(Loan, on_delete=models.CASCADE, related_name="transactions")
    # Sous-portefeuille d'origine (Credits.jsx, espace client) — optionnel : les
    # décaissements/mouvements saisis côté admin (AdminCreditsDashboard) n'en ont pas.
    subwallet = models.ForeignKey(
        "LoanSubWallet", null=True, blank=True, on_delete=models.SET_NULL, related_name="transactions",
    )
    date = models.DateField(default=timezone.localdate)
    kind = models.CharField(max_length=16, choices=Kind.choices, default=Kind.OTHER)
    label = models.CharField(max_length=200, blank=True)    # ex. « Décaissement initial »
    # Signe : + = fonds sortis vers le client (décaissement) ; − = remboursement.
    amount = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    currency = models.CharField(max_length=3, choices=Loan.Currency.choices, default=Loan.Currency.USD)
    original_amount = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    original_currency = models.CharField(max_length=3, blank=True)
    payment_method = models.CharField(max_length=80, blank=True)
    reference = models.CharField(max_length=80, blank=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.VALIDE)
    verified_by = models.CharField(max_length=80, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["date", "created_at"]

    def __str__(self) -> str:
        return f"{self.loan.reference} {self.get_kind_display()} {self.amount}"


class LoanConfigHistory(models.Model):
    """Audit des changements taux/maturité/statut (onglet « Historique & Audit »)."""
    loan = models.ForeignKey(Loan, on_delete=models.CASCADE, related_name="config_history")
    action = models.CharField(max_length=120)
    user = models.CharField(max_length=120, blank=True)
    details = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.loan.reference} — {self.action}"


class LoanNote(models.Model):
    """Note libre attachée à un dossier."""
    loan = models.ForeignKey(Loan, on_delete=models.CASCADE, related_name="notes")
    author = models.CharField(max_length=120, blank=True)
    text = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"Note {self.loan.reference}"


class LoanSubWallet(models.Model):
    """Enveloppe budgétaire par module agricole (Credits.jsx, espace client) — le
    montant financé est ventilé par poste (semences, mécanisation, main-d'œuvre...) et
    chaque module se dépense indépendamment (« Payer ») ou se réajuste entre modules
    (« Réajuster »), sans jamais dépasser le total approuvé du dossier."""
    loan = models.ForeignKey(Loan, on_delete=models.CASCADE, related_name="subwallets")
    module_key = models.CharField(max_length=40)   # clé stable (ex. "semences") — cf. MODULES_CONFIG frontend
    label = models.CharField(max_length=120)
    allocated_amount = models.DecimalField(max_digits=16, decimal_places=2, default=Decimal("0"))
    balance = models.DecimalField(max_digits=16, decimal_places=2, default=Decimal("0"))

    class Meta:
        ordering = ["id"]
        constraints = [
            models.UniqueConstraint(fields=["loan", "module_key"], name="unique_loan_subwallet_module"),
            models.CheckConstraint(condition=models.Q(balance__gte=0), name="subwallet_balance_gte_0"),
        ]

    def __str__(self) -> str:
        return f"{self.loan.reference} — {self.label}"


class LoanGuarantee(models.Model):
    """Garantie enregistrée pour un dossier (morale, épargne nantie, actif physique...)."""
    loan = models.ForeignKey(Loan, on_delete=models.CASCADE, related_name="guarantee_items")
    type = models.CharField(max_length=40)
    label = models.CharField(max_length=200, blank=True)
    description = models.CharField(max_length=300, blank=True)
    value = models.DecimalField(max_digits=16, decimal_places=2, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["id"]

    def __str__(self) -> str:
        return f"{self.loan.reference} — {self.label or self.type}"
