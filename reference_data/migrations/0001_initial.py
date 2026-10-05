from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    initial = True
    dependencies = []

    operations = [
        migrations.CreateModel(
            name="ReferenceFileUpload",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("file", models.FileField(upload_to="reference_data/")),
                ("file_type", models.CharField(
                    choices=[
                        ("value_chains", "Chaînes de valeur"),
                        ("suppliers", "Fournisseurs agréés"),
                        ("rates", "Grille de taux"),
                    ],
                    max_length=30,
                )),
                ("version", models.CharField(blank=True, max_length=50)),
                ("uploaded_by", models.CharField(max_length=255)),
                ("uploaded_at", models.DateTimeField(auto_now_add=True)),
                ("activated_by", models.CharField(blank=True, max_length=255)),
                ("activated_at", models.DateTimeField(blank=True, null=True)),
                ("status", models.CharField(
                    choices=[
                        ("pending_validation", "En attente d'activation"),
                        ("active", "Active"),
                        ("archived", "Archivée"),
                        ("rejected", "Rejetée"),
                    ],
                    default="pending_validation",
                    max_length=25,
                )),
                ("validation_report", models.JSONField(default=dict)),
                ("diff_summary", models.JSONField(default=dict)),
                ("row_count", models.IntegerField(default=0)),
            ],
            options={"ordering": ["-uploaded_at"]},
        ),
        migrations.CreateModel(
            name="ValueChain",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("code", models.CharField(db_index=True, max_length=50)),
                ("label", models.CharField(max_length=100)),
                ("active", models.BooleanField(default=True)),
                ("cycle_months", models.IntegerField()),
                ("cost_per_hectare_usd", models.DecimalField(decimal_places=2, max_digits=12)),
                ("cost_per_hectare_cdf", models.DecimalField(decimal_places=2, max_digits=14)),
                ("module_weights", models.JSONField()),
                ("risk_factor", models.DecimalField(decimal_places=3, max_digits=5)),
                ("min_score_required", models.IntegerField()),
                ("base_rate", models.DecimalField(decimal_places=2, max_digits=5)),
                ("harvest_months", models.JSONField(default=list)),
                ("eligible_guarantees", models.JSONField(default=list)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("source_file", models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name="value_chains",
                    to="reference_data.referencefileupload",
                )),
            ],
            options={"ordering": ["label"]},
        ),
    ]
