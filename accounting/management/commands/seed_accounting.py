"""Chargement IDEMPOTENT du plan comptable (annexe A) et du catalogue d'écritures (annexe B).

Conforme au standard du projet : données de référence en commande de management avec
`update_or_create`, jamais en `RunPython` de migration — le paramétrage doit pouvoir être
rechargé après un ajustement du référentiel, sans nouvelle migration.

Le rechargement ne DÉTRUIT jamais : un compte retiré des définitions est désactivé
(`actif=False`) s'il est mouvementé, et seuls les comptes vierges hors référentiel peuvent
être supprimés — et uniquement avec `--purge-comptes-vierges`.
"""
from __future__ import annotations

from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction

from accounting.definitions import (
    CATALOGUE,
    CLASSES_RISQUE,
    PLAN_COMPTABLE,
    REGLES_CONSOMMATION,
    SOURCES_EVENEMENTS,
)
from accounting.models import (
    ClasseRisque,
    CompteComptable,
    EventEntryTemplate,
    EventEntryTemplateLine,
    RegleConsommation,
    SourceEvenements,
)


class Command(BaseCommand):
    help = "Charge (idempotent) le plan comptable et le catalogue d'écritures AGRICAP."

    def add_arguments(self, parser):
        parser.add_argument(
            "--purge-comptes-vierges", action="store_true",
            help="Supprime les comptes hors référentiel ET jamais mouvementés (sans effet "
                 "sur les comptes de cantonnement 419-OFF-*).",
        )

    @transaction.atomic
    def handle(self, *args, **options):
        crees, majs = self._charger_plan_comptable()
        templates, lignes = self._charger_catalogue()
        classes = self._charger_classes_risque()
        sources = self._charger_sources_evenements()
        regles = self._charger_regles_consommation()
        desactives = self._desactiver_comptes_obsoletes()
        supprimes = 0
        if options["purge_comptes_vierges"]:
            supprimes = self._purger_comptes_vierges()

        if options.get("verbosity", 1):
            self.stdout.write(self.style.SUCCESS(
                f"Plan comptable : {crees} compte(s) créé(s), {majs} mis à jour, "
                f"{desactives} désactivé(s), {supprimes} supprimé(s).\n"
                f"Catalogue : {templates} schéma(s), {lignes} ligne(s) de schéma.\n"
                f"Classes de risque : {classes} amorcée(s) (créées uniquement si absentes — "
                f"un taux ajusté par le comité n'est JAMAIS écrasé par un rechargement).\n"
                f"Sources d'événements : {sources} amorcée(s).\n"
                f"Règles de consommation d'événements : {regles} amorcée(s) (même règle : "
                f"un mapping ajusté n'est jamais remis d'usine)."
            ))

    # ------------------------------------------------------------------ PLAN COMPTABLE
    def _charger_plan_comptable(self) -> tuple[int, int]:
        crees = majs = 0
        parents: dict[str, CompteComptable] = {}

        for racine, intitule, classe, nature, devises, transitoire in PLAN_COMPTABLE:
            codes = [f"{racine}{d}" for d in devises] if devises else [racine]
            for index, code in enumerate(codes):
                devise = devises[index] if devises else ""
                compte, cree = CompteComptable.objects.update_or_create(
                    code=code,
                    defaults={
                        "racine": racine,
                        "intitule": intitule,
                        "classe": classe,
                        "nature": nature,
                        "devise": devise,
                        "est_transitoire": transitoire,
                        "actif": True,
                    },
                )
                parents[code] = compte
                crees += int(cree)
                majs += int(not cree)
        return crees, majs

    def _codes_du_referentiel(self) -> set[str]:
        codes: set[str] = set()
        for racine, _, _, _, devises, _ in PLAN_COMPTABLE:
            codes.update([f"{racine}{d}" for d in devises] if devises else [racine])
        return codes

    def _desactiver_comptes_obsoletes(self) -> int:
        """Un compte retiré du référentiel n'est jamais supprimé s'il porte des écritures :
        il est désactivé (plus postable, toujours consultable et auditable)."""
        connus = self._codes_du_referentiel()
        obsoletes = (
            CompteComptable.objects
            .filter(actif=True, cantonnement="")
            .exclude(code__in=connus)
        )
        compteur = 0
        for compte in obsoletes:
            compte.actif = False
            compte.save(update_fields=["actif"])
            compteur += 1
        return compteur

    def _purger_comptes_vierges(self) -> int:
        connus = self._codes_du_referentiel()
        candidats = (
            CompteComptable.objects
            .filter(cantonnement="", lignes__isnull=True, enfants__isnull=True)
            .exclude(code__in=connus)
        )
        compteur = 0
        for compte in candidats:
            compte.delete()  # la garde du modèle refuse tout compte mouvementé
            compteur += 1
        return compteur

    # ------------------------------------------------------------- CLASSES DE RISQUE
    def _charger_classes_risque(self) -> int:
        """`get_or_create`, PAS `update_or_create` : la grille PAR est un paramètre du
        comité (principe 8). Une fois la première amorce posée, un rechargement du
        référentiel ne doit surtout pas remettre les taux d'usine — ce serait une
        modification de provision silencieuse."""
        compteur = 0
        for code, libelle, jmin, jmax, taux, souffrance, ordre in CLASSES_RISQUE:
            _, cree = ClasseRisque.objects.get_or_create(
                code=code,
                defaults={
                    "libelle": libelle,
                    "jours_min": jmin,
                    "jours_max": jmax,
                    "taux_provision": Decimal(taux),
                    "en_souffrance": souffrance,
                    "ordre": ordre,
                    "actif": True,
                    "modifie_par": "seed_accounting",
                },
            )
            compteur += int(cree)
        return compteur

    # -------------------------------------------------- SOURCES D'ÉVÉNEMENTS
    def _charger_sources_evenements(self) -> int:
        """`get_or_create` : déclarer une file est une décision d'exploitation. Un préfixe
        de référence ajusté après coup ne doit surtout pas revenir d'usine — les pièces
        déjà émises portent l'ancien, et deux conventions de référence pour une même file
        rendraient la reprise après incident illisible."""
        compteur = 0
        for code, modele, libelle, prefixe, note in SOURCES_EVENEMENTS:
            _, cree = SourceEvenements.objects.get_or_create(
                code=code,
                defaults={
                    "modele": modele,
                    "libelle": libelle,
                    "prefixe_reference": prefixe,
                    "note": note,
                    "actif": True,
                    "modifie_par": "seed_accounting",
                },
            )
            compteur += int(cree)
        return compteur

    # ----------------------------------------------- RÈGLES DE CONSOMMATION
    def _charger_regles_consommation(self) -> int:
        """`get_or_create` (comme la grille PAR) : le mapping « événement → écriture » est
        un paramétrage comptable. Une fois qu'un comptable a pointé $TRESORERIE sur le bon
        compte, un rechargement du référentiel ne doit pas le ramener à la valeur d'usine —
        ce serait un changement d'imputation silencieux."""
        compteur = 0
        for source, type_evenement, mode, schema, origine, tresorerie, note in REGLES_CONSOMMATION:
            _, cree = RegleConsommation.objects.get_or_create(
                source=source,
                type_evenement=type_evenement,
                defaults={
                    "mode": mode,
                    "schema": schema,
                    "evenement_origine": origine,
                    "compte_tresorerie": tresorerie,
                    "note": note,
                    "actif": True,
                    "modifie_par": "seed_accounting",
                },
            )
            compteur += int(cree)
        return compteur

    # ---------------------------------------------------------------------- CATALOGUE
    def _charger_catalogue(self) -> tuple[int, int]:
        nb_templates = nb_lignes = 0
        for code, schema in CATALOGUE.items():
            template, _ = EventEntryTemplate.objects.update_or_create(
                code=code,
                defaults={
                    "libelle": schema["libelle"],
                    "journal": schema["journal"],
                    "description": schema.get("description", ""),
                    "actif": True,
                },
            )
            nb_templates += 1

            ordres_attendus = {ligne[0] for ligne in schema["lignes"]}
            # Les lignes retirées d'un schéma disparaissent du paramétrage (elles n'ont
            # jamais porté d'écriture : ce sont des gabarits, pas des données probantes).
            template.lignes.exclude(ordre__in=ordres_attendus).delete()

            for ordre, sens, compte_racine, devise_regle, montant_ref, condition in schema["lignes"]:
                EventEntryTemplateLine.objects.update_or_create(
                    template=template, ordre=ordre,
                    defaults={
                        "sens": sens,
                        "compte_racine": compte_racine,
                        "devise_regle": devise_regle,
                        "montant_ref": montant_ref,
                        "condition": condition,
                        "libelle": schema["libelle"],
                    },
                )
                nb_lignes += 1
        return nb_templates, nb_lignes
