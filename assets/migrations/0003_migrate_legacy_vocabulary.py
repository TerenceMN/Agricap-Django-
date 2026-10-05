"""Migration de l'ancien vocabulaire des actifs vers le cycle de vie vérifié.

Catégories : `equipment|property|vehicle|other` → `materiel|foncier|vehicule|autre`.

Statuts — décision structurante : `free` devient **`declare`**, pas `verifie`.

L'ancien modèle n'avait aucune notion de vérification : un actif « libre »
n'avait été contrôlé par personne, et le client pouvait même écrire son statut
lui-même via PATCH. Les promouvoir en `verifie` reviendrait à certifier en masse
des actifs que nul n'a jamais vus, en violation directe du principe 9
(« toute garantie est opposable ou n'est pas »).

Conséquence assumée : les actifs existants ne sont plus mobilisables en garantie
tant qu'un agent terrain ne les a pas vérifiés et valorisés. C'est le
comportement correct — l'alternative silencieuse serait un risque de crédit réel.

`pledged` → `gage` : ces actifs étaient effectivement nantis, on préserve l'état.
Leur `gage_application` reste nul (l'ancien modèle ne le traçait pas) : ils
devront être rapprochés manuellement de leur dossier.
"""
from django.db import migrations


TYPE_FORWARD = {
    "equipment": "materiel",
    "property": "foncier",
    "vehicle": "vehicule",
    "other": "autre",
}
TYPE_BACKWARD = {v: k for k, v in TYPE_FORWARD.items()}

STATUS_FORWARD = {
    "free": "declare",
    "pledged": "gage",
}
# Le retour arrière est volontairement lossy : `verifie`, `rejete` et `libere`
# n'ont pas d'équivalent dans l'ancien vocabulaire à deux états.
STATUS_BACKWARD = {
    "declare": "free",
    "verifie": "free",
    "libere": "free",
    "rejete": "free",
    "gage": "pledged",
}


def _remap(apps, mapping_type, mapping_status):
    Asset = apps.get_model("assets", "Asset")
    for old, new in mapping_type.items():
        Asset.objects.filter(type=old).update(type=new)
    for old, new in mapping_status.items():
        Asset.objects.filter(status=old).update(status=new)


def forward(apps, schema_editor):
    _remap(apps, TYPE_FORWARD, STATUS_FORWARD)


def backward(apps, schema_editor):
    _remap(apps, TYPE_BACKWARD, STATUS_BACKWARD)


class Migration(migrations.Migration):

    dependencies = [
        ("assets", "0002_asset_documents_asset_gage_application_and_more"),
    ]

    operations = [
        migrations.RunPython(forward, backward),
    ]
