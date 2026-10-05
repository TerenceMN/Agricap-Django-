"""
Résorption de la dette du fichier statique `credits/static/credits/feuille_besoins_template.xlsx`
(CONVENTIONS.md principe 11) : migrer ce fichier vers le mécanisme de template versionné `dataio`.

    # 1) amorcer un template en attente (maker = "seed")
    python manage.py seed_feuille_besoins_template

    # 2) l'activer avec un checker ≠ maker (sinon le principe maker-checker le refuse)
    python manage.py seed_feuille_besoins_template --activate --maker seed --checker admin-2

Idempotent sur l'amorçage : si un template `pending` ou `active` de même SHA-256 existe
déjà, on ne recrée rien.
"""
from __future__ import annotations

import os

from django.core.files import File
from django.core.management.base import BaseCommand, CommandError

from dataio import services_templates as tpl_svc
from dataio.models import FileTemplate, KIND_FEUILLE_BESOINS
from dataio.services import file_sha256

#: Fichier statique historique (lecture seule — on ne modifie jamais `credits/**`).
DEFAULT_STATIC = os.path.join(
    os.path.dirname(__file__), "..", "..", "..",
    "credits", "static", "credits", "feuille_besoins_template.xlsx",
)


class Command(BaseCommand):
    help = ("Amorce un template de feuille de besoins (principe 11) depuis un .xlsx, "
            "en statut « pending ». Option --activate pour l'activer (checker ≠ maker).")

    def add_arguments(self, parser):
        parser.add_argument("--path", default=os.path.abspath(DEFAULT_STATIC),
                            help="Chemin du .xlsx source (défaut : fichier statique historique).")
        parser.add_argument("--kind", default=KIND_FEUILLE_BESOINS)
        parser.add_argument("--maker", default="seed", help="sub du maker (uploader).")
        parser.add_argument("--activate", action="store_true",
                            help="Active immédiatement le template amorcé.")
        parser.add_argument("--checker", default="",
                            help="sub du checker (obligatoirement ≠ maker si --activate).")

    def handle(self, *args, **opts):
        path = opts["path"]
        kind = opts["kind"]
        maker = opts["maker"]

        if not os.path.exists(path):
            raise CommandError(f"Fichier introuvable : {path}")

        sha = file_sha256(path)
        existing = FileTemplate.objects.filter(
            kind=kind, sha256=sha,
            status__in=[FileTemplate.Status.PENDING, FileTemplate.Status.ACTIVE],
        ).first()
        if existing:
            self.stdout.write(self.style.WARNING(
                f"Template identique déjà présent (id={existing.pk}, "
                f"statut={existing.status}) — rien à faire."))
            tpl = existing
        else:
            with open(path, "rb") as fh:
                django_file = File(fh, name=os.path.basename(path))
                tpl = tpl_svc.upload_template(django_file, kind=kind, uploaded_by=maker)
            self.stdout.write(self.style.SUCCESS(
                f"Template amorcé : id={tpl.pk}, kind={kind}, v{tpl.version}, "
                f"statut=pending, {len(tpl.schema.get('sheet_names', []))} feuille(s), "
                f"{len(tpl.schema.get('rubriques', []))} rubrique(s)."))

        if opts["activate"]:
            checker = opts["checker"]
            if not checker or checker == maker:
                raise CommandError(
                    "--activate exige --checker différent de --maker (principe maker-checker).")
            if tpl.status == FileTemplate.Status.ACTIVE:
                self.stdout.write(self.style.WARNING("Déjà actif — rien à activer."))
                return
            tpl_svc.activate_template(tpl, activator_sub=checker)
            self.stdout.write(self.style.SUCCESS(
                f"Template activé (checker={checker}). Le schéma dérivé est désormais la "
                f"règle de validation des fichiers client."))
        else:
            self.stdout.write(
                "Template en attente. Activez-le (checker ≠ maker) via "
                "POST /api/dataio/templates/<id>/activate ou "
                "--activate --checker <sub>.")
