"""`AlertRule.threshold` : de `float` à `Decimal` (principe 4).

Un seuil d'alerte est une frontière : sa seule raison d'être est d'être franchie
au bon moment. Stocké en binaire, un seuil saisi à 79,9 valait
79,900000000000005684…, et une règle « score < 79,9 » se déclenchait — ou non —
sur la valeur d'égalité exacte selon l'arrondi de la mesure. Même raisonnement
que pour les seuils du moteur de scoring (`referentiel.InstitutionConfig`).

Réversible : `AlterField` sait revenir à `FloatField`. Les règles seedées par
0002 et 0004 (24, 72, 80, 60, 5, 3) traversent la conversion sans changer de
valeur.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('alerts', '0005_alertrule_notify_phone'),
    ]

    operations = [
        migrations.AlterField(
            model_name='alertrule',
            name='threshold',
            field=models.DecimalField(decimal_places=2, max_digits=12),
        ),
    ]
