"""Ajoute `ChartAccount.nature` (ACTIF/PASSIF/CHARGE/PRODUIT) — indispensable pour ventiler
Bilan/Compte de résultat/SIG correctement : SYSCOHADA mélange actif et passif DANS une même
classe (ex. classe 4 : 411 Clients = actif, 401 Fournisseurs = passif), donc `class_no` seul
ne suffit pas. Migration séparée de 0002 (plutôt que d'y ajouter directement le champ) pour
rester correcte même si 0002 a déjà été appliquée sur une base existante."""
from __future__ import annotations

from django.db import migrations, models

DEFAULT_NATURE_BY_CLASS = {
    1: "PASSIF", 2: "ACTIF", 3: "ACTIF", 5: "ACTIF", 6: "CHARGE", 7: "PRODUIT",
}

# Exceptions à la classe par défaut : classe 4 (comptes de tiers, mixte par nature),
# classe 5 sous-famille 56 (crédits de trésorerie = dette bancaire, pas une disponibilité),
# classe 8 (mixte, moitié charges moitié produits).
NATURE_OVERRIDES = {
    # ------------------------------------------------------------------ CLASSE 4 (mixte)
    "40": "PASSIF", "401": "PASSIF", "402": "PASSIF", "408": "PASSIF", "409": "ACTIF",
    "41": "ACTIF", "411": "ACTIF", "412": "ACTIF", "4121": "ACTIF",
    # Exception : 4111 "Clients — comptes épargne" est un dépôt d'épargne collecté par
    # AGRICAP (une DETTE envers le client), pas une créance — nature inverse de son parent
    # 411 malgré le rattachement hiérarchique (le compte 4111 sert uniquement à ISOLER le
    # solde épargne du solde crédit sous le même sous-compte SYSCOHADA, pas à hériter sa
    # nature comptable).
    "4111": "PASSIF",
    "414": "ACTIF", "415": "ACTIF", "416": "ACTIF", "418": "ACTIF", "419": "PASSIF",
    "42": "PASSIF", "421": "ACTIF", "422": "PASSIF", "423": "PASSIF", "424": "PASSIF",
    "425": "PASSIF", "426": "PASSIF", "427": "PASSIF", "428": "PASSIF",
    "43": "PASSIF", "431": "PASSIF", "432": "PASSIF", "433": "PASSIF", "438": "PASSIF",
    "44": "PASSIF", "441": "PASSIF", "442": "PASSIF", "443": "PASSIF", "444": "PASSIF",
    "445": "ACTIF", "446": "PASSIF", "447": "PASSIF", "448": "PASSIF", "449": "PASSIF",
    "45": "PASSIF", "451": "PASSIF", "452": "PASSIF", "458": "ACTIF",
    "46": "PASSIF", "461": "PASSIF", "462": "PASSIF", "463": "PASSIF", "465": "PASSIF",
    "466": "PASSIF", "467": "ACTIF",
    "47": "ACTIF", "471": "ACTIF", "472": "PASSIF", "474": "ACTIF", "475": "ACTIF",
    "476": "ACTIF", "477": "PASSIF", "478": "ACTIF", "479": "PASSIF",
    "48": "ACTIF", "481": "PASSIF", "482": "PASSIF", "483": "PASSIF", "484": "PASSIF",
    "485": "ACTIF", "486": "ACTIF", "488": "ACTIF",
    "49": "ACTIF", "490": "ACTIF", "491": "ACTIF", "4911": "ACTIF", "492": "ACTIF",
    "493": "ACTIF", "494": "ACTIF", "495": "ACTIF", "496": "ACTIF", "497": "ACTIF",
    "498": "ACTIF", "499": "ACTIF",
    # ------------------------------------------------------ CLASSE 5 : 56 = dette, pas actif
    "56": "PASSIF", "561": "PASSIF", "564": "PASSIF", "565": "PASSIF", "566": "PASSIF",
    # ------------------------------------------------------------------ CLASSE 8 (mixte)
    "81": "CHARGE", "82": "PRODUIT", "83": "CHARGE", "84": "PRODUIT", "85": "CHARGE",
    "86": "PRODUIT", "87": "CHARGE", "88": "PRODUIT", "89": "CHARGE",
}


def set_nature(apps, schema_editor):
    ChartAccount = apps.get_model("ledger", "ChartAccount")
    for account in ChartAccount.objects.all():
        nature = NATURE_OVERRIDES.get(account.code) or DEFAULT_NATURE_BY_CLASS.get(account.class_no, "ACTIF")
        if account.nature != nature:
            account.nature = nature
            account.save(update_fields=["nature"])


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("ledger", "0002_seed_syscohada_chart"),
    ]

    operations = [
        migrations.AddField(
            model_name="chartaccount",
            name="nature",
            field=models.CharField(
                max_length=8,
                choices=[("ACTIF", "Actif"), ("PASSIF", "Passif"), ("CHARGE", "Charge"), ("PRODUIT", "Produit")],
                default="ACTIF",
            ),
        ),
        migrations.RunPython(set_nature, noop_reverse),
    ]
