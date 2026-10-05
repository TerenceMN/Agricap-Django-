from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone
import credits.models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ("reference_data", "0001_initial"),
        ("accounts", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="NeedsSheet",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("uploaded_by", models.CharField(max_length=255)),
                ("uploaded_at", models.DateTimeField(auto_now_add=True)),
                ("raw_file", models.FileField(upload_to="credits/needs_sheets/")),
                ("value_chain", models.ForeignKey(
                    blank=True, null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name="needs_sheets",
                    to="reference_data.valuechain",
                )),
                ("area_ha", models.DecimalField(blank=True, decimal_places=2, max_digits=8, null=True)),
                ("currency", models.CharField(default="USD", max_length=3)),
                ("parsed_ok", models.BooleanField(default=False)),
                ("warnings", models.JSONField(default=list)),
                ("anomalies", models.JSONField(default=list)),
                ("total_by_module", models.JSONField(default=dict)),
                ("grand_total", models.DecimalField(decimal_places=2, default=0, max_digits=15)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={"ordering": ["-created_at"]},
        ),
        migrations.CreateModel(
            name="NeedItem",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("sheet", models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="items",
                    to="credits.needssheet",
                )),
                ("module", models.CharField(max_length=20, choices=[
                    ("semences", "Semences & Intrants"),
                    ("mecanisation", "Mécanisation"),
                    ("maindoeuvre", "Main-d'œuvre"),
                    ("equipements", "Équipements"),
                    ("postrecolte", "Post-récolte"),
                    ("logistique", "Logistique"),
                    ("commercialisation", "Commercialisation"),
                    ("reserve", "Réserve"),
                ])),
                ("label", models.CharField(max_length=300)),
                ("quantity", models.DecimalField(blank=True, decimal_places=3, max_digits=12, null=True)),
                ("unit", models.CharField(blank=True, max_length=60)),
                ("unit_price", models.DecimalField(blank=True, decimal_places=2, max_digits=14, null=True)),
                ("declared_total", models.DecimalField(blank=True, decimal_places=2, max_digits=14, null=True)),
                ("computed_total", models.DecimalField(blank=True, decimal_places=2, max_digits=14, null=True)),
                ("suggested_supplier", models.CharField(blank=True, max_length=200)),
                ("supplier_warning", models.CharField(blank=True, max_length=200)),
                ("source_sheet_index", models.IntegerField(default=2)),
            ],
            options={"ordering": ["module", "id"]},
        ),
        migrations.CreateModel(
            name="ScoringCriterion",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("code", models.CharField(max_length=50, unique=True)),
                ("label", models.CharField(max_length=200)),
                ("compute_method", models.CharField(max_length=40, choices=[
                    ("repayment_history", "Historique de remboursement"),
                    ("needs_coherence", "Cohérence besoins vs référentiel"),
                    ("debt_ratio", "Ratio endettement / capacité"),
                    ("kyc_seniority", "Ancienneté & KYC"),
                    ("sector_risk", "Risque filière"),
                ])),
                ("max_points", models.IntegerField()),
                ("weight", models.DecimalField(decimal_places=2, default=1, max_digits=5)),
                ("active", models.BooleanField(default=True)),
                ("config", models.JSONField(default=dict)),
                ("order", models.IntegerField(default=10)),
            ],
            options={"ordering": ["order"]},
        ),
        migrations.CreateModel(
            name="CreditApplication",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("code", models.CharField(default=credits.models._gen_code, max_length=40, unique=True)),
                ("client", models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name="credit_applications",
                    to="accounts.fintechuser",
                )),
                ("initiated_by_sub", models.CharField(blank=True, max_length=255)),
                ("value_chain", models.ForeignKey(
                    blank=True, null=True,
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name="applications",
                    to="reference_data.valuechain",
                )),
                ("reference_version", models.ForeignKey(
                    blank=True, null=True,
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name="credit_applications",
                    to="reference_data.referencefileupload",
                )),
                ("area_ha", models.DecimalField(blank=True, decimal_places=2, max_digits=8, null=True)),
                ("currency", models.CharField(
                    choices=[("CDF", "Franc Congolais"), ("USD", "Dollar US")],
                    default="USD", max_length=3,
                )),
                ("amount_requested", models.DecimalField(blank=True, decimal_places=2, max_digits=15, null=True)),
                ("amount_approved", models.DecimalField(blank=True, decimal_places=2, max_digits=15, null=True)),
                ("needs_sheet", models.OneToOneField(
                    blank=True, null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name="credit_application",
                    to="credits.needssheet",
                )),
                ("prefill_snapshot", models.JSONField(default=dict)),
                ("overridden_fields", models.JSONField(default=list)),
                ("status", models.CharField(
                    choices=[
                        ("draft", "Brouillon"),
                        ("submitted", "Soumise"),
                        ("in_analysis", "En analyse"),
                        ("approved", "Approuvée"),
                        ("rejected", "Rejetée"),
                        ("adjourned", "Ajournée"),
                        ("pending_disbursement", "En attente de décaissement"),
                        ("active", "Active (décaissée)"),
                        ("closed", "Clôturée"),
                    ],
                    default="draft", max_length=25,
                )),
                ("submitted_at", models.DateTimeField(blank=True, null=True)),
                ("score_result", models.JSONField(default=dict)),
                ("guarantee_type", models.CharField(blank=True, max_length=20)),
                ("reviewed_by_sub", models.CharField(blank=True, max_length=255)),
                ("reviewed_at", models.DateTimeField(blank=True, null=True)),
                ("approval_comment", models.TextField(blank=True)),
                ("rejection_reason_code", models.CharField(
                    blank=True, max_length=30,
                    choices=[
                        ("score_insuffisant", "Score insuffisant"),
                        ("garantie", "Garantie insuffisante"),
                        ("endettement", "Taux d'endettement trop élevé"),
                        ("incoherences", "Incohérences dans le dossier"),
                        ("autre", "Autre"),
                    ],
                )),
                ("rejection_comment", models.TextField(blank=True)),
                ("disbursed_at", models.DateTimeField(blank=True, null=True)),
                ("disbursed_by_sub", models.CharField(blank=True, max_length=255)),
                ("disbursed_amount", models.DecimalField(blank=True, decimal_places=2, max_digits=15, null=True)),
                ("client_consent_at", models.DateTimeField(blank=True, null=True)),
                ("client_consent_method", models.CharField(blank=True, max_length=20)),
                ("client_consent_expires", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={"ordering": ["-created_at"]},
        ),
        migrations.CreateModel(
            name="ModuleAllocation",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("application", models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="module_allocations",
                    to="credits.creditapplication",
                )),
                ("module", models.CharField(max_length=20)),
                ("cost", models.DecimalField(decimal_places=2, max_digits=15)),
                ("financing_pct", models.DecimalField(decimal_places=2, max_digits=5)),
                ("amount_financed", models.DecimalField(decimal_places=2, max_digits=15)),
                ("source", models.CharField(default="needs_sheet", max_length=20)),
                ("overridden", models.BooleanField(default=False)),
            ],
            options={"unique_together": {("application", "module")}},
        ),
    ]
