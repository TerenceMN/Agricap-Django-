"""
Ingestion d'un classeur de référence AGRICAP → tables (écriture à l'upload).

Appelé par la commande `import_referentiel` ET par l'endpoint d'upload admin :
une seule logique. Le référentiel v3 étant hétérogène (colonnes différentes par
famille de chaîne), on capture TOUTES les colonnes puis on dérive les concepts
communs par correspondance sémantique d'en-têtes. On n'invente rien : un concept
introuvable reste None (→ `NON ÉVALUABLE` en aval).
"""
from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from decimal import Decimal

import openpyxl
from django.db import transaction

from .chains import BY_SHEET
from .models import InstitutionConfig, ReferenceRange, ReferentielVersion
from .range_parser import to_decimal_range, to_number, to_range

CALIBRATION_SHEET = "16_Calibrage_Gouvernance"


def _norm(text) -> str:
    if text is None:
        return ""
    s = unicodedata.normalize("NFD", str(text))
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return " ".join(s.lower().split())


@dataclass
class IngestReport:
    version_id: int
    version_label: str
    ranges_by_chain: dict[str, int]
    config_loaded: bool
    warnings: list[str]

    @property
    def total_ranges(self) -> int:
        return sum(self.ranges_by_chain.values())


# --- Détection d'en-tête ---------------------------------------------------
_TITLE_PREFIXES = ("chaine de valeur", "valeurs indicatives", "colonnes", "legende", "note")


def _find_header(ws) -> int | None:
    """Ligne d'en-tête = 1re ligne (1..8) avec ≥4 cellules dont la 1re n'est pas un titre."""
    for r, row in enumerate(ws.iter_rows(min_row=1, max_row=8, values_only=True), start=1):
        non_empty = [c for c in row if c not in (None, "")]
        first = _norm(row[0]) if row else ""
        if len(non_empty) >= 4 and first and not any(first.startswith(p) for p in _TITLE_PREFIXES):
            return r
    return None


# --- Dérivation sémantique des concepts ------------------------------------
def _headers(ws, header_row) -> list[tuple[int, str, str]]:
    """(index, en-tête brut, en-tête normalisé) pour la ligne d'en-tête."""
    row = next(ws.iter_rows(min_row=header_row, max_row=header_row, values_only=True))
    return [(i, str(h).strip(), _norm(h)) for i, h in enumerate(row) if h not in (None, "")]


def _find_range(headers, row, keywords, *, exclude=("median", "mois hauts", "mois bas")):
    """
    Cherche un concept par mots-clés d'en-tête. Si deux colonnes correspondent avec
    « min »/« max », retourne (min, max) ; sinon parse la 1re colonne comme plage.
    Retourne (texte_source, lo, hi).
    """
    matches = [(i, h, n) for (i, h, n) in headers
               if any(k in n for k in keywords) and not any(e in n for e in exclude)]
    if not matches:
        return "", None, None

    def val(i):
        return row[i] if i < len(row) else None

    col_min = next((m for m in matches if "min" in m[2]), None)
    col_max = next((m for m in matches if "max" in m[2]), None)
    if col_min and col_max:
        lo = to_number(val(col_min[0]))
        hi = to_number(val(col_max[0]))
        text = f"{val(col_min[0])}–{val(col_max[0])}"
        if lo is not None and hi is not None and lo > hi:
            lo, hi = hi, lo
        return text, lo, hi

    i, h, n = matches[0]
    raw = val(i)
    lo, hi = to_range(raw)
    return (str(raw) if raw not in (None, "") else ""), lo, hi


_RENDEMENT_KW = ("rendement", "production", "poids vif", "poids moyen", "gmq", "ponte",
                 "prolificite", "densite", "capacite", "litres", "taux de ponte", "parametre technique")
_COUT_KW = ("cout de production", "cout de transfo", "cout matiere", "cout",)
_PRIX_KW = ("prix de vente", "prix unitaire", "prix",)
_PERTE_KW = ("mortalite", "perte", "taux de survie", "survie", "colonisation",)


def _derive_perte(headers, row) -> tuple[str, float | None]:
    """Perte/mortalité en fraction. Une métrique de survie/colonisation est inversée."""
    text, lo, hi = _find_range(headers, row, _PERTE_KW)
    if hi is None:
        return text, None
    # Détecte si le concept trouvé est une « survie/colonisation » (métrique positive).
    is_survie = any(("survie" in n or "colonisation" in n) and any(k in n for k in _PERTE_KW)
                    for (_, _, n) in headers)
    return (text, round(1 - lo, 4) if (is_survie and lo is not None) else hi)


def _build_range(version, chain, headers, row):
    """
    Construit un `ReferenceRange` (non sauvegardé) depuis (headers, row).
    Renvoie ("stop", None) sur une légende de bas de tableau, ("skip", None) sur
    une ligne vide, ("row", ReferenceRange) sinon. Partagé par les deux sources de
    données : le classeur (`_ingest_chain_sheet`) et les lignes éditées en base
    (`rebuild_chain_from_records`), pour une dérivation identique.
    """
    name = row[0] if row else None
    nname = _norm(name)
    if not nname:
        return "skip", None
    if any(nname.startswith(p) for p in _TITLE_PREFIXES):
        return "stop", None  # légende de bas de tableau → fin

    def by_kw(*keywords):
        """Valeur brute de la 1re colonne dont l'en-tête contient un des mots-clés."""
        for (i, h, n) in headers:
            if any(k in n for k in keywords):
                return row[i] if i < len(row) else None
        return None

    cols = {h: (row[i] if i < len(row) else None) for (i, h, n) in headers}
    rend_param = next((h for (i, h, n) in headers if any(k in n for k in _RENDEMENT_KW)), "")
    _, rend_lo, rend_hi = _find_range(headers, row, _RENDEMENT_KW)
    if rend_lo is None and rend_hi is None:
        # Layout élevage : le libellé (« GMQ… ») et ses bornes « Valeur min/max »
        # sont dans des colonnes distinctes.
        _, rend_lo, rend_hi = _find_range(headers, row, ("valeur",))
    cout_txt, cout_lo, cout_hi = _find_range(headers, row, _COUT_KW)
    prix_txt, prix_lo, prix_hi = _find_range(headers, row, _PRIX_KW)
    perte_txt, perte_max = _derive_perte(headers, row)

    return "row", ReferenceRange(
        version=version,
        chain_code=chain.code, chain_slug=chain.slug, chain_libelle=chain.libelle,
        name=str(name).strip()[:160],
        systeme=str(by_kw("systeme") or "").strip()[:160],
        cycle_months=to_number(by_kw("cycle", "duree du cycle")),
        parametre_cle=str(rend_param)[:160],
        unite=str(by_kw("unite") or "").strip()[:60],
        rendement_min=rend_lo, rendement_max=rend_hi,
        cout_text=cout_txt[:120], cout_min=cout_lo, cout_max=cout_hi,
        prix_text=prix_txt[:120], prix_min=prix_lo, prix_max=prix_hi,
        perte_text=perte_txt[:120], perte_max=perte_max,
        statut=str(by_kw("statut") or "").strip()[:60],
        source=str(by_kw("source") or "").strip()[:255],
        date_maj=str(by_kw("date maj") or "").strip()[:40],
        zone=str(by_kw("zone") or "").strip()[:200],
        observations=str(by_kw("observations") or "").strip(),
        columns={str(k): (None if v is None else str(v)) for k, v in cols.items()},
    )


def _ingest_chain_sheet(version, chain, ws) -> tuple[int, list[str]]:
    header_row = _find_header(ws)
    if not header_row:
        return 0, [f"{chain.sheet}: en-tête introuvable, feuille ignorée."]
    headers = _headers(ws, header_row)

    objs: list[ReferenceRange] = []
    for row in ws.iter_rows(min_row=header_row + 1, values_only=True):
        status, rr = _build_range(version, chain, headers, row)
        if status == "stop":
            break
        if status == "skip":
            continue
        objs.append(rr)

    ReferenceRange.objects.bulk_create(objs)
    return len(objs), []


@transaction.atomic
def rebuild_chain_from_records(version, chain, header_names, row_dicts) -> int:
    """
    Re-dérive les `ReferenceRange` d'une chaîne à partir des lignes ÉDITÉES en base
    (couche générique). `header_names` = colonnes ordonnées ; `row_dicts` = valeurs
    par en-tête. Remplace les plages typées de cette chaîne dans `version` — c'est
    ce que le moteur relit après une correction admin.
    """
    headers = [(i, str(nm), _norm(nm)) for i, nm in enumerate(header_names)]
    objs: list[ReferenceRange] = []
    for rd in row_dicts:
        row = tuple(rd.get(nm) for nm in header_names)
        status, rr = _build_range(version, chain, headers, row)
        if status == "stop":
            break
        if status == "skip":
            continue
        objs.append(rr)

    ReferenceRange.objects.filter(version=version, chain_code=chain.code).delete()
    ReferenceRange.objects.bulk_create(objs)
    return len(objs)


@transaction.atomic
def activer_config(cfg: InstitutionConfig) -> InstitutionConfig:
    """Rend `cfg` la SEULE configuration active — point d'écriture unique.

    Trois chemins écrivent aujourd'hui `InstitutionConfig` : l'import du
    référentiel v3 (feuille 16), la ré-dérivation depuis les lignes éditées en
    base, et l'ingestion des normes du simulateur de cycle
    (`referentiel.normes_v4`). Chacun recopiait la même paire
    « désactiver les autres / sauver celle-ci ». Deux d'entre eux l'ont déjà
    fait dans le mauvais ordre au moins une fois dans l'histoire du fichier :
    tant que la désactivation et l'activation ne sont pas dans la même
    transaction, une erreur entre les deux laisse l'institution SANS
    configuration active, et `InstitutionConfig.active()` retombe alors
    silencieusement sur les valeurs par défaut du modèle — c'est-à-dire sur des
    seuils que le comité n'a jamais votés.

    Un seul endroit, atomique, et le principe 6 est tenu.
    """
    InstitutionConfig.objects.filter(is_active=True).update(is_active=False)
    cfg.is_active = True
    cfg.save()
    return cfg


@transaction.atomic
def rebuild_config_from_records(version, header_names, row_dicts) -> bool:
    """Re-dérive `InstitutionConfig` (feuille 16) depuis les lignes éditées en base.

    Lecture en `to_decimal_range` (et non `to_range`) : ces cellules deviennent les
    seuils du moteur de décision, pas des ordres de grandeur — elles ne doivent
    jamais transiter par un `float` (principe 4, cf. `InstitutionConfig`).
    """
    if not header_names:
        return False
    cfg = InstitutionConfig(version=version, is_active=True)
    raw: list[list[str]] = []
    found = False
    for rd in row_dicts:
        cells = [rd.get(nm) for nm in header_names]
        non_empty = [str(c) for c in cells if c not in (None, "")]
        if non_empty:
            raw.append(non_empty)
        label = _norm(cells[0]) if cells else ""
        value = cells[1] if len(cells) > 1 else None
        found = _appliquer_parametre(cfg, label, value) or found
    cfg.raw = {"feuille_16": raw, "avertissements": _verifier_poids(cfg)}
    activer_config(cfg)
    return found


# Paramètre feuille 16 (normalisé) → attribut InstitutionConfig.
_CFG_KEYS = {
    "seuil dscr (avis": "seuil_dscr",
    "seuil dscr stresse": "seuil_dscr_stresse",
    "couverture des garanties": "couverture_min",
    "score global minimal": "score_global_min",
    "ponderation comportemental": "poids_comportemental",
}

#: Paramètres que la feuille 16 écrit en POURCENTAGE (« 30 % ») là où
#: `InstitutionConfig` les stocke en POINTS de base 100 (30,00).
#:
#: DÉFAUT CORRIGÉ (mesuré sur la base de développement, juillet 2026)
#: ------------------------------------------------------------------
#: `to_decimal_range("30 %")` rend `Decimal("0.30")` — ce qui est juste pour un
#: champ FRACTION (`decote_garantie`, `frais_dossier`) et faux pour un champ en
#: points. La pondération comportementale était donc écrite à 0,30 au lieu de
#: 30,00, et la somme des cinq poids valait :
#:
#:     25,00 + 20,00 + 10,00 + 0,30 + 15,00 = 70,30
#:
#: `credits.analyse.poids_effectifs()` exige exactement 100 et retombait donc,
#: à CHAQUE analyse, sur ses poids de secours codés en dur. Effet net : la seule
#: pondération que le comité pouvait calibrer depuis son classeur — celle du
#: critère qui pèse le plus lourd, 30 % du score — n'avait aucun effet, et le
#: seul signal était un warning de log que personne ne lit. Le principe 8 était
#: respecté sur la forme (la valeur vivait en base) et vide sur le fond (elle
#: n'était jamais appliquée).
#:
#: Les quatre autres poids ne figurent pas dans la feuille 16 : ils gardent les
#: défauts du modèle (25 / 20 / 10 / 15), qui somment à 100 avec 30. La grille
#: complète, elle, se recalibre par le simulateur de cycle v4
#: (`referentiel.normes_v4`, onglet 12), qui porte les cinq.
_CFG_EN_POURCENT = frozenset({"poids_comportemental"})

#: Les cinq poids du moteur — leur somme est un invariant vérifié après lecture.
_ATTRS_POIDS = ("poids_technique", "poids_financier", "poids_stress",
                "poids_comportemental", "poids_garanties")


def _appliquer_parametre(cfg: InstitutionConfig, label: str, value) -> bool:
    """Pose sur `cfg` le paramètre de feuille 16 porté par `label`, s'il en porte un.

    Lecture en `to_decimal_range` (et non `to_range`) : ces cellules deviennent
    les seuils du moteur de décision, pas des ordres de grandeur — elles ne
    doivent jamais transiter par un `float` (principe 4).
    """
    for needle, attr in _CFG_KEYS.items():
        if needle not in label:
            continue
        _, hi = to_decimal_range(value)
        if hi is None:
            return False
        if attr in _CFG_EN_POURCENT:
            hi = (hi * Decimal(100)).quantize(Decimal("0.01"))
        setattr(cfg, attr, hi)
        return True
    return False


def _verifier_poids(cfg: InstitutionConfig) -> list[str]:
    """Avertit si la grille de pondération ne sera pas retenue par le moteur.

    On ne corrige pas la somme à la place du comité : inventer un poids serait
    pire que de signaler qu'il manque. Mais on refuse de laisser le seul signal
    dans un log d'exécution — ici, l'admin qui importe le classeur le lit.
    """
    total = sum((Decimal(str(getattr(cfg, a))) for a in _ATTRS_POIDS), Decimal(0))
    if total == Decimal(100):
        return []
    return [
        f"Pondérations : leur somme vaut {total} et non 100 — le moteur les "
        f"IGNORERA et scorera avec ses poids de secours (25/20/10/30/15). "
        f"Corrigez la feuille 16, ou recalibrez la grille complète par le "
        f"simulateur de cycle v4 (manage.py import_normes_v4)."
    ]


def _ingest_calibration(version, ws) -> tuple[bool, list[str]]:
    """Feuille 16 → `InstitutionConfig`. `to_decimal_range` : ces valeurs sont
    les paramètres du moteur, elles restent exactes de la cellule à la colonne."""
    cfg = InstitutionConfig(version=version, is_active=True)
    raw: list[list] = []
    found = False
    for row in ws.iter_rows(values_only=True):
        cells = [c for c in row if c not in (None, "")]
        if cells:
            raw.append([str(c) for c in cells])
        if len(row) < 2:
            continue
        label = _norm(row[0])
        found = _appliquer_parametre(cfg, label, row[1]) or found

    avertissements = _verifier_poids(cfg)
    if not found:
        avertissements.append("Feuille 16: aucun seuil reconnu (valeurs par défaut).")
    cfg.raw = {"feuille_16": raw, "avertissements": avertissements}
    activer_config(cfg)
    return found, avertissements


@transaction.atomic
def ingest_workbook(path_or_file, *, label: str, source_filename: str = "") -> IngestReport:
    """Parse un classeur référentiel et remplit les tables. Renvoie un rapport."""
    wb = openpyxl.load_workbook(path_or_file, data_only=True, read_only=True)
    version = ReferentielVersion.objects.create(label=label, source_filename=source_filename)
    ReferentielVersion.objects.exclude(pk=version.pk).update(is_active=False)

    ranges_by_chain: dict[str, int] = {}
    warnings: list[str] = []
    for sheet_name in wb.sheetnames:
        chain = BY_SHEET.get(sheet_name)
        if chain:
            n, w = _ingest_chain_sheet(version, chain, wb[sheet_name])
            ranges_by_chain[chain.code] = n
            warnings += w

    config_loaded = False
    if CALIBRATION_SHEET in wb.sheetnames:
        config_loaded, w = _ingest_calibration(version, wb[CALIBRATION_SHEET])
        warnings += w
    else:
        warnings.append("Feuille 16 absente : configuration par défaut.")
        InstitutionConfig.objects.filter(is_active=True).update(is_active=False)
        InstitutionConfig.objects.create(version=version, is_active=True)

    wb.close()
    return IngestReport(
        version_id=version.pk, version_label=version.label,
        ranges_by_chain=ranges_by_chain, config_loaded=config_loaded, warnings=warnings,
    )
