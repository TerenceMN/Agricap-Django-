"""Modèle canonique des investissements — fusion des 3 schémas mock incompatibles
(`agricapDataService.js` / `investmentData.js` / `investorSpaceData.js`) qui alimentaient
3 pages/consoles frontend différentes (AdminConsole, AdminInvestments, InvestorSpace). Les
mouvements d'argent réels sont TOUJOURS délégués à `transactions`/`caisses`/`ledger` et
seulement référencés ici en FK (`Movement.transaction`) — une seule source de vérité
comptable, jamais dupliquée."""
from __future__ import annotations

from decimal import Decimal

from django.db import models
from django.db.models import Q

from common.choices import FlowStatus


class InvestmentConfig(models.Model):
    """Paramètres du module investissement — principe 8 : « les règles vivent en base ».

    Un seul enregistrement actif. Tout ce qui gouverne une décision (quorum du comité,
    coefficients du score de santé, seuils d'alerte, politique de sursouscription par
    défaut) vit ici et non dans le code : le comité doit pouvoir recalibrer sans
    redéploiement, et la formule affichée à l'investisseur doit correspondre
    exactement aux paramètres appliqués.
    """

    is_active = models.BooleanField(default=True)

    #: Quorum du comité d'investissement. `null` = on hérite du quorum du comité de
    #: crédit (`credits.committee.committee_quorum()`, lui-même lu dans
    #: `InstitutionConfig`) — un seul paramétrage de gouvernance tant que
    #: l'institution n'a pas voulu les dissocier.
    committee_quorum = models.PositiveIntegerField(null=True, blank=True)

    # Score de santé /100 (Annexe D) :
    #   100 − a×taux_défaut − b×max(0, H−h₀)×100 − c×part_projets_en_retard
    health_coeff_default = models.DecimalField(max_digits=6, decimal_places=3, default=Decimal("4"))     # a
    health_coeff_concentration = models.DecimalField(max_digits=6, decimal_places=3, default=Decimal("50"))  # b
    health_coeff_late = models.DecimalField(max_digits=6, decimal_places=3, default=Decimal("1"))        # c
    #: h₀ — seuil de Herfindahl au-delà duquel la concentration pénalise (Annexe D).
    concentration_threshold = models.DecimalField(max_digits=5, decimal_places=4, default=Decimal("0.25"))
    #: Seuil d'alerte du taux de défaut en valeur (5 % par défaut, Annexe D).
    default_rate_alert = models.DecimalField(max_digits=5, decimal_places=4, default=Decimal("0.05"))

    #: Décote de provision appliquée au capital non remboursé d'un projet en défaut
    #: (P12) pour la valorisation latente — Annexe D, « décote de provision pour P12 ».
    #: 1,0000 = provision à 100 %, donc valeur retenue nulle. Un recouvrement
    #: RÉELLEMENT encaissé prime toujours sur ce paramètre : un fait bat une hypothèse.
    p12_provision_rate = models.DecimalField(max_digits=5, decimal_places=4, default=Decimal("1.0000"))
    #: Âge maximal d'une valorisation d'expert avant péremption (Annexe D exige une
    #: expertise DATÉE pour les titres de capital). Au-delà, on retombe au pair et on
    #: le dit — une expertise de 2023 n'est pas une valeur de 2026.
    expert_valuation_max_age_months = models.PositiveIntegerField(default=12)

    #: Écart (en %) au-delà duquel un rapport de performance déclenche une observation
    #: de risque. Était codé en dur à 10 dans `submit_performance_report` — un seuil
    #: métier dans le code est un seuil que le comité ne peut pas corriger (principe 8).
    performance_deviation_alert_percent = models.DecimalField(
        max_digits=5, decimal_places=2, default=Decimal("10.00"))

    #: Politique appliquée aux offres qui ne précisent rien (voir `Offer.Oversubscription`).
    default_oversubscription_policy = models.CharField(max_length=10, default="QUEUE")
    #: Part de l'objectif en deçà de laquelle la levée échoue à l'échéance (min-funding).
    default_min_funding_ratio = models.DecimalField(max_digits=5, decimal_places=4, default=Decimal("0.7000"))

    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Configuration investissement"

    @classmethod
    def active(cls) -> "InvestmentConfig":
        cfg = cls.objects.filter(is_active=True).order_by("-pk").first()
        return cfg or cls(is_active=True)  # instance non persistée = valeurs par défaut

    def __str__(self) -> str:
        return f"InvestmentConfig(actif={self.is_active})"


class Project(models.Model):
    """Un projet d'investissement EST une demande de crédit client.

    Décision du fondateur (juillet 2026) : **1 projet = 1 dossier de crédit**. Le
    dossier (`credits.CreditApplication`) porte l'emprunteur, la filière, la zone, le
    montant demandé et — via le moteur d'analyse — le score. Le projet porte la LEVÉE
    (objectif, offres, souscriptions, cycle P01→P13). Les grandeurs communes ne sont
    donc pas re-saisies ici : elles sont LUES dans le dossier (principe 6, une seule
    source par concept). Les colonnes locales restent le repli des projets non encore
    rattachés — jamais une seconde vérité concurrente d'un dossier rattaché.

    **Anonymat (décision du fondateur, P08).** Pendant la levée (P01→P07), un non-staff
    ne reçoit ni nom, ni contact, ni localisation fine de l'emprunteur : filière, zone
    LARGE, score et nature du risque uniquement. À partir du décaissement (P08),
    l'identité devient lisible pour les SEULS investisseurs dont une souscription a été
    encaissée sur ce projet — jamais pour tout porteur du rôle `invest`. La règle est
    appliquée par le choix du sérialiseur (`investments.serializers`), jamais par un
    `if` d'affichage côté écran.
    """

    class Status(models.TextChoices):
        #: « Analyse initiale » (P02), « Due diligence » (P03) et « Comité
        #: d'investissement » (P04) ont été RETIRÉS (décision du fondateur, août
        #: 2026). Le module ne score plus et ne fait plus approuver par un comité :
        #: le projet publie une pièce jointe, l'investisseur décide en souscrivant.
        #:
        #: Leurs codes ne sont pas réattribués. Un `P02` figure encore au journal
        #: d'audit et dans les transitions déjà enregistrées ; le recycler pour une
        #: autre étape rendrait l'historique faux plutôt que seulement obsolète.
        P01 = "P01", "Prospection"
        P05 = "P05", "Approbation — pièce jointe"
        P06 = "P06", "Levée de fonds"
        P07 = "P07", "Souscription clôturée"
        P08 = "P08", "Décaissement"
        P09 = "P09", "En cours"
        P10 = "P10", "Remboursement"
        P11 = "P11", "Clôturé"
        P12 = "P12", "Défaut"
        P13 = "P13", "Annulé"

    code = models.CharField(max_length=32, unique=True, db_index=True)

    #: Dossier de crédit financé par ce projet — **un-à-un**.
    #:
    #: `OneToOneField` et non `ForeignKey` : la contrainte d'unicité est le fond de la
    #: décision (« deux projets ne peuvent pas financer le même dossier »), pas une
    #: précaution. Elle vit en base, pas dans une vue.
    #: `null=True` le temps de la reprise : les projets créés avant cette décision
    #: n'ont pas de dossier, et on ne leur en invente pas.
    #: `PROTECT` : un dossier de crédit qui a été financé par des investisseurs ne
    #: s'efface pas — c'est une pièce probante (principe 3).
    credit_application = models.OneToOneField(
        "credits.CreditApplication", null=True, blank=True, on_delete=models.PROTECT,
        related_name="investment_project",
        help_text="Dossier de crédit instruit par le module crédit. Source unique du "
                  "promoteur, de la filière, de la zone, du montant demandé et du score.",
    )

    title = models.CharField(max_length=200)
    sector = models.CharField(max_length=120, blank=True)
    location = models.CharField(max_length=150, blank=True)
    promoter = models.CharField(max_length=200, blank=True)
    promoter_contact = models.CharField(max_length=150, blank=True)
    issuer = models.CharField(max_length=200, blank=True)
    description = models.TextField(blank=True)
    objectives = models.TextField(blank=True)
    risk_analysis = models.TextField(blank=True)
    fund_allocation = models.JSONField(default=dict, blank=True)
    impact_esg = models.TextField(blank=True)
    image_url = models.CharField(max_length=500, blank=True)

    funding_target = models.DecimalField(max_digits=16, decimal_places=2, default=Decimal("0"))
    #: Σ souscriptions ENCAISSÉES (B10). Une réservation n'est pas de l'argent : elle
    #: n'entre jamais ici (principe 8 du cahier HAZINA, §3 de la mission).
    funded_amount = models.DecimalField(max_digits=16, decimal_places=2, default=Decimal("0"))
    #: Σ décaissée au promoteur (B11), Σ retours encaissés (B12), Σ distribuée (B13).
    disbursed_amount = models.DecimalField(max_digits=16, decimal_places=2, default=Decimal("0"))
    returned_amount = models.DecimalField(max_digits=16, decimal_places=2, default=Decimal("0"))
    distributed_amount = models.DecimalField(max_digits=16, decimal_places=2, default=Decimal("0"))

    #: Conditions posées par le comité en P05 — « approbation conditionnelle » n'a de
    #: sens que si les conditions sont écrites et leur levée tracée.
    committee_conditions = models.TextField(blank=True)
    conditions_cleared_at = models.DateTimeField(null=True, blank=True)
    defaulted_at = models.DateTimeField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)

    status = models.CharField(max_length=3, choices=Status.choices, default=Status.P01)

    #: `risk_category` est un LIBELLÉ éditorial (« Faible », « Agricole — aléa
    #: climatique »), pas une note calculée : il décrit le projet à l'investisseur.
    #: Il survit au retrait du scoring, contrairement à `risk_score` et
    #: `global_score` qui étaient des NOTES produites par un moteur que ce module
    #: n'a plus.
    risk_category = models.CharField(max_length=80, blank=True)

    start_date = models.DateField(null=True, blank=True)
    expected_maturity = models.DateField(null=True, blank=True)

    #: Valorisation d'expert des titres de capital (Annexe D : « valorisation d'expert
    #: DATÉE pour les actions »). Trois champs indissociables — une valeur sans date ni
    #: source n'est pas une expertise, c'est une opinion : la valorisation n'est retenue
    #: que si les trois sont renseignés et que la date n'est pas périmée
    #: (`InvestmentConfig.expert_valuation_max_age_months`). Sinon : au pair, et la
    #: méthode affichée le dit.
    expert_valuation = models.DecimalField(max_digits=16, decimal_places=2, null=True, blank=True)
    expert_valuation_date = models.DateField(null=True, blank=True)
    expert_valuation_source = models.CharField(max_length=200, blank=True)

    manager_sub = models.CharField(max_length=255, blank=True)
    manager_name = models.CharField(max_length=150, blank=True)

    created_by = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.CheckConstraint(condition=Q(funding_target__gte=0) & Q(funded_amount__gte=0),
                                    name="project_amounts_nonneg"),
        ]
        indexes = [models.Index(fields=["status", "sector", "location"])]

    def __str__(self) -> str:
        return f"{self.code} — {self.title}"

    @property
    def progress_percent(self) -> int:
        if not self.funding_target:
            return 0
        return max(0, min(100, round(float(self.funded_amount) / float(self.funding_target) * 100)))

    @property
    def is_investable(self) -> bool:
        return self.status == Project.Status.P06

    # ── Données DÉRIVÉES du dossier de crédit (principe 6) ────────────────────
    #
    # Chaque accesseur lit le dossier quand il existe, et retombe sur la colonne
    # locale sinon. Aucun ne recopie : la colonne locale n'est jamais réécrite
    # depuis le dossier après le rattachement, pour qu'un auditeur voie toujours
    # quelle valeur venait d'où.

    @property
    def borrower_name(self) -> str:
        """Nom de l'emprunteur — l'identité protégée jusqu'à P08."""
        app = self.credit_application
        if app is None:
            return self.promoter
        client = app.client
        return client.company_name or client.full_name or client.sub

    @property
    def borrower_contact(self) -> str:
        app = self.credit_application
        if app is None:
            return self.promoter_contact
        return app.client.phone or app.client.email

    @property
    def value_chain_label(self) -> str:
        """Filière — servie à tous, y compris pendant l'anonymat : elle ne désigne
        personne."""
        app = self.credit_application
        if app is None or app.value_chain_id is None:
            return self.sector
        return app.value_chain.label

    @property
    def fine_location(self) -> str:
        """Localisation FINE (ville/commune de l'agence d'instruction) — identité."""
        app = self.credit_application
        if app is None or app.agency_id is None:
            return self.location
        return app.agency.city or self.location

    @property
    def broad_zone(self) -> str:
        """Zone LARGE (province) — la seule géographie servie pendant l'anonymat.

        Sans dossier rattaché, elle est INDÉTERMINABLE : `Project.location` est un
        texte libre dont personne ne connaît la granularité (« Kongo-Central » est une
        province, « Kimbanseke » une commune). On retourne une chaîne vide et on le
        DIT dans la réponse plutôt que de servir une localisation dont on ignore si
        elle désigne l'emprunteur. C'est exactement ce que le rattachement au dossier
        vient réparer.
        """
        app = self.credit_application
        if app is not None and app.agency_id:
            return app.agency.province or ""
        return ""

    @property
    def requested_amount(self):
        """Montant demandé — celui du dossier de crédit quand il existe."""
        app = self.credit_application
        if app is None or app.amount_requested is None:
            return self.funding_target
        return app.amount_requested


class Offer(models.Model):
    class TypeOfTitle(models.TextChoices):
        OBLIGATION = "OBLIGATION", "Obligation"
        ACTION = "ACTION", "Action"
        PART_SOCIALE = "PART_SOCIALE", "Part sociale"

    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Brouillon"
        PREPARATION = "PREPARATION", "Préparation"
        OUVERT = "OUVERT", "Ouvert"
        SUSPENDU = "SUSPENDU", "Suspendu"
        CLOTURE = "CLOTURE", "Clôturé"

    class Frequency(models.TextChoices):
        MONTHLY = "monthly", "Mensuel"
        QUARTERLY = "quarterly", "Trimestriel"
        ANNUAL = "annual", "Annuel"
        BULLET = "bullet", "In fine (à terme)"

    class Oversubscription(models.TextChoices):
        """Traitement des souscriptions au-delà de l'objectif — paramétrable par offre.

        QUEUE   : premier arrivé premier servi ; le surplus part en liste d'attente et
                  n'est servi que si une réservation antérieure tombe.
        PRORATA : toutes les réservations sont acceptées ; à la clôture, chacune est
                  réduite au prorata et le surplus n'est jamais encaissé.
        REJECT  : la réservation qui dépasse l'objectif est refusée sur-le-champ.
        """

        QUEUE = "QUEUE", "File d'attente (premier arrivé)"
        PRORATA = "PRORATA", "Prorata à la clôture"
        REJECT = "REJECT", "Refus au-delà de l'objectif"

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="offers")
    code = models.CharField(max_length=32, unique=True, db_index=True)
    type_of_title = models.CharField(max_length=16, choices=TypeOfTitle.choices, default=TypeOfTitle.OBLIGATION)
    coupon_rate = models.DecimalField(max_digits=6, decimal_places=3, default=Decimal("0"))
    payment_frequency = models.CharField(max_length=12, choices=Frequency.choices, default=Frequency.QUARTERLY)
    maturity_months = models.IntegerField(default=24)
    min_ticket = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0"))
    bond_unit_value = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("100"))
    min_bonds = models.IntegerField(default=1)
    max_bonds = models.IntegerField(default=0)
    available_bonds = models.IntegerField(default=0)
    funding_goal = models.DecimalField(max_digits=16, decimal_places=2, default=Decimal("0"))
    #: RÉSERVÉ ≠ ENCAISSÉ. `reserved_amount` = engagements pris (souscriptions vivantes,
    #: hors liste d'attente) ; `funded_amount` = argent réellement reçu (B10).
    reserved_amount = models.DecimalField(max_digits=16, decimal_places=2, default=Decimal("0"))
    funded_amount = models.DecimalField(max_digits=16, decimal_places=2, default=Decimal("0"))
    #: Seuil de min-funding : en deçà, la levée échoue à l'échéance et les souscripteurs
    #: sont remboursés (P13). 0 = pas de plancher (dérivé de la config à la création).
    min_funding_amount = models.DecimalField(max_digits=16, decimal_places=2, default=Decimal("0"))
    subscription_deadline = models.DateField(null=True, blank=True)
    oversubscription_policy = models.CharField(
        max_length=10, choices=Oversubscription.choices, default=Oversubscription.QUEUE,
    )
    closed_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.OUVERT)
    collateral_value = models.DecimalField(max_digits=16, decimal_places=2, default=Decimal("0"))
    loan_to_value = models.DecimalField(max_digits=6, decimal_places=3, default=Decimal("0"))
    guarantees = models.JSONField(default=list, blank=True)
    conversion_fee = models.DecimalField(max_digits=6, decimal_places=3, default=Decimal("0"))
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.CheckConstraint(condition=Q(available_bonds__gte=0), name="offer_available_bonds_nonneg"),
        ]
        indexes = [models.Index(fields=["project", "status"])]

    def __str__(self) -> str:
        return f"{self.code} ({self.project.code})"


class Collateral(models.Model):
    offer = models.OneToOneField(Offer, on_delete=models.CASCADE, related_name="collateral")
    debt_type = models.CharField(max_length=80, blank=True)
    guarantees = models.JSONField(default=list, blank=True)
    collateral_value = models.DecimalField(max_digits=16, decimal_places=2, default=Decimal("0"))
    loan_to_value = models.DecimalField(max_digits=6, decimal_places=3, default=Decimal("0"))


class Investor(models.Model):
    class InvestorType(models.TextChoices):
        INDIVIDUAL = "INDIVIDUAL", "Individuel"
        INSTITUTIONAL = "INSTITUTIONAL", "Institutionnel"
        CORPORATE = "CORPORATE", "Corporate"

    class KycStatus(models.TextChoices):
        VALIDE = "VALIDE", "Validé"
        EN_ATTENTE = "EN_ATTENTE", "En attente"
        REJETE = "REJETE", "Rejeté"
        EXPIRE = "EXPIRE", "Expiré"

    class RiskProfile(models.TextChoices):
        CONSERVATIVE = "CONSERVATIVE", "Conservateur"
        MODERATE = "MODERATE", "Modéré"
        AGGRESSIVE = "AGGRESSIVE", "Agressif"

    class Status(models.TextChoices):
        ACTIVE = "ACTIVE", "Actif"
        SUSPENDED = "SUSPENDED", "Suspendu"

    user = models.OneToOneField("accounts.FintechUser", on_delete=models.CASCADE, related_name="investor_profile")
    investor_type = models.CharField(max_length=16, choices=InvestorType.choices, default=InvestorType.INDIVIDUAL)
    kyc_status = models.CharField(max_length=12, choices=KycStatus.choices, default=KycStatus.EN_ATTENTE)
    risk_profile = models.CharField(max_length=14, choices=RiskProfile.choices, default=RiskProfile.MODERATE)
    assigned_manager_sub = models.CharField(max_length=255, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.ACTIVE)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return f"Investor({self.user_id})"


class SubPortfolio(models.Model):
    investor = models.ForeignKey(Investor, on_delete=models.CASCADE, related_name="sub_portfolios")
    name = models.CharField(max_length=150)
    description = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return f"{self.name} ({self.investor_id})"


class Subscription(models.Model):
    """Une souscription RÉSERVE, elle n'encaisse pas.

    `RESERVED` = engagement pris, aucun franc n'a bougé. `SETTLED` = argent reçu et
    comptabilisé (B10) — c'est le seul état qui alimente `funded_amount`, qui autorise
    le décaissement et qui compte dans le XIRR. `WAITLISTED` = au-delà de l'objectif,
    en file d'attente (politique QUEUE). `REFUNDED` = souscription encaissée puis
    remboursée (min-funding non atteint ou annulation P13, contrepassation de B10).
    """

    class Status(models.TextChoices):
        RESERVED = "RESERVED", "Réservée (non encaissée)"
        WAITLISTED = "WAITLISTED", "Liste d'attente"
        SETTLED = "SETTLED", "Encaissée"
        PENDING = "PENDING", "En attente (hérité)"
        ACTIVE = "ACTIVE", "Actif"
        REPAYMENT = "REPAYMENT", "Remboursement"
        COMPLETED = "COMPLETED", "Terminé"
        DEFAULTED = "DEFAULTED", "Défaut"
        REFUNDED = "REFUNDED", "Remboursée"
        CANCELLED = "CANCELLED", "Annulé"

    #: États dans lesquels la souscription pèse sur l'objectif de l'offre.
    LIVE_STATUSES = ("RESERVED", "SETTLED", "ACTIVE", "REPAYMENT", "COMPLETED", "DEFAULTED")
    #: États dans lesquels l'argent a réellement été reçu.
    FUNDED_STATUSES = ("SETTLED", "ACTIVE", "REPAYMENT", "COMPLETED", "DEFAULTED")

    class PaymentStatus(models.TextChoices):
        PAID = "PAID", "Payé"
        UNPAID = "UNPAID", "Impayé"
        OVERDUE = "OVERDUE", "En retard"

    investor = models.ForeignKey(Investor, on_delete=models.CASCADE, related_name="subscriptions")
    offer = models.ForeignKey(Offer, on_delete=models.PROTECT, related_name="subscriptions")
    sub_portfolio = models.ForeignKey(SubPortfolio, null=True, blank=True, on_delete=models.SET_NULL,
                                       related_name="subscriptions")
    #: Montant RÉSERVÉ. Il ne bouge pas : la réduction prorata s'inscrit dans
    #: `allocated_amount`, et l'encaissement dans `settled_amount` — pour qu'un
    #: auditeur voie ce qui a été promis ET ce qui a été servi.
    amount = models.DecimalField(max_digits=16, decimal_places=2)
    allocated_amount = models.DecimalField(max_digits=16, decimal_places=2, default=Decimal("0"))
    settled_amount = models.DecimalField(max_digits=16, decimal_places=2, default=Decimal("0"))
    refunded_amount = models.DecimalField(max_digits=16, decimal_places=2, default=Decimal("0"))
    bonds = models.IntegerField(default=0)
    #: Rang dans la file (politique QUEUE) — 0 pour les souscriptions servies.
    queue_rank = models.PositiveIntegerField(default=0)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.RESERVED)
    payment_status = models.CharField(max_length=10, choices=PaymentStatus.choices, default=PaymentStatus.UNPAID)
    coupon_rate_snapshot = models.DecimalField(max_digits=6, decimal_places=3, default=Decimal("0"))
    subscription_date = models.DateField(auto_now_add=True)
    reserved_at = models.DateTimeField(null=True, blank=True)
    #: Horodatage de l'encaissement : c'est LA date de flux du XIRR côté investisseur.
    settled_at = models.DateTimeField(null=True, blank=True)
    refunded_at = models.DateTimeField(null=True, blank=True)
    next_payment_date = models.DateField(null=True, blank=True)
    total_received = models.DecimalField(max_digits=16, decimal_places=2, default=Decimal("0"))
    created_by = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.CheckConstraint(condition=Q(amount__gte=0), name="subscription_amount_nonneg"),
            models.CheckConstraint(
                condition=Q(settled_amount__gte=0) & Q(allocated_amount__gte=0) & Q(refunded_amount__gte=0),
                name="subscription_lifecycle_amounts_nonneg",
            ),
        ]
        indexes = [models.Index(fields=["investor", "status"]), models.Index(fields=["offer"])]

    @property
    def is_funded(self) -> bool:
        return self.status in Subscription.FUNDED_STATUSES

    def __str__(self) -> str:
        return f"Sub({self.investor_id}->{self.offer_id}) {self.amount}"


class Movement(models.Model):
    class Type(models.TextChoices):
        DEPOSIT = "DEPOSIT", "Dépôt"
        #: Réservation — trace applicative, AUCUN mouvement d'argent (pas d'écriture).
        SUBSCRIPTION = "SUBSCRIPTION", "Souscription (réservation)"
        #: Encaissement de la souscription — B10.
        SETTLEMENT = "SETTLEMENT", "Encaissement souscription"
        #: Remboursement d'une souscription encaissée — contrepassation de B10.
        REFUND = "REFUND", "Remboursement souscription"
        #: Décaissement vers le projet — B11.
        DISBURSEMENT = "DISBURSEMENT", "Décaissement projet"
        #: Retour du projet — B12.
        PROJECT_RETURN = "PROJECT_RETURN", "Encaissement retour projet"
        #: Distribution aux investisseurs au prorata — B13.
        DISTRIBUTION = "DISTRIBUTION", "Distribution investisseurs"
        COUPON_REPAYMENT = "COUPON_REPAYMENT", "Remboursement coupon"
        CAPITAL_REPAYMENT = "CAPITAL_REPAYMENT", "Remboursement capital"
        WITHDRAWAL = "WITHDRAWAL", "Retrait"
        FEES = "FEES", "Frais"

    type = models.CharField(max_length=20, choices=Type.choices)
    investor = models.ForeignKey(Investor, null=True, blank=True, on_delete=models.SET_NULL, related_name="movements")
    project = models.ForeignKey(Project, null=True, blank=True, on_delete=models.SET_NULL, related_name="movements")
    subscription = models.ForeignKey(Subscription, null=True, blank=True, on_delete=models.SET_NULL,
                                      related_name="movements")
    geographic_zone = models.CharField(max_length=120, blank=True)
    assigned_manager_sub = models.CharField(max_length=255, blank=True)
    amount = models.DecimalField(max_digits=16, decimal_places=2)
    currency = models.CharField(max_length=3, default="USD")
    status = models.CharField(max_length=20, choices=FlowStatus.choices, default=FlowStatus.POSTED)
    transaction = models.ForeignKey("transactions.Transaction", null=True, blank=True, on_delete=models.SET_NULL,
                                     related_name="investment_movements")
    date_time = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-date_time"]

    def __str__(self) -> str:
        return f"{self.type} {self.amount} {self.currency}"


class RepaymentSchedule(models.Model):
    """Échéance de RETOUR d'une offre — ce que le projet financé doit rendre.

    Produite par `investments/echeancier_retour.py` au décaissement (B11), lue par
    la garde P09→P10, par « prochain paiement » (Annexe D) et par la ventilation
    capital / rendement de chaque encaissement (B12).

    **La nature d'une ligne est unique** : une échéance rend du capital (retour au
    cantonnement 419-OFF) OU du rendement (produit 719), jamais les deux. C'est
    cette unicité qui rend un encaissement ventilable sans rien deviner — une
    offre in fine produit donc deux lignes à la même date plutôt qu'une ligne mixte.
    """

    class Kind(models.TextChoices):
        COUPON = "COUPON", "Coupon"
        CAPITAL = "CAPITAL", "Capital"
        BULLET = "BULLET", "In fine"

    class Status(models.TextChoices):
        PENDING = "PENDING", "En attente"
        PAID = "PAID", "Payé"
        OVERDUE = "OVERDUE", "En retard"
        CANCELLED = "CANCELLED", "Annulé"

    offer = models.ForeignKey(Offer, on_delete=models.CASCADE, related_name="schedules")
    due_date = models.DateField()
    amount_due = models.DecimalField(max_digits=16, decimal_places=2)
    #: Cumul IMPUTÉ sur cette échéance par les encaissements de retour. Un retour
    #: partiel existe (le promoteur paie ce qu'il peut) : sans ce champ, deux
    #: encaissements successifs imputeraient deux fois la même échéance et la
    #: ventilation comptable serait fausse dès le second.
    amount_paid = models.DecimalField(max_digits=16, decimal_places=2, default=Decimal("0"))
    kind = models.CharField(max_length=10, choices=Kind.choices, default=Kind.COUPON)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    paid_movement = models.ForeignKey(Movement, null=True, blank=True, on_delete=models.SET_NULL,
                                       related_name="schedule_entries")

    class Meta:
        ordering = ["due_date"]
        constraints = [
            models.CheckConstraint(condition=Q(amount_paid__gte=0),
                                    name="repayment_schedule_amount_paid_nonneg"),
        ]

    @property
    def outstanding(self) -> Decimal:
        """Reste dû sur l'échéance — jamais négatif."""
        reste = Decimal(self.amount_due) - Decimal(self.amount_paid)
        return reste if reste > Decimal("0") else Decimal("0.00")

    def __str__(self) -> str:
        return f"{self.offer.code} {self.kind} {self.due_date}"


class ProjectTransition(models.Model):
    """Journal append-only des transitions P01→P13 — acteur, horodatage, motif.

    Double du journal d'audit (`audit.services.record`, seul journal transverse) : ici
    la trace est interrogeable PAR PROJET sans traverser tout l'audit, ce dont a besoin
    l'écran « historique du dossier ». Jamais d'UPDATE ni de DELETE (principe 3) : on
    n'annule pas une transition, on en pose une nouvelle.
    """

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="transitions")
    from_status = models.CharField(max_length=3)
    to_status = models.CharField(max_length=3)
    actor_sub = models.CharField(max_length=255, blank=True)
    actor_role = models.CharField(max_length=64, blank=True)
    #: Motif obligatoire — une transition sans motif n'est pas reconstituable.
    reason = models.TextField()
    details = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["created_at", "id"]
        indexes = [models.Index(fields=["project", "created_at"])]

    def __str__(self) -> str:
        return f"{self.project_id}: {self.from_status}→{self.to_status}"


class InvestmentEvent(models.Model):
    """Événement métier append-only — le SEUL contrat entre `investments` et la
    comptabilité (`accounting`, construit par un autre agent).

    Ce module ne passe aucune écriture : il déclare que quelque chose s'est produit
    (encaissement d'une souscription, décaissement d'un projet, retour, distribution,
    remboursement) avec son montant, sa devise, son cantonnement d'offre et sa date.
    Le moteur d'écritures consomme la file (`consumed_at` / `journal_reference`) et
    applique le catalogue B10→B13. Aucun consommateur n'est requis pour que le module
    fonctionne : l'événement est produit, qu'il soit lu ou non.
    """

    class Type(models.TextChoices):
        #: B10 — encaissement souscription : 501/511 → 419-OFF.
        SUBSCRIPTION_SETTLED = "SUBSCRIPTION_SETTLED", "Encaissement souscription (B10)"
        #: Contrepassation B10 — remboursement d'une souscription encaissée.
        SUBSCRIPTION_REFUNDED = "SUBSCRIPTION_REFUNDED", "Remboursement souscription (B10 contrepassé)"
        #: B11 — décaissement projet : 419-OFF → 501/511.
        PROJECT_DISBURSED = "PROJECT_DISBURSED", "Décaissement projet (B11)"
        #: B12 — encaissement retour projet : 501/511 → 719 + 419-OFF.
        PROJECT_RETURN_RECEIVED = "PROJECT_RETURN_RECEIVED", "Encaissement retour projet (B12)"
        #: B13 — distribution investisseur au prorata : 419-OFF → 501/511.
        DISTRIBUTION_PAID = "DISTRIBUTION_PAID", "Distribution investisseur (B13)"
        #: Sans écriture — signale à la compta qu'une provision est à constituer.
        PROJECT_DEFAULTED = "PROJECT_DEFAULTED", "Défaut projet (provision à constituer)"

    event_type = models.CharField(max_length=32, choices=Type.choices, db_index=True)
    project = models.ForeignKey(Project, null=True, blank=True, on_delete=models.PROTECT,
                                 related_name="accounting_events")
    offer = models.ForeignKey(Offer, null=True, blank=True, on_delete=models.PROTECT,
                               related_name="accounting_events")
    subscription = models.ForeignKey(Subscription, null=True, blank=True, on_delete=models.PROTECT,
                                      related_name="accounting_events")
    investor = models.ForeignKey(Investor, null=True, blank=True, on_delete=models.PROTECT,
                                  related_name="accounting_events")
    amount = models.DecimalField(max_digits=16, decimal_places=2, default=Decimal("0"))
    currency = models.CharField(max_length=3, default="USD")
    #: Sous-compte de cantonnement attendu par la compta (419-OFF-xxxx, Annexe A).
    segregation_account = models.CharField(max_length=32, blank=True)
    occurred_at = models.DateTimeField(db_index=True)
    actor_sub = models.CharField(max_length=255, blank=True)
    payload = models.JSONField(default=dict, blank=True)
    #: Renseignés par le consommateur comptable — jamais par ce module.
    consumed_at = models.DateTimeField(null=True, blank=True)
    journal_reference = models.CharField(max_length=64, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["occurred_at", "id"]
        indexes = [
            models.Index(fields=["event_type", "occurred_at"]),
            models.Index(fields=["consumed_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.event_type} {self.amount} {self.currency}"


class Distribution(models.Model):
    """Distribution au prorata des souscriptions ENCAISSÉES d'une offre (B13).

    Une distribution ne peut jamais excéder ce qui a été effectivement encaissé du
    projet (B12) et non encore distribué : « pas de distribution sans encaissement ».
    """

    class Kind(models.TextChoices):
        COUPON = "COUPON", "Coupon / rendement"
        CAPITAL = "CAPITAL", "Remboursement de capital"
        REFUND = "REFUND", "Remboursement de souscription"

    offer = models.ForeignKey(Offer, on_delete=models.PROTECT, related_name="distributions")
    kind = models.CharField(max_length=10, choices=Kind.choices, default=Kind.COUPON)
    total_amount = models.DecimalField(max_digits=16, decimal_places=2)
    currency = models.CharField(max_length=3, default="USD")
    value_date = models.DateField()
    executed_by = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-value_date", "-id"]

    def __str__(self) -> str:
        return f"Distribution({self.offer_id}) {self.kind} {self.total_amount}"


class DistributionLine(models.Model):
    """Part d'un investisseur dans une distribution — la quote-part se calcule sur les
    montants ENCAISSÉS, jamais sur les montants réservés."""

    distribution = models.ForeignKey(Distribution, on_delete=models.CASCADE, related_name="lines")
    subscription = models.ForeignKey(Subscription, on_delete=models.PROTECT, related_name="distribution_lines")
    investor = models.ForeignKey(Investor, on_delete=models.PROTECT, related_name="distribution_lines")
    share = models.DecimalField(max_digits=9, decimal_places=8, default=Decimal("0"))
    amount = models.DecimalField(max_digits=16, decimal_places=2)

    class Meta:
        ordering = ["id"]
        constraints = [
            models.UniqueConstraint(fields=["distribution", "subscription"],
                                     name="unique_distribution_line_per_subscription"),
        ]


class AnalystObservation(models.Model):
    class Category(models.TextChoices):
        TECHNICAL = "TECHNICAL", "Technique"
        FINANCIAL = "FINANCIAL", "Financier"
        MARKET = "MARKET", "Marché"
        RISK = "RISK", "Risque"
        ENVIRONMENTAL = "ENVIRONMENTAL", "Environnemental"
        SOCIAL = "SOCIAL", "Social"

    class RiskFlag(models.TextChoices):
        LOW = "LOW", "Faible"
        MEDIUM = "MEDIUM", "Moyen"
        HIGH = "HIGH", "Élevé"

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="observations")
    analyst_name = models.CharField(max_length=150, blank=True)
    category = models.CharField(max_length=16, choices=Category.choices, default=Category.RISK)
    risk_flag = models.CharField(max_length=10, choices=RiskFlag.choices, default=RiskFlag.LOW)
    observation = models.TextField(blank=True)
    recommendation = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.project.code} [{self.category}] {self.risk_flag}"


class ProjectQuestion(models.Model):
    class Status(models.TextChoices):
        ANSWERED = "ANSWERED", "Répondu"
        PENDING = "PENDING", "En attente"

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="questions")
    investor = models.ForeignKey(Investor, on_delete=models.CASCADE, related_name="questions")
    question = models.TextField()
    question_date = models.DateTimeField(auto_now_add=True)
    answer = models.TextField(blank=True)
    answer_date = models.DateTimeField(null=True, blank=True)
    answered_by = models.CharField(max_length=255, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)

    class Meta:
        ordering = ["-question_date"]

    def __str__(self) -> str:
        return f"Q({self.project.code}) {self.status}"


class PerformanceReport(models.Model):
    class ValidationStatus(models.TextChoices):
        VALIDATED = "VALIDATED", "Validé"
        PENDING = "PENDING", "En attente"

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="performance_reports")
    reporting_period = models.CharField(max_length=40, blank=True)
    submission_date = models.DateTimeField(auto_now_add=True)
    # Revenus et coûts : des MONTANTS. `DecimalField` et non `FloatField` (principe 4)
    # — un binaire flottant ne représente pas 0,10 exactement, et ces montants nourrissent
    # les trois écarts qui déclenchent une observation de risque sur le dossier.
    actual_revenue = models.DecimalField(max_digits=16, decimal_places=2, default=Decimal("0"))
    forecast_revenue = models.DecimalField(max_digits=16, decimal_places=2, default=Decimal("0"))
    actual_costs = models.DecimalField(max_digits=16, decimal_places=2, default=Decimal("0"))
    forecast_costs = models.DecimalField(max_digits=16, decimal_places=2, default=Decimal("0"))
    #: Quantités produites — trois décimales (tonnes, litres…), pas deux : ce ne sont
    #: pas des montants, mais elles se comparent au centième d'unité près.
    actual_production = models.DecimalField(max_digits=16, decimal_places=3, default=Decimal("0"))
    forecast_production = models.DecimalField(max_digits=16, decimal_places=3, default=Decimal("0"))
    #: Écart de REVENU (réalisé − prévu) / prévu × 100. Le nom est générique pour des
    #: raisons historiques : il est consommé tel quel par le front. Ses deux frères
    #: ci-dessous lèvent l'ambiguïté plutôt que de le renommer sous les pieds d'un
    #: écran livré. Quantizés à 0,01 par `services._deviation_percent`, et STOCKÉS
    #: comme tels : un écart figé au rapport ne se relit pas à 19,999999999999996 %.
    deviation_percent = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0"))
    #: Écart de COÛTS. Attention au sens : un écart POSITIF est défavorable (les coûts
    #: dépassent la prévision), là où un écart de revenu positif est favorable. Deux
    #: grandeurs de même forme et de sens opposé — d'où `unfavorable_deviations`.
    cost_deviation_percent = models.DecimalField(max_digits=12, decimal_places=2,
                                                  default=Decimal("0"))
    #: Écart de PRODUCTION (positif = favorable, comme le revenu).
    production_deviation_percent = models.DecimalField(max_digits=12, decimal_places=2,
                                                        default=Decimal("0"))
    deviation_comments = models.TextField(blank=True)
    validation_status = models.CharField(max_length=10, choices=ValidationStatus.choices,
                                          default=ValidationStatus.PENDING)
    validated_by = models.CharField(max_length=255, blank=True)
    validation_date = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-submission_date"]

    def __str__(self) -> str:
        return f"PerfReport({self.project.code}) {self.reporting_period}"


class PerformanceReportFile(models.Model):
    report = models.ForeignKey(PerformanceReport, on_delete=models.CASCADE, related_name="files")
    file = models.FileField(upload_to="performance_reports/")
    uploaded_at = models.DateTimeField(auto_now_add=True)


class ObligationPosition(models.Model):
    """Position obligataire d'un investisseur — « du cash converti en obligation ».

    Une position NAÎT D'UN DÉBIT RÉEL de portefeuille et d'un encaissement de
    souscription (B10) : elle n'est jamais déclarée. Voir `investments/obligations.py`,
    seul chemin de création côté API.

    **Les termes ne s'inventent pas.** `coupon_amount`, `rate` et `term_months`
    portaient les constantes du prototype (250 / 9 % / 24 mois) en valeurs par défaut :
    toute position créée sans les préciser héritait donc de termes que personne n'avait
    décidés. Les défauts sont supprimés — les trois champs sont désormais renseignés
    depuis l'`Offer` qui porte les conditions, et une création sans offre est refusée.
    Ce sont des SNAPSHOTS (même parti pris que `Subscription.coupon_rate_snapshot`) :
    un recalibrage ultérieur de l'offre ne réécrit pas les conditions d'une position
    déjà souscrite.
    """

    class Status(models.TextChoices):
        ACTIF = "ACTIF", "Actif"
        EN_ATTENTE = "EN_ATTENTE", "En attente"
        MATURE = "MATURE", "Maturé"

    investor = models.ForeignKey(Investor, on_delete=models.CASCADE, related_name="obligation_positions")
    #: Offre dont la position TIRE ses termes. Nullable pour les positions
    #: antérieures à cette règle uniquement — le service de création l'exige.
    offer = models.ForeignKey(Offer, null=True, blank=True, on_delete=models.SET_NULL,
                               related_name="obligation_positions")
    #: Souscription encaissée qui a financé la position — le lien vers l'argent.
    #: `PROTECT` : la pièce qui prouve l'encaissement ne s'efface pas.
    subscription = models.OneToOneField(Subscription, null=True, blank=True, on_delete=models.PROTECT,
                                         related_name="obligation_position")
    name = models.CharField(max_length=150)
    coupon_amount = models.DecimalField(max_digits=12, decimal_places=2)
    invested_amount = models.DecimalField(max_digits=16, decimal_places=2)
    rate = models.DecimalField(max_digits=6, decimal_places=3)
    term_months = models.IntegerField()
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.ACTIF)
    date_created = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return f"{self.name} ({self.investor_id})"


class BondWithdrawal(models.Model):
    class Status(models.TextChoices):
        EN_ATTENTE = "EN_ATTENTE", "En attente"
        APPROUVE = "APPROUVE", "Approuvé"
        PAYE = "PAYE", "Payé"

    position = models.ForeignKey(ObligationPosition, on_delete=models.CASCADE, related_name="withdrawals")
    amount = models.DecimalField(max_digits=16, decimal_places=2)
    penalty_rate = models.DecimalField(max_digits=5, decimal_places=3, default=Decimal("0.02"))
    reason = models.CharField(max_length=255, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.EN_ATTENTE)
    date = models.DateTimeField(auto_now_add=True)


class BondConversion(models.Model):
    class Status(models.TextChoices):
        EN_ATTENTE = "EN_ATTENTE", "En attente"
        APPROUVE = "APPROUVE", "Approuvé"
        REJETE = "REJETE", "Rejeté"

    position = models.ForeignKey(ObligationPosition, on_delete=models.CASCADE, related_name="conversions")
    coupons = models.IntegerField(default=0)
    value = models.DecimalField(max_digits=16, decimal_places=2)
    shares = models.IntegerField(default=0)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.EN_ATTENTE)
    date = models.DateTimeField(auto_now_add=True)


class SecondaryMarketListing(models.Model):
    class Status(models.TextChoices):
        OPEN = "OPEN", "Ouvert"
        SOLD = "SOLD", "Vendu"
        CANCELLED = "CANCELLED", "Annulé"

    subscription = models.ForeignKey(Subscription, on_delete=models.CASCADE, related_name="listings")
    ask_price = models.DecimalField(max_digits=16, decimal_places=2)
    fee_rate = models.DecimalField(max_digits=5, decimal_places=3, default=Decimal("0.015"))
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.OPEN)
    buyer = models.ForeignKey(Investor, null=True, blank=True, on_delete=models.SET_NULL, related_name="purchases")
    created_at = models.DateTimeField(auto_now_add=True)


class TargetAllocation(models.Model):
    """Allocation CIBLE d'un sous-portefeuille — append-only et versionnée.

    Une cible d'allocation engage une décision d'investissement : savoir QUI l'a fixée
    et QUAND fait partie de la donnée. On ne la modifie donc pas, on en pose une
    nouvelle version, et l'historique reste lisible (principe 3).

    Les poches reprennent la nomenclature de `services.portfolio_allocation` —
    `bonds`, `cash`, `stocks` — pour que cible et réel se comparent poche à poche
    sans table de correspondance (principe 6).
    """

    POCHES = ("bonds", "cash", "stocks")

    sub_portfolio = models.ForeignKey(SubPortfolio, on_delete=models.CASCADE,
                                       related_name="target_allocations")
    version = models.PositiveIntegerField()
    #: Pourcentages par poche, en points (60 = 60 %). Somme contrôlée à 100 au service.
    weights = models.JSONField(default=dict)
    note = models.CharField(max_length=255, blank=True)
    created_by = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-version"]
        constraints = [
            models.UniqueConstraint(fields=["sub_portfolio", "version"],
                                     name="one_target_allocation_per_version"),
        ]

    def save(self, *args, **kwargs):
        if self.pk is not None:
            raise ValueError(
                "TargetAllocation est append-only : une cible ne se modifie pas, "
                "on en enregistre une nouvelle version."
            )
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError("TargetAllocation est append-only : une cible ne se supprime pas.")

    def __str__(self) -> str:
        return f"{self.sub_portfolio_id} v{self.version} {self.weights}"


class AnalysisAttachment(models.Model):
    """Pièce jointe d'une analyse — le document qui la fonde.

    Une analyse technique ou financière repose sur des pièces (relevé de production,
    plan de trésorerie, devis) : les chiffres saisis sans elles ne sont pas
    vérifiables. Le fichier est rattaché à sa NATURE d'analyse, pas au projet en
    vrac, pour qu'un lecteur sache ce qu'il justifie.

    Modèle à part plutôt qu'un `FileField` sur chaque analyse : une analyse s'appuie
    souvent sur plusieurs pièces, et une pièce ne se remplace pas — on en ajoute une.
    """

    class Kind(models.TextChoices):
        TECHNICAL = "technical", "Analyse technique"
        FINANCIAL = "financial", "Analyse financière"
        #: Pièce déposée à la CRÉATION du projet, avant qu'aucune analyse n'existe.
        #:
        #: Le formulaire de création demandait de trancher « technique » ou
        #: « financier » dès le dépôt. Cette classification n'avait qu'un
        #: consommateur : le critère « Complétude du dossier » de l'ancien moteur
        #: d'investissement, qui comptait la présence d'une pièce de chaque nature.
        #: Ce moteur a été retiré — la note vient désormais du module crédit — et
        #: le tri n'était donc plus lu par personne. Il restait un choix imposé à
        #: l'opérateur au moment où il a le moins d'éléments pour le faire :
        #: un business plan justifie les deux analyses, et aucune des deux
        #: n'existe encore.
        #:
        #: La nature se choisit là où elle a un sens : à l'écran des analyses, où
        #: l'on rattache une pièce à l'analyse qu'elle fonde.
        DOSSIER = "dossier", "Pièce du dossier"

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="analysis_attachments")
    kind = models.CharField(max_length=10, choices=Kind.choices)
    file = models.FileField(upload_to="investments/analyses/")
    original_name = models.CharField(max_length=255, blank=True)
    label = models.CharField(max_length=200, blank=True)
    uploaded_by = models.CharField(max_length=255, blank=True)
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-uploaded_at"]
        indexes = [models.Index(fields=["project", "kind"])]

    def __str__(self) -> str:
        return f"{self.project_id} {self.kind} {self.original_name}"


class RebalancingOrder(models.Model):
    """Ordre de rééquilibrage d'une poche — append-only, exécuté par le chemin normal.

    Rééquilibrer, ce n'est pas déplacer un solde d'une colonne à l'autre : c'est
    souscrire, ou céder. L'ordre enregistré ici est donc une INTENTION datée, reliée
    à la souscription que `funding.reserve` a créée — jamais un second chemin
    monétaire (principe 6 : `reserve` / `settle` restent les seuls).

    Aucun champ de statut : l'état d'exécution se LIT sur la souscription liée. Un
    statut recopié ici divergerait de la souscription au premier encaissement, et un
    auditeur aurait deux vérités pour un même fait.
    """

    class Direction(models.TextChoices):
        RENFORCER = "RENFORCER", "Poche à renforcer"
        ALLEGER = "ALLEGER", "Poche à alléger"

    sub_portfolio = models.ForeignKey(SubPortfolio, on_delete=models.CASCADE,
                                       related_name="rebalancing_orders")
    #: Version de la cible au moment de l'ordre — un ordre s'apprécie contre la
    #: cible qui l'a motivé, pas contre celle qui sera en vigueur plus tard.
    target_version = models.PositiveIntegerField()
    bucket = models.CharField(max_length=16)
    direction = models.CharField(max_length=10, choices=Direction.choices)
    #: Écart mesuré à l'instant de l'ordre (points de pourcentage et montant).
    gap_points = models.DecimalField(max_digits=7, decimal_places=2, default=Decimal("0"))
    gap_amount = models.DecimalField(max_digits=16, decimal_places=2, default=Decimal("0"))
    #: Montant réellement engagé par l'ordre — celui de la souscription créée.
    amount = models.DecimalField(max_digits=16, decimal_places=2, default=Decimal("0"))
    subscription = models.OneToOneField(Subscription, null=True, blank=True,
                                         on_delete=models.PROTECT,
                                         related_name="rebalancing_order")
    reason = models.CharField(max_length=255, blank=True)
    created_by = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [models.Index(fields=["sub_portfolio", "created_at"])]

    def save(self, *args, **kwargs):
        if self.pk is not None:
            raise ValueError(
                "RebalancingOrder est append-only : un ordre passé ne se réécrit pas, "
                "on en enregistre un nouveau."
            )
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError("RebalancingOrder est append-only : un ordre ne se supprime pas.")

    def __str__(self) -> str:
        return f"{self.sub_portfolio_id} {self.direction} {self.bucket} {self.amount}"


# ── Séries de référence (benchmarks) ─────────────────────────────────────────


class BenchmarkSeries(models.Model):
    """Série de référence publiée par un tiers (BCC, INS…) — maker ≠ checker.

    Une comparaison n'a de sens que si la référence est identifiée : sa DEVISE, sa
    FRÉQUENCE, sa SOURCE et sa LICENCE font partie de la donnée. Comparer un
    rendement en USD à un indice en CDF produit un écart qui ne veut rien dire ; le
    module refuse donc de publier un écart sans afficher les deux devises.

    Le référentiel est activé par un checker différent du maker (principe : maker ≠
    checker sur tout référentiel), et il n'existe aucun chemin de saisie libre : les
    points arrivent par la commande d'import (`import_benchmark_series`).
    """

    class Frequency(models.TextChoices):
        DAILY = "DAILY", "Quotidienne"
        MONTHLY = "MONTHLY", "Mensuelle"
        QUARTERLY = "QUARTERLY", "Trimestrielle"
        ANNUAL = "ANNUAL", "Annuelle"

    class Kind(models.TextChoices):
        #: Taux annuel exprimé en fraction (0,09 = 9 %) — comparable à un rendement.
        RATE = "RATE", "Taux annuel"
        #: Niveau d'indice (base 100) — non comparable à un rendement sans variation.
        INDEX = "INDEX", "Niveau d'indice"

    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Brouillon"
        ACTIVE = "ACTIVE", "Active"
        ARCHIVED = "ARCHIVED", "Archivée"

    code = models.CharField(max_length=32, unique=True, db_index=True)
    label = models.CharField(max_length=200)
    currency = models.CharField(max_length=3, default="USD")
    frequency = models.CharField(max_length=10, choices=Frequency.choices,
                                  default=Frequency.MONTHLY)
    kind = models.CharField(max_length=8, choices=Kind.choices, default=Kind.RATE)
    source = models.CharField(max_length=200)
    #: Licence de rediffusion. Une série qu'on n'a pas le droit de publier ne se
    #: publie pas : le champ est obligatoire pour activer.
    licence = models.CharField(max_length=200)
    notes = models.TextField(blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.DRAFT)
    created_by = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    activated_by = models.CharField(max_length=255, blank=True)
    activated_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["code"]
        verbose_name = "Série de référence"
        verbose_name_plural = "Séries de référence"

    def __str__(self) -> str:
        return f"{self.code} — {self.label} [{self.status}]"


class BenchmarkPoint(models.Model):
    """Un point daté d'une série — append-only et versionné par lot d'import.

    Une correction de source ne réécrit pas le point : elle en pose un de version
    supérieure à la même date. La valeur courante d'une date est celle de la version
    la plus élevée, et l'écart entre deux versions reste lisible — c'est la même
    discipline que les révisions de feuille de besoins.
    """

    series = models.ForeignKey(BenchmarkSeries, on_delete=models.CASCADE, related_name="points")
    point_date = models.DateField(db_index=True)
    #: Six décimales : un taux n'est pas un montant, il garde sa précision (principe 4).
    value = models.DecimalField(max_digits=18, decimal_places=6)
    import_version = models.PositiveIntegerField(default=1)
    source_reference = models.CharField(max_length=255, blank=True)
    imported_by = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["point_date", "-import_version"]
        constraints = [
            models.UniqueConstraint(fields=["series", "point_date", "import_version"],
                                     name="one_benchmark_point_per_version"),
        ]
        indexes = [models.Index(fields=["series", "point_date"])]

    def save(self, *args, **kwargs):
        if self.pk is not None:
            raise ValueError(
                "BenchmarkPoint est append-only : une correction de source pose une "
                "nouvelle version du point, elle ne réécrit pas l'ancienne."
            )
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError("BenchmarkPoint est append-only : un point ne se supprime pas.")

    def __str__(self) -> str:
        return f"{self.series_id} {self.point_date} v{self.import_version} = {self.value}"


# ── Référentiel ESG et mesures de projet ─────────────────────────────────────


class EsgCriterion(models.Model):
    """Critère ESG du référentiel — versionné, maker ≠ checker.

    Un critère porte son UNITÉ (tonnes de CO₂e, emplois, hectares…), son SENS
    d'amélioration (une valeur haute est-elle bonne ?) et son POIDS. Sans les trois,
    deux projets ne se comparent pas : on ne moyenne pas des tonnes avec des emplois,
    et « 12 » n'est ni bon ni mauvais tant qu'on ignore dans quel sens ça compte.

    Le référentiel se corrige en posant une NOUVELLE version du critère — l'ancienne
    passe en archivé et reste lisible, parce que les mesures déjà collectées ont été
    prises contre elle.
    """

    class Direction(models.TextChoices):
        #: Une valeur PLUS HAUTE est meilleure (emplois créés, hectares agroécologie).
        HIGHER_IS_BETTER = "UP", "Une valeur plus haute est meilleure"
        #: Une valeur PLUS BASSE est meilleure (émissions, intrants chimiques).
        LOWER_IS_BETTER = "DOWN", "Une valeur plus basse est meilleure"

    class Pillar(models.TextChoices):
        ENVIRONMENT = "E", "Environnement"
        SOCIAL = "S", "Social"
        GOVERNANCE = "G", "Gouvernance"

    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Brouillon"
        ACTIVE = "ACTIVE", "Actif"
        ARCHIVED = "ARCHIVED", "Archivé"

    code = models.CharField(max_length=32, db_index=True)
    version = models.PositiveIntegerField(default=1)
    label = models.CharField(max_length=200)
    pillar = models.CharField(max_length=1, choices=Pillar.choices, default=Pillar.ENVIRONMENT)
    unit = models.CharField(max_length=40)
    direction = models.CharField(max_length=4, choices=Direction.choices,
                                  default=Direction.HIGHER_IS_BETTER)
    #: Poids en points. La somme des critères ACTIFS est contrôlée à 100 au service
    #: d'activation — une pondération qui ne somme pas à 100 n'est pas une pondération.
    weight = models.DecimalField(max_digits=6, decimal_places=2, default=Decimal("0"))
    #: Bornes de plausibilité de la mesure — refus au-delà, jamais d'écrêtage muet.
    min_value = models.DecimalField(max_digits=18, decimal_places=4, null=True, blank=True)
    max_value = models.DecimalField(max_digits=18, decimal_places=4, null=True, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.DRAFT)
    created_by = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    activated_by = models.CharField(max_length=255, blank=True)
    activated_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["pillar", "code", "-version"]
        constraints = [
            models.UniqueConstraint(fields=["code", "version"],
                                     name="one_esg_criterion_per_version"),
            models.UniqueConstraint(fields=["code"], condition=Q(status="ACTIVE"),
                                     name="one_active_esg_criterion_per_code"),
        ]

    def __str__(self) -> str:
        return f"{self.code} v{self.version} — {self.label} [{self.status}]"


class ProjectEsgMeasure(models.Model):
    """Mesure ESG d'un projet — déclarée, puis vérifiée, puis retenue.

    Même cycle que les actifs gageables (`assets.Asset`) et même vocabulaire
    (principe 6) : `value` est DÉCLARÉE, `valeur_retenue` est fixée par le
    vérificateur, et c'est elle — et elle seule — qui entre dans un agrégat. Une
    valeur non vérifiée existe, elle est lisible, elle ne compte pas.

    La déclaration est append-only : ni la valeur déclarée, ni sa date de collecte,
    ni sa pièce ne se réécrivent. Une correction est une NOUVELLE mesure, et l'écart
    entre deux mesures successives est lui-même une donnée.
    """

    class Status(models.TextChoices):
        DECLARE = "declare", "Déclaré"
        VERIFIE = "verifie", "Vérifié"
        REJETE = "rejete", "Rejeté"

    #: Champs figés à la création — la garde de `save()` les protège nommément.
    CHAMPS_FIGES = ("project_id", "criterion_id", "value", "collected_on", "source",
                     "file", "declared_by")

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="esg_measures")
    criterion = models.ForeignKey(EsgCriterion, on_delete=models.PROTECT,
                                   related_name="measures")
    #: Valeur DÉCLARÉE, dans l'unité du critère.
    value = models.DecimalField(max_digits=18, decimal_places=4)
    #: Valeur RETENUE par le vérificateur — seule à entrer dans les agrégats.
    valeur_retenue = models.DecimalField(max_digits=18, decimal_places=4, null=True, blank=True)
    collected_on = models.DateField()
    source = models.CharField(max_length=200)
    file = models.FileField(upload_to="investments/esg/", null=True, blank=True)
    original_name = models.CharField(max_length=255, blank=True)
    status = models.CharField(max_length=8, choices=Status.choices, default=Status.DECLARE)
    declared_by = models.CharField(max_length=255, blank=True)
    verifie_par_sub = models.CharField(max_length=255, blank=True)
    verifie_le = models.DateTimeField(null=True, blank=True)
    motif_rejet = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-collected_on", "-created_at", "-id"]
        indexes = [models.Index(fields=["project", "criterion", "collected_on"])]

    def save(self, *args, **kwargs):
        if self.pk is not None:
            ancienne = ProjectEsgMeasure.objects.filter(pk=self.pk).values(
                *self.CHAMPS_FIGES).first()
            if ancienne:
                for champ, valeur in ancienne.items():
                    if getattr(self, champ) != valeur:
                        raise ValueError(
                            f"ProjectEsgMeasure est append-only sur la déclaration : "
                            f"« {champ} » ne se réécrit pas. Enregistrez une nouvelle mesure."
                        )
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError(
            "ProjectEsgMeasure est append-only : une mesure d'impact ne se supprime pas.")

    def __str__(self) -> str:
        return f"{self.project_id} {self.criterion_id} {self.value} [{self.status}]"


