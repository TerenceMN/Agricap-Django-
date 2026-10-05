"""Socle comptable AGRICAP — partie double BI-MONNAIE native (HAZINA, annexes A/B/E).

Différence STRUCTURANTE avec l'app `ledger` existante : ici la devise est portée par la
LIGNE (`LigneEcriture.devise`), pas par la pièce. C'est ce qui rend représentable une
opération mixte FC/USD — donc le mécanisme 588FX de l'annexe E, impossible à écrire avec un
modèle mono-devise par écriture.

Invariants portés par ce module :
  * Σ débits = Σ crédits **PAR DEVISE** avant persistance (`services.verifier_equilibre`) ;
  * append-only : une pièce `VALIDEE` est immuable, on contrepasse (jamais d'UPDATE/DELETE) ;
  * aucun compte mouvementé n'est supprimable (FK `PROTECT` + garde explicite).

Limite assumée et documentée : l'équilibre par devise est un invariant AGRÉGÉ cross-lignes,
qu'un `CheckConstraint` SQL ne peut pas exprimer. Il est donc verrouillé (a) dans le service
de validation, seul chemin d'écriture, (b) par les gardes d'immuabilité qui empêchent de
déséquilibrer une pièce après coup, et (c) par un contrôle global rejouable
(`services.controler_integrite`) couvert par un test bloquant.
"""
from __future__ import annotations

from decimal import Decimal

from django.db import models
from django.db.models import Q

from common.exceptions import ValidationFailed


class Devise(models.TextChoices):
    """Les deux devises structurelles d'AGRICAP. `FC` est la nomenclature de l'annexe A
    (l'app `fx` historique utilise `CDF` implicitement — divergence signalée au fondateur)."""

    FC = "FC", "Franc congolais"
    USD = "USD", "Dollar américain"


class Nature(models.TextChoices):
    """Sens normal du solde — repris à l'identique de `ledger.ChartAccount.Nature` pour ne
    pas créer un quatrième vocabulaire (principe 6 : une seule nomenclature par concept)."""

    ACTIF = "ACTIF", "Actif"
    PASSIF = "PASSIF", "Passif"
    CHARGE = "CHARGE", "Charge"
    PRODUIT = "PRODUIT", "Produit"


class Journal(models.TextChoices):
    JCR = "JCR", "Journal crédit"
    JEP = "JEP", "Journal épargne"
    JCA = "JCA", "Journal caisse"
    JMM = "JMM", "Journal mobile money"
    JFX = "JFX", "Journal change"
    JIN = "JIN", "Journal investissement"
    JOD = "JOD", "Journal opérations diverses"


class CompteComptable(models.Model):
    """Un compte du plan comptable canonique (annexe A).

    Un compte est MONO-DEVISE quand l'annexe le dédouble (413FC / 413USD) : `racine` porte
    alors le code générique de l'annexe et `code` = racine + devise. Les comptes sans devise
    (`devise` vide) acceptent n'importe quelle devise de ligne — c'est le cas des comptes de
    résultat de change 613FX / 712FX, que l'annexe A laisse sans dédoublement.
    """

    code = models.CharField(max_length=32, unique=True, db_index=True)
    racine = models.CharField(max_length=24, db_index=True)
    intitule = models.CharField(max_length=200)
    classe = models.PositiveSmallIntegerField()
    nature = models.CharField(max_length=8, choices=Nature.choices)
    devise = models.CharField(max_length=3, choices=Devise.choices, blank=True)
    est_transitoire = models.BooleanField(
        default=False,
        help_text="Compte de passage (581, 588FX) dont le solde doit tendre vers zéro.",
    )
    cantonnement = models.CharField(
        max_length=64, blank=True, db_index=True,
        help_text="Référence de l'offre pour les sous-comptes de cantonnement 419-OFF-xxxx.",
    )
    actif = models.BooleanField(default=True)
    parent = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="enfants",
    )
    cree_le = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["code"]
        verbose_name = "compte comptable"
        verbose_name_plural = "comptes comptables"

    def __str__(self) -> str:
        return f"{self.code} — {self.intitule}"

    def accepte_devise(self, devise: str) -> bool:
        return not self.devise or self.devise == devise

    def delete(self, *args, **kwargs):
        """Un compte mouvementé n'est JAMAIS supprimable (la FK `PROTECT` l'interdit déjà au
        niveau base ; on lève ici un message métier explicite plutôt qu'une `ProtectedError`
        opaque). Un compte non mouvementé se désactive plutôt qu'il ne se supprime."""
        if self.lignes.exists():
            raise ValidationFailed(
                f"Le compte {self.code} est mouvementé : suppression interdite. "
                "Désactivez-le (actif=False) — la comptabilité est append-only."
            )
        return super().delete(*args, **kwargs)


class PieceComptable(models.Model):
    """Une opération = une pièce = n lignes indivisibles.

    Cycle de vie : `BROUILLON → VALIDEE`. Aucun autre statut, aucun retour arrière : une
    pièce validée est immuable et se corrige par CONTREPASSATION (pièce inverse) suivie, le
    cas échéant, d'une pièce rectificative. Les trois pièces restent liées.
    """

    class Statut(models.TextChoices):
        BROUILLON = "BROUILLON", "Brouillon"
        VALIDEE = "VALIDEE", "Validée"

    reference = models.CharField(max_length=64, unique=True, db_index=True)
    date_operation = models.DateField(db_index=True)
    journal = models.CharField(max_length=3, choices=Journal.choices)
    libelle = models.CharField(max_length=255, blank=True)
    statut = models.CharField(max_length=10, choices=Statut.choices, default=Statut.BROUILLON)

    evenement = models.CharField(
        max_length=16, blank=True, db_index=True,
        help_text="Code du schéma du catalogue (B1…B16). Vide = opération diverse (OD).",
    )
    saisie_manuelle = models.BooleanField(
        default=False,
        help_text="OD saisie à la main : la validation exige un checker DIFFÉRENT du maker. "
                  "Les pièces produites par le catalogue (événements métier) ne sont pas "
                  "concernées — elles n'ont pas d'auteur humain à opposer.",
    )

    # --- Traçabilité de la correction (les trois pièces liées) ------------------------
    piece_contrepassee = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.PROTECT,
        related_name="contrepassations",
        help_text="Renseigné sur la pièce INVERSE : désigne la pièce qu'elle annule.",
    )
    piece_rectifiee = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.PROTECT,
        related_name="rectifications",
        help_text="Renseigné sur la pièce CORRIGÉE : désigne la pièce d'origine erronée.",
    )
    motif = models.TextField(blank=True)

    # --- Gouvernance du change --------------------------------------------------------
    taux_change = models.ForeignKey(
        "accounting.TauxChange", null=True, blank=True, on_delete=models.PROTECT,
        related_name="pieces",
        help_text="Taux journalisé — OBLIGATOIRE dès qu'une pièce porte plusieurs devises.",
    )

    # --- Lien vers l'événement métier d'origine ---------------------------------------
    origine_type = models.CharField(max_length=64, blank=True)
    origine_id = models.CharField(max_length=64, blank=True)

    cree_par = models.CharField(max_length=255, blank=True)
    cree_le = models.DateTimeField(auto_now_add=True)
    valide_par = models.CharField(max_length=255, blank=True)
    valide_le = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-date_operation", "-cree_le"]
        verbose_name = "pièce comptable"
        verbose_name_plural = "pièces comptables"
        indexes = [models.Index(fields=["statut", "date_operation"])]

    def __str__(self) -> str:
        return f"{self.reference} [{self.journal}] {self.date_operation} ({self.statut})"

    @property
    def est_validee(self) -> bool:
        return self.statut == self.Statut.VALIDEE

    def save(self, *args, **kwargs):
        """Garde d'append-only : une pièce déjà `VALIDEE` en base ne se ré-enregistre pas."""
        if self.pk:
            ancien = PieceComptable.objects.filter(pk=self.pk).values_list("statut", flat=True).first()
            if ancien == self.Statut.VALIDEE:
                raise ValidationFailed(
                    f"La pièce {self.reference} est validée : elle est immuable. "
                    "Utilisez la contrepassation (services.contrepasser_piece)."
                )
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationFailed(
            "DELETE n'existe pas en comptabilité : contrepassez la pièce "
            f"{self.reference} (services.contrepasser_piece)."
        )


class LigneEcriture(models.Model):
    """Une ligne porte SA devise — aucune agrégation multi-devises implicite n'est possible."""

    piece = models.ForeignKey(PieceComptable, on_delete=models.PROTECT, related_name="lignes")
    compte = models.ForeignKey(CompteComptable, on_delete=models.PROTECT, related_name="lignes")
    devise = models.CharField(max_length=3, choices=Devise.choices)
    debit = models.DecimalField(max_digits=20, decimal_places=2, default=Decimal("0.00"))
    credit = models.DecimalField(max_digits=20, decimal_places=2, default=Decimal("0.00"))
    libelle = models.CharField(max_length=255, blank=True)
    ordre = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["piece_id", "ordre", "id"]
        verbose_name = "ligne d'écriture"
        verbose_name_plural = "lignes d'écriture"
        constraints = [
            models.CheckConstraint(condition=Q(debit__gte=0), name="acc_ligne_debit_positif"),
            models.CheckConstraint(condition=Q(credit__gte=0), name="acc_ligne_credit_positif"),
            models.CheckConstraint(
                condition=~(Q(debit__gt=0) & Q(credit__gt=0)),
                name="acc_ligne_pas_debit_et_credit",
            ),
            models.CheckConstraint(
                condition=Q(debit__gt=0) | Q(credit__gt=0),
                name="acc_ligne_non_nulle",
            ),
        ]
        indexes = [models.Index(fields=["compte", "devise"])]

    def __str__(self) -> str:
        return f"{self.compte_id} {self.devise} D:{self.debit} C:{self.credit}"

    @property
    def montant_signe(self) -> Decimal:
        return self.debit - self.credit

    def save(self, *args, **kwargs):
        if self.piece_id:
            statut = (
                PieceComptable.objects.filter(pk=self.piece_id)
                .values_list("statut", flat=True).first()
            )
            if statut == PieceComptable.Statut.VALIDEE:
                raise ValidationFailed(
                    "Impossible d'ajouter ou de modifier une ligne sur une pièce validée."
                )
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationFailed(
            "DELETE n'existe pas sur une ligne d'écriture : contrepassez la pièce."
        )


class TauxChange(models.Model):
    """PROJECTION LOCALE, EN LECTURE SEULE, d'un taux gouverné par l'app `fx`.

    Ce modèle n'est PLUS un point de saisie. La source de vérité du taux de change est
    `fx.ExchangeRate`, qui porte le versionnement append-only, le maker-checker au-delà du
    seuil d'`InstitutionConfig`, la source tracée et le refus de retomber sur la veille.
    Deux modèles pour la même grandeur violaient le principe 6 et faisaient un incident de
    données en puissance (principe 11).

    Ce qui subsiste ici, et pourquoi : `PieceComptable.taux_change` pointe sur cette table,
    de sorte qu'une pièce reste AUTO-PORTANTE — le taux appliqué se lit sur la pièce, sans
    dépendre d'une jointure inter-app ni d'une re-lecture d'un référentiel qui aura bougé.
    Chaque ligne est fabriquée par `accounting.fx._projeter` depuis un taux `fx` ACTIF et
    porte son identité dans `source_reference` (« fx.ExchangeRate#42 v2 (BCC/USD) »).

    Cible : remplacer la clé étrangère par un pointeur direct vers `fx.ExchangeRate` — une
    migration qui touche deux apps et se coordonne, cf. rapport de livraison.

    Convention : `taux` = nombre d'unités de `devise_contre` pour 1 unité de `devise_base`
    (ex. base=USD, contre=FC, taux=2800 → 1 USD = 2 800 FC), valeur = cours PIVOT
    `(achat + vente) / 2` du taux `fx` d'origine.
    """

    class Usage(models.TextChoices):
        OPERATIONNEL = "OPERATIONNEL", "Opérationnel (transactions du jour)"
        CLOTURE = "CLOTURE", "Clôture (arrêté comptable)"

    class Source(models.TextChoices):
        BCC = "BCC", "Banque Centrale du Congo"
        INTERNE = "INTERNE", "Décision interne"
        MARCHE = "MARCHE", "Marché parallèle constaté"

    date_taux = models.DateField(db_index=True)
    usage = models.CharField(max_length=16, choices=Usage.choices)
    devise_base = models.CharField(max_length=3, choices=Devise.choices, default=Devise.USD)
    devise_contre = models.CharField(max_length=3, choices=Devise.choices, default=Devise.FC)
    taux = models.DecimalField(max_digits=18, decimal_places=6)

    source = models.CharField(max_length=16, choices=Source.choices, default=Source.BCC)
    source_reference = models.CharField(max_length=255, blank=True)

    saisi_par = models.CharField(max_length=255, blank=True)
    valide_par = models.CharField(max_length=255, blank=True)
    cree_le = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-date_taux", "usage"]
        verbose_name = "taux de change"
        verbose_name_plural = "taux de change"
        constraints = [
            models.UniqueConstraint(
                fields=["date_taux", "usage", "devise_base", "devise_contre"],
                name="acc_taux_unique_par_jour_et_usage",
            ),
            models.CheckConstraint(condition=Q(taux__gt=0), name="acc_taux_strictement_positif"),
            models.CheckConstraint(
                condition=~Q(devise_base=models.F("devise_contre")),
                name="acc_taux_devises_distinctes",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.date_taux} {self.usage} 1 {self.devise_base} = {self.taux} {self.devise_contre}"


class EventEntryTemplate(models.Model):
    """Schéma d'écriture du catalogue (annexe B, B1→B16) — EN BASE, jamais en code.

    Le code exécute (`catalogue.construire_lignes`), le paramétrage décide.
    """

    code = models.CharField(max_length=16, unique=True, db_index=True)
    libelle = models.CharField(max_length=255)
    journal = models.CharField(max_length=3, choices=Journal.choices)
    description = models.TextField(blank=True)
    actif = models.BooleanField(default=True)
    version = models.PositiveIntegerField(default=1)
    cree_le = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["code"]
        verbose_name = "schéma d'écriture"
        verbose_name_plural = "schémas d'écriture"

    def __str__(self) -> str:
        return f"{self.code} — {self.libelle}"


class EventEntryTemplateLine(models.Model):
    """Une ligne de schéma. Résolution à l'exécution :

    * `compte_racine` littéral (« 413 ») → résolu en 413FC/413USD selon la devise de la ligne ;
    * `compte_racine` commençant par « $ » → placeholder fourni par l'appelant
      (`$TRESORERIE`, `$CANTONNEMENT`) car l'annexe laisse le choix (501/511/53x) ;
    * `devise_regle` dit QUELLE devise s'applique (celle de l'opération, de la source ou de
      la cible d'un change, ou une devise fixe) ;
    * `montant_ref` est la clé du montant dans le contexte de l'événement ;
    * `condition` restreint la ligne à une variante (B16 : GAIN ou PERTE).
    """

    class Sens(models.TextChoices):
        DEBIT = "DEBIT", "Débit"
        CREDIT = "CREDIT", "Crédit"

    class DeviseRegle(models.TextChoices):
        OPERATION = "OPERATION", "Devise de l'opération"
        SOURCE = "SOURCE", "Devise source (change)"
        CIBLE = "CIBLE", "Devise cible (change)"
        FC = "FC", "Toujours FC"
        USD = "USD", "Toujours USD"

    template = models.ForeignKey(
        EventEntryTemplate, on_delete=models.CASCADE, related_name="lignes",
    )
    sens = models.CharField(max_length=6, choices=Sens.choices)
    compte_racine = models.CharField(max_length=32)
    devise_regle = models.CharField(
        max_length=12, choices=DeviseRegle.choices, default=DeviseRegle.OPERATION,
    )
    montant_ref = models.CharField(max_length=48)
    condition = models.CharField(
        max_length=16, blank=True,
        help_text="Vide = ligne toujours produite. Sinon, produite si la condition est active.",
    )
    libelle = models.CharField(max_length=255, blank=True)
    ordre = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["template_id", "ordre", "id"]
        verbose_name = "ligne de schéma"
        verbose_name_plural = "lignes de schéma"
        constraints = [
            models.UniqueConstraint(
                fields=["template", "ordre"], name="acc_template_ordre_unique",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.template_id} {self.ordre} {self.sens} {self.compte_racine}"


class SourceEvenements(models.Model):
    """Une FILE d'événements métier que la comptabilité lit — déclarée en base.

    `investments` a été la première (`InvestmentEvent`) ; `credits` (B1→B4) et `savings`
    (B8/B9) suivent. Sans cette table, le consommateur porterait le nom de la file
    d'`investments` en dur et brancher une deuxième file serait un déploiement — alors que
    c'est une décision d'exploitation.

    Ce qu'une file doit exposer pour être lisible (le CONTRAT, minimal et suffisant) :

    * `event_type` — le type métier, clé du mapping `RegleConsommation` ;
    * `amount` — le montant du FAIT, strictement positif et JAMAIS signé : le sens d'une
      écriture est porté par le schéma de l'annexe B, jamais par le signe d'un montant ;
    * `currency`, `occurred_at` (aware — c'est la date COMPTABLE), `payload` (JSON) ;
    * `consumed_at` / `journal_reference` — écrits par le consommateur, par personne d'autre.

    Tout le reste est facultatif et n'est lu QUE si le schéma appliqué le réclame : une file
    de crédit n'a pas de compte de cantonnement d'offre à porter, et on ne doit pas lui en
    demander un.
    """

    code = models.CharField(
        max_length=64, unique=True, db_index=True,
        help_text="Identifiant de la file, tel qu'écrit dans « RegleConsommation.source » "
                  "(convention : « app_label.ModelName »).",
    )
    modele = models.CharField(
        max_length=64, blank=True,
        help_text="Modèle Django réellement lu (« app_label.ModelName »). Vide = le code "
                  "fait foi. Distinct du code pour qu'une file puisse être renommée, ou "
                  "qu'un banc d'essai pointe une file existante sans usurper son identité.",
    )
    libelle = models.CharField(max_length=200, blank=True)
    prefixe_reference = models.CharField(
        max_length=8,
        help_text="Préfixe des références de pièce produites depuis cette file (INV, CRE…). "
                  "Il rend la référence unique entre files : deux événements de deux files "
                  "peuvent porter le même identifiant.",
    )
    note = models.TextField(blank=True)
    actif = models.BooleanField(
        default=True,
        help_text="Désactivée, la file cesse d'être consommée et ses événements "
                  "s'accumulent — ils ne se perdent pas et ne s'écrivent pas de travers.",
    )
    modifie_par = models.CharField(max_length=255, blank=True)
    modifie_le = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["code"]
        verbose_name = "source d'événements métier"
        verbose_name_plural = "sources d'événements métier"

    def __str__(self) -> str:
        return f"{self.code} → {self.prefixe_reference}"

    @property
    def chemin_modele(self) -> str:
        return self.modele or self.code


class RegleConsommation(models.Model):
    """Mapping « événement métier → écriture » — EN BASE, jamais en dur (principe 8).

    `EventEntryTemplate` dit COMMENT s'écrit un schéma de l'annexe B ; cette table dit
    QUEL événement métier déclenche QUEL schéma, et avec quel compte de trésorerie quand
    l'annexe laisse le choix (501/511/53x). Sans elle, le consommateur porterait un
    `if event_type == …` : un mapping comptable dans le code est un mapping que le
    comptable ne peut ni lire ni corriger.

    Trois modes, et un seul est « ne rien faire » :

    * `PIECE` — application d'un schéma du catalogue (B10…B13) ;
    * `CONTREPASSATION` — annulation de la pièce réellement passée pour l'événement
      d'origine (`evenement_origine`), retrouvée par sa `journal_reference` ;
    * `SANS_ECRITURE` — l'annexe B ne définit AUCUNE écriture pour cet événement. Il
      reste alors NON CONSOMMÉ, donc visible dans la file et dans chaque rapport de
      consommation, jusqu'à arbitrage. On n'invente pas une écriture pour vider une file.
    """

    class Mode(models.TextChoices):
        PIECE = "PIECE", "Pièce nouvelle depuis un schéma du catalogue"
        CONTREPASSATION = "CONTREPASSATION", "Contrepassation de la pièce d'origine"
        SANS_ECRITURE = "SANS_ECRITURE", "Aucune écriture définie par l'annexe B"

    source = models.CharField(
        max_length=64, db_index=True,
        help_text="Modèle producteur des événements (ex. « investments.InvestmentEvent »).",
    )
    type_evenement = models.CharField(max_length=48, db_index=True)
    mode = models.CharField(max_length=16, choices=Mode.choices, default=Mode.PIECE)
    schema = models.CharField(
        max_length=16, blank=True,
        help_text="Code du schéma du catalogue appliqué en mode PIECE (B10, B11…).",
    )
    evenement_origine = models.CharField(
        max_length=48, blank=True,
        help_text="Mode CONTREPASSATION : type de l'événement dont la pièce est annulée.",
    )
    compte_tresorerie = models.CharField(
        max_length=32, blank=True,
        help_text="Racine du compte résolvant $TRESORERIE (501/511/53x). Un événement peut "
                  "l'écraser via « compteTresorerie » dans son payload.",
    )
    note = models.TextField(blank=True)
    actif = models.BooleanField(default=True)
    modifie_par = models.CharField(max_length=255, blank=True)
    modifie_le = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["source", "type_evenement"]
        verbose_name = "règle de consommation d'événement"
        verbose_name_plural = "règles de consommation d'événement"
        constraints = [
            models.UniqueConstraint(
                fields=["source", "type_evenement"],
                name="acc_une_regle_par_type_evenement",
            ),
        ]

    def __str__(self) -> str:
        cible = self.schema or self.evenement_origine or "—"
        return f"{self.type_evenement} → {self.mode} {cible}"


# ===========================================================================
#  MAKER-CHECKER SUR LE PLAN COMPTABLE
# ===========================================================================

class DemandeCompteComptable(models.Model):
    """Ajout d'un compte au plan comptable — JAMAIS en un seul geste (annexe A :
    « extension maker-checker »).

    Le maker décrit le compte, un checker DIFFÉRENT l'approuve, et c'est l'approbation
    qui crée le `CompteComptable`. Un compte refusé laisse une trace : le plan comptable
    d'AGRICAP doit pouvoir s'expliquer compte par compte, y compris ce qu'on a refusé
    d'y mettre.

    La suppression n'existe pas dans l'autre sens : un compte mouvementé est protégé par
    `CompteComptable.delete`, et un compte devenu inutile se désactive.
    """

    class Statut(models.TextChoices):
        EN_ATTENTE = "EN_ATTENTE", "En attente de validation"
        APPROUVEE = "APPROUVEE", "Approuvée"
        REJETEE = "REJETEE", "Rejetée"

    code = models.CharField(max_length=32, db_index=True)
    racine = models.CharField(max_length=24)
    intitule = models.CharField(max_length=200)
    classe = models.PositiveSmallIntegerField()
    nature = models.CharField(max_length=8, choices=Nature.choices)
    devise = models.CharField(max_length=3, choices=Devise.choices, blank=True)
    est_transitoire = models.BooleanField(default=False)
    cantonnement = models.CharField(max_length=64, blank=True)
    parent_code = models.CharField(max_length=32, blank=True)
    justification = models.TextField()

    statut = models.CharField(max_length=12, choices=Statut.choices, default=Statut.EN_ATTENTE)
    demande_par = models.CharField(max_length=255)
    demande_le = models.DateTimeField(auto_now_add=True)
    decide_par = models.CharField(max_length=255, blank=True)
    decide_le = models.DateTimeField(null=True, blank=True)
    motif_decision = models.TextField(blank=True)

    compte = models.ForeignKey(
        CompteComptable, null=True, blank=True, on_delete=models.PROTECT,
        related_name="demandes",
        help_text="Renseigné à l'approbation : le compte réellement créé.",
    )

    class Meta:
        ordering = ["-demande_le"]
        verbose_name = "demande d'ouverture de compte"
        verbose_name_plural = "demandes d'ouverture de compte"
        constraints = [
            models.UniqueConstraint(
                fields=["code"], condition=Q(statut="EN_ATTENTE"),
                name="acc_une_seule_demande_en_attente_par_code",
            ),
        ]

    def __str__(self) -> str:
        return f"Demande {self.code} ({self.statut})"

    def delete(self, *args, **kwargs):
        raise ValidationFailed(
            "Une demande d'ouverture de compte ne se supprime pas : elle se rejette "
            "(la trace du refus fait partie de la gouvernance du plan comptable)."
        )


# ===========================================================================
#  PROVISIONNEMENT DU RISQUE DE CRÉDIT (principe 6)
# ===========================================================================

class ClasseRisque(models.Model):
    """Grille de classification PAR — EN BASE, jamais en dur (principe 8 de MKOPO).

    Les bornes sont exprimées en jours de retard de la plus ancienne échéance non
    intégralement réglée. Elles doivent couvrir [0, ∞[ sans trou ni recouvrement :
    `provisions.verifier_couverture` le vérifie et le test le verrouille.
    """

    code = models.CharField(max_length=16, unique=True, db_index=True)
    libelle = models.CharField(max_length=200)
    jours_min = models.PositiveIntegerField(
        help_text="Borne INCLUSE, en jours de retard.",
    )
    jours_max = models.PositiveIntegerField(
        null=True, blank=True,
        help_text="Borne INCLUSE. NULL = classe terminale (pas de borne supérieure).",
    )
    taux_provision = models.DecimalField(
        max_digits=6, decimal_places=4,
        help_text="Fraction de l'encours à provisionner (0.2500 = 25 %).",
    )
    en_souffrance = models.BooleanField(
        default=False,
        help_text="Classe qui déclenche le déclassement automatique 413 → 416 (schéma B5).",
    )
    ordre = models.PositiveSmallIntegerField(default=0)
    actif = models.BooleanField(default=True)
    modifie_par = models.CharField(max_length=255, blank=True)
    modifie_le = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["ordre", "jours_min"]
        verbose_name = "classe de risque (PAR)"
        verbose_name_plural = "classes de risque (PAR)"
        constraints = [
            models.CheckConstraint(
                condition=Q(taux_provision__gte=0) & Q(taux_provision__lte=1),
                name="acc_classe_taux_entre_0_et_1",
            ),
            models.CheckConstraint(
                condition=Q(jours_max__isnull=True) | Q(jours_max__gte=models.F("jours_min")),
                name="acc_classe_bornes_ordonnees",
            ),
        ]

    def __str__(self) -> str:
        borne = "∞" if self.jours_max is None else str(self.jours_max)
        return f"{self.code} [{self.jours_min}–{borne} j] {self.taux_provision}"

    def contient(self, jours_retard: int) -> bool:
        if jours_retard < self.jours_min:
            return False
        return self.jours_max is None or jours_retard <= self.jours_max


class ClassementCredit(models.Model):
    """Photo, à une date d'arrêté, de la classification d'UN crédit — append-only.

    C'est la pièce justificative de la provision : deux ans plus tard, un auditeur doit
    pouvoir rejouer pourquoi tel crédit était en PAR90 le 31/12 et combien il pesait.
    """

    date_arrete = models.DateField(db_index=True)
    loan_id = models.PositiveIntegerField(db_index=True)
    loan_reference = models.CharField(max_length=64, db_index=True)
    classe = models.ForeignKey(ClasseRisque, on_delete=models.PROTECT, related_name="classements")
    jours_retard = models.PositiveIntegerField(default=0)
    encours = models.DecimalField(max_digits=20, decimal_places=2)
    devise = models.CharField(max_length=3, choices=Devise.choices)
    en_souffrance = models.BooleanField(default=False)
    piece_declassement = models.ForeignKey(
        PieceComptable, null=True, blank=True, on_delete=models.PROTECT,
        related_name="declassements",
        help_text="Pièce B5 (413 → 416) produite par CET arrêté, le cas échéant.",
    )
    piece_reclassement = models.ForeignKey(
        PieceComptable, null=True, blank=True, on_delete=models.PROTECT,
        related_name="reclassements",
        help_text="Pièce B17 (416 → 413) produite par CET arrêté quand le crédit revient à "
                  "bonne fin. Champ distinct du déclassement : les deux mouvements sont des "
                  "événements économiques opposés, pas une correction l'un de l'autre.",
    )
    cree_par = models.CharField(max_length=255, blank=True)
    cree_le = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-date_arrete", "loan_reference"]
        verbose_name = "classement de crédit"
        verbose_name_plural = "classements de crédit"
        constraints = [
            models.UniqueConstraint(
                fields=["date_arrete", "loan_id"], name="acc_un_classement_par_credit_et_arrete",
            ),
            models.CheckConstraint(condition=Q(encours__gte=0), name="acc_classement_encours_positif"),
        ]

    def __str__(self) -> str:
        return f"{self.date_arrete} {self.loan_reference} → {self.classe_id}"

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValidationFailed(
                "Un classement est une photo datée : il ne se modifie pas, on en produit "
                "un nouveau à l'arrêté suivant."
            )
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationFailed("Un classement de crédit ne se supprime pas (append-only).")


class ArreteProvision(models.Model):
    """Résultat d'une clôture de provisionnement, POUR UNE DEVISE.

    Le stock cible (`provision_requise`) est comparé au solde réel de 137 en base
    (`provision_anterieure`) : l'écart devient une dotation B6 ou une reprise B7 —
    jamais les deux, jamais une écriture « de confort ».
    """

    date_arrete = models.DateField(db_index=True)
    devise = models.CharField(max_length=3, choices=Devise.choices)
    provision_requise = models.DecimalField(max_digits=20, decimal_places=2)
    provision_anterieure = models.DecimalField(max_digits=20, decimal_places=2)
    dotation = models.DecimalField(max_digits=20, decimal_places=2, default=Decimal("0.00"))
    reprise = models.DecimalField(max_digits=20, decimal_places=2, default=Decimal("0.00"))
    encours_portefeuille = models.DecimalField(
        max_digits=20, decimal_places=2, default=Decimal("0.00"),
        help_text="Base de calcul : encours classé, issu de `portfolio`.",
    )
    encours_comptable = models.DecimalField(
        max_digits=20, decimal_places=2, default=Decimal("0.00"),
        help_text="Solde 413 + 416 au grand livre à la même date — l'écart est un signal, "
                  "pas une correction automatique.",
    )
    nombre_credits = models.PositiveIntegerField(default=0)
    piece = models.ForeignKey(
        PieceComptable, null=True, blank=True, on_delete=models.PROTECT,
        related_name="arretes_provision",
    )
    cree_par = models.CharField(max_length=255, blank=True)
    cree_le = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-date_arrete", "devise"]
        verbose_name = "arrêté de provision"
        verbose_name_plural = "arrêtés de provision"
        constraints = [
            models.UniqueConstraint(
                fields=["date_arrete", "devise"], name="acc_un_arrete_par_date_et_devise",
            ),
            models.CheckConstraint(
                condition=~(Q(dotation__gt=0) & Q(reprise__gt=0)),
                name="acc_arrete_dotation_ou_reprise",
            ),
        ]

    def __str__(self) -> str:
        return f"Arrêté {self.date_arrete} {self.devise} → {self.provision_requise}"

    def save(self, *args, **kwargs):
        if self.pk and ArreteProvision.objects.filter(pk=self.pk).exists():
            # Seul le rattachement de la pièce est autorisé après création (même
            # transaction) ; tout le reste est figé.
            autorise = {"piece", "piece_id"}
            champs = set(kwargs.get("update_fields") or [])
            if not champs or not champs.issubset(autorise):
                raise ValidationFailed(
                    "Un arrêté de provision est figé : reprenez un nouvel arrêté à une "
                    "date postérieure."
                )
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationFailed("Un arrêté de provision ne se supprime pas (append-only).")


class LigneArreteProvision(models.Model):
    """Détail par classe de risque d'un arrêté — c'est ce qui rend la provision explicable."""

    arrete = models.ForeignKey(ArreteProvision, on_delete=models.PROTECT, related_name="lignes")
    classe = models.ForeignKey(ClasseRisque, on_delete=models.PROTECT, related_name="lignes_arrete")
    nombre_credits = models.PositiveIntegerField(default=0)
    encours = models.DecimalField(max_digits=20, decimal_places=2, default=Decimal("0.00"))
    taux_applique = models.DecimalField(max_digits=6, decimal_places=4)
    provision = models.DecimalField(max_digits=20, decimal_places=2, default=Decimal("0.00"))

    class Meta:
        ordering = ["arrete_id", "classe__ordre"]
        verbose_name = "ligne d'arrêté de provision"
        verbose_name_plural = "lignes d'arrêté de provision"
        constraints = [
            models.UniqueConstraint(
                fields=["arrete", "classe"], name="acc_une_ligne_par_classe_et_arrete",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.arrete_id} {self.classe_id} {self.encours} × {self.taux_applique}"

    def delete(self, *args, **kwargs):
        raise ValidationFailed("Le détail d'un arrêté ne se supprime pas (append-only).")
