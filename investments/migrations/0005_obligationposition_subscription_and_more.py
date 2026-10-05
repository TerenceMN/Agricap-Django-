"""Un projet EST une demande de crédit ; une obligation naît d'un encaissement.

Trois changements de fond, tous réversibles (aucun `RunPython`, aucune donnée
transformée — les colonnes existantes gardent leurs valeurs) :

1. `Project.credit_application` — rattachement **un-à-un** au dossier de crédit
   (`OneToOneField` : l'unicité « deux projets ne financent pas le même dossier » vit
   en base). `null=True` le temps de la reprise des projets antérieurs.
2. `Project.global_score` : `FloatField` → `DecimalField(5,1)`. Un score qui décide
   d'une entrée en due diligence et qui est publié à l'investisseur est une grandeur
   financière : `float` y est proscrit (principe 4). Même forme que
   `credits.AnalyseCredit.score_global`, dont il est désormais la projection.
3. `ObligationPosition` : les valeurs par défaut du prototype (coupon 250, taux 9 %,
   maturité 24 mois) sont SUPPRIMÉES et un lien vers la souscription encaissée est
   ajouté. Les colonnes restent NOT NULL : une position sans termes n'existe pas, et
   les positions déjà en base conservent les valeurs qu'elles portent. La suppression
   d'un `default` n'altère aucune ligne existante — elle interdit seulement d'en créer
   de nouvelles sans termes décidés.

La dépendance `credits` est épinglée sur `0013_creditapplication_agency` (la dernière
migration de `credits` versionnée à la date de ce lot) et non sur `__latest__` : ce
module n'est pas propriétaire de `credits`, et se raccrocher à une migration en cours
d'écriture par un autre agent rendrait ce fichier dépendant d'un travail non figé.
`CreditApplication` existe depuis `credits/0001` — le point d'ancrage est sûr.
"""

import django.db.models.deletion
from decimal import Decimal
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('credits', '0013_creditapplication_agency'),
        ('investments', '0004_investmentconfig_performance_deviation_alert_percent_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='obligationposition',
            name='subscription',
            field=models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='obligation_position', to='investments.subscription'),
        ),
        migrations.AddField(
            model_name='project',
            name='credit_application',
            field=models.OneToOneField(blank=True, help_text='Dossier de crédit instruit par le module crédit. Source unique du promoteur, de la filière, de la zone, du montant demandé et du score.', null=True, on_delete=django.db.models.deletion.PROTECT, related_name='investment_project', to='credits.creditapplication'),
        ),
        migrations.AlterField(
            model_name='obligationposition',
            name='coupon_amount',
            field=models.DecimalField(decimal_places=2, max_digits=12),
        ),
        migrations.AlterField(
            model_name='obligationposition',
            name='rate',
            field=models.DecimalField(decimal_places=3, max_digits=6),
        ),
        migrations.AlterField(
            model_name='obligationposition',
            name='term_months',
            field=models.IntegerField(),
        ),
        migrations.AlterField(
            model_name='project',
            name='global_score',
            field=models.DecimalField(decimal_places=1, default=Decimal('0'), max_digits=5),
        ),
    ]
