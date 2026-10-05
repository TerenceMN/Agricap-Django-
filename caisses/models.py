"""Comptes de trésorerie (agences/HQ, Wallets.jsx+Treasury.jsx) et portefeuilles clients
(ClientWallet.jsx) — rigueur stricte : Decimal, `CheckConstraint(balance>=0)`, verrouillage
ordonné + idempotence dans `services.py`."""
from __future__ import annotations

import uuid
from decimal import Decimal

from django.db import models
from django.db.models import F, Q

from common.choices import FlowStatus


def _generate_otp_id() -> str:
    return uuid.uuid4().hex


def _generate_payment_reference() -> str:
    """Notre référence propre, portée dans le corps envoyé au fournisseur.

    Le protocole Makuta n'offre AUCUN jeton d'idempotence et ne met ni horodatage ni nonce
    dans le contenu signé (cf. `common/makuta.py`, écart n°3) : cette référence est la seule
    chose qui permette, chez eux comme chez nous, de dire « c'est le même paiement »."""
    return f"AGC{uuid.uuid4().hex[:24]}"


class TreasuryAccount(models.Model):
    class Kind(models.TextChoices):
        CAISSE = "CAISSE", "Caisse"
        BANQUE = "BANQUE", "Banque"
        MOBILE_MONEY = "MOBILE_MONEY", "Mobile Money"

    class Currency(models.TextChoices):
        CDF = "CDF", "CDF"
        USD = "USD", "USD"

    class RiskLevel(models.TextChoices):
        FAIBLE = "FAIBLE", "Faible"
        MODERE = "MODERE", "Modéré"
        ELEVE = "ELEVE", "Élevé"

    class Status(models.TextChoices):
        ACTIF = "ACTIF", "Actif"
        EN_TRAITEMENT = "EN_TRAITEMENT", "En traitement"
        EN_OBSERVATION = "EN_OBSERVATION", "En observation"
        BLOQUE = "BLOQUE", "Bloqué"
        ARCHIVE = "ARCHIVE", "Archivé"

    code = models.CharField(max_length=32, unique=True, db_index=True)
    name = models.CharField(max_length=150)
    kind = models.CharField(max_length=14, choices=Kind.choices, default=Kind.CAISSE)
    agency = models.ForeignKey("agencies.Agency", null=True, blank=True, on_delete=models.PROTECT,
                                related_name="treasury_accounts")  # null = compte HQ
    # Rattachement à l'intégration API (`kind=MOBILE_MONEY` uniquement) — permet la
    # synchronisation de connectivité via `partners.services.sync_partner` plutôt qu'un
    # compte Mobile Money isolé sans lien avec le disjoncteur/health-check du partenaire.
    partner = models.ForeignKey("partners.Partner", null=True, blank=True, on_delete=models.SET_NULL,
                                 related_name="treasury_accounts")
    currency = models.CharField(max_length=3, choices=Currency.choices, default=Currency.USD)
    balance = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal("0"))
    initial_amount = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal("0"))
    manager_sub = models.CharField(max_length=255, blank=True)
    scope = models.CharField(max_length=120, blank=True)
    risk_level = models.CharField(max_length=10, choices=RiskLevel.choices, default=RiskLevel.FAIBLE)
    status = models.CharField(max_length=14, choices=Status.choices, default=Status.ACTIF)
    # Plafond journalier d'entrées de caisse (billetage physique, `kind=CAISSE` uniquement) —
    # null = pas de plafond configuré. Suivi par rapport à la session de caisse du jour
    # (`CashRegisterSession.cash_in_total`), pas un cumul indépendant.
    daily_ceiling = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    created_by = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.CheckConstraint(condition=Q(balance__gte=0), name="treasury_balance_nonneg"),
        ]
        indexes = [models.Index(fields=["agency", "status"])]

    def __str__(self) -> str:
        return f"{self.code} — {self.name} ({self.currency})"


class CashRegisterSession(models.Model):
    """Séance de caisse journalière (billetage physique, `TreasuryAccount.kind=CAISSE`) —
    ouverture avec comptage initial, clôture avec comptage final comparé au solde système
    (`TreasuryAccount.balance`, déjà tenu à jour en temps réel par chaque mouvement) :
    au-delà de la tolérance, la caisse est gelée (`TreasuryAccount.status=BLOQUE`) plutôt que
    de laisser un écart non expliqué passer inaperçu."""
    class Status(models.TextChoices):
        OPEN = "OPEN", "Ouverte"
        CLOSED = "CLOSED", "Clôturée"
        DISCREPANCY = "DISCREPANCY", "Écart constaté"

    account = models.ForeignKey(TreasuryAccount, on_delete=models.CASCADE, related_name="register_sessions")
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.OPEN)
    opened_by = models.CharField(max_length=255, blank=True)
    opening_count = models.DecimalField(max_digits=18, decimal_places=2)
    opening_balance_expected = models.DecimalField(max_digits=18, decimal_places=2)
    opened_at = models.DateTimeField(auto_now_add=True)
    # Cumul des entrées de caisse (`services.adjust_account(direction="in")`) enregistrées
    # PENDANT que cette session est ouverte — base de la vérification du plafond journalier.
    cash_in_total = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal("0"))
    closed_by = models.CharField(max_length=255, blank=True)
    closing_count = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    closing_balance_expected = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    discrepancy = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-opened_at"]

    def __str__(self) -> str:
        return f"{self.account.code} [{self.status}] {self.opened_at}"


class CaisseConfig(models.Model):
    """Paramètres de gouvernance des caisses modifiables par le comité SANS redéploiement
    (principe 8 : « les règles vivent en base, pas dans le code »). L'enregistrement le plus
    récent fait foi ; aucune ligne = repli codé AVEC warning loggé (voir
    `cash_register.discrepancy_tolerance`) — comportement strictement inchangé tant que rien
    n'est saisi.

    `DecimalField` et non `FloatField` (principe 4) : la tolérance est comparée au centime
    à un écart de billetage physique. En binaire, `float` réintroduirait l'imprécision que
    tout `caisses` bannit (soldes, mouvements, seuils sont en `Decimal` de bout en bout)."""

    #: Tolérance d'écart de caisse (billetage) : au-delà, la clôture gèle le compte
    #: (`cash_register.close_session`). Montant ABSOLU, valeur unique institution — pas de
    #: complication per-compte/per-devise tant qu'aucun besoin ne l'exige (principe 6).
    discrepancy_tolerance = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal("1"))
    updated_by = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"CaisseConfig(discrepancy_tolerance={self.discrepancy_tolerance})"


class ClientWallet(models.Model):
    class Status(models.TextChoices):
        ACTIF = "ACTIF", "Actif"
        BLOQUE = "BLOQUE", "Bloqué"

    user = models.ForeignKey("accounts.FintechUser", on_delete=models.CASCADE, related_name="wallets")
    currency = models.CharField(max_length=3, choices=TreasuryAccount.Currency.choices,
                                 default=TreasuryAccount.Currency.USD)
    balance = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal("0"))
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.ACTIF)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["user", "currency"], name="wallet_user_currency_unique"),
            models.CheckConstraint(condition=Q(balance__gte=0), name="wallet_balance_nonneg"),
        ]

    def __str__(self) -> str:
        return f"{self.user_id} [{self.currency}] {self.balance}"


class FundTransfer(models.Model):
    from_account = models.ForeignKey(TreasuryAccount, on_delete=models.PROTECT, related_name="transfers_out")
    to_account = models.ForeignKey(TreasuryAccount, on_delete=models.PROTECT, related_name="transfers_in")
    amount = models.DecimalField(max_digits=18, decimal_places=2)
    currency = models.CharField(max_length=3, choices=TreasuryAccount.Currency.choices)
    reason = models.CharField(max_length=255, blank=True)
    status = models.CharField(max_length=20, choices=FlowStatus.choices, default=FlowStatus.POSTED)
    idempotency_key = models.CharField(max_length=128, blank=True)
    created_by = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.CheckConstraint(condition=~Q(from_account=F("to_account")), name="transfer_distinct_accounts"),
        ]

    def __str__(self) -> str:
        return f"{self.from_account.code} -> {self.to_account.code} : {self.amount} {self.currency}"


class WalletMovement(models.Model):
    class Kind(models.TextChoices):
        DEPOSIT = "DEPOSIT", "Dépôt"
        WITHDRAW = "WITHDRAW", "Retrait"
        FX_BUY = "FX_BUY", "Achat devise"
        FX_SELL = "FX_SELL", "Vente devise"
        REGULARIZATION = "REGULARIZATION", "Régularisation"
        # Contre-passation d'un mouvement antérieur (ex. décaissement réservé puis refusé
        # par le fournisseur). Distinct de REGULARIZATION, qui est un crédit forcé décidé
        # par un agent : ici personne ne décide, on annule un engagement qui n'a pas eu lieu.
        REVERSAL = "REVERSAL", "Contre-passation"

    wallet = models.ForeignKey(ClientWallet, on_delete=models.CASCADE, related_name="movements")
    kind = models.CharField(max_length=14, choices=Kind.choices)
    amount = models.DecimalField(max_digits=18, decimal_places=2)
    status = models.CharField(max_length=20, choices=FlowStatus.choices, default=FlowStatus.POSTED)
    created_at = models.DateTimeField(auto_now_add=True)

    #: Référence métier — `AG-CD-AG-BUK-01-AIRTEL-000042` (cf. `common.references`).
    #:
    #: `null` autorisé pour les lignes antérieures à cette colonne : la reprise
    #: d'historique se fait par `manage.py backfill_references`, jamais par une
    #: valeur inventée à la migration. Une référence est soit construite depuis
    #: les données de la ligne, soit absente et visible comme telle.
    reference_id = models.CharField(max_length=80, unique=True, null=True, blank=True,
                                     db_index=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.wallet_id} {self.kind} {self.amount}"

    def save(self, *args, **kwargs):
        """Pose la référence à la CRÉATION, une fois la clé primaire connue.

        Le calcul vit ici et non chez les appelants : `WalletMovement` est créé
        depuis neuf endroits (`payments`, `services`, `regularization`,
        `withdrawal_tiers`). Neuf appels à un helper, c'est neuf occasions d'en
        oublier un — et un mouvement sans référence ne se voit que le jour où un
        client la demande.

        `_contexte_reference` est un attribut TRANSITOIRE que le créateur pose
        quand il connaît l'ordre de paiement à l'origine du mouvement. Il est
        nécessaire : l'ordre pointe vers son mouvement APRÈS l'avoir créé, donc
        le lien inverse n'existe pas encore à cet instant.

        La référence n'est jamais RECALCULÉE : une fois posée, elle est figée
        (principe 3). Renommer une agence ne réécrit pas ce qu'un client a noté.
        """
        creation = self._state.adding
        super().save(*args, **kwargs)
        if creation and not self.reference_id:
            from common.references import reference_mouvement
            self.reference_id = reference_mouvement(
                self, ordre=getattr(self, "_contexte_reference", None))
            # `update` et non un second `save()` : on écrit une seule colonne et
            # on n'entre pas dans une récursion de sauvegarde.
            type(self).objects.filter(pk=self.pk).update(reference_id=self.reference_id)


class WithdrawalThreshold(models.Model):
    """Seuils de palier par devise (ClientWallet.jsx retrait) — même principe que
    `transactions.ValidationThreshold`, mais dupliqué (pas de FK partagée) car le retrait
    de portefeuille et la validation de transaction sont deux workflows distincts."""
    currency = models.CharField(max_length=3, choices=TreasuryAccount.Currency.choices, unique=True)
    auto_limit = models.DecimalField(max_digits=18, decimal_places=2)
    manager_limit = models.DecimalField(max_digits=18, decimal_places=2)

    def __str__(self) -> str:
        return f"{self.currency} auto<{self.auto_limit} manager<{self.manager_limit}"


class WithdrawalRequest(models.Model):
    """Retrait au-dessus du palier auto : le solde n'est débité qu'à l'approbation (palier
    manager) ou à l'atteinte du quorum (palier quorum) — jamais à la création de la
    demande, pour ne jamais tenir des fonds "gelés" sans mécanisme de hold dédié.

    Deux jambes possibles au règlement (décision « une seule porte », cf. `caisses/channels`) :
    interne (`channel` vide/`agent` : espèces/agence, aucun fournisseur) ou externe
    (`mobile_money`/`bank`). Un retrait externe débite d'abord le portefeuille — comme un
    retrait interne — PUIS déclenche un décaissement Makuta (`payout_order`) vers la
    `counterparty` : le versement ne part JAMAIS avant l'approbation humaine (P2)."""
    wallet = models.ForeignKey(ClientWallet, on_delete=models.CASCADE, related_name="withdrawal_requests")
    amount = models.DecimalField(max_digits=18, decimal_places=2)
    status = models.CharField(max_length=20, choices=FlowStatus.choices, default=FlowStatus.PENDING_VALIDATION)
    auto_validated = models.BooleanField(default=False)
    #: Canal de versement (`caisses.channels`). Vide/`agent` = interne (aucun fournisseur).
    channel = models.CharField(max_length=14, blank=True)
    #: Destination externe (numéro Mobile Money / compte) quand `channel` est externe —
    #: format non spécifié par la documentation fournisseur, stocké tel que saisi.
    counterparty = models.CharField(max_length=64, blank=True)
    movement = models.ForeignKey(WalletMovement, null=True, blank=True, on_delete=models.SET_NULL,
                                  related_name="withdrawal_request")
    #: Ordre de décaissement Makuta créé au règlement d'un retrait externe (traçabilité de la
    #: jambe fournisseur). Nul pour un retrait interne.
    payout_order = models.ForeignKey("PaymentOrder", null=True, blank=True, on_delete=models.SET_NULL,
                                      related_name="settled_withdrawal")
    idempotency_key = models.CharField(max_length=128, blank=True)
    created_by = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.CheckConstraint(condition=Q(amount__gt=0), name="withdrawal_request_amount_positive"),
        ]

    def __str__(self) -> str:
        return f"WithdrawalRequest({self.wallet_id}, {self.amount}) [{self.status}]"


class WithdrawalOtpChallenge(models.Model):
    id = models.CharField(max_length=36, primary_key=True, default=_generate_otp_id)
    request = models.ForeignKey(WithdrawalRequest, on_delete=models.CASCADE, related_name="otp_challenges")
    approver_sub = models.CharField(max_length=255)
    code_hash = models.CharField(max_length=128)
    channel = models.CharField(max_length=10, choices=[("EMAIL", "Email")], default="EMAIL")
    attempts = models.IntegerField(default=0)
    max_attempts = models.IntegerField(default=5)
    expires_at = models.DateTimeField()
    verified_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return f"OTP {self.id[:8]} request={self.request_id} approver={self.approver_sub}"


class WithdrawalApproval(models.Model):
    class Decision(models.TextChoices):
        APPROVED = "APPROVED", "Approuvé"
        REJECTED = "REJECTED", "Rejeté"

    request = models.ForeignKey(WithdrawalRequest, on_delete=models.CASCADE, related_name="approvals")
    approver_sub = models.CharField(max_length=255)
    approver_role = models.CharField(max_length=40)
    decision = models.CharField(max_length=10, choices=Decision.choices)
    otp_verified_at = models.DateTimeField(null=True, blank=True)
    decided_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["request", "approver_sub"], name="one_decision_per_withdrawal_approver"),
        ]
        ordering = ["decided_at"]

    def __str__(self) -> str:
        return f"request={self.request_id} {self.approver_sub} -> {self.decision}"


class RegularizationThreshold(models.Model):
    """Seuils de palier des ordres de régularisation (Support.jsx « Crédit forcé ») —
    modèle dupliqué de `WithdrawalThreshold` (pas de FK partagée, même principe que les
    autres seuils de l'app) : une régularisation est un crédit forcé sur décision d'un
    agent, un risque de nature différente d'un retrait client, donc configurable
    séparément."""
    currency = models.CharField(max_length=3, choices=TreasuryAccount.Currency.choices, unique=True)
    auto_limit = models.DecimalField(max_digits=18, decimal_places=2)
    manager_limit = models.DecimalField(max_digits=18, decimal_places=2)

    def __str__(self) -> str:
        return f"{self.currency} auto<{self.auto_limit} manager<{self.manager_limit}"


class RegularizationOrder(models.Model):
    """Crédit forcé sur un portefeuille client (ex. dépôt mobile money reçu chez l'opérateur
    mais jamais crédité en DB) — remplace l'action `force_credit` simulée de Support.jsx par
    un vrai `WalletMovement`, gouverné par le même palier auto/manager/quorum + OTP que
    `withdrawal_tiers`."""
    wallet = models.ForeignKey(ClientWallet, on_delete=models.CASCADE, related_name="regularization_orders")
    ticket = models.ForeignKey("support.Ticket", null=True, blank=True, on_delete=models.SET_NULL,
                                related_name="regularization_orders")
    amount = models.DecimalField(max_digits=18, decimal_places=2)
    reason = models.TextField(blank=True)
    status = models.CharField(max_length=20, choices=FlowStatus.choices, default=FlowStatus.PENDING_VALIDATION)
    auto_validated = models.BooleanField(default=False)
    movement = models.ForeignKey(WalletMovement, null=True, blank=True, on_delete=models.SET_NULL,
                                  related_name="regularization_order")
    idempotency_key = models.CharField(max_length=128, blank=True)
    created_by = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.CheckConstraint(condition=Q(amount__gt=0), name="regularization_amount_positive"),
        ]

    def __str__(self) -> str:
        return f"RegularizationOrder({self.wallet_id}, {self.amount}) [{self.status}]"


class RegularizationOtpChallenge(models.Model):
    id = models.CharField(max_length=36, primary_key=True, default=_generate_otp_id)
    order = models.ForeignKey(RegularizationOrder, on_delete=models.CASCADE, related_name="otp_challenges")
    approver_sub = models.CharField(max_length=255)
    code_hash = models.CharField(max_length=128)
    channel = models.CharField(max_length=10, choices=[("EMAIL", "Email")], default="EMAIL")
    attempts = models.IntegerField(default=0)
    max_attempts = models.IntegerField(default=5)
    expires_at = models.DateTimeField()
    verified_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return f"OTP {self.id[:8]} order={self.order_id} approver={self.approver_sub}"


class PaymentOrder(models.Model):
    """Ordre de paiement adressé à la plateforme **Makuta** — le chaînon manquant entre un
    mouvement de portefeuille et un vrai déplacement d'argent.

    Trois choix structurent ce modèle :

    1. **Une nomenclature de statuts propre, distincte de `common.choices.FlowStatus`**
       (principe 6 : une seule nomenclature PAR CONCEPT — ce n'en est pas le même). Le cycle
       de vie d'un ordre chez un fournisseur externe comporte un état que le workflow interne
       n'a pas et ne peut pas avoir : `INDETERMINATE`, « la requête est partie, l'issue est
       INCONNUE ». `FlowStatus` n'offre que `posted`/`rejected` — deux certitudes. Plier
       l'incertitude sur une certitude, c'est exactement l'erreur qui fait payer deux fois.

    2. **Le portefeuille n'est crédité qu'à `CONFIRMED`** (`movement` reste nul avant). Un
       dépôt annoncé n'est pas un dépôt reçu. Un ordre `SENT`, `AWAITING_CONFIRMATION` ou
       `INDETERMINATE` n'a produit aucun `WalletMovement`.

    3. **Le pied de trésorerie n'est PAS posé ici.** `treasury_account` est une traçabilité
       (sur quel compte Mobile Money les fonds atterrissent), pas un compte mouvementé
       automatiquement : l'écriture de contrepartie relève du module trésorerie/comptabilité
       et de son propriétaire. Trou déclaré, pas trou silencieux.
    """

    class Direction(models.TextChoices):
        COLLECTION = "COLLECTION", "Encaissement (client → AGRICAP)"
        PAYOUT = "PAYOUT", "Décaissement (AGRICAP → client)"

    class Status(models.TextChoices):
        PENDING = "PENDING", "Créé — rien n'est parti"
        SENT = "SENT", "Envoyé — aucune réponse encore enregistrée"
        AWAITING_CONFIRMATION = "AWAITING_CONFIRMATION", "Accusé par le fournisseur — issue non connue"
        INDETERMINATE = "INDETERMINATE", "Issue INCONNUE — à réconcilier"
        CONFIRMED = "CONFIRMED", "Confirmé par le fournisseur"
        REFUSED = "REFUSED", "Refusé par le fournisseur"
        CANCELLED = "CANCELLED", "Annulé avant tout envoi"

    #: Statuts terminaux : plus aucune transition, plus aucune écriture monétaire.
    SETTLED_STATUSES = (Status.CONFIRMED, Status.REFUSED, Status.CANCELLED)
    #: Statuts qui appellent une réconciliation manuelle (P2 : outillée, jamais automatique).
    OPEN_STATUSES = (Status.SENT, Status.AWAITING_CONFIRMATION, Status.INDETERMINATE)

    reference = models.CharField(max_length=64, unique=True, db_index=True,
                                 default=_generate_payment_reference)
    wallet = models.ForeignKey(ClientWallet, on_delete=models.PROTECT, related_name="payment_orders")
    treasury_account = models.ForeignKey(TreasuryAccount, null=True, blank=True, on_delete=models.SET_NULL,
                                          related_name="payment_orders")
    direction = models.CharField(max_length=12, choices=Direction.choices)
    #: Nom LOGIQUE de l'opération fournisseur (clé de `settings.MAKUTA["OPERATIONS"]`).
    #: Aucun endpoint Makuta n'est codé en dur : la documentation fournie ne décrit que
    #: l'authentification, pas le catalogue des opérations.
    operation = models.CharField(max_length=64)
    #: Contrepartie chez le fournisseur (numéro Mobile Money, compte marchand…) — format
    #: non spécifié par la documentation, stocké tel que saisi.
    counterparty = models.CharField(max_length=64, blank=True)
    amount = models.DecimalField(max_digits=18, decimal_places=2)
    currency = models.CharField(max_length=3, choices=TreasuryAccount.Currency.choices)
    status = models.CharField(max_length=24, choices=Status.choices, default=Status.PENDING)
    metadata = models.JSONField(default=dict, blank=True)

    #: Ce qui est RÉELLEMENT parti (chemin + corps signé), figé à l'envoi : sans cela, une
    #: réconciliation six mois plus tard compare une réponse à un corps reconstruit.
    request_path = models.CharField(max_length=255, blank=True)
    request_body = models.JSONField(null=True, blank=True)
    provider_reference = models.CharField(max_length=128, blank=True, db_index=True)
    last_response = models.JSONField(null=True, blank=True)
    failure_detail = models.TextField(blank=True)

    #: Mouvement de portefeuille produit par l'ordre. COLLECTION : créé à la confirmation
    #: SEULEMENT. PAYOUT : créé à la CRÉATION (les fonds sont réservés — on n'ordonne pas
    #: un décaissement qu'on ne détient pas), contre-passé par `reversal_movement` en cas
    #: de refus.
    movement = models.ForeignKey(WalletMovement, null=True, blank=True, on_delete=models.SET_NULL,
                                  related_name="payment_order")
    reversal_movement = models.ForeignKey(WalletMovement, null=True, blank=True, on_delete=models.SET_NULL,
                                           related_name="payment_order_reversal")

    idempotency_key = models.CharField(max_length=128, blank=True)
    created_by = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    settled_at = models.DateTimeField(null=True, blank=True)

    #: Référence MÉTIER — `AG-CD-AG-BUK-01-AIRTEL-000042` (cf. `common.references`).
    #:
    #: Distincte de `reference` ci-dessus, et les deux restent : `reference`
    #: (« AGC… ») est la clé TECHNIQUE — idempotence, rapprochement fournisseur,
    #: corrélation des journaux. `reference_id` est celle qu'on lit, qu'on cite et
    #: qu'on recherche. Les fondre obligerait à changer une clé d'idempotence pour
    #: changer un libellé, ce qui rejouerait des paiements.
    reference_id = models.CharField(max_length=80, unique=True, null=True, blank=True,
                                     db_index=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.CheckConstraint(condition=Q(amount__gt=0), name="payment_order_amount_positive"),
        ]
        indexes = [
            models.Index(fields=["status"], name="payment_order_status_idx"),
            models.Index(fields=["status", "created_at"], name="payment_order_status_date_idx"),
        ]

    def save(self, *args, **kwargs):
        """Pose la référence métier à la création — même contrat que `WalletMovement`.

        Ici le contexte est déjà là : un ordre connaît son compte de trésorerie,
        donc son agence et son partenaire, dès sa création. Aucun attribut
        transitoire n'est nécessaire.
        """
        creation = self._state.adding
        super().save(*args, **kwargs)
        if creation and not self.reference_id:
            from common.references import reference_ordre
            self.reference_id = reference_ordre(self)
            type(self).objects.filter(pk=self.pk).update(reference_id=self.reference_id)

    def __str__(self) -> str:
        return f"{self.reference} {self.direction} {self.amount} {self.currency} [{self.status}]"


class PaymentOrderEvent(models.Model):
    """Journal **append-only** d'un ordre de paiement (principe 3).

    `PaymentOrder.status` est un curseur : il dit où l'on en est. Ce journal est la preuve :
    il dit d'où vient chaque changement, qui l'a provoqué et sur quelle réponse fournisseur.
    Toute écriture corrective (réconciliation) y laisse son motif obligatoire — sans quoi
    « l'ordre est passé de INDETERMINATE à CONFIRMED » est une affirmation sans auteur."""

    class Source(models.TextChoices):
        SYSTEM = "SYSTEM", "Système AGRICAP"
        PROVIDER_RESPONSE = "PROVIDER_RESPONSE", "Réponse synchrone du fournisseur"
        RECONCILIATION = "RECONCILIATION", "Réconciliation (relecture de statut)"
        CALLBACK = "CALLBACK", "Rappel entrant du fournisseur"

    class Kind(models.TextChoices):
        CREATED = "CREATED", "Ordre créé"
        SENT = "SENT", "Requête envoyée"
        RESPONSE = "RESPONSE", "Réponse enregistrée"
        TRANSPORT_ERROR = "TRANSPORT_ERROR", "Erreur de transport — issue inconnue"
        CONFIRMED = "CONFIRMED", "Issue confirmée"
        REFUSED = "REFUSED", "Issue refusée"
        UNCLASSIFIED = "UNCLASSIFIED", "Réponse non classable (contrat fournisseur manquant)"
        WALLET_POSTED = "WALLET_POSTED", "Mouvement de portefeuille posté"
        WALLET_REVERSED = "WALLET_REVERSED", "Mouvement de portefeuille contre-passé"
        CANCELLED = "CANCELLED", "Ordre annulé avant envoi"
        CALLBACK_REJECTED = "CALLBACK_REJECTED", "Rappel entrant refusé (non authentifié)"

    order = models.ForeignKey(PaymentOrder, on_delete=models.CASCADE, related_name="events")
    kind = models.CharField(max_length=24, choices=Kind.choices)
    source = models.CharField(max_length=20, choices=Source.choices, default=Source.SYSTEM)
    from_status = models.CharField(max_length=24, blank=True)
    to_status = models.CharField(max_length=24, blank=True)
    actor = models.CharField(max_length=255, blank=True)
    motive = models.TextField(blank=True)
    payload = models.JSONField(default=dict, blank=True)
    at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["at", "id"]
        indexes = [models.Index(fields=["order", "at"], name="payment_event_order_at_idx")]

    def save(self, *args, **kwargs):
        if self.pk and not self._state.adding:
            raise RuntimeError("Journal d'ordre de paiement : append-only, aucune modification.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise RuntimeError("Journal d'ordre de paiement : append-only, aucune suppression.")

    def __str__(self) -> str:
        return f"{self.order_id} {self.kind} ({self.from_status}→{self.to_status})"


class RegularizationApproval(models.Model):
    class Decision(models.TextChoices):
        APPROVED = "APPROVED", "Approuvé"
        REJECTED = "REJECTED", "Rejeté"

    order = models.ForeignKey(RegularizationOrder, on_delete=models.CASCADE, related_name="approvals")
    approver_sub = models.CharField(max_length=255)
    approver_role = models.CharField(max_length=40)
    decision = models.CharField(max_length=10, choices=Decision.choices)
    otp_verified_at = models.DateTimeField(null=True, blank=True)
    decided_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["order", "approver_sub"],
                                     name="one_decision_per_regularization_approver"),
        ]
        ordering = ["decided_at"]

    def __str__(self) -> str:
        return f"order={self.order_id} {self.approver_sub} -> {self.decision}"


# ── Écarts d'arrondi ─────────────────────────────────────────────────────────────────
#
# Décision du fondateur : tout montant du système porte deux décimales, et le
# sous-centime qui naît d'une division est CONSERVÉ dans un compte d'administration
# distinct de tout compte opérationnel, auditable.
#
# Pourquoi un modèle à part et non un `TreasuryAccount` de plus : un compte de trésorerie
# porte un solde à deux décimales (`decimal_places=2`) — il ne PEUT pas héberger ce qu'on
# lui demande de garder. Y loger les résidus les arrondirait à nouveau, et le mécanisme
# perdrait précisément ce qu'il est censé retenir. Le compte d'écarts vit donc à huit
# décimales, et sa séparation d'avec les caisses est structurelle, pas conventionnelle.


class RoundingResidueAccount(models.Model):
    """Compte d'écarts d'arrondi — un seul par devise, jamais mouvementé à la main.

    `balance` est un CUMUL SIGNÉ : positif quand les arrondis ont retenu moins que les
    montants exacts (l'institution conserve la fraction), négatif quand ils ont retenu
    plus (elle l'a avancée). Un compte qui ne pourrait que croître ne serait pas un
    compte d'écarts mais un prélèvement — voir `common/money.py` sur le sens du résidu.
    """

    currency = models.CharField(max_length=3, unique=True, db_index=True)
    #: Huit décimales : la précision de conservation du résidu (`common.money`).
    balance = models.DecimalField(max_digits=24, decimal_places=8, default=Decimal("0"))
    #: Nombre d'écritures reçues — un compteur de contrôle : il doit égaler le nombre de
    #: lignes du journal pour cette devise. Deux façons de compter la même chose, donc
    #: une divergence détectable.
    entry_count = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["currency"]

    def __str__(self) -> str:
        return f"Écarts d'arrondi {self.currency} : {self.balance}"

    @property
    def centimes_entiers(self) -> Decimal:
        """Part du cumul qui atteint le centime — ce qui serait matérialisable.

        Exposé en lecture seule et volontairement NON balayé automatiquement : déverser
        ces centimes dans une caisse serait un mouvement d'argent, donc un acte qui
        relève du maker-checker et d'une écriture comptable, pas d'un effet de bord de
        calcul.
        """
        from common.money import CENTIME
        return (self.balance / CENTIME).to_integral_value(rounding="ROUND_DOWN") * CENTIME


class RoundingResidueEntry(models.Model):
    """Journal APPEND-ONLY des résidus d'arrondi.

    Chaque ligne porte de quoi RECONSTITUER le calcul : le montant exact avant arrondi,
    le montant retenu, et l'opération d'origine. Un auditeur doit pouvoir repartir d'une
    ligne et retrouver le centime, deux ans après, sans avoir à deviner quelle
    conversion l'a produit.
    """

    class Source(models.TextChoices):
        FX_CONVERSION = "FX_CONVERSION", "Conversion de change"
        SAVINGS_INTEREST = "SAVINGS_INTEREST", "Intérêts d'épargne"
        SAVINGS_DEPOSIT = "SAVINGS_DEPOSIT", "Dépôt d'épargne"
        SAVINGS_WITHDRAWAL = "SAVINGS_WITHDRAWAL", "Retrait d'épargne"
        LOAN_SCHEDULE = "LOAN_SCHEDULE", "Échéancier de crédit"
        PAYMENT_ORDER = "PAYMENT_ORDER", "Ordre de paiement"
        OTHER = "OTHER", "Autre"

    account = models.ForeignKey(RoundingResidueAccount, on_delete=models.PROTECT,
                                related_name="entries")
    #: Résidu signé, à huit décimales (`common.money.RESIDU_QUANTUM`).
    amount = models.DecimalField(max_digits=24, decimal_places=8)
    #: Montant exact avant arrondi, et montant effectivement retenu : les deux sont
    #: stockés parce que `exact = retenu + résidu` doit se vérifier ligne à ligne, sans
    #: dépendre d'un recalcul dont les règles auraient pu changer entre-temps.
    exact_amount = models.DecimalField(max_digits=24, decimal_places=8)
    rounded_amount = models.DecimalField(max_digits=18, decimal_places=2)
    source = models.CharField(max_length=20, choices=Source.choices, default=Source.OTHER)
    #: Opération d'origine, même convention que `audit.services.record`.
    entity_type = models.CharField(max_length=60, blank=True)
    entity_id = models.CharField(max_length=80, blank=True)
    actor_sub = models.CharField(max_length=255, blank=True)
    detail = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["account", "-created_at"]),
            models.Index(fields=["source", "-created_at"]),
            models.Index(fields=["entity_type", "entity_id"]),
        ]

    def save(self, *args, **kwargs):
        """Append-only : une ligne écrite ne se réécrit pas (principe 3).

        Le garde est ici, au plus près de la table, et non dans le service : une pièce
        probante protégée seulement par la discipline de ses appelants finit par être
        modifiée par le énième appelant qui ne connaissait pas la règle.
        """
        if self.pk is not None:
            raise ValueError(
                "RoundingResidueEntry est append-only : une écriture d'écart d'arrondi "
                "ne se modifie pas. Enregistrez une écriture inverse."
            )
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError(
            "RoundingResidueEntry est append-only : une écriture d'écart d'arrondi ne "
            "se supprime pas."
        )

    def __str__(self) -> str:
        return f"{self.source} {self.amount:+} {self.account.currency}"
