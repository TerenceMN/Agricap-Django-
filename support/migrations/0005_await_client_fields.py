from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("support", "0004_add_mm_audit_financial_models"),
    ]

    operations = [
        # Nouveau statut en-attente-client (pas de migration SQL — CharField libre)
        migrations.AlterField(
            model_name="ticket",
            name="status",
            field=models.CharField(
                max_length=18,
                choices=[
                    ("ouvert", "Ouvert"),
                    ("en-traitement", "En traitement"),
                    ("escalade", "Escaladé"),
                    ("en-attente-client", "En attente client"),
                    ("resolu", "Résolu"),
                    ("rejete", "Rejeté"),
                ],
                default="ouvert",
            ),
        ),
        # Champs en-attente-client
        migrations.AddField(
            model_name="ticket",
            name="awaiting_since",
            field=models.DateTimeField(null=True, blank=True),
        ),
        migrations.AddField(
            model_name="ticket",
            name="await_client_question",
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name="ticket",
            name="sla_accumulated_pause_seconds",
            field=models.IntegerField(default=0),
        ),
        migrations.AddField(
            model_name="ticket",
            name="await_task_j2_id",
            field=models.CharField(max_length=200, blank=True),
        ),
        migrations.AddField(
            model_name="ticket",
            name="await_task_j5_id",
            field=models.CharField(max_length=200, blank=True),
        ),
        migrations.AddField(
            model_name="ticket",
            name="await_task_j7_id",
            field=models.CharField(max_length=200, blank=True),
        ),
        # action_source sur les messages
        migrations.AddField(
            model_name="ticketmessage",
            name="action_source",
            field=models.CharField(max_length=100, blank=True),
        ),
    ]
