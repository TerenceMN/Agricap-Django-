"""Amorce les règles d'alertes par défaut — adaptées des règles seedées dans la
spécification d'origine (AGENCY_OFFLINE_15/60, RECON_AGE_24H, COMPLIANCE_LT_90/80,
PARTNER_DOWN_2) aux métriques réellement calculables dans ce système (pas de heartbeat
d'agence, donc « suspendue depuis » plutôt que « hors ligne depuis »)."""
from __future__ import annotations

from django.db import migrations

RULES = [
    ("AGENCY_SUSPENDED_24H", "Agence suspendue > 24h", "AGENCY_SUSPENDED", ">=", 24, "WARNING"),
    ("AGENCY_SUSPENDED_72H", "Agence suspendue > 72h", "AGENCY_SUSPENDED", ">=", 72, "CRITICAL"),
    ("RECON_OVERDUE_24H", "Rapprochement ouvert > 24h", "RECONCILIATION_OVERDUE", ">=", 24, "WARNING"),
    ("RECON_OVERDUE_72H", "Rapprochement ouvert > 72h", "RECONCILIATION_OVERDUE", ">=", 72, "CRITICAL"),
    ("COMPLIANCE_LOW_80", "Score de conformité < 80%", "COMPLIANCE_SCORE_LOW", "<", 80, "WARNING"),
    ("COMPLIANCE_LOW_60", "Score de conformité < 60%", "COMPLIANCE_SCORE_LOW", "<", 60, "CRITICAL"),
    ("TX_OVERDUE_HIGH", "Transactions en attente en retard", "TRANSACTION_OVERDUE", ">=", 5, "WARNING"),
    ("PARTNER_FAILURES_HIGH", "Échecs consécutifs partenaire", "PARTNER_FAILURES", ">=", 5, "CRITICAL"),
]


def seed_rules(apps, schema_editor):
    AlertRule = apps.get_model("alerts", "AlertRule")
    for code, name, metric, operator, threshold, severity in RULES:
        AlertRule.objects.get_or_create(
            code=code,
            defaults={"name": name, "metric": metric, "operator": operator, "threshold": threshold,
                      "severity": severity},
        )


def unseed_rules(apps, schema_editor):
    AlertRule = apps.get_model("alerts", "AlertRule")
    AlertRule.objects.filter(code__in=[r[0] for r in RULES]).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("alerts", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(seed_rules, unseed_rules),
    ]
