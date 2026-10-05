"""
Amorçage des règles du moteur d'analyse — barèmes de score et référentiel filière.

    python manage.py seed_analyse

**Idempotente** (`update_or_create`) : rejouable à chaque déploiement sans créer
de doublon ni écraser un recalibrage du comité par accident — sauf demande
explicite `--force`, qui réécrit les courbes aux valeurs de la SPEC §5.

Pourquoi une commande et pas une fixture ni un `RunPython` :
  - une fixture `loaddata` écrase par PK et casserait un barème recalibré ;
  - un `RunPython` de migration figerait des seuils métier dans l'historique de
    schéma, alors que le principe 8 les veut modifiables par le comité sans
    redéploiement. Une migration crée des TABLES, pas des règles.
"""
from __future__ import annotations

import io

from django.core.management.base import BaseCommand
from django.db import transaction

from credits.apprentissage import N_MIN_DEFAUT, REGLE_APPRENTISSAGE
from credits.models import AnalysisRule, BaremeScore, ReferentielFiliere
from credits.needs_parser import REGLE_COHERENCE, SEUILS_COHERENCE_DEFAUT
from credits.referentiel_loader import (
    ReferentielIntrouvable, charger_depuis_simulateur, simulateurs_disponibles,
)

#: Seuils OPÉRATIONNELS — ceux qui ne sont ni une courbe ni une règle de
#: scoring, et qui n'ont donc rien à faire dans `BaremeScore` (dont la
#: machinerie de révision prévisualise l'impact sur le golden set : un effectif
#: minimal n'a aucun impact à prévisualiser). Ils vivaient en dur dans le code —
#: 1,30 / 0,70 / 1,8 / 5 % dans le parseur, 30 dans la boucle d'apprentissage —
#: ce que le principe 8 interdit. Les valeurs POSÉES ICI sont identiques à
#: celles qui étaient codées : on rend le paramètre éditable sans déplacer
#: aucune frontière.
REGLES_SEUILS = [
    {
        "rule_id": REGLE_COHERENCE,
        "name": "Cohérence de la feuille de besoins vs référentiel filière",
        "description": (
            "Déclenche quand le total du plan s'écarte du coût de référence de la "
            "filière, ou quand un module pèse anormalement lourd dans le budget. "
            "Les valeurs de référence ne sortent JAMAIS vers le client "
            "(principe 7) : il reçoit le sens de l'écart et l'action attendue."
        ),
        "severity_default": "a_justifier",
        "thresholds": {cle: str(valeur) for cle, valeur in SEUILS_COHERENCE_DEFAUT.items()},
    },
    {
        "rule_id": REGLE_APPRENTISSAGE,
        "name": "Boucle d'apprentissage — effectif minimal par filière",
        "description": (
            "Nombre de dossiers clos et contributifs à partir duquel une filière "
            "devient CANDIDATE à un référentiel appris (principe 10). Franchir ce "
            "seuil ne bascule rien : la version apprise passe par un comité, en "
            "maker-checker."
        ),
        "severity_default": "info",
        "thresholds": {"n_min_cas_reels": str(N_MIN_DEFAUT)},
    },
]

#: Les trois barèmes de la SPEC §5, plus le barème de décision.
#: Abscisses et ordonnées en CHAÎNES : un JSON `float` ferait rentrer le binaire
#: flottant dans le calcul de score par la porte de la base (principe 4).
BAREMES = [
    {
        "code": "DSCR",
        "libelle": "Capacité financière — DSCR → score",
        "points": [
            {"x": "0.4", "y": "0"}, {"x": "0.7", "y": "25"},
            {"x": "1.0", "y": "50"}, {"x": "1.3", "y": "85"},
            {"x": "1.5", "y": "100"},
        ],
        "parametres": {},
    },
    {
        "code": "ECART_TECHNIQUE",
        "libelle": "Fiabilité technique — écart moyen au référentiel → score",
        "points": [
            {"x": "0.00", "y": "100"}, {"x": "0.15", "y": "85"},
            {"x": "0.30", "y": "60"}, {"x": "0.50", "y": "30"},
            {"x": "0.80", "y": "0"},
        ],
        "parametres": {},
    },
    {
        "code": "COUVERTURE_GARANTIES",
        "libelle": "Garanties — ratio de couverture après décote → score",
        "points": [
            {"x": "0.0", "y": "0"}, {"x": "0.5", "y": "40"},
            {"x": "1.0", "y": "75"}, {"x": "1.5", "y": "100"},
        ],
        # Plafond appliqué tant que les garanties ne sont pas constituées
        # (SPEC §4 : « score indicatif »). En base, donc recalibrable.
        "parametres": {"plafond_non_constituees": "60"},
    },
    {
        # AJOUT à la SPEC, assumé : son pseudo-code portait ces seuils en dur
        # (`>= 75`, `>= 60`, `>= 45`, `CHOC_STRESS = 0.25`), ce que le principe 8
        # interdit. `points` reste vide : ce barème n'est pas une courbe.
        "code": "DECISION",
        "libelle": "Barème de décision à 4 niveaux + choc du stress test",
        "points": [],
        "parametres": {
            "approbation": {"score_min": "75", "dscr_min": "1.2",
                            "sans_hors_plage": True},
            "approbation_cond": {"score_min": "60", "dscr_min": "1.0"},
            "revue": {"score_min": "45"},
            "choc_revenus": "0.25",
            # Grille lettre (SPEC §6). Elle vivait recopiée à la main dans trois
            # fichiers du front : un barème dans le navigateur, que le comité ne
            # pouvait pas recalibrer et qui apprenait au client où sont les
            # frontières (principes 7 et 8). Elle vit ici, le serveur seul
            # l'applique et ne sert que la lettre.
            "lettres": [{"lettre": "A", "min": "85"}, {"lettre": "B", "min": "70"},
                        {"lettre": "C", "min": "50"}, {"lettre": "D", "min": "0"}],
        },
    },
    {
        # GRILLE DE TARIFICATION UNIQUE — elle vivait en dur et en DOUBLE :
        # `scoring._propose_rate` surcotait de +2,5 sur la bande [55, 70[ quand
        # `dataio_simulator` surcotait de +2,0 sur la même bande. Deux taux pour un
        # même client selon l'écran (20,5 % contre 20,0 % sur une base de 18 %).
        #
        # Arbitrage : +2,0 — la valeur déjà annoncée au client par le simulateur,
        # et la seule qui rende la grille symétrique du bonus de −2,0. Le comité
        # qui veut 2,5 le décide désormais par révision de barème (maker ≠ checker,
        # impact prévisualisé sur le golden set), sans redéploiement.
        "code": "TAUX",
        "libelle": "Grille de tarification — bande de score → ajustement du taux",
        "points": [],
        "parametres": {
            "grille": [
                {"score_min": "85", "ajustement": "-2.0",
                 "libelle": "Excellent — bonification"},
                {"score_min": "70", "ajustement": "0.0",
                 "libelle": "Solide — taux de base"},
                {"score_min": "55", "ajustement": "2.0",
                 "libelle": "Recevable — surcote de risque"},
                {"score_min": "0", "ajustement": "5.0",
                 "libelle": "Limite — surcote maximale"},
            ],
            "plancher_ratio_base": "0.7",
        },
    },
]

#: Les référentiels filière ne sont PLUS écrits ici.
#:
#: Ils étaient renseignés à la main, avec des coûts par module répartis pour
#: retomber sur le total du classeur maïs (9 111 USD). Le total tombait juste,
#: la répartition non : 850 USD/ha de semences là où le classeur en donne
#: 126,60 — un facteur 6,7 sur un poste du critère de fiabilité technique,
#: soit 25 % du score.
#:
#: Ils sont désormais lus dans les simulateurs ingérés (`credits.referentiel_loader`).
#: Principe 1 : ce qui sert à scorer vient de la base, jamais d'une estimation.

class Command(BaseCommand):
    help = ("Crée ou met à jour les barèmes de score et le référentiel filière du "
            "moteur d'analyse (idempotent).")

    def add_arguments(self, parser):
        parser.add_argument(
            "--force", action="store_true",
            help=("Réécrit les courbes et paramètres même s'ils ont été modifiés "
                  "en base (écrase un recalibrage du comité)."),
        )

    @transaction.atomic
    def handle(self, *args, **opts):
        force: bool = opts["force"]
        # Silencieuse en verbosity=0 : la commande est appelee dans les tests.
        if int(opts.get("verbosity", 1)) == 0:
            self.stdout = io.StringIO()

        for spec in BAREMES:
            existant = BaremeScore.objects.filter(code=spec["code"]).first()
            if existant and not force:
                # On réactive sans toucher aux valeurs : un barème désactivé par
                # erreur doit pouvoir être relevé sans perdre son calibrage.
                if not existant.actif:
                    existant.actif = True
                    existant.save(update_fields=["actif"])
                    self.stdout.write(f"  = {spec['code']} : réactivé (valeurs conservées)")
                else:
                    self.stdout.write(f"  = {spec['code']} : déjà présent, inchangé")
                continue

            obj, cree = BaremeScore.objects.update_or_create(
                code=spec["code"],
                defaults={
                    "libelle": spec["libelle"],
                    "points": spec["points"],
                    "parametres": spec["parametres"],
                    "actif": True,
                    "version": (existant.version + 1) if existant else 1,
                },
            )
            verbe = "créé" if cree else "réécrit (--force)"
            self.stdout.write(self.style.SUCCESS(f"  + {obj.code} : {verbe} v{obj.version}"))

        for spec in REGLES_SEUILS:
            existante = AnalysisRule.objects.filter(rule_id=spec["rule_id"]).first()
            if existante and not force:
                # Même parti pris que les barèmes : on relève une règle
                # désactivée sans toucher aux seuils que le comité a réglés.
                if not existante.active:
                    existante.active = True
                    existante.save(update_fields=["active"])
                    self.stdout.write(
                        f"  = {spec['rule_id']} : réactivée (seuils conservés)")
                else:
                    self.stdout.write(f"  = {spec['rule_id']} : déjà présente, inchangée")
                continue
            obj, cree = AnalysisRule.objects.update_or_create(
                rule_id=spec["rule_id"],
                defaults={k: v for k, v in spec.items() if k != "rule_id"},
            )
            verbe = "créée" if cree else "réécrite (--force)"
            self.stdout.write(self.style.SUCCESS(
                f"  + {obj.rule_id} : {verbe} — {obj.thresholds}"))

        for source in simulateurs_disponibles():
            try:
                spec = charger_depuis_simulateur(source)
            except ReferentielIntrouvable as exc:
                # On n'invente pas ce qui manque : un référentiel partiellement
                # deviné score sans le dire. On saute et on le signale.
                self.stdout.write(self.style.WARNING(f"  ! {source.original_name} : {exc}"))
                continue
            lignage = spec.pop("_lignage")
            existant = ReferentielFiliere.objects.filter(code=spec["code"]).first()
            if existant and not force:
                self.stdout.write(f"  = {spec['code']} : déjà présent, inchangé")
                continue
            obj, cree = ReferentielFiliere.objects.update_or_create(
                code=spec["code"],
                defaults={**{k: v for k, v in spec.items() if k != "code"},
                          "actif": True,
                          "version": (existant.version + 1) if existant else 1},
            )
            verbe = "créé" if cree else "réécrit (--force)"
            self.stdout.write(self.style.SUCCESS(
                f"  + {obj.code} : {verbe} v{obj.version} — "
                f"{lignage['totalCycleLu']} USD sur {lignage['quantiteReference']} "
                f"{lignage['uniteReference']} "
                f"(source dataio #{lignage['dataSourceId']} rev {lignage['revision']})"))
            self._alerter_couverture(lignage)

    def _alerter_couverture(self, lignage: dict) -> None:
        """Une référence amputée se dit à l'écran, pas seulement dans un log.

        Le référentiel est écrit quand même — bloquer priverait toute une
        filière d'instruction — mais l'opérateur qui lance l'amorçage voit
        immédiatement quels coûts n'ont trouvé aucun module, et combien.
        """
        couverture = (lignage or {}).get("couvertureRubriques") or {}
        non_reconnues = couverture.get("nonReconnues") or []
        if not non_reconnues:
            return
        self.stdout.write(self.style.WARNING(
            f"    ! {couverture['totalNonReconnu']} USD non classés "
            f"({couverture['partNonReconnuePct']} % du classeur) — "
            f"la référence de cette filière en est amputée :"))
        for entree in non_reconnues:
            attente = " [arbitrage en attente]" if entree.get("arbitrageEnAttente") else ""
            self.stdout.write(self.style.WARNING(
                f"        · « {entree['rubrique']} » {entree['montant']} USD{attente}"))

        self.stdout.write(self.style.SUCCESS(
            f"Moteur d'analyse : {BaremeScore.objects.filter(actif=True).count()} barème(s) "
            f"actif(s), {ReferentielFiliere.objects.filter(actif=True).count()} référentiel(s) "
            f"actif(s)."))
