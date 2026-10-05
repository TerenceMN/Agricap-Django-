"""Provenance des taux déjà en base au moment où la gouvernance est introduite.

`source` naît avec la valeur par défaut `MANUELLE`, ce qui serait FAUX pour les lignes
issues de la synchronisation `fetch_bcc_rates` : elles viennent bien de la publication de
la BCC. On ne peut pas le deviner ligne à ligne, mais on le sait par construction — avant
cette migration, le seul producteur de lignes `tier="BCC"` était le job de synchronisation
(ou une saisie manuelle recopiant le cours BCC : dans les deux cas, la source EST la BCC).

Les paliers STAFF/CLIENT restent `MANUELLE` : ce sont des décisions commerciales internes.
"""
from django.db import migrations

BCC_RATES_URL = ("https://www.bcc.cd/operations-et-marches/domaine-operationnel/"
                 "operations-de-change/cours-de-change")


def marquer_source_bcc(apps, schema_editor):
    ExchangeRate = apps.get_model("fx", "ExchangeRate")
    ExchangeRate.objects.filter(tier="BCC").update(
        source="BCC", source_reference=BCC_RATES_URL,
    )


def revenir_source_par_defaut(apps, schema_editor):
    ExchangeRate = apps.get_model("fx", "ExchangeRate")
    ExchangeRate.objects.filter(tier="BCC", source="BCC",
                                source_reference=BCC_RATES_URL).update(
        source="MANUELLE", source_reference="",
    )


class Migration(migrations.Migration):

    dependencies = [
        ("fx", "0002_alter_exchangerate_options_and_more"),
    ]

    operations = [
        migrations.RunPython(marquer_source_bcc, revenir_source_par_defaut),
    ]
