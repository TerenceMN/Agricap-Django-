from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("credits", "0002_creditguarantee"),
    ]

    operations = [
        migrations.AddField(
            model_name="creditapplication",
            name="submitted_by_sub",
            field=models.CharField(blank=True, default="", max_length=255),
            preserve_default=False,
        ),
    ]
