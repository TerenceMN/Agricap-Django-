"""
Feuille de besoins client → DataSource `dataio` (chantier 1 de la SPEC).

Principe 1 du module : **ce qui est scoré = ce qui est en base.** Le classeur du
client n'est plus lu en mémoire puis jeté : il est validé, ingéré en tables
génériques (`DataSource → DataTable → DataColumn → DataRecord`) et rattaché au
dossier. La simulation et le scoring lisent ensuite les `DataRecord` de la
révision courante — jamais le fichier, jamais un payload client.

Pipeline (principe 5 — valider avant d'ingérer, ingérer avant de calculer) :

    upload → structure (feuilles, colonnes, rubriques)
           → types (quantité / prix / total numériques ≥ 0)
           → cohérence interne (Σ feuille 4 par rubrique = feuille 5, TOTAL = Σ)
           → ingestion dataio (commit, nouvelle révision)
           → extraction des totaux depuis les DataRecord

Arrêt au premier étage en échec, mais TOUTES les erreurs de cet étage sont
collectées et renvoyées en 422 structuré `{code, message}` — jamais un message
générique.
"""
from __future__ import annotations

import unicodedata
from decimal import Decimal, InvalidOperation

import openpyxl

from dataio import services as dataio_services
from dataio.models import DataSource, KIND_FEUILLE_BESOINS
from dataio.services_templates import validate_structure
from common.money import quantifier

CENT = Decimal("0.01")

SHEET_BESOINS = "4_Besoins_Financiers"
SHEET_SYNTHESE = "5_Synthese_Besoins"
REQUIRED_SHEETS = (SHEET_BESOINS, SHEET_SYNTHESE)

#: Les 8 modules du simulateur — nomenclature canonique (cf. `NeedItem.MODULES`).
MODULE_CODES = (
    "semences", "mecanisation", "maindoeuvre", "equipements",
    "postrecolte", "logistique", "commercialisation", "reserve",
)

MODULE_LABELS = {
    "semences": "Semences & Intrants",
    "mecanisation": "Opérations mécanisées",
    "maindoeuvre": "Main d'œuvre",
    "equipements": "Équipement & petit matériel",
    "postrecolte": "Récolte & post-récolte",
    "logistique": "Logistique",
    "commercialisation": "Commercialisation",
    "reserve": "Réserve d'exploitation",
}

#: Rôles de colonne attendus en feuille 4 → fragments d'en-tête acceptés.
#: L'ordre des rôles compte : « coût unitaire » est cherché avant « total », sinon
#: « Montant total » capterait le rôle prix.
COLUMN_ROLES_F4 = (
    ("rubrique", "Rubrique", ("rubrique",)),
    ("designation", "Désignation", ("description", "designation", "libelle", "intitule")),
    ("quantite", "Quantité", ("quantite", "qte")),
    ("prix_unitaire", "Prix unitaire", ("cout unitaire", "prix unitaire", "prix/u", "cout/u")),
    ("total", "Total", ("montant total", "total")),
)

COLUMN_ROLES_F5 = (
    ("rubrique", "Rubrique", ("rubrique",)),
    ("total", "Total rubrique", ("total rubrique", "montant total", "total", "montant")),
)

#: Colonne facultative de la feuille 4 : le total ligne vaut Qté × PU × Fréquence.
FREQUENCE_FRAGMENTS = ("frequence", "nb de fois", "recurrence")

#: Colonnes de la feuille 4 qui ENRICHISSENT une ligne sans conditionner sa
#: validité : leur absence n'est pas une erreur de structure. Tenues à part de
#: `COLUMN_ROLES_F4` pour que le contrôle « colonne manquante » continue de ne
#: porter que sur ce qui est indispensable au calcul.
COLUMN_ROLES_F4_OPTION = (
    ("unite", "Unité", ("unite",)),
    ("fournisseur", "Fournisseur", ("fournisseur", "prestataire", "vendeur")),
    ("observations", "Observations", ("observation", "remarque", "commentaire")),
)


class NeedsSheetValidationError(Exception):
    """Le classeur est refusé. `errors` = liste de `{code, message}` (→ 422)."""

    def __init__(self, errors: list[dict]) -> None:
        self.errors = errors
        super().__init__("; ".join(e["message"] for e in errors))


class NeedsSheetReadError(Exception):
    """Le fichier ne porte pas les feuilles d'une feuille de besoins AGRICAP."""


# ── Normalisation ─────────────────────────────────────────────────────────────

#: Ligatures que la décomposition Unicode NFD ne défait PAS : « œ » et « æ » sont
#: des lettres à part entière, pas des lettres accentuées. Sans ce repli, la
#: rubrique « Main d'œuvre » des classeurs ne contenait PAS la chaîne « oeuvre »
#: — le fragment de mapping prévu pour elle ne servait à rien, et c'est le
#: fragment « main » qui la rattrapait… en attrapant aussi « Maintenance ».
_LIGATURES = {"œ": "oe", "Œ": "oe", "æ": "ae", "Æ": "ae"}


def normalize(text) -> str:
    """Minuscule, sans accent NI ligature, espaces compactés — base de toute
    comparaison de libellés."""
    if text is None:
        return ""
    s = str(text)
    for ligature, remplacement in _LIGATURES.items():
        s = s.replace(ligature, remplacement)
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return " ".join(s.lower().split())


#: Fragment de rubrique (normalisé) → code module. Premier fragment trouvé gagne,
#: d'où l'ordre : « equipement » avant « materiel », « mecanis » avant
#: « operation », « commercialisation » avant « conditionnement ».
#:
#: POURQUOI CETTE TABLE S'EST ALLONGÉE (défaut mesuré, juillet 2026)
#: -----------------------------------------------------------------
#: Les classeurs du client suivent le template officiel : leurs 8 rubriques se
#: mappent toutes. Les simulateurs INSTITUTIONNELS, eux, renomment librement les
#: rubriques selon la filière (« Alimentation » pour du poulet, « Substrat &
#: blanc de champignon » pour des pleurotes). Sept simulateurs sur quatorze
#: portaient ainsi des libellés qu'aucun fragment ne reconnaissait — et
#: `referentiel_loader` les ignorait EN SILENCE.
#:
#: L'effet n'est pas une perte symétrique : c'est une ASYMÉTRIE. Le plan du
#: demandeur était lu en entier, la référence de sa filière amputée. Sur le
#: poulet de chair, « Alimentation » (2 920 USD, le premier poste d'un cycle
#: avicole) n'entrait pas dans la référence : 77,8 % des coûts manquaient d'un
#: seul côté de la comparaison, et tout demandeur de la filière paraissait
#: massivement surcoté. Un faux refus systématique, invisible.
#:
#: Les fragments ajoutés le sont sur un raisonnement SÉMANTIQUE, jamais
#: positionnel : les classeurs ont bien 8 rubriques dans le même ordre, mais
#: leurs auteurs ont réutilisé les emplacements librement (l'alimentation d'un
#: élevage occupe la place des « opérations mécanisées » d'une culture). Se fier
#: au rang aurait rangé de l'aliment dans la mécanisation.
_RUBRIQUE_FRAGMENTS: tuple[tuple[str, str], ...] = (
    # ── Intrants (module « Semences & Intrants ») ─────────────────────────────
    ("semence", "semences"),
    ("intrant", "semences"),
    #: Alimentation animale : premier poste d'un cycle d'élevage, et un INTRANT
    #: au sens plein. ARBITRAGE SIGNALÉ AU FONDATEUR : le module s'appelle
    #: « Semences & Intrants », libellé pensé pour les cultures. Si
    #: l'institution veut suivre l'aliment à part, cela demande un 9e module au
    #: référentiel — décision de nomenclature, pas de code (principe 6).
    ("alimentation", "semences"),
    ("poussin", "semences"),        # jeunes sujets = cheptel de départ
    ("alevin", "semences"),
    ("geniteur", "semences"),
    ("colonie", "semences"),        # colonies d'abeilles peuplant les ruches
    ("substrat", "semences"),       # substrat + blanc de champignon = la « semence »
    ("plants", "semences"),         # plants forestiers / agroforestiers
    ("consommable", "semences"),
    # ── Opérations de production (module « Opérations mécanisées ») ───────────
    ("mecanis", "mecanisation"),
    ("operation", "mecanisation"),
    ("conduite", "mecanisation"),       # conduite du rucher / de production
    ("fonctionnement", "mecanisation"),  # fonctionnement du moulin
    ("preparation", "mecanisation"),
    ("sterilisation", "mecanisation"),
    # ── Main-d'œuvre ─────────────────────────────────────────────────────────
    #: « oeuvre » et non « main » : le fragment « main » attrapait « Maintenance
    #: & pièces » et comptait 360 USD d'entretien en salaires. Un poste MAL
    #: classé fausse le poids du module autant qu'un poste absent — et il est
    #: plus difficile à voir, puisque rien ne manque.
    ("oeuvre", "maindoeuvre"),
    # ── Équipement ───────────────────────────────────────────────────────────
    ("equipement", "equipements"),
    ("materiel", "equipements"),
    # ── Récolte et aval de production ────────────────────────────────────────
    ("recolte", "postrecolte"),
    ("peche", "postrecolte"),        # pêche & conditionnement (aquaculture)
    ("extraction", "postrecolte"),   # extraction du miel
    ("transformation", "postrecolte"),
    # ── Aval commercial ──────────────────────────────────────────────────────
    ("logistique", "logistique"),
    ("commercialisation", "commercialisation"),
    ("reserve", "reserve"),
)

#: Rubriques dont le rattachement est RÉELLEMENT ambigu, et qu'on refuse de
#: ranger arbitrairement. Elles restent non classées — donc signalées — jusqu'à
#: arbitrage. Les nommer ici ne les excuse pas : cela permet à un test de
#: distinguer une ambiguïté connue et arbitrée d'un libellé NOUVEAU qui, lui,
#: doit faire échouer le garde-fou.
#:
#:   « Maintenance & pièces » (simulateur 14, moulin à maïs, 360 USD) —
#:   entre `equipements` (des pièces sont du petit matériel) et `mecanisation`
#:   (l'entretien fait partie du fonctionnement de la machine). Recommandation
#:   au fondateur : `equipements`. Tant qu'il n'a pas tranché, ces 360 USD
#:   restent HORS référence et le disent, plutôt que d'être rangés au hasard.
RUBRIQUES_AMBIGUES: tuple[str, ...] = (
    "maintenance",
)


def rubrique_to_module(rubrique) -> str | None:
    """Code module d'une rubrique du classeur, ou `None` si non reconnue."""
    low = normalize(rubrique)
    if not low or is_total_row(low):
        return None
    for fragment, code in _RUBRIQUE_FRAGMENTS:
        if fragment in low:
            return code
    return None


def est_ambigue(rubrique) -> bool:
    """La rubrique fait-elle partie des ambiguïtés connues et assumées ?"""
    low = normalize(rubrique)
    return any(fragment in low for fragment in RUBRIQUES_AMBIGUES)


def analyser_couverture_rubriques(paires, *, origine: str = "") -> dict:
    """Ce que le mapping a classé, et ce qu'il a laissé tomber — à voix haute.

    `paires` : itérable de `(libellé de rubrique, montant `Decimal` ou None)`.

    Un coût qu'on ne sait pas classer est une INFORMATION, pas un zéro. Cette
    fonction est le contraire du `continue` silencieux qui a coûté 77,8 % de la
    référence avicole : elle mesure la part non reconnue, la journalise en
    warning, et la rend exploitable par l'appelant (rapport d'ingestion, rapport
    d'analyse, test de non-régression).

    Elle ne décide rien et ne lève rien : la lecture d'un référentiel ne doit pas
    échouer parce qu'une rubrique est inconnue — elle doit le DIRE.
    """
    import logging

    total = Decimal("0")
    total_classe = Decimal("0")
    non_reconnues: list[dict] = []

    for libelle, montant in paires:
        if libelle is None or not str(libelle).strip():
            continue
        if is_total_row(libelle):
            continue
        valeur = montant if isinstance(montant, Decimal) else to_decimal(montant)
        if valeur is None:
            continue
        total += valeur
        if rubrique_to_module(libelle) is None:
            non_reconnues.append({
                "rubrique": str(libelle).strip(),
                "montant": str(quantifier(valeur)),
                "arbitrageEnAttente": est_ambigue(libelle),
            })
        else:
            total_classe += valeur

    non_reconnu = total - total_classe
    part = (
        (non_reconnu / total * Decimal(100)).quantize(CENT)
        if total > 0 else Decimal("0.00")
    )

    if non_reconnues:
        logging.getLogger(__name__).warning(
            "Rubriques non classées%s : %s USD sur %s (%s %%) — %s. "
            "Ces coûts n'entrent dans AUCUN module : la référence de la filière "
            "est amputée d'autant, et tout dossier comparé à elle paraîtra "
            "surcoté. Compléter le mapping (`_RUBRIQUE_FRAGMENTS`) ou arbitrer "
            "le rattachement.",
            f" ({origine})" if origine else "", quantifier(non_reconnu), quantifier(total), part,
            ", ".join(f"« {e['rubrique']} » {e['montant']}" for e in non_reconnues),
        )

    return {
        "total": quantifier(total),
        "totalClasse": quantifier(total_classe),
        "totalNonReconnu": quantifier(non_reconnu),
        "partNonReconnuePct": part,
        "nonReconnues": non_reconnues,
    }


def is_total_row(rubrique) -> bool:
    """True pour la ligne « TOTAL GÉNÉRAL » (et ses variantes) de la feuille 5."""
    low = normalize(rubrique)
    return low.startswith("total") or "total general" in low


def to_decimal(value) -> Decimal | None:
    """Décimal d'une cellule Excel OU d'une valeur `DataRecord` (toujours du texte).

    `None` si la valeur n'est pas un nombre — le contrôle `TYPE_INVALIDE` s'appuie
    dessus. Aucun `float` n'est utilisé : principe 4.
    """
    if value is None or value == "":
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return Decimal(str(value))
    s = str(value).strip().replace(" ", "").replace(" ", "")
    if not s:
        return None
    # 1 234,56 → 1234.56 ; on ne devine jamais un séparateur de milliers ambigu.
    if "," in s and "." not in s:
        s = s.replace(",", ".")
    try:
        return Decimal(s)
    except (InvalidOperation, ValueError):
        return None


# ── Repérage des colonnes ─────────────────────────────────────────────────────

def _map_columns(header: list[str], roles) -> dict[str, int]:
    """Rôle de colonne → index, en respectant l'ordre de priorité des rôles."""
    taken: set[int] = set()
    found: dict[str, int] = {}
    normalized = [normalize(h) for h in header]
    for role, _label, fragments in roles:
        for fragment in fragments:
            idx = next(
                (i for i, h in enumerate(normalized)
                 if i not in taken and h and fragment in h),
                None,
            )
            if idx is not None:
                found[role] = idx
                taken.add(idx)
                break
    return found


def _find_frequence(header: list[str], used: set[int]) -> int | None:
    for i, h in enumerate(header):
        if i in used:
            continue
        n = normalize(h)
        if any(f in n for f in FREQUENCE_FRAGMENTS):
            return i
    return None


def _cell(row: tuple, idx: int | None):
    if idx is None or idx >= len(row):
        return None
    return row[idx]


# ── Validation (les 6 contrôles de la SPEC) ───────────────────────────────────

def validate_needs_sheet(path: str, value_chain_code: str = "") -> tuple[list[dict], dict]:
    """Valide un classeur de feuille de besoins CONTRE le template actif (principe 11).

    `value_chain_code` (la filière du dossier, `MAIS`/`RIZ`/…) est résolu vers sa chaîne
    `01`-`14` : c'est le template de CETTE chaîne qui sert de règle, le même que celui
    téléchargé par le client (`GET needs-sheet-template/?value_chain_code=...`). Omis,
    le template générique du kind est utilisé (compatibilité).

    Renvoie `(errors, template_ref)` :
      - `errors` : liste de `{code, message}` (→ 422), `[]` si tout passe ;
      - `template_ref` : `{templateId, version}` du template ACTIF ayant servi à la
        validation structurelle — à consigner dans le rapport de validation, pour
        que le dossier garde la référence de SA version de template. `{}` si aucun
        template actif (l'unique erreur est alors `TEMPLATE_NOT_CONFIGURED`).

    Pipeline (principe 5, arrêt au premier étage en échec) :
      structure (feuilles / colonnes / rubriques) → types → cohérence interne.

    La FORME n'est plus comparée à un schéma codé en dur (`REQUIRED_SHEETS`,
    `COLUMN_ROLES_*`) mais au schéma **dérivé du template actif**, via
    `dataio.validate_structure` : le fichier servi au client
    (`GET needs-sheet-template/`) est exactement celui contre lequel on valide —
    une seule source (principe 11). Les contrôles de type et de cohérence (N2)
    restent métier et vivent ici.
    """
    # ── Étages 1-2 : structure, contre le template ACTIF (principe 11) ────────
    from common.filieres import numero_pour
    chain_code = numero_pour(value_chain_code) if value_chain_code else ""
    struct_errors, template_ref = validate_structure(path, KIND_FEUILLE_BESOINS, chain_code=chain_code)
    if not template_ref:
        # Aucun template actif → refus explicite (TEMPLATE_NOT_CONFIGURED),
        # jamais une validation « best effort » contre un schéma de repli.
        return struct_errors, {}
    if struct_errors:
        return struct_errors, template_ref

    # ── Étages 3-4 : types + cohérence interne (métier, N2) ───────────────────
    try:
        wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    except Exception as exc:  # classeur illisible / corrompu
        return ([{"code": "CLASSEUR_ILLISIBLE",
                  "message": f"Le classeur n'a pas pu être ouvert : {exc}"}],
                template_ref)
    try:
        return _validate_values(wb), template_ref
    finally:
        wb.close()


def _validate_values(wb) -> list[dict]:
    """Types et cohérence interne, la structure ayant déjà été validée en amont.

    Défense en profondeur : si une feuille ou une colonne nécessaire à la LECTURE
    des montants manque malgré tout (template actif incohérent), on le signale au
    lieu de lever un `KeyError` — le chemin nominal ne l'atteint jamais, la
    structure ayant été validée contre le template actif.
    """
    sheets = {normalize(n): n for n in wb.sheetnames}
    if (normalize(SHEET_BESOINS) not in sheets
            or normalize(SHEET_SYNTHESE) not in sheets):
        return [{
            "code": "FEUILLE_MANQUANTE",
            "message": ("Feuilles de besoins introuvables après validation "
                        "structurelle — le template actif ne décrit pas une feuille "
                        "de besoins AGRICAP standard."),
        }]

    ws4 = wb[sheets[normalize(SHEET_BESOINS)]]
    ws5 = wb[sheets[normalize(SHEET_SYNTHESE)]]
    header4, rows4 = dataio_services.sheet_rows(ws4)
    header5, rows5 = dataio_services.sheet_rows(ws5)

    cols4 = _map_columns(header4, COLUMN_ROLES_F4)
    cols5 = _map_columns(header5, COLUMN_ROLES_F5)

    if ("rubrique" not in cols4 or "total" not in cols4
            or "rubrique" not in cols5 or "total" not in cols5):
        return [{
            "code": "COLONNE_MANQUANTE",
            "message": ("Colonnes nécessaires à la lecture des montants introuvables "
                        "malgré une structure validée — vérifiez le template actif."),
        }]

    errors: list[dict] = []

    # ── Étage 3 : types ──────────────────────────────────────────────────────
    freq_idx = _find_frequence(header4, set(cols4.values()))
    parsed4: list[tuple[str, Decimal]] = []   # (module, total ligne)
    for n, row in enumerate(rows4, start=1):
        raw_rubrique = _cell(row, cols4["rubrique"])
        cells = {role: _cell(row, idx) for role, idx in cols4.items()}
        empty = all(c in (None, "") for c in cells.values())
        if empty:
            continue
        # Ligne de sous-total du classeur (« TOTAL BESOINS DU CYCLE ») : elle n'est
        # pas un besoin, et l'additionner doublerait la rubrique correspondante.
        if is_total_row(raw_rubrique):
            continue
        module = rubrique_to_module(raw_rubrique)
        if module is None:
            errors.append({
                "code": "RUBRIQUE_INCONNUE",
                "message": (f"Feuille « {SHEET_BESOINS} », ligne {n} : rubrique "
                            f"« {raw_rubrique or '(vide)'} » non reconnue. Utilisez la "
                            f"liste déroulante des 8 rubriques officielles."),
            })
            continue
        for role, label in (("quantite", "Quantité"), ("prix_unitaire", "Prix unitaire"),
                            ("total", "Total")):
            value = to_decimal(cells.get(role))
            if value is None:
                errors.append({
                    "code": "TYPE_INVALIDE",
                    "message": (f"Feuille « {SHEET_BESOINS} », ligne {n} "
                                f"(« {raw_rubrique} ») : « {label} » doit être un nombre, "
                                f"valeur lue « {cells.get(role)} »."),
                })
            elif value < 0:
                errors.append({
                    "code": "TYPE_INVALIDE",
                    "message": (f"Feuille « {SHEET_BESOINS} », ligne {n} "
                                f"(« {raw_rubrique} ») : « {label} » ne peut pas être "
                                f"négatif ({value})."),
                })
        total = to_decimal(cells.get("total"))
        if total is not None and total >= 0:
            parsed4.append((module, total))

    totals5: dict[str, Decimal] = {}
    grand_total_declared: Decimal | None = None
    for n, row in enumerate(rows5, start=1):
        raw = _cell(row, cols5["rubrique"])
        value = to_decimal(_cell(row, cols5["total"]))
        if is_total_row(raw):
            if value is None:
                errors.append({
                    "code": "TYPE_INVALIDE",
                    "message": (f"Feuille « {SHEET_SYNTHESE} » : « TOTAL GÉNÉRAL » doit "
                                f"être un nombre, valeur lue « {_cell(row, cols5['total'])} »."),
                })
            else:
                grand_total_declared = value
            continue
        code = rubrique_to_module(raw)
        if code is None:
            continue
        if value is None:
            errors.append({
                "code": "TYPE_INVALIDE",
                "message": (f"Feuille « {SHEET_SYNTHESE} », rubrique « {raw} » : le total "
                            f"doit être un nombre, valeur lue "
                            f"« {_cell(row, cols5['total'])} »."),
            })
        elif value < 0:
            errors.append({
                "code": "TYPE_INVALIDE",
                "message": (f"Feuille « {SHEET_SYNTHESE} », rubrique « {raw} » : le total "
                            f"ne peut pas être négatif ({value})."),
            })
        else:
            totals5[code] = totals5.get(code, Decimal("0")) + value
    if errors:
        return errors

    # ── Étage 4 : cohérence interne ──────────────────────────────────────────
    sums4: dict[str, Decimal] = {code: Decimal("0") for code in MODULE_CODES}
    for module, total in parsed4:
        sums4[module] = sums4.get(module, Decimal("0")) + total

    for code in MODULE_CODES:
        detail = quantifier(sums4.get(code, Decimal("0")))
        synthese = quantifier(totals5.get(code, Decimal("0")))
        if abs(detail - synthese) > CENT:
            errors.append({
                "code": "INCOHERENCE_INTERNE",
                "message": (
                    f"Rubrique « {MODULE_LABELS[code]} » : la feuille "
                    f"« {SHEET_SYNTHESE} » annonce {synthese} alors que la somme des "
                    f"lignes de « {SHEET_BESOINS} » vaut {detail} "
                    f"(écart {quantifier(synthese - detail)}). La synthèse doit se calculer "
                    f"depuis le détail — ne saisissez rien directement en feuille 5."
                ),
            })

    total_rubriques = quantifier(sum(totals5.values(), Decimal("0")))
    if grand_total_declared is not None:
        declared = quantifier(grand_total_declared)
        if abs(declared - total_rubriques) > CENT:
            errors.append({
                "code": "TOTAL_INCOHERENT",
                "message": (
                    f"« TOTAL GÉNÉRAL » annoncé {declared} ≠ somme des 8 rubriques "
                    f"{total_rubriques} (écart {quantifier(declared - total_rubriques)})."
                ),
            })
    return errors


# ── Lecture ───────────────────────────────────────────────────────────────────

def _colonnes_facultatives(header: list[str], prises: set[int]) -> dict[str, int]:
    """Rôles facultatifs de la feuille 4, sans jamais voler une colonne déjà tenue
    par un rôle obligatoire."""
    trouves = _map_columns(header, COLUMN_ROLES_F4_OPTION)
    return {role: idx for role, idx in trouves.items() if idx not in prises}


def lire_classeur(path: str) -> dict:
    """Lignes de détail et totaux par module d'un classeur de feuille de besoins.

    LE lecteur, et il n'y en a qu'un (principe 6). `credits/needs_parser.py` en
    portait un second, et c'est ce doublon qui a été payé :

    - il repérait les feuilles **par position** (`wb.worksheets[n-1]`) avec un
      repli par nom qui n'était jamais atteint. Sur un classeur du template
      courant — qui porte `4_Besoins_Financiers` et `5_Synthese_Besoins`, pas six
      feuilles « une par module » — il lisait donc la synthèse en croyant lire la
      post-récolte, le plan de financement en croyant lire la logistique, et
      annonçait des écarts de 50 % entre des grandeurs sans rapport ;
    - il ignorait la colonne **Fréquence**, alors que le total d'une ligne vaut
      Qté × PU × Fréquence : une ligne à 10 × 14 × 2 = 280 était « recalculée »
      à 140 et signalée comme une incohérence du client.

    Les deux défauts produisaient des avertissements sur des dossiers CORRECTS —
    le pire état pour un contrôle, puisqu'il apprend à l'analyste à les ignorer.

    Lit exactement comme `_validate_values` et comme l'ingestion : les feuilles
    par NOM, les colonnes par rôle, les rubriques par le mapping unique. Les
    montants restent en `Decimal` (principe 4) ; la conversion pour l'affichage
    appartient à l'appelant.

    Retourne `{"items", "totalByModule", "grandTotal", "warnings"}`.
    Lève `NeedsSheetReadError` si les feuilles attendues sont absentes.
    """
    try:
        wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    except Exception as exc:  # noqa: BLE001 — classeur illisible / corrompu
        raise NeedsSheetReadError(f"Le classeur n'a pas pu être ouvert : {exc}") from exc
    try:
        sheets = {normalize(n): n for n in wb.sheetnames}
        manquantes = [n for n in REQUIRED_SHEETS if normalize(n) not in sheets]
        if manquantes:
            raise NeedsSheetReadError(
                "Ce classeur n'est pas une feuille de besoins AGRICAP : "
                f"« {' », « '.join(manquantes)} » introuvable(s). Feuilles présentes : "
                f"{', '.join(wb.sheetnames)}. Téléchargez le modèle officiel via "
                "GET /api/credits/needs-sheet-template/."
            )
        header4, rows4 = dataio_services.sheet_rows(wb[sheets[normalize(SHEET_BESOINS)]])
        header5, rows5 = dataio_services.sheet_rows(wb[sheets[normalize(SHEET_SYNTHESE)]])
    finally:
        wb.close()

    cols4 = _map_columns(header4, COLUMN_ROLES_F4)
    cols5 = _map_columns(header5, COLUMN_ROLES_F5)
    if "rubrique" not in cols4 or "total" not in cols4:
        raise NeedsSheetReadError(
            f"Feuille « {SHEET_BESOINS} » : colonnes « Rubrique » et « Montant total » "
            f"introuvables. En-têtes lus : {', '.join(h for h in header4 if h)}."
        )
    if "rubrique" not in cols5 or "total" not in cols5:
        raise NeedsSheetReadError(
            f"Feuille « {SHEET_SYNTHESE} » : colonnes « Rubrique » et « Total rubrique » "
            f"introuvables. En-têtes lus : {', '.join(h for h in header5 if h)}."
        )

    prises = set(cols4.values())
    freq_idx = _find_frequence(header4, prises)
    if freq_idx is not None:
        prises.add(freq_idx)
    opt4 = _colonnes_facultatives(header4, prises)

    items: list[dict] = []
    warnings: list[str] = []
    sums4: dict[str, Decimal] = {code: Decimal("0") for code in MODULE_CODES}

    for n, row in enumerate(rows4, start=1):
        raw_rubrique = _cell(row, cols4["rubrique"])
        if is_total_row(raw_rubrique):
            # « TOTAL BESOINS DU CYCLE » : un report, pas un besoin. L'additionner
            # doublerait la rubrique correspondante.
            continue
        designation = _cell(row, cols4.get("designation"))
        label = str(designation or raw_rubrique or "").strip()
        module = rubrique_to_module(raw_rubrique)
        if module is None:
            if label:
                warnings.append(
                    f"Feuille « {SHEET_BESOINS} », ligne {n} : rubrique "
                    f"« {raw_rubrique or '(vide)'} » non reconnue — cette ligne n'entre "
                    f"dans aucun module et n'est donc comptée nulle part. Utilisez l'une "
                    f"des 8 rubriques officielles."
                )
            continue

        qte = to_decimal(_cell(row, cols4.get("quantite")))
        prix = to_decimal(_cell(row, cols4.get("prix_unitaire")))
        freq = to_decimal(_cell(row, freq_idx)) if freq_idx is not None else None
        declare = to_decimal(_cell(row, cols4["total"]))

        calcule = None
        if qte is not None and prix is not None:
            calcule = qte * prix * (freq if freq is not None else Decimal("1"))

        # Le classeur fait foi sur ce qu'il DÉCLARE : c'est cette colonne que la
        # synthèse totalise et que l'ingestion écrit en base. Le recalcul sert à
        # signaler l'écart, pas à corriger le dossier dans le dos du client.
        retenu = declare if declare is not None else calcule
        if retenu is not None:
            sums4[module] += retenu

        if (declare is not None and calcule is not None
                and abs(quantifier(calcule) - quantifier(declare)) > CENT):
            facteurs = "quantité × prix unitaire"
            if freq is not None and freq != Decimal("1"):
                facteurs += " × fréquence"
            warnings.append(
                f"Feuille « {SHEET_BESOINS} », ligne {n} (« {label} ») : "
                f"« Montant total » annonce {quantifier(declare)} alors que "
                f"{facteurs} vaut {quantifier(calcule)}."
            )

        items.append({
            "module": module,
            "label": label[:255],
            "quantity": str(qte) if qte is not None else None,
            "unit": str(_cell(row, opt4.get("unite")) or "").strip(),
            "unit_price": str(prix) if prix is not None else None,
            "declared_total": str(declare) if declare is not None else None,
            "computed_total": str(calcule) if calcule is not None else None,
            "suggested_supplier": str(_cell(row, opt4.get("fournisseur")) or "").strip(),
            "observations": str(_cell(row, opt4.get("observations")) or "").strip(),
        })

    totals5: dict[str, Decimal] = {code: Decimal("0") for code in MODULE_CODES}
    grand_declare: Decimal | None = None
    for row in rows5:
        raw = _cell(row, cols5["rubrique"])
        valeur = to_decimal(_cell(row, cols5["total"]))
        if is_total_row(raw):
            if valeur is not None and grand_declare is None:
                grand_declare = valeur
            continue
        code = rubrique_to_module(raw)
        if code is not None and valeur is not None:
            totals5[code] += valeur

    # Cohérence interne (N2) — les écarts RÉELS, ceux qui se lisent entre deux
    # grandeurs qui doivent coïncider. Ce sont des avertissements et non des
    # refus : ce chemin sert le parcours client avant création du dossier ; le
    # refus 422 appartient à l'ingestion (`validate_needs_sheet`).
    for code in MODULE_CODES:
        detail = quantifier(sums4[code])
        synthese = quantifier(totals5[code])
        if abs(detail - synthese) > CENT:
            warnings.append(
                f"Rubrique « {MODULE_LABELS[code]} » : la feuille « {SHEET_SYNTHESE} » "
                f"annonce {synthese} alors que la somme des lignes de "
                f"« {SHEET_BESOINS} » vaut {detail} (écart {quantifier(synthese - detail)})."
            )

    total_rubriques = quantifier(sum(totals5.values(), Decimal("0")))
    if grand_declare is not None:
        declare_q = quantifier(grand_declare)
        if abs(declare_q - total_rubriques) > CENT:
            warnings.append(
                f"« TOTAL GÉNÉRAL » annoncé {declare_q} ≠ somme des 8 rubriques "
                f"{total_rubriques} (écart {quantifier(declare_q - total_rubriques)})."
            )

    return {
        "items": items,
        "totalByModule": {code: quantifier(totals5[code]) for code in MODULE_CODES},
        # Le TOTAL GÉNÉRAL du classeur quand il est là — c'est le montant que le
        # client a sous les yeux ; un écart avec la somme des rubriques est déjà
        # signalé ci-dessus plutôt que corrigé en silence.
        "grandTotal": quantifier(grand_declare) if grand_declare is not None else total_rubriques,
        "warnings": warnings,
    }


# ── Ingestion ─────────────────────────────────────────────────────────────────

def dataset_key_for(application) -> str:
    """Une lignée versionnée par dossier : 1 dossier = 1 `dataset_key`."""
    return f"fb__{application.code}"


def parse_and_ingest(file, application, uploaded_by: str = "") -> dict:
    """Valide puis ingère la feuille de besoins d'un dossier.

    Lève `NeedsSheetValidationError` (→ 422 structuré) si un contrôle échoue ; la
    source refusée reste en base au statut STAGED, rattachée au dossier : la
    trajectoire des tentatives est elle-même une donnée d'analyse (principe 4.3).

    Retourne `{needs_source_id, revision, sha256, totals, grand_total}`.
    """
    source = DataSource(
        original_name=file.name[:255],
        dataset_key=dataset_key_for(application),
        credit_application=application,
        uploaded_by=uploaded_by or "",
    )
    source.file = file
    source.save()

    dataio_services.inspect(source)   # pose kind + sha256, n'écrit aucune ligne

    if source.kind != KIND_FEUILLE_BESOINS:
        raise NeedsSheetValidationError([{
            "code": "CLASSEUR_NON_RECONNU",
            "message": (
                "Ce classeur n'est pas une feuille de besoins AGRICAP : les feuilles "
                f"« {SHEET_BESOINS} » et « {SHEET_SYNTHESE} » doivent être présentes "
                "dans un classeur d'au plus 10 feuilles. Téléchargez le modèle officiel "
                "via GET /api/credits/needs-sheet-template/."
            ),
        }])

    # Validation structurelle CONTRE le template actif (principe 11) + N2.
    # `template_ref` = {templateId, version} de la règle de validation appliquée.
    vc_code = application.value_chain.code if application.value_chain_id else ""
    errors, template_ref = validate_needs_sheet(source.file.path, vc_code)
    if errors:
        raise NeedsSheetValidationError(errors)

    # Inventaire des feuilles AVANT ingestion : c'est lui qui décide de ce qui
    # entre en base, et c'est lui qu'on journalise.
    inventaire = rapport_feuilles(source.file.path)

    # Toutes les feuilles de DONNÉES du template, et plus seulement 4 et 5.
    #
    # Le classeur client en porte six (identification, paramètres techniques,
    # besoins, synthèse, plan de financement, plan de décaissement) ; deux
    # étaient ingérées. Les quatre autres étaient validées à l'upload puis
    # jetées : leur contenu n'atteignait jamais la base, donc jamais le moteur.
    # C'est ce qui obligeait à ressaisir à la main la dimension du projet, que
    # le client avait pourtant déclarée en « 2_Identification_Projet ».
    #
    # Repli sur les deux feuilles historiques si l'inventaire n'a rien produit
    # (aucun template actif) : on ne dégrade jamais l'ingestion existante.
    feuilles = inventaire.get("ingerees") or list(dataio_services.FEUILLE_BESOINS_SHEETS)
    dataio_services.commit(source, by=uploaded_by or "", sheets=feuilles)

    application.needs_source = source
    application.save(update_fields=["needs_source", "updated_at"])

    # Rapport de validation : le dossier garde la référence de SA version de
    # template (principe 11). Journalisé (append-only) plutôt que stocké dans un
    # nouveau champ — aucune migration, et l'auditeur reconstitue quelle règle de
    # validation a été appliquée à quelle révision de la feuille de besoins.
    _record_validation(source, application, template_ref, by=uploaded_by or "",
                       inventaire=inventaire)

    totals = extract_module_totals(source)
    return {
        "needs_source_id": source.pk,
        "revision": source.revision,
        "sha256": source.sha256,
        "templateId": template_ref.get("templateId"),
        "templateVersion": template_ref.get("version"),
        # Inventaire des feuilles : l'agent de terrain voit ce qui a été lu et ce
        # qui a été ignoré, au lieu de le supposer.
        "feuilles": {k: v for k, v in inventaire.items() if k != "template"},
        "totals": {k: str(v) for k, v in totals.items()},
        "grand_total": str(quantifier(sum(totals.values(), Decimal("0")))),
    }


#: Feuilles du template qui sont de la MISE EN FORME, jamais des données : les
#: ingérer créerait des tables vides que le moteur devrait apprendre à ignorer.
#:
#: Liste par EXCLUSION et non par inclusion, et c'est délibéré : une feuille de
#: données ajoutée au template par l'admin est alors ingérée d'office, sans
#: modification de code (principe 11 — le template fait la règle). L'inverse —
#: une liste blanche — est ce qui a fait qu'on n'ingérait que deux feuilles sur
#: huit pendant que le classeur en portait six de données.
FEUILLES_MISE_EN_FORME = ("REF_Listes", "1_Accueil_Instructions", "1_Accueil_Parametres")


def _feuilles_de_donnees(schema: dict) -> list[str]:
    """Feuilles du template actif qui portent des données, dans l'ordre du classeur."""
    exclues = {dataio_services._norm(n) for n in FEUILLES_MISE_EN_FORME}
    return [s["name"] for s in schema.get("sheets", [])
            if dataio_services._norm(s["name"]) not in exclues]


def rapport_feuilles(path: str) -> dict:
    """Inventaire des feuilles du classeur client face au template ACTIF.

    Ce que la spec du moteur exige et qui manquait : « parcourir l'ensemble des
    feuilles du classeur ; les feuilles non reconnues sont ignorées ou
    SIGNALÉES ». La validation structurelle vérifiait déjà que les feuilles du
    template sont présentes chez le client ; elle ne disait rien de l'inverse —
    une feuille que le client ajoute (« Mes calculs », « Feuil1 ») disparaissait
    sans trace, et une feuille du template présente mais non ingérée aussi.

    Le rapport distingue quatre états, parce qu'ils appellent quatre réactions
    différentes de l'agent de terrain :

      - `reconnues`        : au template, donc lues ;
      - `ingerees`         : reconnues ET porteuses de données écrites en base
                             (ce sont les seules que le scoring pourra lire) ;
      - `nonReconnues`     : présentes chez le client, absentes du template —
                             ignorées, mais l'agent doit savoir que le client y a
                             peut-être mis quelque chose ;
      - `attenduesAbsentes`: au template, absentes du classeur (l'erreur
                             `FEUILLE_MANQUANTE` les porte déjà, on les répète
                             ici pour que le rapport se lise seul).

    Rend `{}` si aucun template n'est actif — l'appelant a déjà son
    `TEMPLATE_NOT_CONFIGURED`, et un inventaire fait contre rien n'aurait pas de
    sens (principe 11 : jamais de validation « best effort »).
    """
    from dataio.services_templates import active_template, template_ref

    tpl = active_template(KIND_FEUILLE_BESOINS)
    if tpl is None:
        return {}

    schema = tpl.schema or {}
    attendues = [s["name"] for s in schema.get("sheets", [])]
    attendues_norm = {dataio_services._norm(n): n for n in attendues}
    donnees_norm = {dataio_services._norm(n) for n in _feuilles_de_donnees(schema)}

    try:
        wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    except Exception:  # noqa: BLE001 — l'appelant lève déjà CLASSEUR_ILLISIBLE
        return {}
    try:
        presentes = list(wb.sheetnames)
    finally:
        wb.close()

    reconnues, non_reconnues, ingerees = [], [], []
    for nom in presentes:
        cle = dataio_services._norm(nom)
        if cle in attendues_norm:
            reconnues.append(nom)
            if cle in donnees_norm:
                ingerees.append(nom)
        else:
            non_reconnues.append(nom)

    presentes_norm = {dataio_services._norm(n) for n in presentes}
    absentes = [nom for cle, nom in attendues_norm.items() if cle not in presentes_norm]

    return {
        "template": template_ref(tpl),
        "feuillesClasseur": presentes,
        "reconnues": reconnues,
        "ingerees": ingerees,
        "nonReconnues": non_reconnues,
        "attenduesAbsentes": absentes,
    }


def _record_validation(source, application, template_ref: dict, by: str,
                       inventaire: dict | None = None) -> None:
    """Trace append-only de la validation d'une feuille de besoins (principe 11).

    Best-effort volontaire : l'ingestion a réussi et la source est déjà commitée ;
    un journal indisponible ne doit pas défaire une ingestion valide. La référence
    de template reste par ailleurs dans le payload retourné (rapport de validation).
    """
    try:
        from audit.services import record
        record(
            actor=by or "", action="credits.needs_sheet.validated",
            entity_type="DataSource", entity_id=str(source.pk),
            details={
                "applicationCode": application.code,
                "needsSourceId": source.pk,
                "revision": source.revision,
                "sha256": source.sha256 or "",
                "templateId": template_ref.get("templateId"),
                "templateVersion": template_ref.get("version"),
                # Quelles feuilles ont été lues, ingérées, ignorées. Sans cette
                # trace, un auditeur ne peut pas dire deux ans après si un poste
                # absent du score manquait au classeur ou n'a simplement jamais
                # été ingéré — deux causes, deux responsabilités.
                "feuilles": {k: v for k, v in (inventaire or {}).items()
                             if k != "template"},
            },
        )
    except Exception:  # noqa: BLE001 — l'audit ne défait pas une ingestion valide
        pass


def extract_module_totals(source: DataSource) -> dict[str, Decimal]:
    """Totaux par module, lus dans les `DataRecord` — jamais dans le fichier.

    Calque ligne à ligne : chaque ligne de `5_Synthese_Besoins` alimente exactement
    un module. Les 8 codes sont toujours présents (0 si la rubrique est à 0).
    """
    totals = {code: Decimal("0") for code in MODULE_CODES}
    table = source.tables.filter(name=SHEET_SYNTHESE).first()
    if table is None:
        return totals

    columns = [c.name for c in table.columns.all()]
    cols = _map_columns(columns, COLUMN_ROLES_F5)
    if "rubrique" not in cols or "total" not in cols:
        return totals
    name_rubrique = columns[cols["rubrique"]]
    name_total = columns[cols["total"]]

    for record in table.records.all():
        code = rubrique_to_module(record.values.get(name_rubrique))
        if not code:
            continue
        value = to_decimal(record.values.get(name_total))
        if value is not None:
            totals[code] = quantifier(totals[code] + value)
    return totals


def needs_source_lineage(source: DataSource | None) -> dict:
    """Traçabilité minimale d'une analyse : de quoi la rejouer à l'identique."""
    if source is None:
        return {}
    return {
        "needs_source_id": source.pk,
        "revision": source.revision,
        "sha256": source.sha256,
        "dataset_key": source.dataset_key,
        "committed_at": source.committed_at.isoformat() if source.committed_at else None,
    }
