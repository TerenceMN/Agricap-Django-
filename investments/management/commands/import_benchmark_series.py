"""Import d'une série de référence (BCC, INS…) — idempotent et versionné.

Pourquoi une COMMANDE et pas un formulaire : une série de référence est une donnée
d'un tiers. Ouvrir une saisie libre reviendrait à laisser quelqu'un taper le taux
directeur de la banque centrale ; le fichier importé, lui, se rejoue, se compare et
porte sa source.

Idempotence : relancer la commande sur le même fichier ne crée AUCUNE ligne. Un point
dont la valeur a changé chez la source donne une nouvelle VERSION à la même date,
l'ancienne restant lisible (append-only) — une correction de source est un fait à
conserver, pas une valeur à écraser.

Format du manifeste (JSON) :

    {
      "series": {"code": "BCC_TD", "label": "Taux directeur BCC",
                 "currency": "CDF", "frequency": "MONTHLY", "kind": "RATE",
                 "source": "Banque Centrale du Congo",
                 "licence": "Publication officielle, rediffusion autorisée"},
      "points": [{"date": "2026-01-31", "value": "0.0950"}, ...]
    }

L'ACTIVATION reste un acte distinct et humain (`--activer-par`), refusée si l'acteur
est celui qui a déclaré la série : maker ≠ checker sur tout référentiel.
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from common.exceptions import BusinessError
from investments import benchmarks
from investments.models import BenchmarkSeries


class Command(BaseCommand):
    help = ("Importe une série de référence et ses points depuis un manifeste JSON. "
            "Idempotent : relancer sur le même fichier n'écrit rien.")

    def add_arguments(self, parser):
        parser.add_argument("--fichier", required=True,
                            help="Chemin du manifeste JSON (série + points).")
        parser.add_argument("--acteur", default="import:cli",
                            help="Identifiant de l'acteur qui déclare/importe (maker).")
        parser.add_argument("--activer-par", default="",
                            help="Identifiant du checker qui ACTIVE la série. "
                                 "Doit différer du déclarant.")
        parser.add_argument("--reference", default="",
                            help="Référence de la publication source (n° de bulletin, URL).")

    def handle(self, *args, **options):
        chemin = Path(options["fichier"])
        if not chemin.exists():
            raise CommandError(f"Fichier introuvable : {chemin}")
        try:
            manifeste = json.loads(chemin.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise CommandError(f"Manifeste JSON illisible : {exc}") from exc

        meta = manifeste.get("series") or {}
        code = (meta.get("code") or "").strip().upper()
        if not code:
            raise CommandError("Le manifeste doit porter `series.code`.")

        acteur = options["acteur"]
        serie = BenchmarkSeries.objects.filter(code=code).first()
        if serie is None:
            try:
                serie = benchmarks.declarer_serie(
                    code=code, label=meta.get("label", code),
                    currency=meta.get("currency", "USD"),
                    frequency=meta.get("frequency", BenchmarkSeries.Frequency.MONTHLY),
                    kind=meta.get("kind", BenchmarkSeries.Kind.RATE),
                    source=meta.get("source", ""), licence=meta.get("licence", ""),
                    notes=meta.get("notes", ""), by=acteur,
                )
            except BusinessError as exc:
                raise CommandError(str(exc)) from exc
            self.stdout.write(self.style.SUCCESS(f"Série {code} déclarée (brouillon)."))
        else:
            self.stdout.write(f"Série {code} déjà déclarée — statut « {serie.status} ».")

        points = []
        for brut in manifeste.get("points", []):
            try:
                points.append((date.fromisoformat(brut["date"]), brut["value"]))
            except (KeyError, TypeError, ValueError) as exc:
                raise CommandError(f"Point illisible dans le manifeste : {brut} ({exc})") from exc

        try:
            resume = benchmarks.importer_points(
                serie=serie, points=points, source_reference=options["reference"], by=acteur)
        except BusinessError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(
            f"Points : {resume['added']} ajouté(s), {resume['corrected']} corrigé(s) "
            f"(nouvelle version), {resume['unchanged']} inchangé(s)."
        )

        checker = options["activer_par"]
        if checker:
            try:
                benchmarks.activer_serie(code=code, by=checker)
            except BusinessError as exc:
                raise CommandError(f"Activation refusée : {exc}") from exc
            self.stdout.write(self.style.SUCCESS(f"Série {code} activée par {checker}."))
        elif serie.status != BenchmarkSeries.Status.ACTIVE:
            self.stdout.write(self.style.WARNING(
                f"Série {code} en brouillon : elle n'est servie à personne tant qu'un "
                "checker ne l'a pas activée (--activer-par)."))
