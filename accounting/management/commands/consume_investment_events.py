"""Consomme la file des événements métier d'investissement et produit leurs écritures.

Pourquoi une COMMANDE et pas un signal : une écriture comptable doit être déclenchée à une
heure connue, sous une identité connue, et pouvoir être rejouée à l'identique. Un signal
`post_save` produirait des pièces au fil de l'eau, dans la transaction d'une vue métier,
sans trace de l'exécution ni possibilité de reprise après incident — et un échec comptable
ferait échouer l'opération métier, ce qui est exactement l'inverse du contrat de la file.

Exemples :

    manage.py consume_investment_events --simulation
    manage.py consume_investment_events --par "cron:compta" --limite 200
    manage.py consume_investment_events --jusqu-au 2026-07-31 --type SUBSCRIPTION_SETTLED
"""
from __future__ import annotations

from datetime import datetime

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from accounting import consommation
from accounting.definitions import SOURCE_INVESTISSEMENT


class Command(BaseCommand):
    help = (
        "Transforme les événements métier non consommés (investments.InvestmentEvent) en "
        "pièces comptables via le catalogue (annexe B), et les marque consommés."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--par", default="",
            help="Identité sous laquelle les pièces sont enregistrées et journalisées "
                 "(ex. « cron:compta »). Une consommation anonyme est refusée.",
        )
        parser.add_argument(
            "--limite", type=int, default=500,
            help="Nombre maximal d'événements traités dans ce passage (défaut : 500).",
        )
        parser.add_argument(
            "--jusqu-au", dest="jusqu_au", default="",
            help="N'examine que les événements survenus jusqu'à cette date incluse "
                 "(AAAA-MM-JJ) — utile pour ne pas consommer au-delà d'une période close.",
        )
        parser.add_argument(
            "--type", dest="types", action="append", default=[],
            help="Restreint à un type d'événement (répétable).",
        )
        parser.add_argument(
            "--source", default=SOURCE_INVESTISSEMENT,
            help="Source des événements (défaut : investments.InvestmentEvent).",
        )
        parser.add_argument(
            "--simulation", action="store_true",
            help="N'écrit RIEN : affiche les pièces qui seraient produites.",
        )

    def handle(self, *args, **options):
        jusqu_au = None
        if options["jusqu_au"]:
            try:
                jusqu_au = datetime.strptime(options["jusqu_au"], "%Y-%m-%d").date()
            except ValueError as exc:
                raise CommandError("--jusqu-au attend une date AAAA-MM-JJ.") from exc

        types = options["types"] or None
        source = options["source"]

        if options["simulation"]:
            return self._simuler(limite=options["limite"], jusqu_au=jusqu_au, types=types,
                                 source=source, verbosity=options.get("verbosity", 1))

        par = (options["par"] or "").strip()
        if not par:
            raise CommandError(
                "Une consommation d'événements s'exécute sous une identité connue : "
                "précisez --par (ex. « cron:compta »). Chaque pièce produite en porte la "
                "trace, et l'audit doit pouvoir désigner qui a déclenché le lot."
            )

        rapport = consommation.consommer_lot(
            par=par, limite=options["limite"], jusqu_au=jusqu_au, types=types, source=source,
        )
        self._afficher(rapport, verbosity=options.get("verbosity", 1))

    # ------------------------------------------------------------------ SIMULATION
    def _simuler(self, *, limite, jusqu_au, types, source, verbosity):
        """Le « à blanc » n'écrit rien — pas même les sous-comptes de cantonnement que la
        résolution ouvrirait : toute la simulation est annulée en sortie."""
        try:
            with transaction.atomic():
                rapport = consommation.simuler_lot(
                    limite=limite, jusqu_au=jusqu_au, types=types, source=source,
                )
                raise _AnnulationSimulation(rapport)
        except _AnnulationSimulation as annulation:
            rapport = annulation.rapport

        if not verbosity:
            return
        self.stdout.write(self.style.WARNING(
            f"SIMULATION — aucune écriture enregistrée. {rapport['examines']} événement(s) "
            f"en file ({rapport['source']})."
        ))
        for plan in rapport["plans"]:
            self.stdout.write(
                f"  #{plan['evenement_id']:<6} {plan['type']:<24} {plan['montant']} "
                f"{plan['devise']} → {plan['schema']} « {plan['reference']} »"
            )
            if plan["comptes"]:
                self.stdout.write(f"        comptes : {plan['comptes']}")
        self._afficher_ecarts(rapport)

    # --------------------------------------------------------------------- RAPPORT
    def _afficher(self, rapport: dict, *, verbosity: int) -> None:
        if not verbosity:
            return
        self.stdout.write(self.style.SUCCESS(
            f"{rapport['examines']} événement(s) examiné(s) — "
            f"{len(rapport['consommes'])} consommé(s), "
            f"{len(rapport['sans_ecriture'])} sans écriture définie, "
            f"{len(rapport['echecs'])} en échec. "
            f"{rapport['restant_en_file']} restant(s) en file."
        ))
        for entree in rapport["consommes"]:
            self.stdout.write(
                f"  #{entree['evenement_id']:<6} {entree['type']:<24} {entree['montant']} "
                f"{entree['devise']} → [{entree['journal']}] {entree['piece']} "
                f"({entree['schema']})"
            )
        self._afficher_ecarts(rapport)

    def _afficher_ecarts(self, rapport: dict) -> None:
        attente = rapport.get("montants_en_attente") or {}
        if any(attente.values()):
            chiffre = ", ".join(f"{m} {d}" for d, m in attente.items() if m)
            self.stdout.write(self.style.WARNING(
                f"\nÉCART CONNU DES ÉTATS FINANCIERS : {chiffre}.\n"
                "  C'est le montant des faits monétaires survenus qui ne sont PAS au grand "
                "livre. Une omission chiffrée — donc arbitrable — et non une approximation."
            ))
        if rapport["sans_ecriture"]:
            self.stdout.write(self.style.WARNING(
                "\nSANS ÉCRITURE DÉFINIE — ces événements RESTENT en file (on n'invente "
                "pas une écriture pour vider une file) :"
            ))
            for entree in rapport["sans_ecriture"]:
                self.stdout.write(
                    f"  #{entree['evenement_id']:<6} {entree['type']:<24} {entree['motif']}"
                )
        if rapport["echecs"]:
            self.stdout.write(self.style.ERROR(
                "\nÉCHECS — événements non consommés, repris au prochain passage :"
            ))
            for entree in rapport["echecs"]:
                self.stdout.write(
                    f"  #{entree['evenement_id']:<6} {entree['type']:<24} "
                    f"[{entree['classe']}] {entree['motif']}"
                )


class _AnnulationSimulation(Exception):
    """Rollback volontaire du mode simulation (le rapport voyage avec l'exception)."""

    def __init__(self, rapport: dict) -> None:
        super().__init__("simulation annulée")
        self.rapport = rapport
