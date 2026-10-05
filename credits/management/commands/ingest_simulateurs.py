"""
Ingestion des simulateurs par chaîne (`AGRICAP_FIN_SIM_NN_Famille_Culture.xlsx`)
dans la couche générique `dataio`, en miroir CLI de l'upload → commit admin.

    python manage.py ingest_simulateurs                 # défaut : dossier standard
    python manage.py ingest_simulateurs <dossier>       # dossier explicite
    python manage.py ingest_simulateurs --check          # ne rien ingérer, seulement vérifier

Pourquoi une commande et pas un `RunPython` ni une fixture :
  - c'est de la DONNÉE, pas du schéma — aucune migration ne crée un référentiel
    (principes 1 et 8 : les valeurs vivent en base, chargées, jamais figées dans
    l'historique de schéma) ;
  - le seul chemin d'ingestion légitime reste `dataio.services` (inspect → commit),
    exactement celui de l'onglet admin : on ne valide/écrit jamais une lecture du
    classeur différente de celle que le moteur relira (principe 5).

**Idempotente** : un simulateur déjà ingéré, courant et de SHA-256 identique est
sauté. Un classeur modifié (SHA-256 différent) crée une nouvelle révision courante
et conserve l'ancienne (historique `dataio`).

Après ingestion, la commande VÉRIFIE que `referentiel_loader.charger_depuis_simulateur`
lit chaque classeur courant sans lever `ReferentielIntrouvable`. Elle n'invente rien :
un classeur dont la structure fait échouer la lecture est signalé précisément
(quel classeur, quel message) et n'est pas complété par des valeurs devinées
(principe 1). La construction des `ReferentielFiliere` reste au ressort de
`seed_analyse`, qui lit ces mêmes simulateurs ingérés.
"""
from __future__ import annotations

import glob
import os

from django.conf import settings
from django.core.files import File
from django.core.management.base import BaseCommand, CommandError

from credits.referentiel_loader import (
    ReferentielIntrouvable,
    charger_depuis_simulateur,
    simulateurs_disponibles,
)
from dataio import services
from dataio.models import KIND_SIMULATEUR, STATUS_COMMITTED, DataSource

#: Sous-dossier standard des simulateurs par chaîne, sous `DOCUMENT_EXCEL_DIR`.
DEFAULT_SUBDIR = "Agricap FIN simulateur Par Chaine"

#: Les seuls classeurs ingérés par cette commande : les simulateurs par filière.
#: Le motif exclut le simulateur générique `..._Cycle_Production_v4.xlsx`, qui ne
#: suit pas la convention `AGRICAP_FIN_SIM_NN_...` et n'alimente aucun référentiel.
GLOB_PATTERN = "AGRICAP_FIN_SIM_*.xlsx"


class Command(BaseCommand):
    help = ("Ingère les simulateurs par chaîne (AGRICAP_FIN_SIM_NN_*.xlsx) dans dataio "
            "et vérifie que le référentiel se lit depuis chaque classeur (idempotent).")

    def add_arguments(self, parser):
        parser.add_argument(
            "directory", nargs="?", default=None,
            help=(f"Dossier des simulateurs (défaut : DOCUMENT_EXCEL_DIR/« {DEFAULT_SUBDIR} »)."),
        )
        parser.add_argument(
            "--check", action="store_true",
            help=("N'ingère rien : vérifie seulement que les simulateurs déjà courants "
                  "se lisent sans valeur inventée."),
        )

    def handle(self, *args, **opts):
        directory = opts["directory"] or os.path.join(
            settings.DOCUMENT_EXCEL_DIR, DEFAULT_SUBDIR)

        if not opts["check"]:
            if not os.path.isdir(directory):
                raise CommandError(f"Dossier introuvable : {directory}")
            paths = sorted(glob.glob(os.path.join(directory, GLOB_PATTERN)))
            if not paths:
                raise CommandError(
                    f"Aucun classeur « {GLOB_PATTERN} » dans {directory}.")
            self.stdout.write(f"Dossier : {directory}")
            for path in paths:
                self._ingest_one(path)

        # ── Vérification du chargement du référentiel (lecture seule) ──────────
        # Le loader construit un référentiel PAR UNITÉ DE RÉFÉRENCE, lue dans le
        # classeur lui-même : hectares pour les cultures, ruches pour l'apiculture,
        # sujets pour l'élevage, m² pour la bioconversion, sacs pour la myciculture,
        # tonnes usinées pour la transformation. Il ne refuse plus que ce qu'il ne
        # sait PAS lire — une dimension de référence absente ou non nommée — et ne
        # l'invente jamais (principe 1).
        self.stdout.write("")
        self.stdout.write("Vérification du chargement du référentiel "
                          "(par unité de référence) :")
        lus = non_couverts = 0
        unites: dict[str, int] = {}
        for source in simulateurs_disponibles():
            try:
                spec = charger_depuis_simulateur(source)
            except ReferentielIntrouvable as exc:
                non_couverts += 1
                # ASCII only : ce texte est écrit sur la console (Windows cp1252),
                # qui n'encode pas les glyphes fantaisie — un rapport ne plante pas.
                self.stdout.write(self.style.WARNING(
                    f"  [!]  {source.original_name} (rev {source.revision}) : {exc}"))
                continue
            lus += 1
            lignage = spec["_lignage"]
            unite = spec["unite_reference"]
            unites[unite] = unites.get(unite, 0) + 1
            modules = ", ".join(sorted(spec["couts_modules"]))
            self.stdout.write(self.style.SUCCESS(
                f"  [ok] {source.original_name} (rev {source.revision}) - "
                f"{lignage['totalCycleLu']} USD sur {lignage['quantiteReference']} "
                f"{unite} (« {lignage['libelleDimension']} ») ; modules : {modules}"))

        total = lus + non_couverts
        style = self.style.SUCCESS if non_couverts == 0 else self.style.WARNING
        repartition = ", ".join(f"{n} en {u}" for u, n in sorted(unites.items()))
        self.stdout.write(style(
            f"Simulateurs courants : {total} — {lus} referentiel(s) charge(s) "
            f"({repartition or 'aucune unite'}), {non_couverts} sans dimension de "
            f"reference lisible (signale, jamais invente)."))

    # ──────────────────────────────────────────────────────────────────────────
    def _ingest_one(self, path: str) -> None:
        name = os.path.basename(path)
        dataset_key = services.dataset_key(name)
        sha = services.file_sha256(path)

        courant = (DataSource.objects
                   .filter(dataset_key=dataset_key, status=STATUS_COMMITTED, is_current=True)
                   .order_by("-revision")
                   .first())
        if courant and courant.sha256 == sha:
            self.stdout.write(f"  = {name} : déjà ingéré (rev {courant.revision}), inchangé.")
            return

        src = DataSource(
            original_name=name,
            dataset_key=dataset_key,
            uploaded_by="cli:ingest_simulateurs",
        )
        with open(path, "rb") as fh:
            src.file.save(name, File(fh), save=False)
        src.save()

        preview = services.inspect(src)  # détecte le kind, calcule le SHA-256
        if src.kind != KIND_SIMULATEUR:
            # Ni ingestion « best effort » ni requalification silencieuse : on
            # refuse ce qui n'est pas un simulateur et on nettoie la source stagée.
            detected = src.kind
            src.file.delete(save=False)
            src.delete()
            self.stdout.write(self.style.WARNING(
                f"  ! {name} : détecté « {detected} », pas « {KIND_SIMULATEUR} » "
                f"({preview.get('n_tables')} feuille(s)) — ignoré."))
            return

        result = services.commit(src, by="cli:ingest_simulateurs")
        verbe = "créé (rev 1)" if not result.get("superseded") else \
            f"nouvelle révision {result['revision']} (précédente conservée)"
        self.stdout.write(self.style.SUCCESS(
            f"  + {name} : {verbe} — {result['tables']} table(s), "
            f"{result['records']} ligne(s)."))
