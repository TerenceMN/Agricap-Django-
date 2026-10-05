"""Paramètre le branchement d'une file d'événements métier sur le moteur d'écritures.

C'est le pendant opérationnel du principe 8 : « le code exécute, le paramétrage décide ».
Sans cette commande, la promesse « brancher une file est un geste de configuration » serait
creuse — il n'existerait aucun chemin pour poser une `SourceEvenements` ou une
`RegleConsommation` en dehors d'un déploiement.

Trois gestes, et un seul écrit :

    # 1. Voir ce qui est branché aujourd'hui, et ce qui reste en souffrance de mapping
    manage.py parametrer_consommation etat

    # 2. Déclarer une file (le producteur donne le chemin du modèle)
    manage.py parametrer_consommation source --code credits.CreditEvent \\
        --prefixe CRE --libelle "File des événements de crédit (B1→B4)" --par "dg"

    # 3. Mapper un type d'événement sur un schéma de l'annexe B
    manage.py parametrer_consommation regle --source credits.CreditEvent \\
        --type LOAN_DISBURSED --schema B1 --tresorerie 511 --par "dg"

Toute écriture est journalisée (`audit`) : un changement d'imputation comptable est une
décision, elle doit porter un nom et une date. Et rien n'est jamais SUPPRIMÉ : une règle
qu'on ne veut plus appliquer se désactive (`--desactiver`), ce qui laisse ses événements
s'accumuler en file — visibles — au lieu de les écrire de travers.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from accounting.models import (
    EventEntryTemplate,
    RegleConsommation,
    SourceEvenements,
)


class Command(BaseCommand):
    help = "Déclare une file d'événements métier ou mappe un événement sur un schéma (annexe B)."

    def add_arguments(self, parser):
        sous = parser.add_subparsers(dest="geste", required=True)

        etat = sous.add_parser("etat", help="Affiche le paramétrage courant.")
        etat.add_argument("--source", default="", help="Restreint à une file.")

        source = sous.add_parser("source", help="Déclare ou ajuste une file d'événements.")
        source.add_argument("--code", required=True,
                            help="Identifiant de la file (« app_label.ModelName »).")
        source.add_argument("--modele", default="",
                            help="Modèle réellement lu, si différent du code.")
        source.add_argument("--prefixe", default="",
                            help="Préfixe des références de pièce (INV, CRE, EPA…).")
        source.add_argument("--libelle", default="")
        source.add_argument("--note", default="")
        source.add_argument("--desactiver", action="store_true")
        source.add_argument("--activer", action="store_true")
        source.add_argument("--par", default="", help="Identité de l'auteur du changement.")

        regle = sous.add_parser("regle", help="Mappe un type d'événement sur un schéma.")
        regle.add_argument("--source", required=True)
        regle.add_argument("--type", dest="type_evenement", required=True)
        regle.add_argument("--mode", default=RegleConsommation.Mode.PIECE,
                           choices=RegleConsommation.Mode.values)
        regle.add_argument("--schema", default="", help="Code du catalogue (B1, B8…).")
        regle.add_argument("--origine", default="",
                           help="Mode CONTREPASSATION : type de l'événement à annuler.")
        regle.add_argument("--tresorerie", default="",
                           help="Racine du compte résolvant $TRESORERIE (501/511/53x).")
        regle.add_argument("--note", default="")
        regle.add_argument("--desactiver", action="store_true")
        regle.add_argument("--activer", action="store_true")
        regle.add_argument("--par", default="", help="Identité de l'auteur du changement.")

    def handle(self, *args, **options):
        geste = options["geste"]
        if geste == "etat":
            return self._etat(options["source"], verbosity=options.get("verbosity", 1))
        par = (options["par"] or "").strip()
        if not par:
            raise CommandError(
                "Un changement d'imputation comptable s'exécute sous une identité connue : "
                "précisez --par. La trace fait partie de la décision."
            )
        if geste == "source":
            return self._source(options, par=par)
        return self._regle(options, par=par)

    # ------------------------------------------------------------------- ÉTAT
    def _etat(self, source: str, *, verbosity: int) -> None:
        if not verbosity:
            return
        sources = SourceEvenements.objects.all()
        if source:
            sources = sources.filter(code=source)
        if not sources:
            self.stdout.write(self.style.WARNING(
                "Aucune file déclarée : la comptabilité ne lit aucun événement métier."
            ))
        for declaration in sources:
            etat = "active" if declaration.actif else "DÉSACTIVÉE"
            self.stdout.write(self.style.SUCCESS(
                f"\n{declaration.code}  [{etat}]  préfixe « {declaration.prefixe_reference} »"
                f"  → modèle {declaration.chemin_modele}"
            ))
            regles = RegleConsommation.objects.filter(source=declaration.code)
            if not regles:
                self.stdout.write(
                    "    (aucune règle : tout événement de cette file resterait en attente)"
                )
            for regle in regles:
                marque = " " if regle.actif else "✗"
                cible = regle.schema or regle.evenement_origine or "—"
                tresorerie = f" via {regle.compte_tresorerie}" if regle.compte_tresorerie else ""
                self.stdout.write(
                    f"  {marque} {regle.type_evenement:<28} {regle.mode:<16} {cible}{tresorerie}"
                )

        orphelines = RegleConsommation.objects.exclude(
            source__in=SourceEvenements.objects.values_list("code", flat=True)
        )
        if orphelines:
            self.stdout.write(self.style.ERROR(
                "\nRÈGLES SANS FILE DÉCLARÉE — elles ne s'appliqueront jamais :"
            ))
            for regle in orphelines:
                self.stdout.write(f"    {regle.source} / {regle.type_evenement}")

    # ------------------------------------------------------------------ SOURCE
    @transaction.atomic
    def _source(self, options: dict, *, par: str) -> None:
        code = options["code"].strip()
        declaration = SourceEvenements.objects.filter(code=code).first()
        if declaration is None and not options["prefixe"]:
            raise CommandError(
                "Une file se déclare avec son préfixe de référence (--prefixe) : c'est lui "
                "qui rend les références de pièce uniques entre files."
            )

        champs = self._champs_modifies(options, {
            "modele": "modele", "libelle": "libelle", "note": "note",
            "prefixe": "prefixe_reference",
        })
        if options["desactiver"]:
            champs["actif"] = False
        if options["activer"]:
            champs["actif"] = True

        if declaration is None:
            declaration = SourceEvenements.objects.create(
                code=code, modifie_par=par, **champs,
            )
            verbe = "déclarée"
        else:
            for nom, valeur in champs.items():
                setattr(declaration, nom, valeur)
            declaration.modifie_par = par
            declaration.save()
            verbe = "ajustée"

        self._journaliser("accounting.source_evenements_parametree", declaration.code, champs,
                          par=par)
        self.stdout.write(self.style.SUCCESS(
            f"File « {declaration.code} » {verbe} (préfixe « "
            f"{declaration.prefixe_reference} », modèle {declaration.chemin_modele})."
        ))
        self._avertir_modele_absent(declaration)

    def _avertir_modele_absent(self, declaration: SourceEvenements) -> None:
        """Une file déclarée sur un modèle qui n'existe pas encore n'est pas une erreur —
        le producteur peut livrer après. Mais elle ne doit pas passer pour branchée."""
        from django.apps import apps

        try:
            apps.get_model(declaration.chemin_modele)
        except (LookupError, ValueError):
            self.stdout.write(self.style.WARNING(
                f"  Le modèle « {declaration.chemin_modele} » n'existe pas encore : la file "
                "est déclarée mais rien n'en sera lu tant que le producteur ne l'aura pas "
                "livrée. Ce n'est pas un branchement, c'est une intention."
            ))

    # ------------------------------------------------------------------- RÈGLE
    @transaction.atomic
    def _regle(self, options: dict, *, par: str) -> None:
        source = options["source"].strip()
        if not SourceEvenements.objects.filter(code=source).exists():
            raise CommandError(
                f"La file « {source} » n'est pas déclarée : une règle qui la vise ne "
                "s'appliquerait jamais. Déclarez-la d'abord "
                "(« parametrer_consommation source »)."
            )
        mode = options["mode"]
        schema = options["schema"].strip()
        origine = options["origine"].strip()

        if mode == RegleConsommation.Mode.PIECE and not schema:
            raise CommandError("Le mode PIECE exige un schéma du catalogue (--schema).")
        if mode == RegleConsommation.Mode.CONTREPASSATION and not origine:
            raise CommandError(
                "Le mode CONTREPASSATION exige le type de l'événement d'origine "
                "(--origine) : on annule une pièce précise, jamais « la dernière »."
            )
        if schema and not EventEntryTemplate.objects.filter(code=schema, actif=True).exists():
            raise CommandError(
                f"Le schéma « {schema} » est absent du catalogue ou inactif. Chargez-le "
                "(« seed_accounting ») ou corrigez le code : une règle qui désigne un "
                "schéma inexistant est une écriture qui n'arrivera jamais."
            )

        champs = self._champs_modifies(options, {
            "mode": "mode", "schema": "schema", "origine": "evenement_origine",
            "tresorerie": "compte_tresorerie", "note": "note",
        })
        if options["desactiver"]:
            champs["actif"] = False
        if options["activer"]:
            champs["actif"] = True

        regle, cree = RegleConsommation.objects.get_or_create(
            source=source, type_evenement=options["type_evenement"],
            defaults={**champs, "modifie_par": par},
        )
        if not cree:
            for nom, valeur in champs.items():
                setattr(regle, nom, valeur)
            regle.modifie_par = par
            regle.save()

        self._journaliser("accounting.regle_consommation_parametree",
                          f"{source}/{regle.type_evenement}", champs, par=par)
        self.stdout.write(self.style.SUCCESS(
            f"Règle « {regle.type_evenement} » {'posée' if cree else 'ajustée'} sur "
            f"{source} : {regle.mode} {regle.schema or regle.evenement_origine or '—'}."
        ))

    # ------------------------------------------------------------------ OUTILS
    @staticmethod
    def _champs_modifies(options: dict, correspondance: dict[str, str]) -> dict:
        """N'écrit QUE ce que l'appelant a explicitement passé : une option absente laisse
        la valeur en base intacte, plutôt que de la remettre à vide."""
        return {
            cible: options[option]
            for option, cible in correspondance.items()
            if options.get(option) not in (None, "")
        }

    @staticmethod
    def _journaliser(action: str, entite: str, champs: dict, *, par: str) -> None:
        try:
            from audit.services import record as audit_record
        except Exception:  # pragma: no cover - l'app audit est toujours installée
            return
        audit_record(
            actor=par, action=action, entity_type="accounting.parametrage",
            entity_id=entite, details={nom: str(valeur) for nom, valeur in champs.items()},
        )
