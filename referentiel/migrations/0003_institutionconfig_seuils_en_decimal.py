"""`InstitutionConfig` : les paramètres du moteur passent de `float` à `Decimal`.

Ces colonnes ne décrivent pas l'institution, elles la décident : les cinq poids
du scoring, les seuils DSCR, le score global minimum et la décote de garantie
sont relus à chaque analyse par `credits/analyse.py` et
`assets/services.py`. En `float`, 0,30 valait 0,29999999999999998889…, et la
somme des poids pouvait manquer 100 d'un ε — ce qui faisait silencieusement
retomber `poids_effectifs()` sur ses poids de secours. Le principe 4 (« Decimal
partout, float nulle part ») s'applique ici à la lettre.

Conversion sans perte de sens : les valeurs existantes sont arrondies à la
précision déclarée (0,001 pour les ratios, 0,01 pour les poids/montants, 0,0001
pour les taux et décotes exprimés en fraction). Réversible : `AlterField` sait
revenir à `FloatField` — au prix, à l'envers, de la précision qu'on vient de
gagner.
"""

from decimal import Decimal
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('referentiel', '0002_institutionconfig_caution_consent_window_hours_and_more'),
    ]

    operations = [
        migrations.AlterField(
            model_name='institutionconfig',
            name='caution_ratio_epargne',
            field=models.DecimalField(decimal_places=3, default=Decimal('2.000'), max_digits=6),
        ),
        migrations.AlterField(
            model_name='institutionconfig',
            name='commissions',
            field=models.DecimalField(decimal_places=4, default=Decimal('0.0100'), max_digits=6),
        ),
        migrations.AlterField(
            model_name='institutionconfig',
            name='couverture_min',
            field=models.DecimalField(decimal_places=3, default=Decimal('1.000'), max_digits=6),
        ),
        migrations.AlterField(
            model_name='institutionconfig',
            name='decote_caution_morale',
            field=models.DecimalField(decimal_places=4, default=Decimal('0.7000'), max_digits=6),
        ),
        migrations.AlterField(
            model_name='institutionconfig',
            name='decote_garantie',
            field=models.DecimalField(decimal_places=4, default=Decimal('0.3000'), max_digits=6),
        ),
        migrations.AlterField(
            model_name='institutionconfig',
            name='frais_dossier',
            field=models.DecimalField(decimal_places=4, default=Decimal('0.0200'), max_digits=6),
        ),
        migrations.AlterField(
            model_name='institutionconfig',
            name='plafond_delegue',
            field=models.DecimalField(decimal_places=2, default=Decimal('25000.00'), max_digits=14),
        ),
        migrations.AlterField(
            model_name='institutionconfig',
            name='poids_comportemental',
            field=models.DecimalField(decimal_places=2, default=Decimal('30.00'), max_digits=6),
        ),
        migrations.AlterField(
            model_name='institutionconfig',
            name='poids_financier',
            field=models.DecimalField(decimal_places=2, default=Decimal('20.00'), max_digits=6),
        ),
        migrations.AlterField(
            model_name='institutionconfig',
            name='poids_garanties',
            field=models.DecimalField(decimal_places=2, default=Decimal('15.00'), max_digits=6),
        ),
        migrations.AlterField(
            model_name='institutionconfig',
            name='poids_stress',
            field=models.DecimalField(decimal_places=2, default=Decimal('10.00'), max_digits=6),
        ),
        migrations.AlterField(
            model_name='institutionconfig',
            name='poids_technique',
            field=models.DecimalField(decimal_places=2, default=Decimal('25.00'), max_digits=6),
        ),
        migrations.AlterField(
            model_name='institutionconfig',
            name='score_global_min',
            field=models.DecimalField(decimal_places=2, default=Decimal('70.00'), max_digits=6),
        ),
        migrations.AlterField(
            model_name='institutionconfig',
            name='seuil_dscr',
            field=models.DecimalField(decimal_places=3, default=Decimal('1.200'), max_digits=6),
        ),
        migrations.AlterField(
            model_name='institutionconfig',
            name='seuil_dscr_stresse',
            field=models.DecimalField(decimal_places=3, default=Decimal('1.000'), max_digits=6),
        ),
        migrations.AlterField(
            model_name='institutionconfig',
            name='taux_interet_annuel',
            field=models.DecimalField(decimal_places=4, default=Decimal('0.2400'), max_digits=6),
        ),
    ]
