"""
Couche générique d'ingestion — le système s'ADAPTE à n'importe quel fichier de
données : chaque feuille devient une table (DataTable), chaque en-tête une colonne
(DataColumn), chaque ligne un enregistrement (DataRecord). Aucune migration n'est
nécessaire quand un nouveau fichier a d'autres tables/colonnes.

Enregistrement MANUEL : à l'upload le fichier est seulement stocké + un aperçu est
calculé (statut STAGED, aucune ligne écrite). L'admin déclenche ensuite « Enregistrer »
(commit) pour écrire les tables/colonnes/lignes ET, si c'est un référentiel, alimenter
les tables typées que le moteur lit (hybride).

Historique : réuploader un fichier de même nom = nouvelle révision COURANTE ; les
révisions précédentes sont conservées (is_current=False) pour l'historique.
"""
from __future__ import annotations

from django.db import models

KIND_REFERENTIEL = "REFERENTIEL"
KIND_SIMULATEUR = "SIMULATEUR"
KIND_ANNEXE = "ANNEXE"
KIND_FEUILLE_BESOINS = "FEUILLE_BESOINS"
KIND_AUTRE = "AUTRE"
KIND_CHOICES = [
    (k, k) for k in (
        KIND_REFERENTIEL, KIND_SIMULATEUR, KIND_ANNEXE, KIND_FEUILLE_BESOINS, KIND_AUTRE,
    )
]

#: `kind` de FileTemplate = type de fichier CLIENT que le template régit. On réutilise la
#: nomenclature dataio existante (principe 6) plutôt que d'inventer un code : un template
#: de feuille de besoins régit les fichiers `KIND_FEUILLE_BESOINS`.
TEMPLATE_KINDS = (KIND_FEUILLE_BESOINS,)

STATUS_STAGED = "STAGED"        # uploadé, analysé, PAS encore enregistré
STATUS_COMMITTED = "COMMITTED"  # enregistré manuellement en base
STATUS_CHOICES = [(s, s) for s in (STATUS_STAGED, STATUS_COMMITTED)]


class DataSource(models.Model):
    """
    Un classeur téléversé. Le fichier est conservé ; les lignes ne sont écrites qu'au
    commit (manuel). Le versionnage se fait par nom de fichier : `dataset_key`.
    """
    file = models.FileField(upload_to="datasources/")
    original_name = models.CharField(max_length=255)
    dataset_key = models.CharField(max_length=255, db_index=True)  # clé logique (nom normalisé)
    kind = models.CharField(max_length=20, choices=KIND_CHOICES, default=KIND_AUTRE)
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default=STATUS_STAGED)

    # Empreinte du fichier source : c'est elle qui rend une analyse rejouable et qui
    # permet de comparer deux révisions (principe 3 — l'écart entre deux analyses est
    # lui-même une donnée).
    sha256 = models.CharField(max_length=64, blank=True, db_index=True)

    # Rattachement optionnel à un dossier de crédit — renseigné pour le seul
    # kind FEUILLE_BESOINS. PROTECT : une pièce probante d'un dossier ne disparaît
    # jamais, même si le dossier est clôturé.
    credit_application = models.ForeignKey(
        "credits.CreditApplication", null=True, blank=True,
        on_delete=models.PROTECT, related_name="needs_sources",
    )

    # Versionnage / historique.
    revision = models.IntegerField(default=1)
    is_current = models.BooleanField(default=False)  # devient True au commit
    supersedes = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL, related_name="superseded_by"
    )

    uploaded_by = models.CharField(max_length=255, blank=True)   # sub IdP
    uploaded_at = models.DateTimeField(auto_now_add=True)
    committed_at = models.DateTimeField(null=True, blank=True)
    committed_by = models.CharField(max_length=255, blank=True)

    # Aperçu calculé à l'upload (feuilles, en-têtes, échantillon) — sans écrire de lignes.
    preview = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-uploaded_at"]

    def __str__(self) -> str:
        return f"{self.original_name} r{self.revision} [{self.status}]"


class DataTable(models.Model):
    """Une table découverte (= une feuille). Créée au commit."""
    source = models.ForeignKey(DataSource, on_delete=models.CASCADE, related_name="tables")
    name = models.CharField(max_length=200)          # nom de la feuille
    position = models.IntegerField(default=0)
    n_rows = models.IntegerField(default=0)
    n_cols = models.IntegerField(default=0)

    class Meta:
        ordering = ["position"]
        unique_together = ("source", "name")

    def __str__(self) -> str:
        return self.name


class DataColumn(models.Model):
    """Une colonne découverte (= un en-tête). Le type est inféré des valeurs."""
    TEXT, NUMBER, PERCENT, RANGE, DATE = "text", "number", "percent", "range", "date"
    table = models.ForeignKey(DataTable, on_delete=models.CASCADE, related_name="columns")
    name = models.CharField(max_length=255)
    position = models.IntegerField(default=0)
    dtype = models.CharField(max_length=12, default=TEXT)

    class Meta:
        ordering = ["position"]

    def __str__(self) -> str:
        return f"{self.name} ({self.dtype})"


class DataRecord(models.Model):
    """Une ligne, valeurs indexées par nom de colonne (flexible)."""
    table = models.ForeignKey(DataTable, on_delete=models.CASCADE, related_name="records")
    row_index = models.IntegerField()
    values = models.JSONField(default=dict)

    class Meta:
        ordering = ["row_index"]
        indexes = [models.Index(fields=["table", "row_index"])]


class FileTemplate(models.Model):
    """
    Template de fichier versionné — cœur du **principe 11**.

    Le template officiel (feuille de besoins, et tout futur formulaire Excel) est un
    fichier de RÉFÉRENCE téléversé par l'admin (maker), puis activé par un SECOND admin
    (checker ≠ maker), exactement comme les `ValueChain` de `reference_data`. Le schéma
    attendu — feuilles, colonnes, ordre, types, rubriques — est **dérivé automatiquement**
    du fichier à son activation (`schema`), jamais maintenu à la main dans le code.

    Un seul template `active` par `kind` : le précédent passe `archived` à l'activation
    du suivant. La validation structurelle d'un fichier client se fait contre le `schema`
    du template actif ; sans template actif → `TEMPLATE_NOT_CONFIGURED`.

    NB : c'est ce modèle qui incarne le « kind=TEMPLATE » du contrat. Son champ `kind`
    désigne le type de fichier CLIENT régi (nomenclature dataio réutilisée, principe 6),
    pas le fait d'être un template (implicite au modèle).
    """

    class Status(models.TextChoices):
        PENDING = "pending", "En attente d'activation"
        ACTIVE = "active", "Actif"
        ARCHIVED = "archived", "Archivé"

    kind = models.CharField(max_length=40, default=KIND_FEUILLE_BESOINS, db_index=True)
    # Chaîne de valeur `01`-`14` (`referentiel.chains`) que ce template régit. Vide =
    # template générique du `kind`, valable pour toute chaîne qui n'a pas la sienne — pas
    # un template "par défaut" au sens fort : dès qu'une chaîne a SON template actif, c'est
    # lui qui s'applique, jamais le générique (cf. `active_template`, principe 11 : une
    # demande de template pour une chaîne précise ne retombe jamais sur un autre).
    chain_code = models.CharField(max_length=2, blank=True, db_index=True)
    file = models.FileField(upload_to="dataio/templates/")
    original_name = models.CharField(max_length=255)

    # Version numérique croissante par `kind` (1, 2, 3…). Le versionnage maker-checker
    # se lit ici, pas dans le nom de fichier.
    version = models.IntegerField(default=1)

    # Empreinte du fichier (principe 3) : rejouabilité + comparaison de révisions.
    sha256 = models.CharField(max_length=64, blank=True, db_index=True)

    status = models.CharField(
        max_length=12, choices=Status.choices, default=Status.PENDING, db_index=True,
    )

    # Schéma dérivé du fichier (feuilles / colonnes / ordre / types / rubriques).
    # Aperçu à l'upload, autoritatif après activation.
    schema = models.JSONField(default=dict, blank=True)

    uploaded_by = models.CharField(max_length=255, blank=True)   # sub IdP (maker)
    uploaded_at = models.DateTimeField(auto_now_add=True)
    activated_by = models.CharField(max_length=255, blank=True)  # sub IdP (checker)
    activated_at = models.DateTimeField(null=True, blank=True)

    # Le template que celui-ci a remplacé à son activation (traçabilité de la lignée).
    supersedes = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL, related_name="superseded_by",
    )

    class Meta:
        ordering = ["-uploaded_at"]
        indexes = [
            models.Index(fields=["kind", "status"]),
            models.Index(fields=["kind", "chain_code", "status"]),
        ]

    def __str__(self) -> str:
        chaine = f" chaîne {self.chain_code}" if self.chain_code else ""
        return f"Template {self.kind}{chaine} v{self.version} [{self.status}]"
