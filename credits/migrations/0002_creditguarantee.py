from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("credits", "0001_initial"),
        ("savings", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="CreditGuarantee",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("application", models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="guarantees",
                    to="credits.creditapplication",
                )),
                ("guarantee_type", models.CharField(max_length=10, choices=[
                    ("epargne", "Bloc d'épargne"),
                    ("morale", "Caution morale"),
                ])),
                ("status", models.CharField(max_length=10, default="pending", choices=[
                    ("pending", "En attente de confirmation"),
                    ("active", "Active"),
                    ("released", "Levée / libérée"),
                    ("expired", "Expirée (délai dépassé)"),
                ])),
                # Champs épargne
                ("savings_plan", models.ForeignKey(
                    blank=True, null=True,
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name="credit_holds",
                    to="savings.savingsplan",
                )),
                ("hold_amount", models.DecimalField(blank=True, decimal_places=2, max_digits=15, null=True)),
                ("hold_currency", models.CharField(default="USD", max_length=3)),
                ("hold_reference", models.CharField(blank=True, max_length=50)),
                ("hold_placed_at", models.DateTimeField(blank=True, null=True)),
                ("hold_released_at", models.DateTimeField(blank=True, null=True)),
                # Champs caution morale
                ("guarantor_sub", models.CharField(blank=True, max_length=255)),
                ("guarantor_name", models.CharField(blank=True, max_length=200)),
                ("guarantor_phone", models.CharField(blank=True, max_length=40)),
                ("guarantor_id_number", models.CharField(blank=True, max_length=80)),
                ("confirmed_by_sub", models.CharField(blank=True, max_length=255)),
                ("confirmed_at", models.DateTimeField(blank=True, null=True)),
                ("expires_at", models.DateTimeField(blank=True, null=True)),
                ("expiry_notified", models.BooleanField(default=False)),
                # Audit
                ("registered_by_sub", models.CharField(blank=True, max_length=255)),
                ("notes", models.TextField(blank=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={"ordering": ["-created_at"]},
        ),
    ]
