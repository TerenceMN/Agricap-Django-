"""
Reprise d'historique des références de transaction.

    python manage.py backfill_references --check   # ne modifie rien
    python manage.py backfill_references

Pourquoi une commande et pas une `RunPython` dans la migration : la référence se
construit à partir de données RELATIONNELLES (agence du compte de trésorerie,
partenaire Mobile Money) qui peuvent être incomplètes au moment où la migration
s'applique. Une migration qui écrirait « HQ-NA » partout figerait cette
approximation dans l'historique, et une référence émise ne se réécrit pas.

La commande est IDEMPOTENTE : elle ne touche que les lignes dont `reference_id`
est vide. Relancée après avoir peuplé `Partner` ou rattaché les agences, elle
complète ce qui manquait sans réécrire ce qui existe.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db import transaction

from caisses.models import PaymentOrder, WalletMovement
from common.references import AGENCE_SIEGE, SEGMENT_ABSENT, reference_mouvement, reference_ordre


class Command(BaseCommand):
    help = ("Attribue une référence métier (AG-CD-…) aux mouvements de portefeuille "
            "et aux ordres de paiement qui n'en ont pas.")

    def add_arguments(self, parser):
        parser.add_argument("--check", action="store_true",
                            help="N'écrit rien : compte et montre un échantillon.")

    def handle(self, *args, **opts):
        mouvements = list(
            WalletMovement.objects
            .filter(reference_id__isnull=True)
            .prefetch_related("payment_order", "withdrawal_request")
        )
        ordres = list(
            PaymentOrder.objects
            .filter(reference_id__isnull=True)
            .select_related("treasury_account__agency", "treasury_account__partner")
        )

        self.stdout.write(f"Mouvements sans reference_id : {len(mouvements)}")
        self.stdout.write(f"Ordres de paiement sans reference_id : {len(ordres)}")

        calculees = [(m, reference_mouvement(m)) for m in mouvements]
        calculees += [(o, reference_ordre(o)) for o in ordres]

        if not calculees:
            self.stdout.write(self.style.SUCCESS("Rien à reprendre."))
            return

        self.stdout.write("")
        self.stdout.write("Echantillon :")
        for objet, reference in calculees[:10]:
            self.stdout.write(f"  {type(objet).__name__:16s} #{objet.pk:<6} -> {reference}")

        # Combien restent en codes explicites : c'est la mesure de ce qu'il reste
        # à configurer (agences rattachées, partenaires enregistrés), pas un échec.
        sans_agence = sum(1 for _, r in calculees if f"-{AGENCE_SIEGE}-" in r)
        sans_operateur = sum(1 for _, r in calculees if r.split("-")[-2] == SEGMENT_ABSENT)
        self.stdout.write("")
        self.stdout.write(
            f"{sans_agence} reference(s) en « {AGENCE_SIEGE} » (aucune agence rattachee) "
            f"et {sans_operateur} en « {SEGMENT_ABSENT} » (aucun operateur enregistre).")
        if sans_operateur:
            self.stdout.write(self.style.WARNING(
                "  Peuplez `partners.Partner` et rattachez-le aux comptes de tresorerie "
                "Mobile Money, puis relancez : la commande completera les lignes restees "
                "vides sans toucher a celles deja referencees."))

        if opts["check"]:
            self.stdout.write("")
            self.stdout.write(self.style.SUCCESS("Lecture seule : rien n'a ete ecrit."))
            return

        with transaction.atomic():
            for objet, reference in calculees:
                type(objet).objects.filter(pk=objet.pk).update(reference_id=reference)

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS(f"{len(calculees)} reference(s) attribuee(s)."))
