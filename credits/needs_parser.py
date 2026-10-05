"""
Lecture d'une Feuille de Besoins déposée AVANT la création du dossier.

Ce module ne lit plus le classeur lui-même : il appelle
`credits.needs_sheet.lire_classeur`, le lecteur unique du projet — celui que
l'ingestion et la validation utilisent déjà. Il ne garde que ce qui lui est
propre et n'existe nulle part ailleurs :

  1. la confrontation du plan au référentiel de la filière (seuils en base) ;
  2. la vérification des fournisseurs suggérés contre l'annuaire agréé ;
  3. le refus d'un gabarit renvoyé vierge.

POURQUOI CE MODULE A MAIGRI (défaut mesuré, août 2026)
------------------------------------------------------
Il portait un SECOND parseur, écrit pour un template disparu où chaque module
occupait sa propre feuille (« feuille 2 = semences », « feuille 7 = réserve »).
Les classeurs courants ne sont pas faits ainsi : ils portent
`4_Besoins_Financiers` et `5_Synthese_Besoins`. Or ce parseur repérait les
feuilles PAR POSITION. Il lisait donc la synthèse en croyant lire la
post-récolte et le plan de financement en croyant lire la logistique, puis
annonçait « total déclaré 2 660 ≠ total recalculé 1 330 (écart 50 %) » — un
écart entre deux grandeurs sans rapport. Il ignorait par ailleurs la colonne
Fréquence, et transformait une ligne juste (10 × 14 × 2 = 280) en anomalie
(« recalculé 140 »).

Quatre avertissements sur un dossier correct : le pire état pour un contrôle,
puisqu'il apprend à l'analyste à ne plus les lire. C'est le coût exact décrit
au principe 6 — deux implémentations d'un même mécanisme, et c'est la mauvaise
qui servait le parcours client.
"""
from __future__ import annotations

import decimal

from credits.needs_sheet import NeedsSheetReadError, lire_classeur

#: Conservé sous son nom historique : `views.parse_needs_sheet_view` l'importe et
#: le traduit en 422 `PARSE_ERROR`. Un seul type d'erreur de lecture pour un seul
#: lecteur.
NeedsSheetParseError = NeedsSheetReadError


def parse_needs_sheet(
    file_path: str,
    value_chain=None,
    area_ha: decimal.Decimal | None = None,
    currency: str = "USD",
) -> dict:
    """
    Lit un classeur Feuille de Besoins et le confronte au référentiel filière.

    Retourne `{"ok", "items", "totalByModule", "grandTotal", "warnings",
               "anomalies", "anomaliesStaff"}`.

    Les totaux sortent en `float` : ils traversent une réponse JSON et un
    `JSONField` (`NeedsSheet.total_by_module`), qui ne savent pas sérialiser un
    `Decimal`. La lecture et les contrôles, eux, se font en `Decimal` du début à
    la fin (principe 4) — c'est la frontière d'affichage qui convertit, pas le
    calcul.
    """
    lecture = lire_classeur(file_path)

    all_items: list[dict] = lecture["items"]
    all_warnings: list[str] = list(lecture["warnings"])
    total_by_module = {code: float(v) for code, v in lecture["totalByModule"].items()}
    grand_total = float(lecture["grandTotal"])

    # ── Classeur sans aucune ligne exploitable ────────────────────────────────
    if not all_items and grand_total <= 0:
        raise NeedsSheetParseError(
            "Aucun besoin n'a été lu dans ce classeur : la feuille "
            "« 4_Besoins_Financiers » ne contient aucune ligne chiffrée. Saisissez "
            "vos besoins réels — une ligne par dépense, avec sa rubrique, sa "
            "quantité et son prix unitaire — puis re-téléversez le fichier. Vous "
            "pouvez aussi les saisir directement à l'écran, sans passer par Excel."
        )

    # ── Gabarit renvoyé sans être rempli ─────────────────────────────────────
    # Les lignes d'exemple du modèle portent la mention en Observations. Un
    # classeur qui n'a QUE des lignes d'exemple n'est pas un dossier.
    def _est_exemple(item: dict) -> bool:
        obs = (item.get("observations") or "").lower()
        return "exemple" in obs or "example" in obs

    if all_items and all(_est_exemple(i) for i in all_items):
        raise NeedsSheetParseError(
            "Vous avez téléversé le gabarit vierge AGRICAP sans le modifier. "
            "Remplacez les lignes d'exemple de la feuille « 4_Besoins_Financiers » "
            "par vos besoins réels, puis re-téléversez le fichier."
        )

    # ── Cohérence vs référentiel filière ─────────────────────────────────────
    anomalies, anomalies_staff = _controler_coherence_referentiel(
        value_chain=value_chain, area_ha=area_ha, currency=currency,
        grand_total=grand_total, total_by_module=total_by_module,
    )

    # ── Vérification fournisseurs agréés ─────────────────────────────────────
    _check_suppliers(all_items, all_warnings)

    return {
        "ok": True,
        "items": all_items,
        "totalByModule": total_by_module,
        "grandTotal": grand_total,
        "warnings": all_warnings,
        # Servi au CLIENT (`POST /needs-sheet/parse/`, puis persisté dans
        # `NeedsSheet.anomalies` que `serialize_application` republie) : aucun
        # chiffre du référentiel n'y figure.
        "anomalies": anomalies,
        # Réservé au STAFF : les mêmes constats, avec la référence et l'écart.
        # Non publié aujourd'hui — sa persistance demande une ligne dans
        # `views._persist_needs_sheet`, fichier tenu par un autre périmètre
        # (signalé au rapport). L'analyste dispose déjà de ces écarts, chiffrés
        # et lignés, dans l'onglet Analyse (`AnalyseCredit.criteres.technique`).
        "anomaliesStaff": anomalies_staff,
    }


def _controler_coherence_referentiel(
    *, value_chain, area_ha, currency: str, grand_total, total_by_module: dict,
) -> tuple[list[str], list[dict]]:
    """Confronte le plan au référentiel — et rend DEUX restitutions.

    FUITE CORRIGÉE (principe 7). Ce contrôle renvoyait au client, dans le même
    payload que son upload : le coût de référence par hectare de sa filière
    (« coût standard Maïs à 3 ha : 3 600 USD »), le ratio exact de son plan à ce
    coût, et le poids modules standard de la filière (« standard : 25 % »). Le
    client apprenait donc, en trois uploads, la grille complète contre laquelle
    il est scoré — et pouvait ajuster son dossier jusqu'à la borne, ce qui est
    exactement le gaming que l'asymétrie d'information doit rendre impossible.
    Ces mêmes chiffres viennent d'être fermés sur `reference-data/value-chains/`
    et au diff de révision : les laisser sortir ici les aurait rouverts par la
    porte de service.

    Ce que le client garde : le FAIT sur SES propres données (son total, son
    pourcentage par module — qu'il peut recalculer lui-même), le sens de
    l'écart, et l'action attendue. Ce qu'il perd : la valeur de référence, le
    ratio et le seuil. Le staff, lui, reçoit tout, en entrées structurées.
    """
    anomalies: list[str] = []
    staff: list[dict] = []
    if value_chain is None or not area_ha or area_ha <= 0 or grand_total <= 0:
        return anomalies, staff

    seuils = _seuils_coherence()
    ref_cost = float(value_chain.cost_per_hectare_usd if currency == "USD"
                     else value_chain.cost_per_hectare_cdf) * float(area_ha)
    ratio = grand_total / ref_cost if ref_cost > 0 else 0

    if ratio > seuils["ratio_max"]:
        anomalies.append(
            f"Vos besoins ({grand_total:,.0f} {currency}) sont nettement "
            f"au-dessus de ce qui est habituellement nécessaire pour un projet "
            f"{value_chain.label} de cette dimension. Un justificatif sera "
            f"demandé à l'analyste."
        )
        staff.append({
            "code": "TOTAL_AU_DESSUS_REFERENCE",
            "totalPlan": round(float(grand_total), 2),
            "totalReference": round(ref_cost, 2),
            "ratio": round(ratio, 3),
            "seuil": seuils["ratio_max"],
            "devise": currency,
        })
    elif ratio < seuils["ratio_min"]:
        anomalies.append(
            f"Vos besoins ({grand_total:,.0f} {currency}) sont nettement "
            f"en dessous de ce qui est habituellement nécessaire pour un projet "
            f"{value_chain.label} de cette dimension. Vérifiez l'exhaustivité "
            f"du dossier : un poste oublié se paie pendant la campagne."
        )
        staff.append({
            "code": "TOTAL_SOUS_REFERENCE",
            "totalPlan": round(float(grand_total), 2),
            "totalReference": round(ref_cost, 2),
            "ratio": round(ratio, 3),
            "seuil": seuils["ratio_min"],
            "devise": currency,
        })

    # Structure des coûts : chaque filière a sa signature (le poids modules du
    # référentiel). Le client apprend que SON module pèse trop lourd ; il
    # n'apprend pas combien il « devrait » peser.
    for module, poids_attendu in (value_chain.module_weights or {}).items():
        actual = total_by_module.get(module, 0)
        actual_pct = actual / grand_total * 100
        if poids_attendu <= seuils["poids_plancher_pct"]:
            # Un module marginal du référentiel : un écart relatif y est du
            # bruit, pas un signal.
            continue
        if actual_pct > poids_attendu * seuils["poids_facteur_max"]:
            anomalies.append(
                f"Le module «{module}» représente {actual_pct:.0f} % de votre "
                f"budget — nettement plus que dans la structure de coûts "
                f"habituelle de cette filière. Vérifiez cette ligne."
            )
            staff.append({
                "code": "POIDS_MODULE_ATYPIQUE",
                "module": module,
                "poidsObservePct": round(actual_pct, 1),
                "poidsReferencePct": round(float(poids_attendu), 1),
                "facteur": round(actual_pct / float(poids_attendu), 2),
                "seuilFacteur": seuils["poids_facteur_max"],
            })
    return anomalies, staff


#: Seuils de secours du contrôle de cohérence (principe 8 : « exception, les
#: valeurs par défaut de secours, avec warning loggé quand elles s'appliquent »).
#: Ils reprennent à l'identique les valeurs qui étaient codées en dur ici —
#: 1,30 / 0,70 / 1,8 / 5 % — pour ne déplacer aucune frontière en même temps
#: qu'on rend le paramètre éditable.
SEUILS_COHERENCE_DEFAUT = {
    "ratio_max": 1.30,
    "ratio_min": 0.70,
    "poids_facteur_max": 1.8,
    "poids_plancher_pct": 5.0,
}

#: Règle paramétrable qui les porte en base (`AnalysisRule.thresholds`),
#: modifiable par le comité sans redéploiement.
REGLE_COHERENCE = "needs_vs_referentiel"


def _seuils_coherence() -> dict:
    """Seuils du contrôle, lus en base — défauts de secours signalés.

    Ces quatre nombres décidaient jusqu'ici, depuis le code, de ce qui est « un
    dossier trop cher » et « un module atypique » : des seuils métier en dur,
    que le comité ne pouvait pas déplacer sans déploiement (principe 8).
    """
    import logging

    from credits.models import AnalysisRule

    regle = AnalysisRule.objects.filter(rule_id=REGLE_COHERENCE, active=True).first()
    seuils = dict(SEUILS_COHERENCE_DEFAUT)
    if regle is None:
        logging.getLogger(__name__).warning(
            "Règle « %s » absente ou inactive : contrôle de cohérence appliqué "
            "avec les seuils de secours %s. À poser en base pour que le comité "
            "puisse les régler sans redéploiement.", REGLE_COHERENCE, seuils,
        )
        return seuils
    for cle, defaut in SEUILS_COHERENCE_DEFAUT.items():
        brut = (regle.thresholds or {}).get(cle)
        if brut is None:
            continue
        try:
            seuils[cle] = float(decimal.Decimal(str(brut)))
        except (decimal.InvalidOperation, TypeError, ValueError):
            logging.getLogger(__name__).warning(
                "Seuil « %s » illisible dans la règle « %s » (« %s ») : repli "
                "sur %s.", cle, REGLE_COHERENCE, brut, defaut,
            )
    return seuils


def _check_suppliers(items: list[dict], warnings: list[str]) -> None:
    """Vérifie les fournisseurs suggérés contre l'annuaire agréé."""
    try:
        from suppliers.models import Supplier
        known_suppliers = set(
            s.name.lower() for s in Supplier.objects.filter(blacklisted=False)
        )
        blacklisted = set(
            s.name.lower() for s in Supplier.objects.filter(blacklisted=True)
        )
    except Exception:
        return  # suppliers app non disponible

    for item in items:
        supplier = item.get("suggested_supplier", "").strip()
        if not supplier:
            continue
        sl = supplier.lower()
        if sl in blacklisted:
            item["supplier_warning"] = f"Fournisseur «{supplier}» sur liste noire."
            warnings.append(
                f"Fournisseur «{supplier}» (ligne «{item['label'][:50]}») : "
                f"sur liste noire — ce fournisseur ne peut pas être utilisé."
            )
        elif known_suppliers and sl not in known_suppliers:
            item["supplier_warning"] = f"Fournisseur «{supplier}» non agréé."
            warnings.append(
                f"Fournisseur «{supplier}» (ligne «{item['label'][:50]}») : "
                f"non trouvé dans l'annuaire des fournisseurs agréés."
            )
