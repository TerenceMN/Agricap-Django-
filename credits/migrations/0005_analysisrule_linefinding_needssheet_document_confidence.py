from django.db import migrations, models
import django.db.models.deletion


def _seed_rules(apps, schema_editor):
    AnalysisRule = apps.get_model("credits", "AnalysisRule")
    rules = [
        {
            "rule_id": "LIGNE_INCOMPLETE",
            "name": "Ligne incomplète",
            "description": "Prix unitaire et montant total tous deux absents sur une ligne de détail.",
            "severity_default": "info",
            "thresholds": {},
        },
        {
            "rule_id": "PRIX_COMPETITIF",
            "name": "Prix unitaire conforme au référentiel",
            "description": "Le prix déclaré est dans la fourchette ±15 % du référentiel filière.",
            "severity_default": "point_fort",
            "thresholds": {"max_deviation_pct": 15},
        },
        {
            "rule_id": "PRIX_UNITAIRE_HORS_FOURCHETTE",
            "name": "Prix unitaire hors fourchette",
            "description": "Le prix unitaire dévie significativement du référentiel filière.",
            "severity_default": "anomalie",
            "thresholds": {"a_justifier_pct": 40, "anomalie_pct": 80, "bloquant_pct": 200},
        },
        {
            "rule_id": "RUBRIQUE_INCOHERENTE",
            "name": "Rubrique sémantiquement incohérente",
            "description": "Le libellé contient des mots-clés d'un autre module (ex. motoculteur en Semences).",
            "severity_default": "a_justifier",
            "thresholds": {},
        },
        {
            "rule_id": "FOURNISSEUR_NON_AGREE",
            "name": "Fournisseur non agréé",
            "description": "Le fournisseur suggéré n'est pas dans l'annuaire des fournisseurs agréés AGRICAP.",
            "severity_default": "a_justifier",
            "thresholds": {},
        },
        {
            "rule_id": "TOTAL_FALSIFIE",
            "name": "Total Synthèse ≠ somme recalculée",
            "description": (
                "Le total de la feuille 5_Synthèse_Besoins diffère de la somme recalculée "
                "depuis les lignes de détail — signe possible de falsification des formules Excel."
            ),
            "severity_default": "bloquant",
            "thresholds": {"tolerance_pct": 5},
        },
        {
            "rule_id": "MODULE_HORS_POIDS",
            "name": "Module hors poids référentiel",
            "description": "La proportion d'un module dévie du poids standard de la filière.",
            "severity_default": "a_justifier",
            "thresholds": {"a_justifier_pct": 30, "anomalie_pct": 80, "min_expected_weight_pct": 5},
        },
        {
            "rule_id": "CONCENTRATION_MODULE",
            "name": "Concentration excessive sur un poste",
            "description": "Un poste représente > 60 % d'un module (avec plusieurs lignes dans ce module).",
            "severity_default": "info",
            "thresholds": {"concentration_threshold_pct": 60},
        },
    ]
    for r in rules:
        AnalysisRule.objects.get_or_create(rule_id=r["rule_id"], defaults=r)


class Migration(migrations.Migration):

    dependencies = [
        ("credits", "0004_disbursementrequest"),
    ]

    operations = [
        # ── 1. document_confidence sur NeedsSheet ─────────────────────────────
        migrations.AddField(
            model_name="needssheet",
            name="document_confidence",
            field=models.FloatField(blank=True, null=True),
        ),

        # ── 2. AnalysisRule ───────────────────────────────────────────────────
        migrations.CreateModel(
            name="AnalysisRule",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                ("rule_id", models.CharField(max_length=60, unique=True)),
                ("name", models.CharField(max_length=200)),
                ("description", models.TextField(blank=True)),
                ("severity_default", models.CharField(
                    choices=[
                        ("info", "Info"),
                        ("point_fort", "Point fort"),
                        ("a_justifier", "À justifier"),
                        ("anomalie", "Anomalie"),
                        ("bloquant", "Bloquant"),
                    ],
                    default="anomalie",
                    max_length=20,
                )),
                ("thresholds", models.JSONField(default=dict)),
                ("active", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={"ordering": ["rule_id"]},
        ),

        # ── 3. LineFinding ────────────────────────────────────────────────────
        migrations.CreateModel(
            name="LineFinding",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                ("needs_sheet", models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="findings",
                    to="credits.needssheet",
                )),
                ("rule", models.ForeignKey(
                    blank=True,
                    null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name="findings",
                    to="credits.analysisrule",
                )),
                ("rule_id_snapshot", models.CharField(max_length=60)),
                ("severity", models.CharField(
                    choices=[
                        ("info", "Info"),
                        ("point_fort", "Point fort"),
                        ("a_justifier", "À justifier"),
                        ("anomalie", "Anomalie"),
                        ("bloquant", "Bloquant"),
                    ],
                    max_length=20,
                )),
                ("source", models.JSONField(default=dict)),
                ("observed", models.JSONField(default=dict)),
                ("reference", models.JSONField(default=dict)),
                ("deviation", models.CharField(blank=True, max_length=30)),
                ("score_impact", models.JSONField(default=dict)),
                ("conclusion", models.TextField()),
                ("recommendation", models.TextField(blank=True)),
                ("analyst_status", models.CharField(
                    choices=[
                        ("a_traiter", "À traiter"),
                        ("justifie", "Justifié"),
                        ("corrige", "Corrigé"),
                        ("confirme_anomalie", "Anomalie confirmée"),
                    ],
                    default="a_traiter",
                    max_length=20,
                )),
                ("analyst_comment", models.TextField(blank=True)),
                ("analyst_updated_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={"ordering": ["created_at"]},
        ),

        # ── 4. Règles initiales ───────────────────────────────────────────────
        migrations.RunPython(
            code=_seed_rules,
            reverse_code=migrations.RunPython.noop,
        ),
    ]
