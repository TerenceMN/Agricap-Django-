"""
Moteur d'analyse documentaire — Partie H du PRD Crédits Agricoles.

Pipeline en 5 passes séquentielles :
  1. Normalisation    → LIGNE_INCOMPLETE
  2. Contrôles ligne  → PRIX_UNITAIRE_HORS_FOURCHETTE, RUBRIQUE_INCOHERENTE, FOURNISSEUR_NON_AGREE
  3. Croisés          → TOTAL_FALSIFIE, MODULE_HORS_POIDS, CONCENTRATION_MODULE
  4. Synthèse module  → conclusions agrégées par module
  5. Liaison score    → chaîne de preuve (findings → critère → impact total)

Conclusions et recommandations : générées par gabarits de phrases (déterministe —
mêmes entrées = mêmes textes).

Point d'entrée public : run_analysis(needs_sheet_id, value_chain, area_ha, currency)
→ sauvegarde les LineFinding en base et met à jour NeedsSheet.document_confidence.
"""
from __future__ import annotations

from typing import Any

# ── Poids de sévérité pour le calcul de confiance ────────────────────────────
_SEV_WEIGHT: dict[str, int] = {
    "bloquant":    20,
    "anomalie":     8,
    "a_justifier":  3,
    "info":         0,
    "point_fort":   0,
}

# ── Impact de chaque règle sur le critère fiabilite_technique ─────────────────
_RULE_IMPACT: dict[str, dict[str, Any]] = {
    "TOTAL_FALSIFIE":                {"criterion": "fiabilite_technique", "points": -20},
    "PRIX_UNITAIRE_HORS_FOURCHETTE": {"criterion": "fiabilite_technique", "points": -4},
    "MODULE_HORS_POIDS":             {"criterion": "fiabilite_technique", "points": -5},
    "CONCENTRATION_MODULE":          {"criterion": "fiabilite_technique", "points": -3},
    "RUBRIQUE_INCOHERENTE":          {"criterion": "fiabilite_technique", "points": -3},
    "FOURNISSEUR_NON_AGREE":         {"criterion": "fiabilite_technique", "points": -1},
    "LIGNE_INCOMPLETE":              {"criterion": "fiabilite_technique", "points": -1},
    "PRIX_COMPETITIF":               {"criterion": "fiabilite_technique", "points": +2},
}

# ── Mots-clés sémantiques par module ─────────────────────────────────────────
_MODULE_KEYWORDS: dict[str, set[str]] = {
    "semences":         {"semence", "intrant", "engrais", "fertilisant", "herbicide",
                         "pesticide", "fongicide", "insecticide", "phyto", "graine", "plant"},
    "mecanisation":     {"tracteur", "motoculteur", "labour", "mecanique", "machine",
                         "motoris", "charrue", "moteur", "mecanise"},
    "maindoeuvre":      {"main", "ouvrier", "salaire", "journalier", "travailleur",
                         "manoeuvre", "employe", "prestataire"},
    "equipements":      {"outil", "materiel", "seau", "brouette", "houe", "machette",
                         "filet", "bache", "arrosoir", "pulverisateur", "pompe", "kit"},
    "postrecolte":      {"recolte", "sechage", "decortique", "transformation", "stockage",
                         "conservation", "sac", "battage", "vannage", "conditionnement"},
    "logistique":       {"transport", "camion", "moto", "carburant", "livraison",
                         "chargement", "fret", "vehicule"},
    "commercialisation":{"vente", "marche", "acheteur", "commission", "taxe", "emballage"},
    "reserve":          {"reserve", "divers", "imprev", "provision", "divers"},
}


# ── Utilitaires ───────────────────────────────────────────────────────────────

def _tok(s: str) -> set[str]:
    """Tokenise : minuscules, sans accents, tokens ≥ 3 lettres."""
    import re
    import unicodedata
    nfkd = unicodedata.normalize("NFD", (s or "").lower())
    ascii_s = "".join(c for c in nfkd if not unicodedata.combining(c))
    return set(w for w in re.split(r"[\W_]+", ascii_s) if len(w) >= 3)


def _pct_dev(observed: float, reference: float) -> float | None:
    if not reference:
        return None
    return (observed - reference) / reference * 100


def _fmt_pct(v: float) -> str:
    sign = "+" if v >= 0 else ""
    return f"{sign}{v:.0f}%"


# ── Moteur principal ──────────────────────────────────────────────────────────

class DocumentReasoningEngine:
    """
    Analyse une NeedsSheet en 5 passes et produit une liste de LineFinding.

    Usage :
        engine = DocumentReasoningEngine(needs_sheet, value_chain, area_ha=5.0)
        result = engine.run()
        # result["findings"]         : liste de dicts (non encore persistés)
        # result["moduleSummaries"]  : {module: {conclusion, counts, deviation}}
        # result["scoreLinks"]       : [{criterion, totalPoints, findings}]
        # result["documentConfidence"]: float 0-100
    """

    def __init__(self, needs_sheet, value_chain=None, area_ha=None, currency: str = "USD"):
        self.ns = needs_sheet
        self.vc = value_chain
        self.area_ha = float(area_ha) if area_ha else 1.0
        self.currency = currency
        self._findings: list[dict] = []
        self._ref_items: list[dict] = []   # [{desc, tokens, module, unit_price, qty, unit}]
        self._ref_totals: dict[str, float] = {}   # {module: total}
        self._ref_version: str = "—"

    def run(self) -> dict:
        self._load_reference()
        items = list(self.ns.items.all().order_by("source_sheet_index", "id"))

        self._pass1_normalize(items)
        self._pass2_unit_controls(items)
        self._pass3_cross_controls(items)
        module_summaries = self._pass4_module_summary(items)
        score_links = self._pass5_score_linkage()
        confidence = self._compute_confidence()

        return {
            "findings": self._findings,
            "moduleSummaries": module_summaries,
            "scoreLinks": score_links,
            "documentConfidence": confidence,
            "referenceVersion": self._ref_version,
        }

    # ── Chargement des données de référence ────────────────────────────────────

    def _load_reference(self):
        try:
            from credits.dataio_simulator import (
                _find_source, _get_records, _safe_float, _rubrique_to_module,
            )
            vc_code = self.vc.code if self.vc else None
            src = _find_source(vc_code)
            if not src:
                return
            self._ref_version = getattr(src, "original_name", "—")

            for row in _get_records(src, "Synthese"):
                rubrique = row.get("Rubrique", "") or ""
                total = _safe_float(row.get("Total rubrique"))
                if not rubrique or total is None or "TOTAL" in rubrique.upper():
                    continue
                mod = _rubrique_to_module(rubrique)
                if mod:
                    self._ref_totals[mod] = self._ref_totals.get(mod, 0) + total

            for row in _get_records(src, "Besoins"):
                desc = (
                    row.get("Description détaillée") or row.get("Description")
                    or row.get("Désignation") or row.get("Designation") or ""
                )
                rubrique = row.get("Rubrique", "") or ""
                unit_price = _safe_float(
                    row.get("Coût unitaire") or row.get("Cout unitaire")
                )
                qty = _safe_float(row.get("Quantité") or row.get("Quantite"))
                unite = row.get("Unité", "") or row.get("Unite", "") or ""
                if not desc or unit_price is None:
                    continue
                mod = _rubrique_to_module(rubrique)
                if mod is None:
                    # Repli historique : la ligne de référence est conservée
                    # (la perdre priverait la comparaison de prix d'un article
                    # réel), mais elle atterrit dans « réserve » — un module qui
                    # n'est pas le sien. Un poste MAL classé fausse le poids du
                    # module autant qu'un poste absent, et il est plus difficile
                    # à voir puisque rien ne manque : on le dit.
                    import logging
                    logging.getLogger(__name__).warning(
                        "Rubrique de référence « %s » non classée (%s) : rangée "
                        "par défaut en « réserve ». Compléter le mapping "
                        "`credits.needs_sheet._RUBRIQUE_FRAGMENTS`.",
                        rubrique, self._ref_version,
                    )
                    mod = "reserve"
                self._ref_items.append({
                    "desc": desc,
                    "tokens": _tok(desc),
                    "module": mod,
                    "unit_price": unit_price,
                    "qty": qty,
                    "unit": unite,
                })
        except Exception:
            pass

    def _find_ref_item(self, label: str) -> dict | None:
        if not self._ref_items:
            return None
        label_tokens = _tok(label)
        best, best_score = None, 0
        for ref in self._ref_items:
            score = len(label_tokens & ref["tokens"])
            if score > best_score:
                best_score = score
                best = ref
        return best if best_score >= 2 else None

    # ── Passe 1 : Normalisation ────────────────────────────────────────────────

    def _pass1_normalize(self, items):
        for idx, item in enumerate(items):
            if (item.unit_price is None
                    and item.computed_total is None
                    and item.declared_total is None):
                self._add(
                    rule_id="LIGNE_INCOMPLETE",
                    severity="info",
                    source={
                        "sheet": f"feuille_{item.source_sheet_index}",
                        "row": idx + 2,
                        "label": item.label,
                        "module": item.module,
                    },
                    observed={"value": None, "unit": item.unit or ""},
                    reference={
                        "value": "ligne avec quantité + prix unitaire",
                        "origin": "règle AGRICAP",
                    },
                    deviation="—",
                    conclusion=(
                        f"Ligne «{item.label}» (module {item.module}) incomplète : "
                        "prix unitaire et montant total manquants."
                    ),
                    recommendation=(
                        "Renseignez la quantité et le prix unitaire. "
                        "Le montant total se calcule automatiquement."
                    ),
                )

    # ── Passe 2 : Contrôles unitaires ─────────────────────────────────────────

    def _pass2_unit_controls(self, items):
        for idx, item in enumerate(items):
            self._check_price(item, idx)
            self._check_rubrique(item, idx)
            self._check_supplier(item, idx)

    def _check_price(self, item, idx: int):
        if item.unit_price is None:
            return
        ref = self._find_ref_item(item.label)
        if not ref:
            return
        dev = _pct_dev(float(item.unit_price), ref["unit_price"])
        if dev is None:
            return
        abs_dev = abs(dev)
        if abs_dev < 15:
            self._add(
                rule_id="PRIX_COMPETITIF",
                severity="point_fort",
                source={
                    "sheet": f"feuille_{item.source_sheet_index}",
                    "row": idx + 2,
                    "label": item.label,
                    "module": item.module,
                },
                observed={"value": float(item.unit_price), "unit": item.unit or ""},
                reference={
                    "value": ref["unit_price"],
                    "origin": f"{self._ref_version}!4_Besoins_Financiers",
                    "unit": ref["unit"],
                    "referentialVersion": self._ref_version,
                },
                deviation=_fmt_pct(dev),
                conclusion=(
                    f"Prix unitaire «{item.label}» ({item.unit_price} {self.currency}) "
                    f"conforme au référentiel ({ref['unit_price']} {ref['unit']})."
                ),
            )
            return
        if abs_dev < 40:
            return
        severity = (
            "bloquant" if abs_dev > 200
            else "anomalie" if abs_dev > 80
            else "a_justifier"
        )
        self._add(
            rule_id="PRIX_UNITAIRE_HORS_FOURCHETTE",
            severity=severity,
            source={
                "sheet": f"feuille_{item.source_sheet_index}",
                "row": idx + 2,
                "label": item.label,
                "module": item.module,
            },
            observed={
                "value": float(item.unit_price),
                "unit": item.unit or "",
                "quantity": float(item.quantity) if item.quantity else None,
            },
            reference={
                "value": ref["unit_price"],
                "origin": f"{self._ref_version}!4_Besoins_Financiers",
                "unit": ref["unit"],
                "referentialVersion": self._ref_version,
            },
            deviation=_fmt_pct(dev),
            conclusion=(
                f"Prix unitaire «{item.label}» déclaré ({item.unit_price} {self.currency}) "
                f"dévie de {_fmt_pct(dev)} par rapport au référentiel "
                f"({ref['unit_price']} {ref['unit']})."
            ),
            recommendation=(
                "Fournissez une facture pro forma, ou ramenez le prix dans la fourchette. "
                "Si justifié (intrant certifié, importé), l'analyste peut marquer «Justifié»."
            ),
        )

    def _check_rubrique(self, item, idx: int):
        label_tokens = _tok(item.label)
        current_mod = item.module
        own_kw = _MODULE_KEYWORDS.get(current_mod, set())
        own_match = label_tokens & own_kw
        for other_mod, keywords in _MODULE_KEYWORDS.items():
            if other_mod == current_mod:
                continue
            match = label_tokens & keywords
            if match and len(match) > len(own_match):
                self._add(
                    rule_id="RUBRIQUE_INCOHERENTE",
                    severity="a_justifier",
                    source={
                        "sheet": f"feuille_{item.source_sheet_index}",
                        "row": idx + 2,
                        "label": item.label,
                        "module": item.module,
                    },
                    observed={"value": item.label, "declaredModule": item.module},
                    reference={
                        "value": other_mod,
                        "origin": "table de correspondance rubrique-module AGRICAP",
                    },
                    deviation=f"classé en «{item.module}», semble «{other_mod}»",
                    conclusion=(
                        f"«{item.label}» est dans le module «{item.module}» "
                        f"mais les mots-clés ({', '.join(sorted(match))}) "
                        f"correspondent au module «{other_mod}»."
                    ),
                    recommendation=(
                        "Vérifiez la classification. "
                        "Si correcte, l'analyste peut valider et ignorer l'avertissement."
                    ),
                )
                break

    def _check_supplier(self, item, idx: int):
        if not item.supplier_warning:
            return
        self._add(
            rule_id="FOURNISSEUR_NON_AGREE",
            severity="a_justifier",
            source={
                "sheet": f"feuille_{item.source_sheet_index}",
                "row": idx + 2,
                "label": item.label,
                "module": item.module,
            },
            observed={"value": item.suggested_supplier},
            reference={
                "value": "fournisseur agréé AGRICAP",
                "origin": "annuaire fournisseurs",
            },
            deviation="non agréé",
            conclusion=(
                f"Fournisseur «{item.suggested_supplier}» pour «{item.label}» : "
                f"{item.supplier_warning}"
            ),
            recommendation=(
                "Privilégiez un fournisseur agréé AGRICAP ou justifiez ce choix "
                "auprès de l'analyste."
            ),
        )

    # ── Passe 3 : Contrôles croisés ───────────────────────────────────────────

    def _pass3_cross_controls(self, items):
        self._check_total_falsifie(items)
        self._check_module_weights(items)
        self._check_concentration(items)

    def _check_total_falsifie(self, items):
        """
        Recompute module totals from NeedItem.computed_total and compare
        to NeedsSheet.total_by_module (from Synthèse Excel).
        Écart > 5 % → TOTAL_FALSIFIE (bloquant).
        """
        if not self.ns.total_by_module:
            return
        recomputed: dict[str, float] = {}
        for item in items:
            val = float(item.computed_total or item.declared_total or 0)
            recomputed[item.module] = recomputed.get(item.module, 0) + val

        for mod, synth_val in self.ns.total_by_module.items():
            synth_val = float(synth_val)
            recalc = recomputed.get(mod, 0)
            if synth_val == 0 and recalc == 0:
                continue
            if synth_val == 0:
                continue
            dev = _pct_dev(recalc, synth_val)
            if dev is None or abs(dev) <= 5:
                continue
            self._add(
                rule_id="TOTAL_FALSIFIE",
                severity="bloquant",
                source={"sheet": "5_Synthese_Besoins", "label": f"total module {mod}"},
                observed={
                    "value": round(recalc, 2),
                    "unit": self.currency,
                    "detail": "recalculé depuis les lignes de détail",
                },
                reference={
                    "value": synth_val,
                    "origin": "5_Synthese_Besoins (formule Excel)",
                    "unit": self.currency,
                    "referentialVersion": self._ref_version,
                },
                deviation=_fmt_pct(dev),
                conclusion=(
                    f"Module «{mod}» : total Synthèse ({synth_val:,.0f} {self.currency}) ≠ "
                    f"somme recalculée du détail ({recalc:,.0f} {self.currency}) "
                    f"— écart {_fmt_pct(dev)}. "
                    "Une formule de la feuille 5 a peut-être été écrasée manuellement."
                ),
                recommendation=(
                    "Vérifiez la feuille 5_Synthèse_Besoins : les totaux doivent être "
                    "des formules automatiques, non des valeurs saisies à la main. "
                    "Le dossier ne peut pas être soumis tant que cet écart n'est pas résolu."
                ),
                score_impact_override={"criterion": "fiabilite_technique", "points": -20},
            )

    def _check_module_weights(self, items):
        """
        Compare la répartition réelle à ValueChain.module_weights.
        Tolérance ±30 % → a_justifier ; >80 % → anomalie.
        """
        if not self.vc or not self.vc.module_weights:
            return
        if not self.ns.total_by_module or not self.ns.grand_total:
            return
        grand = float(self.ns.grand_total)
        if grand == 0:
            return
        for mod, expected_pct in self.vc.module_weights.items():
            if float(expected_pct) < 5:
                continue
            actual = float(self.ns.total_by_module.get(mod, 0))
            actual_pct = actual / grand * 100
            dev = _pct_dev(actual_pct, float(expected_pct))
            if dev is None or abs(dev) <= 30:
                continue
            severity = "anomalie" if abs(dev) > 80 else "a_justifier"
            self._add(
                rule_id="MODULE_HORS_POIDS",
                severity=severity,
                source={"sheet": "5_Synthese_Besoins", "label": f"module {mod}"},
                observed={"value": round(actual_pct, 1), "unit": "% du total", "absolute": actual},
                reference={
                    "value": float(expected_pct),
                    "origin": f"ValueChain.module_weights ({self.vc.code})",
                    "unit": "%",
                },
                deviation=_fmt_pct(dev),
                conclusion=(
                    f"Module «{mod}» représente {actual_pct:.1f} % du total "
                    f"(référentiel {self.vc.label} : {expected_pct:.0f} %) "
                    f"— écart {_fmt_pct(dev)}."
                ),
                recommendation=(
                    f"Vérifiez les montants du module «{mod}». "
                    "Si l'écart est contextuel (ex. mécanisation non requise cette campagne), "
                    "précisez-le en commentaire."
                ),
            )

    def _check_concentration(self, items):
        """Un poste > 60 % du module (et non seul) est signalé."""
        by_module: dict[str, list] = {}
        for item in items:
            by_module.setdefault(item.module, []).append(item)
        for mod, mod_items in by_module.items():
            if len(mod_items) <= 1:
                continue
            mod_total = sum(float(i.computed_total or i.declared_total or 0) for i in mod_items)
            if mod_total == 0:
                continue
            for item in mod_items:
                val = float(item.computed_total or item.declared_total or 0)
                pct = val / mod_total * 100
                if pct > 60:
                    self._add(
                        rule_id="CONCENTRATION_MODULE",
                        severity="info",
                        source={
                            "sheet": f"feuille_{item.source_sheet_index}",
                            "label": item.label,
                            "module": mod,
                        },
                        observed={"value": round(pct, 1), "unit": f"% du module {mod}", "absolute": val},
                        reference={
                            "value": 60,
                            "origin": "règle de concentration AGRICAP",
                            "unit": "%",
                        },
                        deviation=_fmt_pct(pct - 60),
                        conclusion=(
                            f"«{item.label}» représente {pct:.0f} % du module «{mod}» "
                            f"({val:,.0f} sur {mod_total:,.0f} {self.currency})."
                        ),
                        recommendation=(
                            "Ce niveau de concentration est inhabituellement élevé. "
                            "Vérifiez que ce poste n'englobe pas plusieurs dépenses distinctes."
                        ),
                    )

    # ── Passe 4 : Synthèse par module ─────────────────────────────────────────

    def _pass4_module_summary(self, items) -> dict[str, dict]:
        modules_seen = {i.module for i in items}
        summaries: dict[str, dict] = {}
        for mod in sorted(modules_seen):
            mod_findings = [
                f for f in self._findings
                if f["source"].get("module") == mod
            ]
            anomalies_c = sum(
                1 for f in mod_findings
                if f["severity"] in ("anomalie", "bloquant")
            )
            a_just_c = sum(
                1 for f in mod_findings if f["severity"] == "a_justifier"
            )
            total_items = sum(1 for i in items if i.module == mod)
            ok_c = max(0, total_items - anomalies_c - a_just_c)

            mod_total = float(
                self.ns.total_by_module.get(mod, 0)
                if self.ns.total_by_module else 0
            )
            ref_total = self._ref_totals.get(mod)
            dev_vs_ref = None
            if ref_total and ref_total > 0 and self.area_ha > 0:
                scaled_ref = ref_total  # référentiel pour 1 ha par défaut dans simulateur
                dev_vs_ref = round(_pct_dev(mod_total, scaled_ref) or 0, 1)

            parts = []
            if ok_c:
                parts.append(f"{ok_c} ligne(s) conforme(s)")
            if anomalies_c:
                parts.append(f"{anomalies_c} anomalie(s)")
            if a_just_c:
                parts.append(f"{a_just_c} à justifier")

            conclusion = (
                f"Module {mod} : {', '.join(parts) or 'aucune ligne analysée'}."
            )
            if dev_vs_ref is not None and abs(dev_vs_ref) > 20:
                conclusion += (
                    f" Coût total ({mod_total:,.0f} {self.currency}) dévie de "
                    f"{_fmt_pct(dev_vs_ref)} vs référentiel filière."
                )

            summaries[mod] = {
                "module": mod,
                "total": mod_total,
                "findingsCount": len(mod_findings),
                "anomaliesCount": anomalies_c,
                "aJustifierCount": a_just_c,
                "deviationVsRef": dev_vs_ref,
                "conclusion": conclusion,
            }
        return summaries

    # ── Passe 5 : Liaison au score ────────────────────────────────────────────

    def _pass5_score_linkage(self) -> list[dict]:
        """
        Groupe les findings par critère de scoring, somme les impacts.
        Invariant : sum(points) = score final - score de base du critère.
        """
        by_crit: dict[str, dict] = {}
        for f in self._findings:
            impact = f.get("scoreImpact", {})
            criterion = impact.get("criterion")
            points = impact.get("points", 0)
            if not criterion:
                continue
            if criterion not in by_crit:
                by_crit[criterion] = {"criterion": criterion, "totalPoints": 0, "findings": []}
            by_crit[criterion]["totalPoints"] += points
            by_crit[criterion]["findings"].append({
                "ruleId": f["ruleId"],
                "severity": f["severity"],
                "label": f["source"].get("label", ""),
                "points": points,
            })
        return list(by_crit.values())

    # ── Confiance documentaire ────────────────────────────────────────────────

    def _compute_confidence(self) -> float:
        penalty = sum(_SEV_WEIGHT.get(f["severity"], 0) for f in self._findings)
        return max(0.0, min(100.0, 100.0 - penalty))

    # ── Helper interne ────────────────────────────────────────────────────────

    def _add(
        self,
        rule_id: str,
        severity: str,
        source: dict,
        observed: dict,
        reference: dict,
        deviation: str,
        conclusion: str,
        recommendation: str = "",
        score_impact_override: dict | None = None,
    ):
        impact = score_impact_override or dict(_RULE_IMPACT.get(rule_id, {}))
        self._findings.append({
            "ruleId": rule_id,
            "severity": severity,
            "source": source,
            "observed": observed,
            "reference": reference,
            "deviation": deviation,
            "scoreImpact": impact,
            "conclusion": conclusion,
            "recommendation": recommendation,
            "analystStatus": "a_traiter",
        })


# ── Point d'entrée public ─────────────────────────────────────────────────────

def run_analysis(
    needs_sheet_id: int,
    value_chain=None,
    area_ha=None,
    currency: str = "USD",
) -> dict:
    """
    Lance le moteur d'analyse sur une NeedsSheet existante en base,
    persiste les LineFinding et met à jour NeedsSheet.document_confidence.

    Retourne le résumé d'analyse (findings count, confidence, module summaries).
    Silencieux en cas d'erreur : l'analyse ne bloque jamais le flux principal.
    """
    from credits.models import NeedsSheet, LineFinding, AnalysisRule

    try:
        ns = NeedsSheet.objects.get(pk=needs_sheet_id)
    except NeedsSheet.DoesNotExist:
        return {}

    engine = DocumentReasoningEngine(
        needs_sheet=ns,
        value_chain=value_chain or ns.value_chain,
        area_ha=area_ha or ns.area_ha,
        currency=currency or ns.currency,
    )
    result = engine.run()

    # Supprimer les anciens findings puis recréer
    LineFinding.objects.filter(needs_sheet=ns).delete()

    rule_cache: dict[str, AnalysisRule | None] = {}
    objs = []
    for f in result["findings"]:
        rid = f["ruleId"]
        if rid not in rule_cache:
            rule_cache[rid] = AnalysisRule.objects.filter(rule_id=rid, active=True).first()
        objs.append(LineFinding(
            needs_sheet=ns,
            rule=rule_cache[rid],
            rule_id_snapshot=rid,
            severity=f["severity"],
            source=f["source"],
            observed=f["observed"],
            reference=f["reference"],
            deviation=f.get("deviation", ""),
            score_impact=f.get("scoreImpact", {}),
            conclusion=f["conclusion"],
            recommendation=f.get("recommendation", ""),
            analyst_status="a_traiter",
        ))
    LineFinding.objects.bulk_create(objs)

    ns.document_confidence = result["documentConfidence"]
    ns.save(update_fields=["document_confidence"])

    return {
        "documentConfidence": result["documentConfidence"],
        "findingsCount": len(result["findings"]),
        "moduleSummaries": result["moduleSummaries"],
        "scoreLinks": result["scoreLinks"],
        "referenceVersion": result["referenceVersion"],
    }


def serialize_analysis_report(ns) -> dict:
    """Sérialise le rapport d'analyse complet d'une NeedsSheet."""
    from credits.models import LineFinding

    findings_qs = LineFinding.objects.filter(needs_sheet=ns).order_by(
        "created_at"
    )

    # Trier par sévérité
    sev_order = {"bloquant": 0, "anomalie": 1, "a_justifier": 2, "info": 3, "point_fort": 4}
    findings = sorted(
        [
            {
                "id": f.pk,
                "ruleId": f.rule_id_snapshot,
                "severity": f.severity,
                "source": f.source,
                "observed": f.observed,
                "reference": f.reference,
                "deviation": f.deviation,
                "scoreImpact": f.score_impact,
                "conclusion": f.conclusion,
                "recommendation": f.recommendation,
                "analystStatus": f.analyst_status,
                "analystComment": f.analyst_comment or None,
            }
            for f in findings_qs
        ],
        key=lambda x: sev_order.get(x["severity"], 5),
    )

    # Recompute module summaries & score links from persisted findings
    by_module: dict[str, list] = {}
    by_crit: dict[str, dict] = {}
    for f in findings:
        mod = f["source"].get("module")
        if mod:
            by_module.setdefault(mod, []).append(f)
        impact = f.get("scoreImpact", {})
        crit = impact.get("criterion")
        pts = impact.get("points", 0)
        if crit:
            if crit not in by_crit:
                by_crit[crit] = {"criterion": crit, "totalPoints": 0, "findings": []}
            by_crit[crit]["totalPoints"] += pts
            by_crit[crit]["findings"].append({
                "ruleId": f["ruleId"], "severity": f["severity"],
                "label": f["source"].get("label", ""), "points": pts,
            })

    return {
        "needsSheetId": ns.pk,
        "documentConfidence": ns.document_confidence,
        "findings": findings,
        "findingsByModule": {
            mod: fs for mod, fs in sorted(by_module.items())
        },
        "scoreLinks": list(by_crit.values()),
        "analysedAt": ns.created_at.isoformat() if ns.created_at else None,
        "hasBlockers": any(f["severity"] == "bloquant" for f in findings),
        "summary": {
            "bloquant":    sum(1 for f in findings if f["severity"] == "bloquant"),
            "anomalie":    sum(1 for f in findings if f["severity"] == "anomalie"),
            "a_justifier": sum(1 for f in findings if f["severity"] == "a_justifier"),
            "point_fort":  sum(1 for f in findings if f["severity"] == "point_fort"),
            "info":        sum(1 for f in findings if f["severity"] == "info"),
        },
    }
