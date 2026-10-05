"""Construit un `ReferentielFiliere` depuis un simulateur déjà ingéré dans `dataio`.

Pourquoi ce module existe — le seed du moteur portait des coûts par module
**inventés**, répartis à la main pour retomber sur le total de 9 111 USD du
classeur maïs. Le total était juste, la répartition ne l'était pas : 850 USD/ha
de semences là où le classeur en donne 126,60. Un facteur 6,7 sur un poste qui
pèse dans le critère de fiabilité technique, soit 25 % du score.

Le principe 1 vaut pour le référentiel comme pour le dossier : **ce qui sert à
scorer vient de la base, pas d'une estimation**. Les simulateurs
`AGRICAP_FIN_SIM_NN_Famille_Culture.xlsx` sont ingérés en tables versionnées ;
c'est la seule source légitime.

Ce que le classeur donne et qu'on lit :
  - `2_Identification_Projet` → DIMENSION de référence (quantité + unité),
                                production principale
  - `5_Synthese_Besoins`      → total par rubrique, pour le cycle entier
  - `3_Parametres_Techniques` → rendement attendu et prix prévu

Ce que le classeur ne donne PAS et qui reste institutionnel : les tolérances
par module. Elles expriment la marge qu'AGRICAP accepte autour de la référence,
pas une donnée agronomique — elles n'ont donc rien à faire dans un simulateur.

MODÈLE « HECTARE » GÉNÉRALISÉ (lot moteur unifié)
-------------------------------------------------
Ce loader ne lisait qu'une ligne « Superficie … (ha) » et REFUSAIT les cinq
filières qui ne se mesurent pas en hectares (08 ruches, 09 sujets, 10 m² de
bioconversion, 13 sacs de substrat, 14 tonnes usinées) : 5 filières sur 14
étaient inscorables, soit 36 % du référentiel institutionnel.

Le refus était juste tant que le modèle n'avait qu'une unité — inventer une
superficie aurait fabriqué des coûts/ha faux. Le correctif n'est donc pas de
deviner une superficie, c'est de porter l'UNITÉ : le classeur nomme lui-même sa
dimension de référence (« Nombre de ruches », « Effectif (nombre de sujets) »,
« Surface de bioconversion (m²) », « Volume usiné sur le cycle (t) »), et cette
ligne est la seule chose à lire. `ReferentielFiliere.unite_reference` existait
déjà : il était figé à « ha » faute d'être renseigné.

Conséquence en aval, et c'est là qu'est l'argent : un coût de 42 USD/ruche ne se
multiplie pas par une superficie. Le dossier doit porter sa quantité DANS CETTE
UNITÉ (`CreditApplication.quantite_reference` + `unite_reference`), et
`credits.analyse.resoudre_quantite_reference` refuse l'analyse si les deux unités
ne concordent pas, plutôt que de multiplier des ruches par des hectares.
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

from credits.needs_sheet import (
    analyser_couverture_rubriques, normalize, rubrique_to_module,
)

#: `AGRICAP_FIN_SIM_01_Cereales_Mais.xlsx` → ("01", "Cereales", "Mais")
_NOM_SIMULATEUR = re.compile(
    r"AGRICAP_FIN_SIM_(?P<numero>\d{2})_(?P<famille>[^_]+)_(?P<culture>[^.]+)\.xlsx",
    re.IGNORECASE,
)

#: Tolérances par défaut — asymétriques et volontairement ainsi : sous-estimer un
#: poste (`tol_inf`) est plus souvent une omission de bonne foi, le sur-estimer
#: (`tol_sup`) plus souvent une inflation. Ces valeurs sont institutionnelles ;
#: elles descendront dans `InstitutionConfig` (principe 8).
TOLERANCE_DEFAUT = {"tol_inf": "0.30", "tol_sup": "0.40"}
#: L'équipement et la réserve tolèrent mieux l'absence : un dossier peut
#: légitimement n'avoir ni l'un ni l'autre.
TOLERANCES_PAR_MODULE = {
    "equipements": {"tol_inf": "0.40", "tol_sup": "0.40"},
    "reserve": {"tol_inf": "0.50", "tol_sup": "0.40"},
}


#: Unités de référence CANONIQUES (principe 6 : le backend définit les codes, le
#: front les affiche). Une filière porte une et une seule de ces unités ; c'est
#: elle qui est stockée dans `ReferentielFiliere.unite_reference` et exigée sur le
#: dossier (`CreditApplication.unite_reference`).
UNITES_CANONIQUES = ("ha", "m2", "ruche", "sujet", "sac", "t")

#: Libellé affichable — le front ne dérive rien, il affiche ce que le serveur sert.
UNITES_LABELS = {
    "ha": "hectare", "m2": "m²", "ruche": "ruche", "sujet": "sujet",
    "sac": "sac de substrat", "t": "tonne",
}

#: Unité notée ENTRE PARENTHÈSES dans le libellé de la rubrique — la source la
#: plus fiable, parce qu'elle est écrite par l'auteur du classeur.
_UNITE_PAR_PARENTHESE = {
    "ha": "ha", "hectare": "ha", "hectares": "ha",
    "m2": "m2", "m²": "m2",
    "t": "t", "tonne": "t", "tonnes": "t",
}

#: À défaut, l'unité se déduit du mot qui NOMME la dimension. Ordre significatif :
#: le premier fragment trouvé gagne, « surface » vient donc après « bioconversion »
#: n'a pas d'importance ici car les unités entre parenthèses sont testées d'abord.
_UNITE_PAR_MOT = (
    ("ruche", "ruche"),
    ("sujet", "sujet"),
    ("tete", "sujet"),
    ("sac", "sac"),
    ("superficie", "ha"),
    ("surface", "ha"),
)

_PARENTHESE = re.compile(r"\(([^)]*)\)")

#: Feuilles OPTIONNELLES du simulateur, lues depuis les `DataTable` déjà ingérées.
#: Leur absence n'empêche pas de charger un référentiel — elle est journalisée
#: dans le lignage. Refuser bloquerait l'instruction d'une filière entière pour
#: une feuille de diagnostic.
SHEET_TRESORERIE = "16_Tresorerie_Mensuelle"
SHEET_CONTROLES = "18_Controles_Vraisemblance"

#: Périodes canoniques du cycle — source unique `referentiel.chains.PERIODES`
#: (principe 6 : on ne recopie pas une nomenclature, on l'importe).
#:
#: Elles servent ici de FILTRE, et c'est leur rôle le plus important. La feuille
#: 16 contient DEUX blocs sous un seul en-tête : le tableau des périodes
#: (5 colonnes) puis une matrice de flux mensuels large de 18 colonnes. `dataio`
#: ne détecte qu'une ligne d'en-tête par feuille : les lignes du second bloc sont
#: donc rangées dans les colonnes du premier, décalées et tronquées — « Mois
#: début » y contient un montant, pas un mois. N'accepter que les lignes dont le
#: libellé est une période connue est ce qui empêche de lire 1 125,33 comme un
#: numéro de mois.
try:  # pragma: no cover - dépendance inter-app, présente en runtime
    from referentiel.chains import PERIODES
except Exception:  # noqa: BLE001
    PERIODES = ("Préparation", "Implantation", "Croissance", "Entretien",
                "Récolte", "Post récolte", "Commercialisation", "Tout le cycle")

_PERIODES_NORM = {normalize(p): p for p in PERIODES}

#: Période dont la fin donne le mois d'encaissement de la recette du cycle.
_PERIODE_VENTE = normalize("Commercialisation")


class ReferentielIntrouvable(Exception):
    """Le simulateur ne porte pas les tables ou les valeurs attendues."""


def _decimal(valeur) -> Decimal | None:
    """Nombre depuis une cellule, ou `None` — jamais une valeur inventée."""
    if valeur is None:
        return None
    texte = str(valeur).strip().replace(" ", "").replace(" ", "").replace(",", ".")
    if not texte:
        return None
    try:
        return Decimal(texte)
    except InvalidOperation:
        return None


def _lignes(source, nom_table: str) -> list[dict]:
    table = source.tables.filter(name=nom_table).first()
    if table is None:
        raise ReferentielIntrouvable(
            f"Le simulateur « {source.original_name} » n'a pas de table "
            f"« {nom_table} » — classeur incomplet ou mal ingéré."
        )
    return [r.values for r in table.records.order_by("row_index")]


def _valeur_par_rubrique(lignes: list[dict], fragment: str,
                         col_cle="Rubrique", col_val="Valeur") -> str | None:
    """Première valeur dont la rubrique contient `fragment` (sans accents)."""
    cible = normalize(fragment)
    for ligne in lignes:
        if cible in normalize(ligne.get(col_cle)):
            return ligne.get(col_val)
    return None


def _unite_du_libelle(libelle: str) -> str | None:
    """Unité canonique portée par un libellé de rubrique, ou `None`.

    Deux sources, dans cet ordre : l'unité notée entre parenthèses (« … (ha) »,
    « … (m²) », « … (t) »), puis le mot qui nomme la dimension (« Nombre de
    ruches », « Effectif (nombre de sujets) »). Aucune unité par défaut : une
    rubrique dont on ne sait pas l'unité n'est pas une dimension de référence,
    et l'ignorer vaut mieux que de la compter en hectares.
    """
    plat = normalize(libelle)
    if not plat:
        return None
    for brut in _PARENTHESE.findall(plat):
        unite = _UNITE_PAR_PARENTHESE.get(brut.strip())
        if unite:
            return unite
    for fragment, unite in _UNITE_PAR_MOT:
        if fragment in plat:
            return unite
    return None


def lire_dimension_reference(lignes: list[dict]) -> tuple[Decimal, str, str] | None:
    """Dimension de référence du classeur : `(quantité, unité, libellé lu)`.

    C'est la généralisation du « modèle hectare » : au lieu de chercher la seule
    rubrique « superficie », on cherche la première rubrique de la feuille
    d'identification qui porte à la fois une UNITÉ reconnaissable et une valeur
    numérique strictement positive.

    `None` si le classeur n'en porte aucune — on le dit, on n'en invente pas une
    (principe 1). C'est le seul cas de refus qui subsiste après généralisation.
    """
    for ligne in lignes:
        libelle = ligne.get("Rubrique")
        unite = _unite_du_libelle(libelle)
        if unite is None:
            continue
        quantite = _decimal(ligne.get("Valeur"))
        if quantite is None or quantite <= 0:
            continue
        return quantite, unite, str(libelle).strip()
    return None


def _lignes_optionnelles(source, nom_table: str) -> list[dict] | None:
    """Comme `_lignes`, mais `None` si la table n'a pas été ingérée."""
    table = source.tables.filter(name=nom_table).first()
    if table is None:
        return None
    return [r.values for r in table.records.order_by("row_index")]


def _colonne(ligne: dict, *fragments: str):
    """Première valeur dont l'EN-TÊTE contient l'un des fragments (sans accents).

    Les quatorze simulateurs n'écrivent pas leurs en-têtes à l'identique
    (« Réf. min » / « Référence min »). Chercher par fragment normalisé évite de
    maintenir une table d'alias par filière, et une colonne introuvable rend
    `None` — jamais une valeur voisine prise par erreur de position.
    """
    for entete, valeur in ligne.items():
        plat = normalize(entete)
        if any(fragment in plat for fragment in fragments):
            return valeur
    return None


def lire_tresorerie(lignes: list[dict] | None, quantite: Decimal) -> dict:
    """Calendrier de trésorerie du cycle, ramené à l'unité de référence.

    Ne retient que les lignes dont le libellé est une PÉRIODE canonique et dont
    les mois sont des entiers cohérents (`1 <= début <= fin`). Tout le reste de
    la feuille — matrice de flux décalée, lignes de synthèse, phrase de lecture —
    est écarté : mieux vaut un calendrier partiel qu'un calendrier où un montant
    a été lu comme un numéro de mois.

    Rend `{}` si rien d'exploitable : le moteur retombe alors sur l'étalement à
    plat, en le disant.
    """
    if not lignes or quantite <= 0:
        return {}

    periodes: list[dict] = []
    total = Decimal(0)
    mois_vente: int | None = None

    for ligne in lignes:
        libelle = normalize(_colonne(ligne, "periode"))
        canonique = _PERIODES_NORM.get(libelle)
        if canonique is None:
            continue

        debut = _decimal(_colonne(ligne, "mois debut"))
        fin = _decimal(_colonne(ligne, "mois fin"))
        montant = _decimal(_colonne(ligne, "montant (usd)", "montant total", "montant"))
        if debut is None or fin is None or montant is None:
            continue
        if debut != debut.to_integral_value() or fin != fin.to_integral_value():
            continue
        debut, fin = int(debut), int(fin)
        if debut < 1 or fin < debut:
            continue

        total += montant
        periodes.append({
            "periode": canonique,
            "montantUnite": str((montant / quantite).quantize(Decimal("0.01"))),
            "moisDebut": debut,
            "moisFin": fin,
        })
        if libelle == _PERIODE_VENTE:
            mois_vente = fin

    if not periodes:
        return {}

    mois_cycle = max(p["moisFin"] for p in periodes)
    return {
        "periodes": periodes,
        "moisCycle": mois_cycle,
        # Mois d'encaissement de la recette : fin de la période de
        # commercialisation. À défaut, la fin du cycle — jamais le mois 1, qui
        # ferait apparaître une trésorerie disponible avant toute production.
        "moisCommercialisation": mois_vente or mois_cycle,
        "totalUnite": str((total / quantite).quantize(Decimal("0.01"))),
    }


def lire_controles(lignes: list[dict] | None) -> list[dict]:
    """Plages de vraisemblance par indicateur technique (niveau N3).

    Ne retient que les lignes qui portent au moins une borne numérique : une
    ligne « Fenêtre de semis respectée » sans min ni max n'est pas un contrôle
    automatisable, et la garder ferait afficher une plage vide comme une
    référence. La ligne « Synthèse » est écartée par la même règle.
    """
    if not lignes:
        return []

    controles: list[dict] = []
    for ligne in lignes:
        indicateur = _colonne(ligne, "indicateur")
        if not indicateur or not str(indicateur).strip():
            continue
        if normalize(indicateur).startswith("synthese"):
            continue

        borne_min = _decimal(_colonne(ligne, "ref. min", "reference min", "min"))
        borne_max = _decimal(_colonne(ligne, "ref. max", "reference max", "max"))
        if borne_min is None and borne_max is None:
            continue

        controle = {"indicateur": str(indicateur).strip()}
        if borne_min is not None:
            controle["min"] = str(borne_min)
        if borne_max is not None:
            controle["max"] = str(borne_max)
        commentaire = _colonne(ligne, "commentaire")
        if commentaire and str(commentaire).strip():
            controle["commentaire"] = str(commentaire).strip()
        controles.append(controle)

    return controles


def charger_depuis_simulateur(source) -> dict:
    """Données d'un `ReferentielFiliere`, lues dans un simulateur ingéré.

    Lève `ReferentielIntrouvable` plutôt que de compléter ce qui manque : un
    référentiel partiellement inventé est pire qu'un référentiel absent, parce
    qu'il score sans le dire.
    """
    nom = re.search(_NOM_SIMULATEUR, source.original_name or "")
    if nom is None:
        raise ReferentielIntrouvable(
            f"« {source.original_name} » ne suit pas la convention "
            "AGRICAP_FIN_SIM_NN_Famille_Culture.xlsx."
        )

    identification = _lignes(source, "2_Identification_Projet")
    dimension = lire_dimension_reference(identification)
    if dimension is None:
        raise ReferentielIntrouvable(
            f"Dimension de référence absente ou nulle dans « {source.original_name} » : "
            "aucune rubrique de « 2_Identification_Projet » ne porte à la fois une "
            "unité reconnue (ha, m², t, ruches, sujets, sacs) et une quantité "
            "strictement positive. Sans elle, les coûts du cycle ne se ramènent à "
            "aucune unité, et le moteur comparerait un plan à une référence de 0."
        )
    quantite, unite, libelle_dimension = dimension

    # ── Coûts par module, ramenés à l'unité de référence de la filière ───────
    #
    # Ce que ce bloc taisait : `rubrique_to_module` rend `None` aussi bien pour
    # la ligne TOTAL (à sauter, légitimement) que pour une rubrique QU'IL NE
    # SAIT PAS CLASSER. Les deux tombaient dans le même `continue`, et les
    # secondes disparaissaient de la référence sans laisser de trace. Sur les
    # simulateurs à libellés locaux — « Alimentation », « Substrat & blanc de
    # champignon », « Fonctionnement du moulin » — cela retirait jusqu'à 77,8 %
    # des coûts d'une filière, d'un seul côté de la comparaison.
    #
    # La couverture est donc mesurée AVANT, journalisée, et publiée dans le
    # lignage : un référentiel amputé reste chargeable — refuser bloquerait
    # l'instruction de toute une filière — mais il ne peut plus l'être en
    # silence.
    lignes_synthese = list(_lignes(source, "5_Synthese_Besoins"))
    couverture = analyser_couverture_rubriques(
        ((ligne.get("Rubrique"), ligne.get("Total rubrique")) for ligne in lignes_synthese),
        origine=source.original_name or f"DataSource#{source.pk}",
    )

    # Somme PAR MODULE, et non affectation : plusieurs rubriques d'un même
    # classeur peuvent relever du même module (un cycle avicole porte
    # « Poussins & produits vétérinaires » ET « Alimentation », deux intrants).
    # L'écriture directe `couts[module] = …` gardait la DERNIÈRE et perdait les
    # précédentes — une seconde amputation silencieuse, invisible tant qu'aucun
    # classeur n'avait deux rubriques par module. Elle serait apparue au premier
    # mapping complété : elle est corrigée avec lui.
    montants: dict[str, Decimal] = {}
    total_lu = Decimal(0)
    for ligne in lignes_synthese:
        module = rubrique_to_module(ligne.get("Rubrique"))
        if module is None:          # ligne TOTAL, ou rubrique non reconnue
            continue
        montant = _decimal(ligne.get("Total rubrique"))
        if montant is None:
            continue
        total_lu += montant
        montants[module] = montants.get(module, Decimal(0)) + montant

    couts: dict[str, dict[str, str]] = {
        module: {
            "ref": str((montant / quantite).quantize(Decimal("0.01"))),
            **TOLERANCES_PAR_MODULE.get(module, TOLERANCE_DEFAUT),
        }
        for module, montant in montants.items()
    }

    if not couts:
        raise ReferentielIntrouvable(
            f"Aucune rubrique exploitable dans « {source.original_name} » : "
            "vérifier la feuille 5_Synthese_Besoins."
        )

    # ── Rendement de référence ───────────────────────────────────────────────
    # `qte_unite` = production attendue PAR UNITÉ DE RÉFÉRENCE (par ha, par ruche,
    # par sujet…). C'est le numérateur du DSCR projeté : le diviser par la bonne
    # dimension est ce qui rend le revenu prévisionnel homogène au plan de coûts.
    rendement = {}
    for ligne in _lignes(source, "3_Parametres_Techniques"):
        qte = _decimal(ligne.get("Rendement attendu"))
        prix = _decimal(ligne.get("Prix prévu"))
        if qte and prix:
            rendement = {
                "qte_unite": str((qte / quantite).quantize(Decimal("0.001"))),
                "prix_unitaire": str(prix),
                "unite": (ligne.get("Unité") or "t").strip(),
            }
            break

    # ── Feuilles de diagnostic (optionnelles) ────────────────────────────────
    # Elles ne conditionnent pas le chargement : une filière dont le simulateur
    # ne porte pas ces feuilles reste scorable, et le lignage dit lesquelles
    # manquaient. C'est l'inverse d'un silence — un référentiel amputé se voit
    # dans sa propre trace d'audit.
    lignes_tresorerie = _lignes_optionnelles(source, SHEET_TRESORERIE)
    lignes_controles = _lignes_optionnelles(source, SHEET_CONTROLES)
    tresorerie = lire_tresorerie(lignes_tresorerie, quantite)
    controles = lire_controles(lignes_controles)

    feuilles_absentes = [nom_feuille for nom_feuille, lignes_lues in (
        (SHEET_TRESORERIE, lignes_tresorerie), (SHEET_CONTROLES, lignes_controles),
    ) if lignes_lues is None]
    feuilles_illisibles = [nom_feuille for nom_feuille, contenu, lignes_lues in (
        (SHEET_TRESORERIE, tresorerie, lignes_tresorerie),
        (SHEET_CONTROLES, controles, lignes_controles),
    ) if lignes_lues is not None and not contenu]

    production = _valeur_par_rubrique(identification, "production principale")
    famille = nom.group("famille").replace("-", " ")

    return {
        "code": (source.original_name or "").removesuffix(".xlsx"),
        "filiere": f"{famille} — {production}" if production else famille,
        "value_chain_code": nom.group("numero"),
        "unite_reference": unite,
        "devise": "USD",
        "couts_modules": couts,
        "rendement_ref": rendement,
        "tresorerie_ref": tresorerie,
        "controles_ref": controles,
        "n_cas_reels": 0,
        # `indicatif` tant qu'aucun dossier réel n'a alimenté la boucle
        # d'apprentissage : une plage issue d'un simulateur n'a pas l'autorité
        # d'une plage apprise sur 30 dossiers (principe 10).
        "source": "indicatif",
        # Traçabilité : quel classeur, quelle révision, quelle dimension, quel
        # total lu. `superficieReference` est conservé (même valeur que
        # `quantiteReference`) : c'est la clé que lisent la commande d'ingestion
        # et les tests existants, et la renommer sans transition casserait une
        # trace d'audit pour un gain cosmétique.
        "_lignage": {
            "dataSourceId": source.pk,
            "revision": source.revision,
            "quantiteReference": str(quantite),
            "uniteReference": unite,
            "libelleDimension": libelle_dimension,
            "superficieReference": str(quantite),
            "totalCycleLu": str(total_lu),
            # Part des coûts du classeur qui n'est entrée dans AUCUN module, et
            # les libellés en cause. Publiée dans le lignage plutôt que dans un
            # log seul : la commande d'ingestion l'affiche, le test de
            # non-régression l'assère, et un référentiel amputé se voit dans sa
            # propre trace d'audit.
            "couvertureRubriques": couverture,
            # Feuilles de diagnostic : ce qui a été lu, ce qui manquait, ce qui
            # était présent mais inexploitable. Sans cette distinction, un
            # calendrier vide se lirait comme « cette filière n'en a pas »,
            # alors qu'il peut signifier « la feuille est là mais mal ingérée ».
            "feuillesDiagnostic": {
                "tresoreriePeriodes": len(tresorerie.get("periodes", [])),
                "controles": len(controles),
                "absentes": feuilles_absentes,
                "presentesMaisInexploitables": feuilles_illisibles,
            },
        },
    }


def simulateurs_disponibles():
    """Les simulateurs courants ingérés, triés par numéro de filière."""
    from dataio.models import DataSource
    sources = DataSource.objects.filter(kind="SIMULATEUR", is_current=True)
    retenus = [s for s in sources if re.search(_NOM_SIMULATEUR, s.original_name or "")]
    return sorted(retenus, key=lambda s: s.original_name or "")
