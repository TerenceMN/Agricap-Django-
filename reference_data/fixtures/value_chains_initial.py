"""
Script de chargement des fixtures initiales (5 chaînes de valeur).
Exécuter avec : python manage.py shell < reference_data/fixtures/value_chains_initial.py
Ou directement : python -c "exec(open('reference_data/fixtures/value_chains_initial.py').read())"
"""
from django.utils import timezone
from reference_data.models import ReferenceFileUpload, ValueChain

CHAINS = [
    {
        "code": "CAFE_ARABICA",
        "label": "Café Arabica",
        "active": True,
        "cycle_months": 9,
        "cost_per_hectare_usd": "4800.00",
        "cost_per_hectare_cdf": "13440000.00",
        "module_weights": {
            "semences": 15, "mecanisation": 10, "maindoeuvre": 30,
            "equipements": 12, "postrecolte": 15, "logistique": 8,
            "commercialisation": 5, "reserve": 5,
        },
        "risk_factor": "0.900",
        "min_score_required": 60,
        "base_rate": "6.00",
        "harvest_months": [3, 4],
        "eligible_guarantees": ["epargne", "morale"],
    },
    {
        "code": "MAIS",
        "label": "Maïs",
        "active": True,
        "cycle_months": 5,
        "cost_per_hectare_usd": "1200.00",
        "cost_per_hectare_cdf": "3360000.00",
        "module_weights": {
            "semences": 25, "mecanisation": 15, "maindoeuvre": 25,
            "equipements": 5, "postrecolte": 12, "logistique": 8,
            "commercialisation": 5, "reserve": 5,
        },
        "risk_factor": "1.000",
        "min_score_required": 55,
        "base_rate": "7.50",
        "harvest_months": [1, 7],
        "eligible_guarantees": ["epargne", "morale"],
    },
    {
        "code": "MANIOC",
        "label": "Manioc",
        "active": True,
        "cycle_months": 12,
        "cost_per_hectare_usd": "900.00",
        "cost_per_hectare_cdf": "2520000.00",
        "module_weights": {
            "semences": 20, "mecanisation": 10, "maindoeuvre": 35,
            "equipements": 5, "postrecolte": 10, "logistique": 10,
            "commercialisation": 5, "reserve": 5,
        },
        "risk_factor": "0.850",
        "min_score_required": 50,
        "base_rate": "8.00",
        "harvest_months": [11, 12],
        "eligible_guarantees": ["epargne", "morale"],
    },
    {
        "code": "HARICOT",
        "label": "Haricot",
        "active": True,
        "cycle_months": 4,
        "cost_per_hectare_usd": "800.00",
        "cost_per_hectare_cdf": "2240000.00",
        "module_weights": {
            "semences": 30, "mecanisation": 8, "maindoeuvre": 28,
            "equipements": 4, "postrecolte": 10, "logistique": 10,
            "commercialisation": 5, "reserve": 5,
        },
        "risk_factor": "1.100",
        "min_score_required": 55,
        "base_rate": "8.50",
        "harvest_months": [4, 9],
        "eligible_guarantees": ["epargne", "morale"],
    },
    {
        "code": "RIZ",
        "label": "Riz",
        "active": True,
        "cycle_months": 6,
        "cost_per_hectare_usd": "1500.00",
        "cost_per_hectare_cdf": "4200000.00",
        "module_weights": {
            "semences": 20, "mecanisation": 18, "maindoeuvre": 28,
            "equipements": 8, "postrecolte": 12, "logistique": 8,
            "commercialisation": 3, "reserve": 3,
        },
        "risk_factor": "1.050",
        "min_score_required": 58,
        "base_rate": "7.00",
        "harvest_months": [2, 8],
        "eligible_guarantees": ["epargne", "morale"],
    },
]

# Créer l'upload système de référence (version initiale, activé d'office)
upload, _ = ReferenceFileUpload.objects.get_or_create(
    file_type=ReferenceFileUpload.FileType.VALUE_CHAINS,
    status=ReferenceFileUpload.Status.ACTIVE,
    version="initial-fixture-v1.0",
    defaults={
        "uploaded_by": "system",
        "activated_by": "system",
        "activated_at": timezone.now(),
        "row_count": len(CHAINS),
        "validation_report": {"valid": True, "errors": [], "rows": CHAINS},
        "diff_summary": {"added": [c["code"] for c in CHAINS], "removed": [], "modified": [], "unchanged": 0},
        "file": "reference_data/fixture-placeholder.xlsx",
    },
)

if not upload.value_chains.exists():
    ValueChain.objects.bulk_create([
        ValueChain(source_file=upload, **chain)
        for chain in CHAINS
    ])
else:
    print("value_chains: déjà chargées, rien fait")
print("value_chains actives:", ValueChain.objects.filter(active=True).count())
