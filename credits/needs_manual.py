"""Saisie manuelle de la feuille de besoins — sans Excel, mais par le MÊME chemin.

Le classeur n'est pas toujours possible : un agent en agence n'a pas Excel sous la
main, un client dicte ses postes au téléphone, un dossier se corrige d'une ligne.
Jusqu'ici, l'absence de fichier interdisait purement le parcours.

CE QUE CE MODULE NE FAIT PAS
----------------------------
Il ne calcule aucun total à part, n'écrit aucun `NeedsSheet` en direct, ne classe
aucune rubrique lui-même. Il **construit un classeur** conforme au template ACTIF
et le rend à l'appelant, qui le fait entrer par le pipeline existant
(`parse_and_ingest` avec un dossier, `parse_needs_sheet` avant sa création).

C'est délibéré, et c'est tout l'enjeu (principes 1, 5, 6 et 11) :

  - la saisie manuelle subit EXACTEMENT la même validation structurelle,
    la même cohérence interne et la même ingestion `dataio` qu'un dépôt Excel.
    Un second chemin d'écriture aurait produit des dossiers acceptés d'un côté et
    refusés de l'autre — et un scoring qui ne lit plus la même chose selon la
    porte d'entrée ;
  - la lignée reste entière : `needs_source_id + revision + sha256`. Une analyse
    faite sur une saisie manuelle se rejoue deux ans après comme n'importe quelle
    autre, parce qu'il y a un classeur derrière, versionné comme les autres ;
  - le template reste la règle. Les rubriques proposées à la saisie sont celles
    du template actif — pas une liste recopiée ici, qui divergerait au premier
    template activé par l'admin.

Le total d'une ligne vaut **quantité × prix unitaire × fréquence**, la même
formule que celle que le lecteur contrôle. Elle est calculée ici parce que
l'écran n'a pas de tableur pour le faire — pas parce qu'il y aurait deux règles.
"""
from __future__ import annotations

import io
from decimal import Decimal, InvalidOperation

import openpyxl

from common.money import est_au_centime, quantifier

from credits.needs_sheet import (
    COLUMN_ROLES_F4,
    COLUMN_ROLES_F5,
    MODULE_CODES,
    MODULE_LABELS,
    SHEET_BESOINS,
    SHEET_SYNTHESE,
    _colonnes_facultatives,
    _find_frequence,
    _map_columns,
    normalize,
    rubrique_to_module,
)

#: En-têtes de repli, utilisés seulement quand AUCUN template n'est actif. Ce
#: n'est pas une validation « best effort » (principe 11) : c'est la forme d'un
#: brouillon qui sera de toute façon refusé à l'ingestion par
#: `TEMPLATE_NOT_CONFIGURED`. Le parcours pré-dossier, lui, n'ingère pas — il
#: doit donc rester praticable.
ENTETE_F4_DEFAUT = [
    "N°", "Rubrique", "Description détaillée", "Unité", "Quantité",
    "Coût unitaire", "Fréquence", "Montant total", "Observations",
]
ENTETE_F5_DEFAUT = ["Rubrique", "Total rubrique", "Part du total", "Commentaire"]

#: Plafond de lignes acceptées en une saisie. Une feuille de besoins réelle en
#: porte quelques dizaines ; au-delà, c'est un import, et un import se fait par
#: fichier.
MAX_LIGNES = 200


class SaisieInvalide(Exception):
    """Saisie refusée. `errors` = liste de `{code, message}` (→ 422).

    Toutes les erreurs de la saisie sont collectées avant d'être levées
    (principe 5) : renvoyer la première seulement ferait corriger les lignes une
    par une, un aller-retour par faute.
    """

    def __init__(self, errors: list[dict]) -> None:
        self.errors = errors
        super().__init__("; ".join(e["message"] for e in errors))


# ── Ce que l'écran de saisie doit proposer ────────────────────────────────────

def _schema_actif() -> dict | None:
    from dataio.models import KIND_FEUILLE_BESOINS
    from dataio.services_templates import active_template

    tpl = active_template(KIND_FEUILLE_BESOINS)
    return (tpl.schema or {}) if tpl is not None else None


def _rubriques_du_template(schema: dict | None) -> list[str]:
    """Libellés de rubrique attendus en feuille de synthèse, dans l'ordre du modèle."""
    rubriques = list((schema or {}).get("rubriques") or [])
    return rubriques or [MODULE_LABELS[code] for code in MODULE_CODES]


def rubriques_de_saisie() -> dict:
    """Options du formulaire de saisie — servies par le serveur, jamais codées au front.

    Une rubrique du template qu'aucun fragment ne sait classer est servie avec
    `module: null` et `saisissable: false` : l'écran la montre grisée plutôt que
    de la proposer puis de laisser la ligne tomber dans le vide. C'est le même
    refus du `continue` silencieux que `analyser_couverture_rubriques`.
    """
    schema = _schema_actif()
    libelles = _rubriques_du_template(schema)
    options = []
    for libelle in libelles:
        module = rubrique_to_module(libelle)
        options.append({
            "libelle": libelle,
            "module": module,
            "moduleLabel": MODULE_LABELS.get(module or "", ""),
            "saisissable": module is not None,
        })
    return {
        "rubriques": options,
        "templateConfigure": schema is not None,
        # L'écran a besoin de savoir que la formule est celle du contrôle serveur,
        # pour ne pas en afficher une autre à côté.
        "formuleTotal": "quantite * prix_unitaire * frequence",
        "maxLignes": MAX_LIGNES,
    }


# ── Normalisation de la saisie ────────────────────────────────────────────────

def _decimal(valeur, *, defaut: Decimal | None = None) -> Decimal | None:
    if valeur is None or (isinstance(valeur, str) and not valeur.strip()):
        return defaut
    try:
        return Decimal(str(valeur).strip().replace(" ", "").replace(",", "."))
    except (InvalidOperation, ValueError):
        return None


def normaliser_lignes(brut) -> list[dict]:
    """Valide la saisie et rend des lignes propres, ou lève `SaisieInvalide`.

    Chaque ligne rendue porte son module (donc son rattachement au référentiel) et
    son total calculé. Rien n'est deviné : une quantité illisible est refusée, pas
    remplacée par zéro — un zéro implicite sortirait un dossier sous-évalué que
    personne ne saurait relire.
    """
    errors: list[dict] = []

    if not isinstance(brut, list) or not brut:
        raise SaisieInvalide([{
            "code": "SAISIE_VIDE",
            "message": "Aucune ligne saisie : une feuille de besoins comporte au moins "
                       "une dépense. Ajoutez une ligne — rubrique, désignation, "
                       "quantité et prix unitaire.",
        }])
    if len(brut) > MAX_LIGNES:
        raise SaisieInvalide([{
            "code": "TROP_DE_LIGNES",
            "message": f"{len(brut)} lignes saisies pour un maximum de {MAX_LIGNES}. "
                       f"Au-delà, déposez un classeur Excel.",
        }])

    lignes: list[dict] = []
    for n, item in enumerate(brut, start=1):
        if not isinstance(item, dict):
            errors.append({"code": "LIGNE_INVALIDE",
                           "message": f"Ligne {n} : format inattendu."})
            continue

        rubrique = str(item.get("rubrique") or "").strip()
        designation = str(item.get("designation") or "").strip()
        module = rubrique_to_module(rubrique) if rubrique else None

        if not rubrique:
            errors.append({
                "code": "RUBRIQUE_MANQUANTE",
                "message": f"Ligne {n} : choisissez une rubrique.",
            })
        elif module is None:
            errors.append({
                "code": "RUBRIQUE_INCONNUE",
                "message": (f"Ligne {n} : la rubrique « {rubrique} » n'est rattachée à "
                            f"aucun module. Choisissez l'une des rubriques proposées."),
            })
        if not designation:
            errors.append({
                "code": "DESIGNATION_MANQUANTE",
                "message": (f"Ligne {n} : indiquez ce que couvre cette dépense. "
                            f"« {rubrique or 'Rubrique'} » seul ne se questionne pas "
                            f"en visite de terrain."),
            })

        quantite = _decimal(item.get("quantite"))
        prix = _decimal(item.get("prix_unitaire"))
        frequence = _decimal(item.get("frequence"), defaut=Decimal("1"))

        for valeur, champ, libelle in ((quantite, "quantite", "Quantité"),
                                       (prix, "prix_unitaire", "Prix unitaire"),
                                       (frequence, "frequence", "Fréquence")):
            if valeur is None:
                errors.append({
                    "code": "TYPE_INVALIDE",
                    "message": (f"Ligne {n} : « {libelle} » doit être un nombre, "
                                f"valeur lue « {item.get(champ)} »."),
                })
            elif valeur < 0:
                errors.append({
                    "code": "TYPE_INVALIDE",
                    "message": f"Ligne {n} : « {libelle} » ne peut pas être négatif ({valeur}).",
                })

        # Principe 4 bis : un montant à plus de deux décimales reçu d'un client est
        # REFUSÉ, jamais arrondi en silence. Le client qui tape 12,005 doit
        # apprendre que le système n'accepte pas ce prix, pas découvrir qu'il a
        # budgété 12,01.
        if prix is not None and prix >= 0 and not est_au_centime(prix):
            errors.append({
                "code": "MONTANT_HORS_CENTIME",
                "message": (f"Ligne {n} : le prix unitaire « {prix} » a plus de deux "
                            f"décimales. Saisissez un montant au centime."),
            })
        if frequence is not None and frequence <= 0:
            errors.append({
                "code": "FREQUENCE_INVALIDE",
                "message": (f"Ligne {n} : la fréquence vaut le nombre de fois où la "
                            f"dépense se répète dans le cycle — au moins 1."),
            })

        if errors:
            continue

        total = quantifier(quantite * prix * frequence)
        lignes.append({
            "module": module,
            "rubrique": rubrique,
            "designation": designation[:255],
            "unite": str(item.get("unite") or "").strip()[:40],
            "quantite": quantite,
            "prix_unitaire": prix,
            "frequence": frequence,
            "total": total,
            "observations": str(item.get("observations") or "").strip()[:255],
        })

    if errors:
        raise SaisieInvalide(errors)
    return lignes


# ── Construction du classeur ──────────────────────────────────────────────────

def _indices_f4(colonnes: list[str]) -> tuple[dict[str, int], int | None, dict[str, int]]:
    cols = _map_columns(colonnes, COLUMN_ROLES_F4)
    prises = set(cols.values())
    freq = _find_frequence(colonnes, prises)
    if freq is not None:
        prises.add(freq)
    return cols, freq, _colonnes_facultatives(colonnes, prises)


def construire_classeur(lignes: list[dict]) -> bytes:
    """Octets d'un classeur de feuille de besoins portant la saisie.

    Reproduit la FORME du template actif — toutes ses feuilles, toutes ses
    colonnes, toutes ses rubriques — pour que `validate_structure` le reconnaisse
    comme n'importe quel dépôt client. La synthèse est calculée depuis le détail,
    jamais saisie : c'est précisément ce que le contrôle `INCOHERENCE_INTERNE`
    vérifie, et il doit pouvoir échouer si un jour cette construction se trompe.
    """
    schema = _schema_actif()
    rubriques = _rubriques_du_template(schema)

    if schema and schema.get("sheets"):
        feuilles = [(s["name"], [c for c in (s.get("columns") or []) if c])
                    for s in schema["sheets"]]
        nom_synthese = schema.get("synthesis_sheet") or SHEET_SYNTHESE
    else:
        feuilles = [(SHEET_BESOINS, list(ENTETE_F4_DEFAUT)),
                    (SHEET_SYNTHESE, list(ENTETE_F5_DEFAUT))]
        nom_synthese = SHEET_SYNTHESE

    presentes = {normalize(nom) for nom, _ in feuilles}
    if normalize(SHEET_BESOINS) not in presentes:
        feuilles.append((SHEET_BESOINS, list(ENTETE_F4_DEFAUT)))
    if normalize(nom_synthese) not in presentes:
        feuilles.append((SHEET_SYNTHESE, list(ENTETE_F5_DEFAUT)))
        nom_synthese = SHEET_SYNTHESE

    # Totaux par module, depuis le détail et lui seul.
    totaux: dict[str, Decimal] = {code: Decimal("0") for code in MODULE_CODES}
    for ligne in lignes:
        totaux[ligne["module"]] += ligne["total"]

    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    erreurs: list[dict] = []

    for nom, colonnes in feuilles:
        ws = wb.create_sheet(title=nom[:31])
        if not colonnes:
            continue
        ws.append(colonnes)

        if normalize(nom) == normalize(SHEET_BESOINS):
            erreurs.extend(_ecrire_detail(ws, colonnes, lignes))
        elif normalize(nom) == normalize(nom_synthese):
            erreurs.extend(_ecrire_synthese(ws, colonnes, rubriques, totaux))

    if erreurs:
        raise SaisieInvalide(erreurs)

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _ecrire_detail(ws, colonnes: list[str], lignes: list[dict]) -> list[dict]:
    cols, freq_idx, opt = _indices_f4(colonnes)
    manquantes = [role for role in ("rubrique", "total") if role not in cols]
    if manquantes:
        return [{
            "code": "TEMPLATE_INCOMPATIBLE",
            "message": (f"Le template actif ne porte pas les colonnes "
                        f"« {', '.join(manquantes)} » en feuille « {SHEET_BESOINS} » : "
                        f"la saisie manuelle ne peut pas y écrire. Déposez un classeur "
                        f"ou faites corriger le template."),
        }]

    numero_idx = next(
        (i for i, c in enumerate(colonnes) if normalize(c) in ("n", "no", "n°", "num")), None)

    for n, ligne in enumerate(lignes, start=1):
        valeurs: list = [None] * len(colonnes)
        if numero_idx is not None:
            valeurs[numero_idx] = n
        valeurs[cols["rubrique"]] = ligne["rubrique"]
        valeurs[cols["total"]] = ligne["total"]
        if "designation" in cols:
            valeurs[cols["designation"]] = ligne["designation"]
        if "quantite" in cols:
            valeurs[cols["quantite"]] = ligne["quantite"]
        if "prix_unitaire" in cols:
            valeurs[cols["prix_unitaire"]] = ligne["prix_unitaire"]
        if freq_idx is not None:
            valeurs[freq_idx] = ligne["frequence"]
        elif ligne["frequence"] != Decimal("1"):
            # Sans colonne pour la porter, la fréquence disparaîtrait du classeur et
            # le total de la ligne cesserait d'être reconstituable. On la replie dans
            # la désignation plutôt que de la perdre.
            ligne = dict(ligne, designation=f"{ligne['designation']} (× {ligne['frequence']})")
            if "designation" in cols:
                valeurs[cols["designation"]] = ligne["designation"]
        if "unite" in opt:
            valeurs[opt["unite"]] = ligne["unite"]
        if "observations" in opt:
            valeurs[opt["observations"]] = ligne["observations"]
        ws.append(valeurs)
    return []


def _ecrire_synthese(ws, colonnes: list[str], rubriques: list[str],
                     totaux: dict[str, Decimal]) -> list[dict]:
    cols = _map_columns(colonnes, COLUMN_ROLES_F5)
    manquantes = [role for role in ("rubrique", "total") if role not in cols]
    if manquantes:
        return [{
            "code": "TEMPLATE_INCOMPATIBLE",
            "message": (f"Le template actif ne porte pas les colonnes "
                        f"« {', '.join(manquantes)} » en feuille « {SHEET_SYNTHESE} » : "
                        f"la saisie manuelle ne peut pas y écrire."),
        }]

    # Toutes les rubriques du template figurent, même à 0 : c'est ce que
    # `validate_structure` exige (`RUBRIQUE_MANQUANTE`). Quand deux rubriques du
    # modèle se rattachent au MÊME module — cas d'un template qui les détaille
    # plus finement que les 8 codes — le total va à la première ; les suivantes
    # portent 0. Le total du module reste juste, et la répartition entre deux
    # libellés du même module ne se devine pas ici.
    servis: set[str] = set()
    total_general = Decimal("0")
    for libelle in rubriques:
        module = rubrique_to_module(libelle)
        if module is not None and module not in servis:
            montant = quantifier(totaux.get(module, Decimal("0")))
            servis.add(module)
        else:
            montant = Decimal("0.00")
        total_general += montant
        valeurs: list = [None] * len(colonnes)
        valeurs[cols["rubrique"]] = libelle
        valeurs[cols["total"]] = montant
        ws.append(valeurs)

    valeurs = [None] * len(colonnes)
    valeurs[cols["rubrique"]] = "TOTAL GÉNÉRAL"
    valeurs[cols["total"]] = quantifier(total_general)
    ws.append(valeurs)
    return []


def classeur_depuis_saisie(brut) -> tuple[bytes, list[dict]]:
    """Saisie brute (payload JSON) → `(octets du classeur, lignes normalisées)`.

    Point d'entrée unique des vues : elles ne manipulent ni openpyxl ni `Decimal`.
    """
    lignes = normaliser_lignes(brut)
    return construire_classeur(lignes), lignes
