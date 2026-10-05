"""`manage.py reconcile_payment_orders` — relire chez le fournisseur, jamais décider.

La file `payments/indeterminate` existait déjà, mais sa relecture se faisait un ordre à la
fois, à la main. Cette commande la déroule : pour chaque ordre dont l'issue est inconnue
(`SENT`, `AWAITING_CONFIRMATION`, `INDETERMINATE`), elle relit le statut chez Makuta
(GET signé) et applique **ce que le fournisseur affirme** — rien de plus.

Ce qu'elle ne fait pas, et ne fera jamais :

* elle **ne réémet aucun paiement**. Aucun POST ne part d'ici. Réconcilier n'est pas
  rejouer ; confondre les deux fait payer deux fois ;
* elle **ne tranche rien**. Une réponse que le paramétrage ne sait pas lire laisse l'ordre
  `INDETERMINATE`, c'est-à-dire dans la file, c'est-à-dire pour un humain (principe 2).
  Le seul moyen de clore un ordre vraiment illisible reste `force_settle`, sur preuve
  externe, avec motif circonstancié et acteur nommé ;
* elle **ne crédite rien directement** : le crédit ne peut venir que de `CONFIRMED`.

Elle est idempotente (un ordre résolu quitte la file ; une confirmation rejouée est un
no-op) et journalisée : chaque relecture laisse un événement `PaymentOrderEvent` et une
trace d'audit avec son motif (principe 3).

    manage.py reconcile_payment_orders                       # toute la file
    manage.py reconcile_payment_orders --dry-run             # ce qui SERAIT relu
    manage.py reconcile_payment_orders --reference AGC-...   # un ordre precis
    manage.py reconcile_payment_orders --status INDETERMINATE --limit 50
    manage.py reconcile_payment_orders --by dg-1 --motive "Cloture de journee du 24/07."
"""
from __future__ import annotations

import json

from django.core.management.base import BaseCommand

from caisses import payments
from caisses.console import wrap as _wrap
from caisses.models import PaymentOrder

_WIDTH = 78

#: Libellés de sortie. Le code interne (`ReconciliationResult`) est stable et scriptable ;
#: ce que lit l'opérateur est en français simple.
_LABELS = {
    payments.ReconciliationResult.CONFIRMED:
        "CONFIRME par le fournisseur (portefeuille credite si encaissement)",
    payments.ReconciliationResult.REFUSED:
        "REFUSE par le fournisseur (fonds reserves rendus si decaissement)",
    payments.ReconciliationResult.AWAITING:
        "EN COURS chez le fournisseur — a relire plus tard",
    payments.ReconciliationResult.UNREADABLE_STATUS:
        "REPONSE ILLISIBLE — issue toujours inconnue, decision humaine requise",
    payments.ReconciliationResult.CONTRACT_MISSING:
        "NON RELISIBLE — contrat fournisseur incomplet (voir check_makuta)",
    payments.ReconciliationResult.UNREACHABLE:
        "FOURNISSEUR INJOIGNABLE — rien n'a change, a relancer",
    payments.ReconciliationResult.NOT_OPEN:
        "SANS OBJET — l'ordre n'attend aucune issue",
    payments.ReconciliationResult.NOT_FOUND:
        "REFERENCE INCONNUE",
    payments.ReconciliationResult.ERROR:
        "ERREUR — a examiner",
    payments.ReconciliationResult.WOULD_READ:
        "SERAIT RELU (--dry-run : rien n'a ete modifie)",
}

#: Ordre d'affichage du bilan : ce qui a bougé d'abord, ce qui appelle un humain ensuite.
_SUMMARY_ORDER = (
    payments.ReconciliationResult.CONFIRMED,
    payments.ReconciliationResult.REFUSED,
    payments.ReconciliationResult.AWAITING,
    payments.ReconciliationResult.UNREADABLE_STATUS,
    payments.ReconciliationResult.CONTRACT_MISSING,
    payments.ReconciliationResult.UNREACHABLE,
    payments.ReconciliationResult.NOT_OPEN,
    payments.ReconciliationResult.NOT_FOUND,
    payments.ReconciliationResult.ERROR,
    payments.ReconciliationResult.WOULD_READ,
)


class Command(BaseCommand):
    help = ("Relit chez Makuta le statut des ordres de paiement dont l'issue est inconnue et "
            "applique l'issue rapportee par le fournisseur. Ne reemet aucun paiement.")

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=500,
                            help="Nombre maximum d'ordres relus (defaut : 500).")
        parser.add_argument("--status", default="", choices=("", *PaymentOrder.OPEN_STATUSES),
                            help="Ne relire que les ordres dans ce statut.")
        parser.add_argument("--reference", action="append", default=[],
                            help="Relire un ordre precis (repetable).")
        parser.add_argument("--dry-run", action="store_true",
                            help="Liste ce qui serait relu, sans appel reseau ni ecriture.")
        parser.add_argument("--by", default="manage.reconcile_payment_orders",
                            help="Acteur inscrit au journal (defaut : nom de la commande).")
        parser.add_argument("--motive", default="",
                            help="Motif inscrit au journal ; un motif par defaut explicite est "
                                 "utilise si celui-ci est vide.")
        parser.add_argument("--format", choices=("text", "json"), default="text",
                            help="Format de sortie (defaut : text).")

    def handle(self, *args, **options):
        report = payments.reconcile_open_orders(
            limit=options["limit"], status=options["status"],
            references=options["reference"] or None, motive=options["motive"],
            by=options["by"], dry_run=options["dry_run"],
        )
        if options["format"] == "json":
            self.stdout.write(json.dumps(report, ensure_ascii=False, indent=2))
            return
        self._render(report)

    def _render(self, report: dict) -> None:
        write = self.stdout.write

        write("=" * _WIDTH)
        write(" RECONCILIATION DES ORDRES DE PAIEMENT")
        if report["dryRun"]:
            write(" MODE --dry-run : aucune lecture reseau, aucune ecriture.")
        write("=" * _WIDTH)

        if not report["configured"]:
            write("")
            for line in _wrap(report["degradation"], _WIDTH - 2):
                write(f" {line}")
            write("")
            write(f" File en attente d'issue : {report['scanned']} ordre(s), inchange(s).")
            write("=" * _WIDTH)
            return

        if not report["results"]:
            write("")
            write(" Aucun ordre en attente d'issue. Rien a reconcilier.")
            write("=" * _WIDTH)
            return

        write("")
        for row in report["results"]:
            transition = (row["before"] if row["before"] == row["after"]
                          else f"{row['before']} -> {row['after']}")
            write(f" {row['reference']}  [{transition}]")
            write(f"   {_LABELS.get(row['result'], row['result'])}")
            for line in _wrap(row["detail"], _WIDTH - 6):
                write(f"     {line}")
        write("")

        write("-" * _WIDTH)
        write(f" BILAN sur {report['scanned']} ordre(s) examine(s) :")
        for result in _SUMMARY_ORDER:
            count = report["totals"].get(result)
            if count:
                write(f"   {count:>4}  {_LABELS[result]}")
        write("-" * _WIDTH)

        left = (report["totals"].get(payments.ReconciliationResult.UNREADABLE_STATUS, 0)
                + report["totals"].get(payments.ReconciliationResult.CONTRACT_MISSING, 0))
        if left:
            write(f" {left} ordre(s) restent SANS ISSUE LISIBLE : le fournisseur ne dit rien que")
            write(" le parametrage sache interpreter. Ils ne seront JAMAIS resolus")
            write(" automatiquement — un humain les clot sur preuve externe (endpoint")
            write(" force-settle, motif circonstancie obligatoire).")
        write("=" * _WIDTH)
