"""`manage.py check_makuta` — la checklist à transmettre à Wolf Technologies.

Cette commande ne teste pas le réseau et n'appelle pas Makuta : elle confronte
`settings.MAKUTA` à ce dont `caisses/payments.py` a besoin, et imprime ce qui manque,
opération par opération. Elle ne devine AUCUNE valeur : un chemin absent reste absent, il
n'est jamais « supposé être /api/collect ».

Sortie : une checklist lisible par un non-développeur, terminée par les questions à poser
au fournisseur, formulées pour être copiées telles quelles dans un courriel.

    manage.py check_makuta                    # checklist complète
    manage.py check_makuta --format json      # même diagnostic, exploitable par un script
    manage.py check_makuta --questions-only   # uniquement les questions au fournisseur
    manage.py check_makuta --strict           # code de sortie 1 s'il reste un bloquant
"""
from __future__ import annotations

import json

from django.core.management.base import BaseCommand

from caisses import makuta_diagnostics as diagnostics
from caisses.console import wrap as _wrap

_WIDTH = 78
_MARKER_WIDTH = 13


class Command(BaseCommand):
    help = ("Valide la configuration Makuta et imprime, operation par operation, ce qui manque "
            "pour qu'un paiement puisse aboutir.")

    def add_arguments(self, parser):
        parser.add_argument("--format", choices=("text", "json"), default="text",
                            help="Format de sortie (defaut : text).")
        parser.add_argument("--questions-only", action="store_true",
                            help="N'imprime que les questions a poser au fournisseur.")
        parser.add_argument("--strict", action="store_true",
                            help="Code de sortie 1 s'il reste au moins un point bloquant "
                                 "(pour un controle de deploiement).")

    def handle(self, *args, **options):
        report = diagnostics.diagnose()

        if options["format"] == "json":
            self.stdout.write(json.dumps(report, ensure_ascii=False, indent=2))
        elif options["questions_only"]:
            self._render_questions(report)
        else:
            self._render(report)

        if options["strict"] and report["counts"]["blocking"]:
            # Pas de `CommandError` : le diagnostic a fonctionné, c'est la configuration qui
            # est incomplète. On le dit par le code de sortie, pas par une pile d'appels.
            raise SystemExit(1)

    # ---------------------------------------------------------------- rendu texte
    def _render(self, report: dict) -> None:
        write = self.stdout.write
        counts = report["counts"]

        write("=" * _WIDTH)
        write(" DIAGNOSTIC MAKUTA — ce qui manque pour que l'argent bouge reellement")
        write(" AGRICAP FINTECH · module caisses · aucun appel reseau n'a ete effectue")
        write("=" * _WIDTH)
        write("")
        write(" Legende :")
        write("   [OK]           fourni et exploitable en l'etat")
        write("   [MANQUE]       BLOQUANT : la fonction concernee ne peut pas s'executer")
        write("   [ECART]        fourni, mais incompatible avec ce que le connecteur sait faire")
        write("   [A CONFIRMER]  exploitable par defaut, mais non confirme par le fournisseur")
        write("")

        for section in report["sections"]:
            write("-" * _WIDTH)
            write(section["title"])
            write("-" * _WIDTH)
            for item in section["items"]:
                marker = f"[{item['state']}]".ljust(_MARKER_WIDTH)
                write(f"  {marker} {item['label']}")
                for line in _wrap(item["detail"], _WIDTH - _MARKER_WIDTH - 6):
                    write(f"  {' ' * _MARKER_WIDTH} {line}")
            write("")

        write("=" * _WIDTH)
        write(f" BILAN : {counts['ok']} point(s) OK · {counts['blocking']} bloquant(s) · "
              f"{counts['toConfirm']} a confirmer (sur {counts['total']}).")
        if report["operational"]:
            write(" Aucun point bloquant : les paiements peuvent etre emis. Les points")
            write(" « a confirmer » restent des risques ouverts, pas des arrets.")
        else:
            write(" Tant qu'un point BLOQUANT subsiste, AUCUN depot ni retrait externe ne peut")
            write(" aboutir. Le connecteur refuse franchement plutot que de deviner : c'est")
            write(" volontaire, et c'est ce qui evite de crediter un portefeuille sur une")
            write(" reponse qu'on ne sait pas lire.")
        write("=" * _WIDTH)
        write("")
        self._render_questions(report)

    def _render_questions(self, report: dict) -> None:
        write = self.stdout.write
        questions = report["questions"]
        write("A DEMANDER A WOLF TECHNOLOGIES (checklist a transmettre telle quelle)")
        write("-" * _WIDTH)
        if not questions:
            write("  Rien a demander : le contrat fournisseur est complet.")
            return
        for index, question in enumerate(questions, start=1):
            lines = _wrap(question, _WIDTH - 6)
            write(f"  {index:>2}. {lines[0]}")
            for line in lines[1:]:
                write(f"      {line}")
        write("")
        write("  Rappel de contexte : la documentation recue decrit l'authentification par")
        write("  signature RSA, et rien d'autre — ni catalogue d'operations, ni schema de")
        write("  requete ou de reponse, ni codes d'erreur, ni format de rappel entrant.")
