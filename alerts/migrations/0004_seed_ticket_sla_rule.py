"""Ajoute la règle par défaut pour le nouveau métrique TICKET_SLA_BREACHED — migration
séparée de 0002 (jamais modifier une migration déjà appliquée)."""
from __future__ import annotations

from django.db import migrations


def seed_rule(apps, schema_editor):
    AlertRule = apps.get_model("alerts", "AlertRule")
    AlertRule.objects.get_or_create(
        code="TICKET_SLA_BREACHED_HIGH",
        defaults={"name": "Tickets en dépassement de SLA", "metric": "TICKET_SLA_BREACHED",
                  "operator": ">=", "threshold": 3, "severity": "WARNING"},
    )


def unseed_rule(apps, schema_editor):
    AlertRule = apps.get_model("alerts", "AlertRule")
    AlertRule.objects.filter(code="TICKET_SLA_BREACHED_HIGH").delete()


class Migration(migrations.Migration):

    dependencies = [
        ("alerts", "0003_alter_alertrule_metric"),
    ]

    operations = [
        migrations.RunPython(seed_rule, unseed_rule),
    ]
