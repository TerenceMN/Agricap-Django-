"""
Ingestion des normes institutionnelles du simulateur de cycle de crédit (v4).

    python manage.py import_normes_v4 --check     # lecture seule, rien n'est écrit
    python manage.py import_normes_v4             # active la config lue
    python manage.py import_normes_v4 "chemin/vers/le.xlsx"

`--check` d'abord, toujours : cette commande remplace la configuration ACTIVE de
l'institution — les seuils DSCR, le score minimal et les cinq pondérations que
le moteur relit à chaque analyse. On regarde ce qui serait repris avant de le
reprendre.
"""
from __future__ import annotations

import os

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from referentiel.models import InstitutionConfig
from referentiel.normes_v4 import (
    ATTRS_POIDS,
    NOM_CLASSEUR,
    NormesIntrouvables,
    ingerer_normes,
    lire_normes,
)


class Command(BaseCommand):
    help = ("Lit les normes du simulateur de cycle de crédit v4 (seuils, frais, "
            "pondérations) et en fait la configuration active de l'institution.")

    def add_arguments(self, parser):
        parser.add_argument(
            "path", nargs="?", default=None,
            help=f"Chemin du classeur (défaut : DOCUMENT_EXCEL_DIR/« {NOM_CLASSEUR} »).")
        parser.add_argument(
            "--check", action="store_true",
            help="N'écrit rien : affiche ce qui serait repris et les écarts avec "
                 "la configuration active.")

    def handle(self, *args, **opts):
        path = opts["path"] or os.path.join(settings.DOCUMENT_EXCEL_DIR, NOM_CLASSEUR)
        if not os.path.exists(path):
            raise CommandError(f"Fichier introuvable : {path}")

        try:
            rapport = (lire_normes(path) if opts["check"]
                       else ingerer_normes(path, source_filename=os.path.basename(path)))
        except NormesIntrouvables as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write(f"Classeur : {path}")
        self.stdout.write(f"Onglets lus : {', '.join(rapport.onglets_lus) or 'aucun'}")
        for onglet in rapport.onglets_absents:
            self.stdout.write(self.style.WARNING(f"  ! onglet absent : {onglet}"))

        # Écart avec la configuration en vigueur — c'est la seule lecture utile :
        # une valeur reprise à l'identique n'est pas un changement, et noyer les
        # trois qui bougent dans douze qui ne bougent pas est ce qui fait rater
        # un recalibrage involontaire.
        courante = InstitutionConfig.active()
        self.stdout.write("")
        self.stdout.write("Paramètres repris :")
        for attr, valeur in sorted(rapport.valeurs.items()):
            avant = getattr(courante, attr, None)
            if avant is not None and avant == valeur:
                self.stdout.write(f"  = {attr:24s} {valeur}  (inchangé)")
            else:
                # « -> » et non « → » : la console Windows du poste de
                # l'institution est en cp1252, qui n'a pas U+2192. La commande
                # plantait sur sa propre sortie au premier paramètre modifié —
                # c'est-à-dire exactement quand elle avait quelque chose à dire.
                self.stdout.write(self.style.WARNING(
                    f"  ~ {attr:24s} {avant} -> {valeur}"))
        if not rapport.valeurs:
            self.stdout.write(self.style.WARNING("  (aucun — vérifiez les libellés du classeur)"))

        if not rapport.poids_appliques:
            self.stdout.write(self.style.WARNING(
                "  ! pondérations NON reprises — la grille actuelle "
                f"({', '.join(f'{a}={getattr(courante, a)}' for a in ATTRS_POIDS)}) "
                "reste en vigueur."))

        for message in rapport.avertissements:
            self.stdout.write(self.style.WARNING(f"  ! {message}"))

        self.stdout.write("")
        if opts["check"]:
            self.stdout.write(self.style.SUCCESS(
                "Lecture seule : la configuration active n'a pas été modifiée."))
        else:
            self.stdout.write(self.style.SUCCESS(
                f"Configuration #{rapport.config_id} activée "
                f"({len(rapport.valeurs)} paramètre(s) repris)."))
