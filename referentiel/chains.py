"""
Les 14 chaînes de valeur AGRICAP (CAHIER §3).

Source de vérité partagée : le classifieur (`credit.services.value_chain`) et
l'import du référentiel s'appuient sur ce module. `sheet` est le nom exact de la
feuille correspondante dans `AGRICAP_REF_Referentiels_Technico_Economiques_v3.xlsx`.

Aucun nom de modèle/table métier n'est codé ailleurs : tout part d'ici.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Chain:
    code: str          # "01".."14"
    slug: str          # identifiant technique
    libelle: str       # libellé affiché (français)
    sheet: str         # feuille du référentiel v3
    specialite: str    # routage gestionnaire spécialisé (§8.3)
    keywords: tuple[str, ...] = field(default=())


# Signaux de détection repris tels quels du CAHIER §3.
CHAINS: tuple[Chain, ...] = (
    Chain("01", "cereales", "Céréales", "01_Cereales", "vegetal",
          ("maïs", "mais", "riz", "sorgho", "mil", "blé", "ble", "semences", "labour", "égrenage", "egrenage")),
    Chain("02", "legumineuses", "Légumineuses", "02_Legumineuses", "vegetal",
          ("haricot", "petit pois", "niébé", "niebe", "soja", "arachide", "voandzou", "battage", "fanes")),
    Chain("03", "tubercules", "Tubercules & racines", "03_Tubercules_Racines", "vegetal",
          ("manioc", "patate douce", "igname", "taro", "pomme de terre", "boutures", "billonnage")),
    Chain("04", "maraichage", "Maraîchage", "04_Maraichage", "vegetal",
          ("tomate", "oignon", "chou", "amarante", "piment", "gombo", "pépinière", "pepiniere",
           "repiquage", "irrigation")),
    Chain("05", "bananes", "Bananes & herbacées", "05_Bananes_Herbacees", "vegetal",
          ("plantain", "banane", "ananas", "papaye", "rejets", "régimes", "regimes",
           "œilletonnage", "oeilletonnage")),
    Chain("06", "fruits", "Fruits tropicaux", "06_Fruits_Tropicaux", "vegetal",
          ("mangue", "avocat", "agrumes", "safou", "maracuja", "plants greffés", "plants greffes",
           "verger", "treillis")),
    Chain("07", "cultures_industrielles", "Cultures industrielles", "07_Cultures_Industrielles", "vegetal",
          ("canne", "coton", "tabac", "thé", "the", "quinquina", "sésame", "sesame", "tournesol",
           "usinier", "exportateur")),
    Chain("08", "apiculture", "Apiculture", "08_Apiculture", "elevage",
          ("ruches", "ruche", "colonies", "miel", "cire", "enfumoir", "enruchement")),
    Chain("09", "elevage", "Élevage bétail & volaille", "09_Elevage_Betail_Volaille", "elevage",
          ("poussins", "poussin", "aliment", "gmq", "ponte", "porc", "chèvre", "chevre", "bovin",
           "poulet", "poule", "sujets", "têtes", "tetes", "volaille", "mouton", "canard")),
    Chain("10", "elevages_non_conv", "Élevages non conventionnels", "10_Elevages_Non_Conventionnels", "elevage",
          ("cobaye", "aulacode", "achatine", "larves bsf", "bsf", "lombriculture", "bioconversion",
           "substrat de bioconversion")),
    Chain("11", "aquaculture", "Aquaculture & pisciculture", "11_Aquaculture_Pisciculture", "aquacole",
          ("alevins", "alevin", "étang", "etang", "tilapia", "clarias", "empoissonnement", "pisciculture")),
    Chain("12", "agroforesterie", "Agroforesterie & bois", "12_Agroforesterie_Bois", "agroforestier",
          ("acacia", "eucalyptus", "cacao", "café", "cafe", "palmier", "hévéa", "hevea", "taungya",
           "makala", "pare-feux", "pare-feu")),
    Chain("13", "myciculture", "Myciculture", "13_Champignons", "vegetal",
          ("pleurotes", "pleurote", "champignon", "blanc de champignon", "substrat", "stérilisation",
           "sterilisation", "fructification")),
    Chain("14", "transformation", "Transformation & provenderie", "14_Transformation_Provenderie", "transformation",
          ("moulin", "mouture", "décorticage", "decorticage", "presse", "provenderie",
           "pasteurisation", "prestation", "usinée", "usinee")),
)

BY_CODE: dict[str, Chain] = {c.code: c for c in CHAINS}
BY_SLUG: dict[str, Chain] = {c.slug: c for c in CHAINS}
BY_SHEET: dict[str, Chain] = {c.sheet: c for c in CHAINS}


# Périodes normalisées du cycle (CAHIER §2A).
PERIODES = (
    "Préparation", "Implantation", "Croissance", "Entretien",
    "Récolte", "Post récolte", "Commercialisation", "Tout le cycle",
)
