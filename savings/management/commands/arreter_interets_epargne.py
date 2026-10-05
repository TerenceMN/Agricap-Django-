"""Arrêté mensuel des intérêts d'épargne (principe 4 bis pour le sous-centime).

Idempotente : relancer sur une période déjà arrêtée ne double aucun intérêt (contrainte
d'unicité `(plan, période)`). C'est ce qui permet de la rejouer après un incident sans
avoir à vérifier d'abord ce qui est passé.

    python manage.py arreter_interets_epargne                 # période courante
    python manage.py arreter_interets_epargne --periode 2026-07
    python manage.py arreter_interets_epargne --simulation    # n'écrit rien
"""
from django.core.management.base import BaseCommand
from django.db import transaction

from savings import interest


class Command(BaseCommand):
    help = "Arrête les intérêts mensuels des plans d'épargne actifs."

    def add_arguments(self, parser):
        parser.add_argument("--periode", default=None, help="Période AAAA-MM (défaut : mois courant).")
        parser.add_argument("--acteur", default="", help="`sub` de l'acteur, pour le journal d'audit.")
        parser.add_argument("--simulation", action="store_true",
                            help="Calcule et affiche sans rien écrire (transaction annulée).")

    def handle(self, *args, **options):
        periode = options["periode"] or interest.periode_de()

        if options["simulation"]:
            # La simulation emprunte le VRAI chemin puis annule : simuler avec un second
            # code de calcul reviendrait à tester autre chose que ce qui s'exécutera.
            try:
                with transaction.atomic():
                    rapport = interest.arreter_tous(periode=periode, actor_sub=options["acteur"])
                    raise _Annulation
            except _Annulation:
                pass
            self.stdout.write(self.style.WARNING(f"SIMULATION (rien n'a été écrit) — {rapport}"))
            return

        rapport = interest.arreter_tous(periode=periode, actor_sub=options["acteur"])
        self.stdout.write(self.style.SUCCESS(
            f"Période {rapport['periode']} : {rapport['traites']} plan(s) arrêté(s), "
            f"{rapport['ignores']} ignoré(s). Total par devise : {rapport['parDevise']}"
        ))


class _Annulation(Exception):
    """Sentinelle d'annulation de la transaction de simulation."""
