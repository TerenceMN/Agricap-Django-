"""
Simulateur INDICATIF de crédit agricole, adossé au moteur unique (`credits.analyse`).

Rôle et frontière — ce module sert le parcours AVANT qu'un dossier soit
instruit : le client (ou l'agent) explore un montant, une filière, un plan de
financement par module, sans qu'aucune `AnalyseCredit` n'existe encore. Il ne
persiste rien et ne décide rien.

Ce qu'il n'est PLUS : un second moteur de scoring. Il portait ses propres
courbes (DSCR par paliers 1,8 / 1,5 / 1,25 / 1,0 codés en dur), ses propres
poids, sa propre règle de fiabilité technique (« ±30 % par module »), sa propre
formule d'échéancier et sa propre grille de taux (+2,0 là où `credits.scoring`
appliquait +2,5 sur la même bande de score). Un client simulait donc à un taux et
à un score que l'instruction ne reproduisait pas.

Désormais, tout ce qui juge vient du moteur unique :
  - courbes de score  → `BaremeScore` (DSCR, ECART_TECHNIQUE, COUVERTURE_GARANTIES) ;
  - pondération       → `analyse.poids_effectifs()` (`InstitutionConfig`) ;
  - fiabilité technique → `analyse.scorer_technique` (le MÊME calcul, sur le même
    `ReferentielFiliere`) ;
  - comportemental    → `analyse.scorer_comportemental` ;
  - recommandation    → `analyse.recommander` ;
  - taux              → `scoring.taux_pour_score` → `analyse.proposer_taux` ;
  - échéancier        → `credits.echeancier.construire_echeancier` (`Decimal`).

Ce qui reste propre à ce module, et qui justifie qu'il existe : la SIMULATION —
trouver la source de référence, estimer un DSCR quand aucune trésorerie n'est
déclarée, appliquer le financement par module, et dire ce qui n'est pas
calculable. Une différence entre la simulation et l'analyse ne peut donc plus
venir que des DONNÉES (feuille non encore ingérée, garanties non constituées,
DSCR estimé et non calculé sur échéancier réel), jamais des RÈGLES.

Lit les tables du fichier SIMULATEUR courant :
  - 5_Synthese_Besoins        → totaux de référence par module (repli)
  - 10_Capacite_Remboursement → paramètres du prêt (taux, durée, DSCR, EBE)
  - 18_Controles_Vraisemblance → plages de référence pour vraisemblance
"""
from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any
from common.money import en_float_optionnel

logger = logging.getLogger(__name__)


# ── Mapping rubrique Excel → code module Python ──────────────────────────────
#
# UNE SEULE TABLE (principe 6). Il en existait TROIS, recopiées à la main dans
# `needs_sheet`, `needs_parser` et ici, avec des fragments différents — donc des
# classements différents pour la même rubrique selon le chemin de lecture. Un
# libellé complété dans l'une restait inconnu des deux autres : c'est ce qui a
# laissé « Alimentation » hors de la référence avicole alors même que le mapping
# était corrigeable en une ligne.
#
# La table canonique vit dans `credits.needs_sheet` (elle sert la validation du
# classeur client, qui fait foi) ; les deux autres chemins la réutilisent.
def _rubrique_to_module(rubrique: str) -> str | None:
    from credits.needs_sheet import rubrique_to_module

    return rubrique_to_module(rubrique)


# ── Financement par module (contrat §1) ───────────────────────────────────────
# Alias de codes reçus dans `module_financing` → code canonique de `MODULE_CODES`.
# Le front envoie les codes canoniques (cf. src/components/simulateur/modules.js),
# mais on tolère les alias historiques du prototype pour ne pas perdre un pct sur
# une divergence d'orthographe (principe 6 : une seule nomenclature, le backend
# fait foi ; le mapping vit ici, pas dans un `if` d'affichage).
_MODULE_ALIASES: dict[str, str] = {
    "maindoeuvre": "maindoeuvre",
    "main_doeuvre": "maindoeuvre",
    "maindoeuvre_": "maindoeuvre",
    "main-doeuvre": "maindoeuvre",
    "maindœuvre": "maindoeuvre",
    "postrecolte": "postrecolte",
    "post_recolte": "postrecolte",
    "post-recolte": "postrecolte",
    "operations": "mecanisation",
    "mecanisation": "mecanisation",
    "intrants": "semences",
}


def _canonical_module(code) -> str | None:
    """Code module canonique (`MODULE_CODES`) d'une clé de `module_financing`.

    `None` si la clé ne correspond à aucun module connu — l'appelant l'ignore
    plutôt que d'inventer un poste (aucun coût fichier ne lui correspondrait).
    """
    from credits.needs_sheet import MODULE_CODES

    if code is None:
        return None
    raw = str(code).strip()
    if raw in MODULE_CODES:
        return raw
    import re
    import unicodedata
    nfkd = unicodedata.normalize("NFD", raw.lower())
    flat = "".join(c for c in nfkd if not unicodedata.combining(c))
    flat = re.sub(r"[^a-z]", "", flat)
    if flat in MODULE_CODES:
        return flat
    return _MODULE_ALIASES.get(raw.lower()) or _MODULE_ALIASES.get(flat)


def normalize_module_financing(raw: dict | None) -> dict[str, int]:
    """`{codeFront: pct}` → `{codeCanonique: pct}`, pct borné à [0, 100] entier.

    Les clés inconnues sont écartées (silencieusement pour le calcul, mais la vue
    peut les signaler) ; un pct hors plage est ramené aux bornes. Vide → `{}`
    (aucun financement partiel : tout est demandé à 100 %).
    """
    out: dict[str, int] = {}
    if not isinstance(raw, dict):
        return out
    for key, value in raw.items():
        module = _canonical_module(key)
        if module is None:
            continue
        try:
            pct = int(round(float(value)))
        except (TypeError, ValueError):
            continue
        out[module] = max(0, min(100, pct))
    return out


def _referentiel_filiere(value_chain):
    """`ReferentielFiliere` actif de la filière, ou `None`.

    Mêmes clés de résolution que `analyse.resoudre_referentiel` : le simulateur et
    le moteur doivent désigner LE MÊME référentiel pour une filière donnée, sinon
    ils compareraient le plan du client à deux références différentes.
    """
    if value_chain is None:
        return None
    try:
        from credits.models import ReferentielFiliere
    except Exception:  # noqa: BLE001
        return None
    qs = ReferentielFiliere.objects.filter(actif=True)
    return (qs.filter(value_chain_code=value_chain.code).first()
            or qs.filter(filiere__iexact=getattr(value_chain, "label", "") or "").first())


def _referentiel_filiere_totals(referentiel, quantite) -> dict[str, float]:
    """Coûts de référence par module (absolus) = coût unitaire × quantité.

    `couts_modules` porte des coûts PAR UNITÉ DE RÉFÉRENCE (`ref` = coût/ha, mais
    aussi coût/ruche, coût/sujet, coût/m², coût/sac, coût/tonne usinée depuis la
    généralisation du modèle hectare). La quantité passée doit être exprimée dans
    l'unité du référentiel — la cohérence est établie par l'appelant.
    """
    if referentiel is None or not quantite:
        return {}
    quantite = Decimal(str(quantite))
    totals: dict[str, float] = {}
    for module, cfg in (referentiel.couts_modules or {}).items():
        try:
            unitaire = Decimal(str(cfg.get("ref", 0)))
        except Exception:  # noqa: BLE001
            continue
        if unitaire > 0:
            totals[module] = float((unitaire * quantite).quantize(Decimal("0.01")))
    return totals


def _safe_float(val) -> float | None:
    if val is None:
        return None
    try:
        return float(str(val).replace(",", ".").strip())
    except (ValueError, TypeError):
        return None


def _normalize(s: str) -> set[str]:
    """Retourne un ensemble de tokens normalisés (minuscules, sans accents, ≥ 3 lettres)."""
    import re
    import unicodedata
    nfkd = unicodedata.normalize("NFD", s.lower())
    ascii_s = "".join(c for c in nfkd if not unicodedata.combining(c))
    return set(w for w in re.split(r"[\W_]+", ascii_s) if len(w) >= 3)


def _find_source(value_chain_code: str | None = None):
    """
    Retourne le DataSource SIMULATEUR le plus adapté à la filière.

    Priorité :
      1. Simulateur spécifique à la filière (match token ≥ 1 mot commun)
      2. Simulateur générique v4
      3. N'importe quel SIMULATEUR courant
    """
    from dataio.models import DataSource

    all_sources = list(DataSource.objects.filter(kind="SIMULATEUR", is_current=True))

    if value_chain_code:
        vc_tokens = _normalize(value_chain_code)
        best_src = None
        best_score = 0
        for src in all_sources:
            name_tokens = _normalize(src.original_name)
            common = len(vc_tokens & name_tokens)
            if common > best_score:
                best_score = common
                best_src = src
        # Accepter le match seulement si au moins 1 token commun
        # et que le fichier n'est pas le simulateur générique
        if best_src and best_score >= 1 and "sim" in _normalize(best_src.original_name):
            # Bonus : rejeter le fichier v4 générique si un fichier par-filière est trouvé
            is_generic = any(tok in _normalize(best_src.original_name) for tok in ("v4", "v3", "v2", "cycle", "production"))
            if not is_generic or best_score >= 2:
                return best_src

    # Fallback : simulateur générique v4
    for keyword in ("v4", "cycle", "production", "simulateur"):
        src = next(
            (s for s in all_sources if keyword in _normalize(s.original_name)),
            None,
        )
        if src:
            return src

    return all_sources[0] if all_sources else None


def _get_records(source, name_fragment: str) -> list[dict]:
    t = source.tables.filter(name__icontains=name_fragment).first()
    if not t:
        return []
    from dataio.models import DataRecord
    return list(DataRecord.objects.filter(table=t).order_by("row_index").values_list("values", flat=True))


# ── Lecture des tables de référence ──────────────────────────────────────────

#: La table 12 (`Analyse_Credit`) du classeur porte AUSSI une pondération des
#: critères. Elle n'est plus lue : la pondération du moteur vit dans
#: `InstitutionConfig` (principe 8), elle est institutionnelle et non
#: filière-dépendante, et deux sources de poids donneraient deux scores globaux
#: pour les mêmes scores de critères. Le classeur reste la source des COÛTS et des
#: paramètres de prêt — pas des règles de notation.


def _read_reference_totals(source) -> dict[str, float]:
    """Table 5 : totaux de référence par rubrique → mappe vers codes modules."""
    totals: dict[str, float] = {}
    for row in _get_records(source, "Synthese"):
        rubrique = row.get("Rubrique", "") or ""
        total    = _safe_float(row.get("Total rubrique"))
        if not rubrique or total is None:
            continue
        if "TOTAL" in rubrique.upper():
            continue
        mod = _rubrique_to_module(rubrique)
        if mod:
            totals[mod] = totals.get(mod, 0) + total
    return totals


def _read_loan_params(source) -> dict[str, float | None]:
    """Table 10 : paramètres financiers (taux, durée, DSCR, TEG…)."""
    import unicodedata, re

    def _n(s: str) -> str:
        """Lowercase + supprime accents."""
        nfkd = unicodedata.normalize("NFD", s.lower())
        return "".join(c for c in nfkd if not unicodedata.combining(c))

    params: dict[str, Any] = {}
    for row in _get_records(source, "Capacite"):
        ind = row.get("Indicateur", "") or ""
        val = row.get("Valeur")
        n = _n(ind)

        # Taux d'intérêt annuel — doit COMMENCER par "taux" (pas "intérêts totaux")
        if re.match(r"^taux\b.*(interet|inter)", n):
            params["rate_annual"] = _safe_float(val)
        # Durée — commence par "dur" + "mois"
        elif re.match(r"^dur", n) and "mois" in n:
            params["duration_months"] = _safe_float(val)
        # Différé — commence par "diff" + "mois"
        elif re.match(r"^diff", n) and "mois" in n:
            params["deferred_months"] = _safe_float(val)
        # DSCR — contient "dscr" mais pas "stress"
        elif "dscr" in n and "stress" not in n:
            params["dscr"] = _safe_float(val)
        # EBE — commence par "ebe" ou "excedent brut"
        elif re.match(r"^ebe\b", n) or re.match(r"^excedent brut", n):
            params["ebe"] = _safe_float(val)
        # TEG — commence par "teg"
        elif re.match(r"^teg\b", n):
            params["teg"] = _safe_float(val)
        # Crédit recommandé / montant de référence
        elif re.match(r"^(credit|pret) recommande", n):
            params["ref_amount"] = _safe_float(val)
        # Service de la dette
        elif "service" in n and "dette" in n:
            params["debt_service"] = _safe_float(val)

    return params


def _read_coherence_ranges(source) -> list[dict]:
    """Table 18 : plages de référence pour contrôles de vraisemblance."""
    ranges = []
    for row in _get_records(source, "Controle"):
        ind = row.get("Indicateur", "") or ""
        ref_min = _safe_float(row.get("Réf. min") or row.get("R\xe9f. min"))
        ref_max = _safe_float(row.get("Réf. max") or row.get("R\xe9f. max"))
        verdict = row.get("Verdict final") or row.get("Verdict") or ""
        if ind and (ref_min is not None or ref_max is not None):
            ranges.append({"indicateur": ind, "min": ref_min, "max": ref_max, "verdict": verdict})
    return ranges


# ── Calcul du score par critère — AUCUNE courbe ici, tout vient des barèmes ──

def _q1(value) -> Decimal:
    """Quantize des scores et des points — le même que `analyse.q1`."""
    from decimal import ROUND_HALF_UP
    return Decimal(str(value)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)


def _points_ponderes(score, poids) -> Decimal:
    """`points = score × poids / 100`, arrondi au dixième — cf. `analyse._points`.

    L'arrondi se fait critère par critère et le total est la somme des points
    arrondis : c'est ce que l'analyste additionne à l'écran, la colonne doit
    tomber juste.
    """
    return _q1(Decimal(str(score)) * Decimal(str(poids)) / Decimal(100))


def _choc_stress(baremes: dict) -> Decimal:
    """Amplitude du choc de revenus, lue dans le barème DECISION (principe 8).

    Le −25 % était codé en dur ici (`* 0.75`) alors que le moteur le lit en base :
    le comité pouvait durcir le stress test de l'instruction sans que la
    simulation suive.
    """
    from credits.analyse import choc_stress, regles_decision
    return choc_stress(regles_decision(baremes.get("DECISION")))


def _baremes_actifs() -> dict:
    """Barèmes actifs indexés par code, sans lever.

    `analyse.charger_baremes()` REFUSE d'analyser quand un barème manque : c'est
    juste pour une analyse qui décide. Ici, un barème manquant ne doit pas rendre
    le parcours client indisponible : le critère concerné devient non calculable
    et sort de la pondération, ce que le module sait déjà faire et dire.
    """
    try:
        from credits.models import BaremeScore
        return {b.code: b for b in BaremeScore.objects.filter(actif=True)}
    except Exception as exc:  # noqa: BLE001
        logger.warning("Barèmes illisibles pour la simulation : %s", exc)
        return {}


def _score_technique(referentiel, ns_totals: dict[str, float], quantite,
                     unite_dossier: str | None, bareme, poids,
                     poids_modules: dict | None = None):
    """Fiabilité technique — délègue à `analyse.scorer_technique`, sans exception.

    C'est LE même calcul que celui de l'instruction : écart relatif par module,
    moyenne des écarts absolus, courbe `ECART_TECHNIQUE`. L'ancienne règle locale
    (« part des modules à ±30 % ») donnait un autre score au même dossier.

    Retourne `(bloc, note, calculable)`. Non calculable — et donc EXCLU de la
    pondération, jamais noté à un milieu arbitraire — dans trois cas explicites :
    aucun référentiel pour la filière, barème absent, ou dimension du dossier
    exprimée dans une autre unité que celle du référentiel.
    """
    from credits.analyse import scorer_technique

    if referentiel is None:
        return None, (
            "Fiabilité technique non calculable : aucun référentiel de coûts n'est "
            "actif pour cette filière. Le réalisme des coûts ne peut être établi "
            "sans référence — le critère est exclu du score plutôt que noté à un "
            "milieu arbitraire."
        ), False
    if bareme is None:
        return None, ("Fiabilité technique non calculable : le barème "
                      "« ECART_TECHNIQUE » n'est pas configuré en base."), False
    if not ns_totals:
        return None, ("Fiabilité technique non calculable : la feuille de besoins "
                      "ne porte aucun montant par module à comparer au "
                      "référentiel."), False

    unite_ref = (referentiel.unite_reference or "ha").strip().lower()
    unite_dossier = (unite_dossier or "ha").strip().lower()
    if unite_dossier != unite_ref:
        return None, (
            f"Fiabilité technique non calculable : la filière se mesure en "
            f"« {unite_ref} » et la simulation est dimensionnée en "
            f"« {unite_dossier} ». Aucune conversion n'existe entre ces unités — "
            f"renseignez la quantité dans l'unité de la filière."
        ), False
    if not quantite or Decimal(str(quantite)) <= 0:
        return None, (
            f"Fiabilité technique non calculable : la dimension du projet "
            f"(en « {unite_ref} ») n'est pas renseignée, les coûts de référence ne "
            f"peuvent pas être ramenés au projet."
        ), False

    # Frontière de type : les totaux arrivent en `float` (payload de vue) et
    # repassent en `Decimal` par leur représentation décimale — le calcul lui-même
    # est intégralement en `Decimal` dans `scorer_technique`.
    totaux = {k: Decimal(str(v)) for k, v in ns_totals.items()}
    # Même pondération par filière qu'à l'instruction : sans elle, le client
    # simulerait avec une moyenne d'écarts non pondérée et serait instruit avec
    # une moyenne pondérée — deux scores techniques pour un même plan, soit
    # exactement la divergence simulateur/instruction que ce module a supprimée.
    bloc = scorer_technique(totaux, referentiel, Decimal(str(quantite)),
                            bareme, Decimal(str(poids)), unite_ref,
                            poids_modules=poids_modules)
    details = bloc["details"]
    ecarts = details.get("ecartsHorsPlage") or []
    if ecarts:
        note = ", ".join(e["message"] for e in ecarts[:4])
    else:
        note = (f"Écart moyen de {details.get('ecartMoyenPct')} % au référentiel "
                f"{details.get('referentiel')}.")
    if details.get("commentaire"):
        note = f"{note} {details['commentaire']}".strip()
    return bloc, note, True


def _score_dscr(dscr, bareme, libelle: str = "Capacité financière"):
    """DSCR → score, par la courbe `DSCR` en base. Aucun palier codé ici."""
    if bareme is None:
        return None, (f"{libelle} non calculable : le barème « DSCR » n'est pas "
                      f"configuré en base."), False
    if dscr is None:
        return None, (f"{libelle} non calculable : aucun DSCR n'a pu être estimé "
                      f"(ni EBE de référence, ni trésorerie déclarée)."), False
    valeur = Decimal(str(dscr))
    score = bareme.evaluer(valeur)
    qualif = ("solide" if valeur >= Decimal("1.3")
              else "acceptable" if valeur >= Decimal("1.0")
              else "insuffisante")
    return score, f"DSCR = {valeur:.2f} ({qualif})", True


def _score_comportemental(client, poids):
    """Historique comportemental — délègue à `analyse.scorer_comportemental`.

    Ce module lisait les transactions du wallet, le moteur lit `portfolio.Loan` :
    deux histoires différentes pour le même client. Celle du moteur fait foi (elle
    porte le remboursement, pas le débit), et son score neutre de 50 est annoncé
    comme tel plutôt que déguisé en performance.
    """
    from credits.analyse import scorer_comportemental

    bloc = scorer_comportemental(client, None, Decimal(str(poids)))
    return bloc, bloc["details"].get("commentaire", ""), True


def _score_garanties(guarantees_data: dict | None, amount, bareme, poids):
    """Couverture des garanties → courbe `COUVERTURE_GARANTIES`, plafond compris.

    Le plafond « garanties non constituées » vit dans les paramètres du barème,
    comme dans `analyse.scorer_garanties` : c'est la même règle, lue au même
    endroit. En simulation, les garanties ne sont par construction jamais
    constituées — le plafond s'applique donc toujours, et c'est dit.
    """
    if bareme is None:
        return None, ("Garanties non calculables : le barème "
                      "« COUVERTURE_GARANTIES » n'est pas configuré en base."), False

    montant = Decimal(str(amount or 0))
    items = (guarantees_data or {}).get("items", []) if isinstance(guarantees_data, dict) else []
    couverture = Decimal("0")
    for g in items:
        brut = g.get("holdAmount") or g.get("valeur_estimee") or 0
        try:
            couverture += Decimal(str(brut))
        except Exception:  # noqa: BLE001
            continue

    ratio = (couverture / montant).quantize(Decimal("0.001")) if montant else Decimal("0")
    score = bareme.evaluer(ratio)
    plafond = Decimal(str((bareme.parametres or {}).get("plafond_non_constituees", "60")))
    score = min(score, plafond)
    if couverture <= 0:
        note = ("Aucune garantie déclarée — score indicatif plafonné en attente de "
                "constitution.")
    else:
        note = (f"Couverture déclarée {ratio}× — score indicatif plafonné tant que "
                f"les garanties ne sont pas constituées et vérifiées.")
    return score, note, True


# ── Tableau d'amortissement ───────────────────────────────────────────────────

def _build_schedule(amount, rate_annual, duration_months: int,
                    deferred: int = 0) -> list[dict]:
    """Échéancier au format legacy, calculé par `credits.echeancier` en `Decimal`.

    Ce module portait sa propre boucle d'amortissement en `float`, dont la
    dernière tranche n'était pas ajustée : le CRD final ne tombait pas exactement
    à zéro et le total différait de celui de l'analyse pour le même prêt. La seule
    formule d'amortissement du module crédit est désormais
    `construire_echeancier` — les centimes du simulateur et ceux de l'instruction
    sont les mêmes.

    `rate_annual` est un TAUX ANNUEL EN FRACTION (0,18) pour rester compatible avec
    les appelants ; `construire_echeancier` le veut en points (18).
    """
    from credits.echeancier import EcheancierError, construire_echeancier

    try:
        lignes = construire_echeancier(
            Decimal(str(amount)), Decimal(str(rate_annual)) * Decimal(100),
            int(duration_months), int(deferred or 0))
    except EcheancierError as exc:
        logger.info("Échéancier de simulation non constructible : %s", exc.message)
        return []

    return [
        {
            "month": l["mois"],
            "principal": float(l["capital"]),
            "interest": float(l["interets"]),
            "payment": float(l["echeance"]),
            "balance": float(l["crd"]),
        }
        for l in lignes
    ]


# ── Point d'entrée principal ─────────────────────────────────────────────────

def dataio_simulate(
    client,
    value_chain_code: str | None,
    needs_sheet=None,
    ns_totals: dict | None = None,
    area_ha: float | None = None,
    amount_requested: float | None = None,
    currency: str = "USD",
    guarantees_data: dict | None = None,
    module_financing: dict | None = None,
    quantite_reference: float | None = None,
    unite_reference: str | None = None,
) -> dict:
    """
    Simulation complète à partir des données de référence (dataio).

    Paramètres :
      client           : FintechUser
      value_chain_code : code filière (ex. 'CAFE_ARABICA')
      needs_sheet      : NeedsSheet ORM (optionnel)
      ns_totals        : dict {module: montant} si déjà calculé côté frontend
      area_ha          : superficie en hectares (filières mesurées en hectares)
      amount_requested : montant demandé
      currency         : 'USD' | 'CDF'
      guarantees_data  : dict avec 'items' (liste de garanties)
      module_financing : dict {module_canonique: pct 0..100} — part demandée par
                         module (contrat §1). Absent = 100 %. Les COÛTS restent
                         lus de `ns_totals` (DataRecord), jamais du payload
                         (principe 1) ; seul le POURCENTAGE demandé vient d'ici.
      quantite_reference / unite_reference :
                         dimension du projet pour les filières qui ne se mesurent
                         PAS en hectares (30 ruches, 1 000 sujets, 100 m²,
                         2 000 sacs, 300 t usinées). À défaut, `area_ha` est
                         retenue avec l'unité « ha ». Si l'unité ne correspond pas
                         à celle du référentiel de la filière, la fiabilité
                         technique est déclarée NON CALCULABLE — jamais convertie.

    Retourne un dict compatible avec CreditSimulateResult (TypeScript).
    """

    # ── Frontière de type ────────────────────────────────────────────────────
    # Les grandeurs d'ENTRÉE arrivent en `float` (payload de vue) et les
    # grandeurs de SORTIE partent en `float` (JSON). Entre les deux, tout ce qui
    # note ou tarife passe par les fonctions `Decimal` du moteur : barèmes,
    # échéancier, grille de taux. Le `float` ne traverse plus un calcul de score.
    amount_requested = en_float_optionnel(amount_requested)
    area_ha = en_float_optionnel(area_ha)
    if ns_totals:
        ns_totals = {k: en_float_optionnel(v) for k, v in ns_totals.items()}

    # ── Filière de référence (ValueChain) ────────────────────────────────────
    value_chain = None
    if value_chain_code:
        try:
            from reference_data.models import ValueChain as _VC
            value_chain = _VC.objects.filter(code=value_chain_code, active=True).first()
        except Exception:
            pass

    # ── Source SIMULATEUR dans la base ───────────────────────────────────────
    source = _find_source(value_chain_code)

    # ── Données de la feuille de besoins ──────────────────────────────────────
    if ns_totals is None:
        ns_totals = {}
    if needs_sheet and not ns_totals:
        ns_totals = needs_sheet.total_by_module or {}

    grand_total_ns = sum(ns_totals.values()) if ns_totals else (amount_requested or 0)

    # ── Données de référence depuis le simulateur ──────────────────────────────
    ref_totals_sim: dict[str, float] = {}
    loan_params: dict = {}

    if source:
        ref_totals_sim = _read_reference_totals(source)
        loan_params    = _read_loan_params(source)

    # ── Totaux de référence par filière ───────────────────────────────────────
    # Priorité : simulateur par filière > référentiel ValueChain (module_weights × superficie)
    # On n'utilise le simulateur générique que si la filière correspond vraiment.
    source_matches_chain = False
    if source and value_chain_code:
        src_tokens = _normalize(source.original_name)
        vc_tokens  = _normalize(value_chain_code)
        source_matches_chain = bool(vc_tokens & src_tokens)

    # ── Dimension du projet, dans l'unité de la filière ───────────────────────
    # `area_ha` reste la valeur par défaut (les 9 filières mesurées en hectares) ;
    # les autres passent `quantite_reference` + `unite_reference`. Aucune
    # conversion : une unité qui ne correspond pas au référentiel rend la
    # fiabilité technique non calculable, elle ne la fausse pas.
    referentiel = _referentiel_filiere(value_chain)
    if quantite_reference is not None:
        quantite = en_float_optionnel(quantite_reference)
        unite = (unite_reference or "").strip().lower() or (
            (referentiel.unite_reference or "ha").strip().lower()
            if referentiel is not None else "ha")
    else:
        quantite = area_ha
        unite = (unite_reference or "ha").strip().lower()

    # Référentiel filière autoritatif (`ReferentielFiliere`, principe 1) — la même
    # source que le moteur `analyse.py`, résolue par les mêmes clés.
    ref_filiere_code = referentiel.code if referentiel is not None else None
    unite_referentiel = ((referentiel.unite_reference or "ha").strip().lower()
                         if referentiel is not None else None)
    ref_filiere_totals = (
        _referentiel_filiere_totals(referentiel, quantite)
        if unite_referentiel == unite else {})

    if source_matches_chain and ref_totals_sim:
        ref_totals = ref_totals_sim
    elif ref_filiere_totals:
        ref_totals = ref_filiere_totals
    elif value_chain and area_ha and unite == "ha" and getattr(value_chain, "module_weights", None):
        # Calcul des références depuis le référentiel filière (coût/ha × superficie)
        cost_per_ha = float(
            value_chain.cost_per_hectare_usd if currency == "USD"
            else getattr(value_chain, "cost_per_hectare_cdf", value_chain.cost_per_hectare_usd)
        )
        total_ref = cost_per_ha * float(area_ha)
        ref_totals = {
            mod: round(total_ref * pct / 100, 2)
            for mod, pct in value_chain.module_weights.items()
            if pct > 0
        }
    elif value_chain and grand_total_ns and getattr(value_chain, "module_weights", None):
        # Pas de superficie → utiliser les poids sur le montant demandé
        ref_totals = {
            mod: round(grand_total_ns * pct / 100, 2)
            for mod, pct in value_chain.module_weights.items()
            if pct > 0
        }
    else:
        ref_totals = ref_totals_sim  # fallback : simulateur générique

    # ── Paramètres du prêt ────────────────────────────────────────────────────
    raw_rate = loan_params.get("rate_annual")
    # Garde-fou : un taux brut > 1 est probablement en % (ex. 18.0 au lieu de 0.18)
    if raw_rate and raw_rate > 1.0:
        raw_rate = raw_rate / 100
    if raw_rate and raw_rate > 1.0:
        raw_rate = None  # valeur incohérente → fallback

    # Taux final : simulateur (si filière correspondante) > ValueChain.base_rate > 18% par défaut
    if raw_rate and source_matches_chain:
        rate_annual = raw_rate
    elif value_chain and getattr(value_chain, "base_rate", None):
        vc_rate = float(value_chain.base_rate)
        rate_annual = vc_rate / 100 if vc_rate > 1 else vc_rate
    else:
        rate_annual = raw_rate or 0.18

    # Durée : simulateur > ValueChain.cycle_months > 12 mois par défaut
    raw_duration = loan_params.get("duration_months")
    if raw_duration and source_matches_chain:
        duration_months = int(raw_duration)
    elif value_chain and getattr(value_chain, "cycle_months", None):
        duration_months = int(value_chain.cycle_months)
    else:
        duration_months = int(raw_duration or 12)

    # Différé : simulateur > 0 mois
    raw_deferred = loan_params.get("deferred_months")
    deferred_months = int(raw_deferred or 0) if source_matches_chain else 0

    ref_dscr = loan_params.get("dscr")
    ref_ebe  = loan_params.get("ebe") if source_matches_chain else None

    # ── Financement par module (contrat §1) ────────────────────────────────────
    # Coûts TOUJOURS lus des DataRecord (`ns_totals`), jamais du payload (principe
    # 1) ; seul le POURCENTAGE demandé par module vient de `module_financing`.
    #   part demandée = coût fichier × pct/100  (module absent = 100 %)
    #   montant demandé ajusté = Σ parts demandées
    # Un module à 0 dans la feuille reste à 0 (aucun financement inventable).
    from credits.needs_sheet import MODULE_CODES

    module_financing = module_financing or {}
    _mf_ordered = [m for m in MODULE_CODES if m in ns_totals]
    _mf_ordered += [m for m in ns_totals if m not in MODULE_CODES]  # postes hors nomenclature
    module_financing_rows: list[dict] = []
    montant_demande_ajuste = 0.0
    for mod in _mf_ordered:
        cout = float(ns_totals.get(mod) or 0)
        pct = module_financing.get(mod, 100)
        part = round(cout * pct / 100.0, 2)
        montant_demande_ajuste += part
        module_financing_rows.append({
            "module": mod,
            "coutFichier": round(cout, 2),
            "pct": pct,
            "partDemandee": part,
        })
    montant_demande_ajuste = round(montant_demande_ajuste, 2)

    # Montant scoré : Σ parts demandées quand un financement par module est fourni
    # (le DSCR et l'échéancier suivent ce montant ajusté, §1) ; sinon comportement
    # inchangé — montant demandé, ou grand total de la feuille.
    if module_financing:
        sim_amount = (montant_demande_ajuste
                      or float(amount_requested or 0)
                      or grand_total_ns
                      or (loan_params.get("ref_amount") or 5000))
    else:
        sim_amount = amount_requested or grand_total_ns or (loan_params.get("ref_amount") or 5000)

    # ── DSCR estimé ───────────────────────────────────────────────────────────
    estimated_dscr: float | None = None

    if ref_ebe and sim_amount and loan_params.get("ref_amount") and source_matches_chain:
        # Filière correspondante : on scale l'EBE de référence proportionnellement au montant
        ref_amount = loan_params["ref_amount"]
        scale = sim_amount / ref_amount if ref_amount else 1.0
        scaled_ebe = ref_ebe * scale
        monthly_interest = sim_amount * rate_annual / 12
        monthly_principal = sim_amount / max(duration_months - deferred_months, 1)
        monthly_service = monthly_principal + monthly_interest
        annual_service = monthly_service * 12
        if annual_service > 0:
            annual_ebe = scaled_ebe * (12 / max(duration_months, 1))
            estimated_dscr = round(annual_ebe / annual_service, 3)
            if not (0 < estimated_dscr <= 10):
                estimated_dscr = ref_dscr

    elif ref_dscr:
        # Simulateur disponible mais filière différente : utiliser le DSCR de référence
        # scalé par le rapport montant_client / montant_référence (proxy)
        ref_amount = loan_params.get("ref_amount") or sim_amount
        if ref_amount and sim_amount and ref_amount > 0:
            ratio = sim_amount / ref_amount
            # DSCR ↓ quand montant ↑ (à EBE constant), ↑ quand montant ↓
            estimated_dscr = round(ref_dscr / ratio if ratio > 0 else ref_dscr, 3)
            estimated_dscr = max(0.1, min(estimated_dscr, 5.0))  # plage raisonnable
        else:
            estimated_dscr = ref_dscr

    # ── Calcul des scores par critère — MÊMES règles que l'instruction ────────
    # Chaque critère renvoie `(score, note, calculable)`. Un critère non
    # calculable est EXCLU de la pondération (« pas de moyenne sans effectif »),
    # jamais noté à un milieu arbitraire qui tirerait la note vers un faux centre.
    from credits.analyse import CRITERES, poids_effectifs, recommander, regles_decision
    from credits.scoring import LABELS_CRITERES, NOTES_RECOMMANDATION, taux_pour_score

    baremes = _baremes_actifs()
    poids = poids_effectifs()          # InstitutionConfig — la pondération unique

    bloc_tech, note_tech, ok_tech = _score_technique(
        referentiel, ns_totals, quantite, unite,
        baremes.get("ECART_TECHNIQUE"), poids["technique"],
        poids_modules=getattr(value_chain, "module_weights", None))

    dscr_stress = (Decimal(str(estimated_dscr)) * (Decimal(1) - _choc_stress(baremes))
                   if estimated_dscr is not None else None)
    bloc_compo, note_compo, ok_compo = _score_comportemental(
        client, poids["comportemental"])

    scores_map: dict[str, tuple] = {
        "technique": ((bloc_tech["score"] if ok_tech else None), note_tech, ok_tech),
        "dscr": _score_dscr(estimated_dscr, baremes.get("DSCR")),
        "stress": _score_dscr(dscr_stress, baremes.get("DSCR"),
                              libelle="Résilience au stress"),
        "comportemental": (bloc_compo["score"], note_compo, ok_compo),
        "garanties": _score_garanties(guarantees_data, sim_amount,
                                      baremes.get("COUVERTURE_GARANTIES"),
                                      poids["garanties"]),
    }

    # ── Pondération — `points = score × poids / 100`, comme à l'instruction ───
    # L'ancien format servait `points = score /100` et `maxPoints = 100` : la
    # somme des lignes affichées ne faisait pas le score global, et l'analyste ne
    # pouvait pas vérifier le total de tête. Ici la colonne se lit « 8,5 / 25 » et
    # sa somme tombe exactement sur la note (invariant CONVENTIONS.md §5.2).
    breakdown = []
    total_points = Decimal("0")
    poids_calculable = Decimal("0")
    n_non_calculable = 0

    for cle in CRITERES:
        score, detail, calculable = scores_map[cle]
        p = Decimal(str(poids[cle]))
        if not calculable or score is None:
            n_non_calculable += 1
            breakdown.append({
                "code": cle,
                "label": LABELS_CRITERES[cle],
                "points": None,
                "maxPoints": float(p),
                "weight": float(p),
                "weightedScore": None,
                "score": None,
                "calculable": False,
                "detail": detail,
            })
            continue
        score = Decimal(str(score))
        points = _points_ponderes(score, p)
        total_points += points
        poids_calculable += p
        breakdown.append({
            "code": cle,
            "label": LABELS_CRITERES[cle],
            "points": float(points),
            "maxPoints": float(p),
            "weight": float(p),
            "weightedScore": float(points),
            "score": float(score),
            "calculable": True,
            "detail": detail,
        })

    # Avec un critère exclu, on renormalise sur les poids restants : la note reste
    # sur 0–100 sans qu'aucune valeur ne soit inventée pour combler le trou.
    if poids_calculable <= 0:
        final_score = None
    elif n_non_calculable:
        final_score = float(_q1(total_points * Decimal(100) / poids_calculable))
    else:
        final_score = float(_q1(total_points))

    # ── Taux proposé — grille UNIQUE (`BaremeScore` « TAUX ») ─────────────────
    # Ce module appliquait +2,0 sur la bande [55, 70[ quand `credits.scoring`
    # appliquait +2,5 : le client simulait à 20 % et était instruit à 20,5 %.
    # Il n'existe plus qu'un seul chemin vers un taux, et il est en base.
    base_rate = rate_annual * 100
    tarification = taux_pour_score(final_score if final_score is not None else 0,
                                   base_rate)
    proposed_rate = tarification["taux"]

    # ── Score minimal requis (depuis ValueChain ou défaut 60) ────────────────
    min_score = int(value_chain.min_score_required) if value_chain else 60

    # Recommandation par le MÊME barème de décision que l'instruction : le
    # simulateur n'a plus sa propre échelle de verdicts. `hors_plage` reste vide —
    # une simulation ne porte pas de justification d'analyste.
    regles = regles_decision(baremes.get("DECISION"))
    recommandation = (
        recommander(Decimal(str(final_score)), Decimal(str(estimated_dscr or 0)),
                    [], regles)
        if final_score is not None else None)
    eligible = (final_score is not None and final_score >= min_score)

    # ── Tableau d'amortissement ───────────────────────────────────────────────
    schedule = _build_schedule(
        amount=sim_amount,
        rate_annual=proposed_rate / 100,
        duration_months=duration_months,
        deferred=deferred_months,
    )
    # Totaux de l'échéancier servis PAR LE SERVEUR : le front affiche un
    # échéancier complet (toutes les lignes) et sa synthèse, mais ne somme
    # jamais lui-même (règle §5 « zéro chiffre métier calculé côté client »).
    # Les lignes viennent de `construire_echeancier` (Decimal) : la somme se fait
    # en Decimal puis sort en `float`, sans accumuler de bruit binaire.
    _somme = lambda cle: float(  # noqa: E731
        sum((Decimal(str(r[cle])) for r in schedule), Decimal("0")))
    schedule_totals = {
        "totalPrincipal": _somme("principal"),
        "totalInterest":  _somme("interest"),
        "totalPayments":  _somme("payment"),
        "count":          len(schedule),
    }

    source_label = (
        source.original_name if source_matches_chain
        else f"Référentiel filière {value_chain_code or 'AGRICAP'}" if value_chain
        else (source.original_name if source else "N/A")
    )

    resultat = {
        "score":            final_score,
        "breakdown":        breakdown,
        "eligible":         eligible,
        "minScoreRequired": min_score,
        "proposedRate":     proposed_rate,
        "scheduleDraft":    schedule,
        "scheduleTotals":   schedule_totals,
        # La note suit la RECOMMANDATION du barème de décision, comme à
        # l'instruction : le simulateur n'a plus ses propres bandes de verdict.
        "valuationNote":    NOTES_RECOMMANDATION.get(
            recommandation,
            "Score non calculable en l'état : voir le détail par critère."),
        "recommandation":   recommandation,
        # Couverture de la note : sur quelle part du barème elle a été calculée.
        # Sans cela, une simulation renormalisée sur 3 critères (70 % des poids)
        # s'affiche comme une note sur 100 et paraît MEILLEURE que l'instruction,
        # qui, elle, aura pu calculer le DSCR. « Pas de moyenne sans effectif »
        # (CONVENTIONS.md §4.6) : la base de calcul se dit.
        "scoreCouverture": {
            "poidsCalculable": float(poids_calculable),
            "poidsTotal": float(sum(Decimal(str(poids[c])) for c in CRITERES)),
            "nbCriteresExclus": n_non_calculable,
            "renormalise": bool(n_non_calculable and poids_calculable > 0),
        },
        # Financement par module (contrat §1) — parts demandées et montant scoré.
        "moduleFinancing":     module_financing_rows,
        "montantDemandeAjuste": montant_demande_ajuste,
        # Tarification : le taux ET la bande qui l'explique. Servi au STAFF
        # uniquement par la vue (la grille ne descend jamais au client — principe
        # 7) ; c'est la vue qui filtre, ce module ne connaît pas le rôle appelant.
        "tarification": {k: v for k, v in tarification.items()
                         if not k.startswith("_")},
        "refData": {
            "source":          source_label,
            "sourceFile":      source.original_name if source else None,
            "sourceMatchesChain": source_matches_chain,
            "referentielFiliere": ref_filiere_code,
            "uniteReference":  unite_referentiel,
            "quantiteReference": quantite,
            "uniteDossier":    unite,
            "dscr":            round(estimated_dscr, 3) if estimated_dscr else None,
            "dscrStress":      float(_q1(dscr_stress) if dscr_stress is not None else 0)
                               if dscr_stress is not None else None,
            "durationMonths":  duration_months,
            "deferredMonths":  deferred_months,
            "rateAnnual":      rate_annual,
            "refTotals":       ref_totals,
            "grandTotalNS":    round(grand_total_ns, 2),
        },
    }
    if final_score is None:
        # Aucun critère calculable : on ne sert pas un 0 qui passerait pour une
        # note. Le motif est déjà dans chaque ligne du `breakdown`.
        resultat["unavailable"] = {
            "code": "SCORE_NON_CALCULABLE",
            "message": ("Aucun critère n'est calculable en l'état : barèmes non "
                        "configurés, ou données de référence absentes. Le détail "
                        "par critère en donne la raison."),
        }
    return resultat
