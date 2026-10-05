"""
Tables de référence AGRICAP — peuplées automatiquement à l'upload d'un classeur
Excel (CAHIER : accès en lecture au référentiel + config institution).

Côté admin : `import_referentiel` / l'endpoint d'upload écrivent ici.
Côté client : la vérification (contrôles de vraisemblance) LIT ces tables.

NB : le référentiel v3 n'a PAS un schéma uniforme (le végétal parle de « Culture »
et de rendement t/ha, l'élevage de « Spéculation », l'aquaculture d'« Espèce » et de
densité/survie…). On capture donc TOUTES les colonnes dans `columns` (JSON) et on
dérive au mieux les concepts communs (coût, prix, rendement, perte, cycle) par
correspondance sémantique d'en-têtes — sans jamais inventer une valeur absente.
"""
from __future__ import annotations

from decimal import Decimal

from django.db import models


class ReferentielVersion(models.Model):
    """Une version du référentiel (traçabilité : `version_referentiel` du JSON §8.3)."""
    label = models.CharField(max_length=120)
    source_filename = models.CharField(max_length=255, blank=True)
    imported_at = models.DateTimeField(auto_now_add=True)
    is_active = models.BooleanField(default=True)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["-imported_at"]

    def __str__(self) -> str:
        return f"{self.label} ({self.imported_at:%Y-%m-%d})"

    @classmethod
    def active(cls) -> "ReferentielVersion | None":
        return cls.objects.filter(is_active=True).order_by("-imported_at").first()


class ReferenceWorkbook(models.Model):
    """Classeur de référence téléversé (fichier source conservé pour audit/relecture)."""
    file = models.FileField(upload_to="referentiels/")
    original_name = models.CharField(max_length=255)
    uploaded_by = models.CharField(max_length=255, blank=True)  # sub IdP
    uploaded_at = models.DateTimeField(auto_now_add=True)
    version = models.ForeignKey(
        ReferentielVersion, null=True, blank=True, on_delete=models.SET_NULL, related_name="workbooks"
    )
    parsed_ok = models.BooleanField(default=False)
    parse_log = models.TextField(blank=True)

    def __str__(self) -> str:
        return self.original_name


class ReferenceRange(models.Model):
    """
    Une ligne du référentiel (une spéculation/culture/espèce d'une chaîne) : plages
    indicatives servant aux contrôles de vraisemblance (CAHIER §4).
    Concepts dérivés (numériques) + `columns` (toutes les colonnes, texte source).
    """
    version = models.ForeignKey(ReferentielVersion, on_delete=models.CASCADE, related_name="ranges")

    chain_code = models.CharField(max_length=2)          # "01".."14"
    chain_slug = models.CharField(max_length=40)
    chain_libelle = models.CharField(max_length=80)

    name = models.CharField(max_length=160)              # 1re colonne (Culture/Spéculation/Espèce/Activité)
    systeme = models.CharField(max_length=160, blank=True)
    cycle_months = models.FloatField(null=True, blank=True)

    # Paramètre technique clé (rendement / production / densité…) — bornes + libellé.
    parametre_cle = models.CharField(max_length=160, blank=True)
    unite = models.CharField(max_length=60, blank=True)
    rendement_min = models.FloatField(null=True, blank=True)
    rendement_max = models.FloatField(null=True, blank=True)

    cout_text = models.CharField(max_length=120, blank=True)
    cout_min = models.FloatField(null=True, blank=True)
    cout_max = models.FloatField(null=True, blank=True)

    prix_text = models.CharField(max_length=120, blank=True)
    prix_min = models.FloatField(null=True, blank=True)
    prix_max = models.FloatField(null=True, blank=True)

    perte_text = models.CharField(max_length=120, blank=True)
    perte_max = models.FloatField(null=True, blank=True)  # fraction 0..1 (mortalité/perte ; survie inversée)

    statut = models.CharField(max_length=60, blank=True)     # « À valider », « Validé »…
    source = models.CharField(max_length=255, blank=True)
    date_maj = models.CharField(max_length=40, blank=True)
    zone = models.CharField(max_length=200, blank=True)
    observations = models.TextField(blank=True)

    columns = models.JSONField(default=dict, blank=True)     # {en-tête: valeur brute} — toutes les colonnes

    class Meta:
        indexes = [models.Index(fields=["version", "chain_code"])]
        ordering = ["chain_code", "name"]

    def __str__(self) -> str:
        return f"[{self.chain_code}] {self.name}"

    @property
    def a_valider(self) -> bool:
        """Statut non validé → garde-fou §9 (décision abaissée)."""
        return "valider" in (self.statut or "").lower()


class InstitutionConfig(models.Model):
    """
    Paramètres de l'institution (CAHIER §8.1 : « jamais codés en dur »), issus de
    la feuille `16_Calibrage_Gouvernance`. Un seul actif à la fois.

    POURQUOI `DecimalField` ET PAS `FloatField` (principe 4). Cette table n'est
    pas un référentiel documentaire : c'est la source des paramètres que le moteur
    de scoring relit à CHAQUE analyse — les cinq poids (`analyse.poids_effectifs()`),
    les seuils DSCR et le score global minimum (`analyse.regles_decision()`), la
    décote appliquée aux garanties (`assets.services.valeur_apres_decote`). En
    binaire, 0,30 n'existe pas : `float` stockait 0,29999999999999998889776975…
    Sur une somme de poids, l'écart se propage jusqu'à faire diverger un score de
    0,1 point — et 0,1 point autour d'une frontière de recommandation change la
    recommandation. `credits/analyse.py` raisonne déjà en `Decimal` de bout en
    bout ; ces colonnes le contredisaient à la source.

    Précisions retenues :
    - ratios de couverture / DSCR / multiples : `0.001` (§4 « 0,001 pour les ratios ») ;
    - points de score et poids (base 100) : `0.01` ;
    - taux, frais et décotes exprimés en FRACTION : `0.0001`, car `0.001` sur une
      fraction ne sait pas représenter un taux courant comme 18,75 %/an (0,1875) ;
    - montants : `0.01` (§4).
    """
    version = models.ForeignKey(
        ReferentielVersion, null=True, blank=True, on_delete=models.SET_NULL, related_name="configs"
    )
    is_active = models.BooleanField(default=True)

    # Seuils prudentiels (feuille 16, section A). Lus par le moteur de décision.
    seuil_dscr = models.DecimalField(max_digits=6, decimal_places=3, default=Decimal("1.200"))
    seuil_dscr_stresse = models.DecimalField(max_digits=6, decimal_places=3, default=Decimal("1.000"))
    couverture_min = models.DecimalField(max_digits=6, decimal_places=3, default=Decimal("1.000"))  # 100 %
    score_global_min = models.DecimalField(max_digits=6, decimal_places=2, default=Decimal("70.00"))

    # Pondérations du score (§8.1). Somme attendue = 100 — invariant vérifié par
    # `analyse.poids_effectifs()`, qui compare à `Decimal(100)` : une somme
    # flottante de 99,99999999999999 le faisait retomber sur les poids de secours.
    poids_technique = models.DecimalField(max_digits=6, decimal_places=2, default=Decimal("25.00"))
    poids_financier = models.DecimalField(max_digits=6, decimal_places=2, default=Decimal("20.00"))
    poids_stress = models.DecimalField(max_digits=6, decimal_places=2, default=Decimal("10.00"))
    poids_comportemental = models.DecimalField(max_digits=6, decimal_places=2, default=Decimal("30.00"))
    poids_garanties = models.DecimalField(max_digits=6, decimal_places=2, default=Decimal("15.00"))

    # Paramètres crédit. Taux/frais/décotes en fraction (0,24 = 24 %).
    taux_interet_annuel = models.DecimalField(max_digits=6, decimal_places=4, default=Decimal("0.2400"))
    frais_dossier = models.DecimalField(max_digits=6, decimal_places=4, default=Decimal("0.0200"))
    commissions = models.DecimalField(max_digits=6, decimal_places=4, default=Decimal("0.0100"))
    duree_max_mois = models.IntegerField(default=24)
    plafond_delegue = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("25000.00"))  # USD
    decote_garantie = models.DecimalField(max_digits=6, decimal_places=4, default=Decimal("0.3000"))

    # Caution solidaire (SPEC §2.5). Principe 8 : ces seuils gouvernent qui peut
    # engager quoi — ils appartiennent au comité, pas à un déploiement. Lus par
    # `credits.guarantor`, qui logge un warning s'il doit retomber sur ses
    # valeurs de secours.
    caution_ratio_epargne = models.DecimalField(
        max_digits=6, decimal_places=3, default=Decimal("2.000"))  # k : Σ cautions ≤ k × épargne
    caution_max_actives = models.IntegerField(default=3)          # cautions vivantes par garant
    caution_consent_window_hours = models.IntegerField(default=72)  # fenêtre de consentement
    # Décote de la caution morale : elle sécurise socialement, pas financièrement.
    # Distincte de `decote_garantie`, qui s'applique aux actifs gagés.
    decote_caution_morale = models.DecimalField(max_digits=6, decimal_places=4, default=Decimal("0.7000"))

    # Phase de déploiement (§9 : validation humaine échantillonnée).
    phase_deploiement = models.CharField(max_length=20, default="PHASE_1")  # PHASE_1|2|3

    raw = models.JSONField(default=dict, blank=True)          # lignes brutes feuille 16

    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return f"Config institution (DSCR>={self.seuil_dscr}, score>={self.score_global_min})"

    @classmethod
    def active(cls) -> "InstitutionConfig":
        cfg = cls.objects.filter(is_active=True).order_by("-updated_at").first()
        return cfg or cls()  # défauts si aucune importée (jamais None)

    @property
    def taux_echantillon(self) -> Decimal:
        """Fraction de dossiers contre-analysés selon la phase (§9).

        `Decimal` comme le reste de la table : ce taux se multiplie à un effectif
        de dossiers, et rien de ce que lit la gouvernance ne doit repasser par un
        flottant (principe 4).
        """
        return {
            "PHASE_1": Decimal("1.00"),
            "PHASE_2": Decimal("0.30"),
            "PHASE_3": Decimal("0.10"),
        }.get(self.phase_deploiement, Decimal("1.00"))
