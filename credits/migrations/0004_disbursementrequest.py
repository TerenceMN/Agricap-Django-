from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("credits", "0003_creditapplication_submitted_by_sub"),
    ]

    operations = [
        migrations.CreateModel(
            name="DisbursementRequest",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("application", models.OneToOneField(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="disbursement_request",
                    to="credits.creditapplication",
                )),
                ("amount", models.DecimalField(decimal_places=2, max_digits=15)),
                ("currency", models.CharField(default="USD", max_length=3)),
                ("status", models.CharField(
                    choices=[
                        ("pending", "En attente de confirmation"),
                        ("confirmed", "Confirmé — décaissé"),
                        ("cancelled", "Annulé"),
                    ],
                    default="pending", max_length=10,
                )),
                ("requested_by_sub", models.CharField(max_length=255)),
                ("requested_at", models.DateTimeField(auto_now_add=True)),
                ("notes", models.TextField(blank=True)),
                ("confirmed_by_sub", models.CharField(blank=True, max_length=255)),
                ("confirmed_at", models.DateTimeField(blank=True, null=True)),
                ("loan_id", models.IntegerField(blank=True, null=True)),
                ("journal_entry_id", models.IntegerField(blank=True, null=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={"ordering": ["-requested_at"]},
        ),
    ]
