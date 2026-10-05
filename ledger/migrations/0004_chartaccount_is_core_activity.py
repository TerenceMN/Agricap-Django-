"""Ajoute `ChartAccount.is_core_activity` — distingue dans le plan comptable ce qui relève
de l'activité RÉELLE d'AGRICAP FINTECH (établissement de crédit bi-monnaie) de ce qui n'est
présent que pour la complétude structurelle SYSCOHADA (classes 2/3/8, comptes de négoce/
production que seule une entreprise commerciale ou industrielle utiliserait). N'empêche
aucune écriture — un compte non-core reste postable si un besoin réel apparaît."""
from __future__ import annotations

from django.db import migrations, models

# Classes entières hors activité courante (présentes uniquement pour la conformité légale
# — toute classe 1-8 est obligatoire même si une institution n'a pas de stocks/immobilisé
# significatif ou d'opérations hors activités ordinaires).
NON_CORE_CLASSES = (2, 3, 8)

# Au sein des classes 6/7 (utilisées au quotidien), les comptes spécifiquement liés à
# l'achat/la revente ou la fabrication de biens physiques — sans objet pour un prêteur.
NON_CORE_PREFIXES = ("601", "602", "603", "701", "702", "703", "704", "705", "72", "73")


def set_core_activity(apps, schema_editor):
    ChartAccount = apps.get_model("ledger", "ChartAccount")
    for account in ChartAccount.objects.all():
        is_core = account.class_no not in NON_CORE_CLASSES and not account.code.startswith(NON_CORE_PREFIXES)
        if account.is_core_activity != is_core:
            account.is_core_activity = is_core
            account.save(update_fields=["is_core_activity"])


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("ledger", "0003_chartaccount_nature"),
    ]

    operations = [
        migrations.AddField(
            model_name="chartaccount",
            name="is_core_activity",
            field=models.BooleanField(default=True),
        ),
        migrations.RunPython(set_core_activity, noop_reverse),
    ]
