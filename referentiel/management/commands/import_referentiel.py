"""
Amorçage CLI de l'ingestion du référentiel (miroir de l'endpoint d'upload admin).

    python manage.py import_referentiel "../Document Excel/AGRICAP_REF_..._v3.xlsx"
    python manage.py import_referentiel            # défaut : DOCUMENT_EXCEL_DIR
"""
from __future__ import annotations

import os

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from referentiel.ingest import ingest_workbook

DEFAULT_NAME = "AGRICAP_REF_Referentiels_Technico_Economiques_v3.xlsx"


class Command(BaseCommand):
    help = "Importe un classeur de référentiels technico-économiques dans les tables."

    def add_arguments(self, parser):
        parser.add_argument("path", nargs="?", help="Chemin du .xlsx (défaut : DOCUMENT_EXCEL_DIR).")
        parser.add_argument("--label", default=None, help="Libellé de version.")

    def handle(self, *args, **opts):
        path = opts["path"] or os.path.join(settings.DOCUMENT_EXCEL_DIR, DEFAULT_NAME)
        if not os.path.exists(path):
            raise CommandError(f"Fichier introuvable : {path}")

        label = opts["label"] or os.path.splitext(os.path.basename(path))[0]
        report = ingest_workbook(path, label=label, source_filename=os.path.basename(path))

        self.stdout.write(self.style.SUCCESS(
            f"Version #{report.version_id} « {report.version_label} » — "
            f"{report.total_ranges} plages sur {len(report.ranges_by_chain)} chaînes."
        ))
        for code in sorted(report.ranges_by_chain):
            self.stdout.write(f"  chaîne {code}: {report.ranges_by_chain[code]} plages")
        self.stdout.write(f"Config institution : {'chargée' if report.config_loaded else 'défauts'}.")
        for w in report.warnings:
            self.stdout.write(self.style.WARNING(f"  ! {w}"))
