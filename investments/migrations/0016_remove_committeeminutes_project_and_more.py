"""Retrait du scoring et du comité d'investissement (décision du fondateur, août 2026).

Ce module ne note plus un projet et ne le fait plus approuver par une instance : le
projet publie une PIÈCE JOINTE, et l'investisseur décide en souscrivant.

**Migration DESTRUCTIVE, et assumée comme telle.** Les analyses de projet, les
analyses technique et financière, les votes et les procès-verbaux signés du comité
sont SUPPRIMÉS, tables comprises. Ce que cela coûte, dit une fois : un projet
financé avant cette date n'a plus, dans cette base, le détail de ce qui a fondé sa
décision.

Ce qui SURVIT, et c'est délibéré : le journal d'audit (`audit.AuditEntry`), qui est
append-only et n'est pas touché ici. Les actes — `investments.project.analyse`,
`investments.committee.decision`, `investments.analysis.technical.save` — y restent
tracés avec leur acteur, leur date et leur charge utile. Un auditeur sait donc
QU'une décision a été prise, par qui et quand ; il n'a plus les pièces détaillées.

`RunPython` n'est pas réversible ici : `reverse_code` remonterait la colonne mais
pas les lignes détruites. On le déclare plutôt que de feindre une réversibilité.
"""
from django.db import migrations, models


def ramener_les_statuts_retires(apps, schema_editor):
    """P02, P03, P04 → P05.

    Ces trois étapes n'existent plus. Un projet qui s'y trouve porterait un statut
    absent des choix : invisible dans les listes filtrées, sans transition possible,
    donc bloqué. On le ramène à l'étape qui les remplace — « Approbation — pièce
    jointe » —, la plus proche en aval, jamais en amont : rétrograder un dossier
    déjà instruit lui ferait refaire un chemin qu'il a parcouru.
    """
    Project = apps.get_model("investments", "Project")
    Project.objects.filter(status__in=("P02", "P03", "P04")).update(status="P05")


def sans_retour(apps, schema_editor):
    """Aucun retour : les étapes supprimées ne se réattribuent pas à l'aveugle."""


class Migration(migrations.Migration):

    dependencies = [
        ('investments', '0015_alter_analysisattachment_kind'),
    ]

    operations = [
        migrations.RunPython(ramener_les_statuts_retires, sans_retour),
        # `DeleteModel` DIRECTEMENT, sans retirer d'abord le champ `project`.
        #
        # `makemigrations` génère un `RemoveField(project)` par modèle avant de le
        # supprimer. Sur SQLite, retirer une colonne reconstruit la table entière —
        # et `InvestmentCommitteeVote` porte une contrainte d'unicité SUR ce champ :
        # la reconstruction échoue en `FieldDoesNotExist: NewInvestmentCommitteeVote
        # has no field named 'project'`. La table part de toute façon en entier ;
        # démonter ses colonnes une à une avant de la jeter n'apporte rien et
        # traverse un état invalide.
        migrations.DeleteModel(name='CommitteeMinutes'),
        migrations.DeleteModel(name='CommitteeMember'),
        migrations.DeleteModel(name='InvestmentCommitteeVote'),
        migrations.DeleteModel(name='ProjectAnalysis'),
        migrations.DeleteModel(name='TechnicalAnalysis'),
        migrations.DeleteModel(name='FinancialAnalysis'),
        migrations.RemoveField(
            model_name='project',
            name='global_score',
        ),
        migrations.RemoveField(
            model_name='project',
            name='risk_score',
        ),
        migrations.AlterField(
            model_name='project',
            name='status',
            field=models.CharField(choices=[('P01', 'Prospection'), ('P05', 'Approbation — pièce jointe'), ('P06', 'Levée de fonds'), ('P07', 'Souscription clôturée'), ('P08', 'Décaissement'), ('P09', 'En cours'), ('P10', 'Remboursement'), ('P11', 'Clôturé'), ('P12', 'Défaut'), ('P13', 'Annulé')], default='P01', max_length=3),
        ),
    ]
