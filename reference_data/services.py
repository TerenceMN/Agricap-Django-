"""
Services métier du module reference_data.
- process_upload : parse + valide le fichier Excel, calcule le diff
- activate_file  : maker-checker, crée les ValueChain, invalide le cache
"""
from __future__ import annotations

import os
import tempfile
from decimal import Decimal, InvalidOperation

from django.core.cache import cache
from django.db import transaction
from django.utils import timezone

from .models import MODULE_WEIGHT_KEYS, ReferenceFileUpload, ValueChain
from .validators import validate_value_chains_sheet, ExcelStructureError

CACHE_KEY_ACTIVE = "reference_data:value_chains:active"

#: Champs dont la modification déplace un score, un montant ou une décision. Le
#: checker doit les voir : c'est exactement sur eux que le maker-checker cesse
#: d'être décoratif. `cost_per_hectare_*` et `module_weights` en font partie et
#: n'étaient pas comparés — un coût/ha pouvait passer de 1 200 à 4 800 USD sans
#: qu'aucune ligne n'apparaisse au diff soumis à l'activation.
SCORING_FIELDS = frozenset({
    "cycle_months", "cost_per_hectare_usd", "cost_per_hectare_cdf",
    "module_weights", "risk_factor", "min_score_required", "base_rate",
    "eligible_guarantees",
})


def _dec(value) -> Decimal | None:
    """`Decimal` d'une cellule, ou `None`. Jamais de `float` : comparer
    `float("0.1") + float("0.2")` à `0.3` est faux, et un diff faux vaut un diff
    absent (principe 4)."""
    if value is None:
        return None
    try:
        return Decimal(str(value).strip().replace(" ", "").replace(",", "."))
    except (InvalidOperation, ValueError):
        return None


def _fmt(value) -> str:
    """Affichage d'un nombre sans zéros parasites : `0.850` → `0.85`, `4800.00` → `4800`."""
    dec = _dec(value)
    if dec is None:
        return "—" if value in (None, "") else str(value)
    normalized = dec.normalize()
    if normalized == normalized.to_integral_value():
        normalized = normalized.quantize(Decimal(1))
    return f"{normalized:f}"


def _num_changed(before, after) -> bool:
    """Vrai si deux valeurs numériques diffèrent réellement (`0.850` ≡ `0.85`).

    Une valeur illisible d'un côté seulement compte comme un changement : mieux
    vaut faire relire au checker une ligne douteuse que la taire.
    """
    a, b = _dec(before), _dec(after)
    if a is None or b is None:
        return str(before) != str(after)
    return a != b


def _seq(value) -> list[str]:
    """Séquence normalisée d'une liste JSON — l'ordre de saisie n'est pas une donnée."""
    if not isinstance(value, (list, tuple)):
        return []
    return sorted({str(v).strip().lower() for v in value if str(v).strip()})


def _weights_changes(before: dict | None, after: dict | None) -> list[str]:
    """Écarts poste par poste entre deux répartitions de poids modules.

    Les poids gouvernent la ventilation du coût de référence par module, donc les
    écarts « hors plage » du critère de fiabilité technique. Un diff qui compare
    les deux dictionnaires en bloc (« poids modifiés ») ne dit pas au checker quel
    poste bouge ni de combien : on énumère.
    """
    before = before if isinstance(before, dict) else {}
    after = after if isinstance(after, dict) else {}
    # Ordre canonique d'abord (models.MODULE_WEIGHT_KEYS), puis tout poste
    # inattendu présent d'un côté ou de l'autre — jamais silencieusement ignoré.
    keys = list(MODULE_WEIGHT_KEYS)
    keys += sorted((set(before) | set(after)) - set(keys))

    changes: list[str] = []
    for key in keys:
        in_before, in_after = key in before, key in after
        if not in_before and not in_after:
            continue
        if in_before and not in_after:
            changes.append(f"poids {key} retiré (était {_fmt(before[key])} %)")
        elif in_after and not in_before:
            changes.append(f"poids {key} ajouté ({_fmt(after[key])} %)")
        elif _num_changed(before[key], after[key]):
            changes.append(f"poids {key} {_fmt(before[key])} → {_fmt(after[key])} %")
    return changes


def _compute_diff(new_rows: list[dict]) -> dict:
    """Compare les nouvelles lignes (par code) aux ValueChains actuellement actives.

    Le diff est la SEULE chose que le checker lit avant d'activer : tout champ
    non comparé ici est un champ que le maker peut changer sans contrôle. On
    couvre donc l'intégralité des champs métier que `activate_file` écrira —
    coûts/ha et poids modules compris (dette CONVENTIONS.md §6).
    """
    active = {
        vc.code: vc
        for vc in ValueChain.objects.filter(
            source_file__status=ReferenceFileUpload.Status.ACTIVE,
            active=True,
        )
    }
    new_codes = {r["code"] for r in new_rows}

    added = [r["code"] for r in new_rows if r["code"] not in active]
    removed = [code for code in active if code not in new_codes]
    modified: list[dict] = []

    for row in new_rows:
        code = row["code"]
        if code not in active:
            continue
        vc = active[code]
        changes: list[str] = []
        fields: list[dict] = []

        def note(field: str, label: str, before, after) -> None:
            changes.append(label)
            fields.append({
                "field": field,
                "before": before if isinstance(before, (dict, list)) else _fmt(before),
                "after": after if isinstance(after, (dict, list)) else _fmt(after),
                "scoring": field in SCORING_FIELDS,
            })

        if (vc.label or "").strip() != str(row.get("label") or "").strip():
            note("label", f"libellé « {vc.label} » → « {row.get('label')} »",
                 vc.label, row.get("label"))

        if _num_changed(vc.cycle_months, row["cycle_months"]):
            note("cycle_months",
                 f"cycle {_fmt(vc.cycle_months)} → {_fmt(row['cycle_months'])} mois",
                 vc.cycle_months, row["cycle_months"])

        # ── Coûts par hectare — jamais comparés jusqu'ici ────────────────────
        # C'est la valeur de référence à laquelle le plan du demandeur est
        # confronté module par module : la multiplier par 4 réécrit tous les
        # écarts du dossier sans qu'aucune ligne de diff ne l'annonce.
        for field, cle, devise in (
            ("cost_per_hectare_usd", "cost_per_hectare_usd", "USD"),
            ("cost_per_hectare_cdf", "cost_per_hectare_cdf", "CDF"),
        ):
            before, after = getattr(vc, field), row[cle]
            if _num_changed(before, after):
                note(field,
                     f"coût/ha {_fmt(before)} → {_fmt(after)} {devise}",
                     before, after)

        # ── Poids modules — jamais comparés jusqu'ici ────────────────────────
        weight_changes = _weights_changes(vc.module_weights, row.get("module_weights"))
        if weight_changes:
            changes.extend(weight_changes)
            fields.append({
                "field": "module_weights",
                "before": vc.module_weights,
                "after": row.get("module_weights"),
                "scoring": True,
            })

        if _num_changed(vc.risk_factor, row["risk_factor"]):
            note("risk_factor",
                 f"risk_factor {_fmt(vc.risk_factor)} → {_fmt(row['risk_factor'])}",
                 vc.risk_factor, row["risk_factor"])

        if _num_changed(vc.min_score_required, row["min_score_required"]):
            note("min_score_required",
                 f"score_min {_fmt(vc.min_score_required)} → {_fmt(row['min_score_required'])}",
                 vc.min_score_required, row["min_score_required"])

        if _num_changed(vc.base_rate, row["base_rate"]):
            note("base_rate",
                 f"taux {_fmt(vc.base_rate)} → {_fmt(row['base_rate'])} %",
                 vc.base_rate, row["base_rate"])

        before_h, after_h = _seq(vc.harvest_months), _seq(row.get("harvest_months"))
        if before_h != after_h:
            note("harvest_months",
                 f"mois de récolte {', '.join(before_h) or '—'} → "
                 f"{', '.join(after_h) or '—'}",
                 vc.harvest_months, row.get("harvest_months"))

        before_g, after_g = _seq(vc.eligible_guarantees), _seq(row.get("eligible_guarantees"))
        if before_g != after_g:
            note("eligible_guarantees",
                 f"garanties éligibles {', '.join(before_g) or '—'} → "
                 f"{', '.join(after_g) or '—'}",
                 vc.eligible_guarantees, row.get("eligible_guarantees"))

        if bool(vc.active) != bool(row.get("active", True)):
            note("active",
                 "filière activée" if row.get("active", True) else "filière désactivée",
                 vc.active, row.get("active", True))

        if changes:
            modified.append({
                "code": code,
                "label": row["label"],
                "changes": changes,
                "fields": fields,
                # Le checker doit pouvoir trier : une correction de libellé et une
                # division par deux du coût/ha n'engagent pas la même relecture.
                "impactsScoring": any(f["scoring"] for f in fields),
            })

    return {
        "added": added,
        "removed": removed,
        "modified": modified,
        "unchanged": len(new_rows) - len(added) - len(modified),
        "totalNew": len(new_rows),
        "scoringImpacted": [m["code"] for m in modified if m["impactsScoring"]],
    }


def process_upload(upload: ReferenceFileUpload) -> dict:
    """
    Valide le fichier Excel rattaché à `upload`.
    Met à jour upload.status, upload.validation_report, upload.diff_summary.
    Retourne le résultat de validation (dict).
    """
    if upload.file_type != ReferenceFileUpload.FileType.VALUE_CHAINS:
        upload.status = ReferenceFileUpload.Status.REJECTED
        upload.validation_report = {
            "valid": False,
            "errors": ["Type de fichier non supporté pour la validation automatique."],
        }
        upload.save(update_fields=["status", "validation_report"])
        return upload.validation_report

    # Copier sur disque pour openpyxl (évite les problèmes de curseur Django Storage)
    with upload.file.open("rb") as fh:
        with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as tmp:
            tmp.write(fh.read())
            tmp_path = tmp.name

    try:
        result = validate_value_chains_sheet(tmp_path)
    except ExcelStructureError as exc:
        upload.status = ReferenceFileUpload.Status.REJECTED
        report = {"valid": False, "structureError": str(exc), "errors": [str(exc)], "rows": []}
        upload.validation_report = report
        upload.save(update_fields=["status", "validation_report"])
        return report
    finally:
        os.unlink(tmp_path)

    if not result["valid"]:
        upload.status = ReferenceFileUpload.Status.REJECTED
        upload.validation_report = result
        upload.row_count = len(result.get("rows", []))
        upload.save(update_fields=["status", "validation_report", "row_count"])
        return result

    # Valide → calculer le diff avant activation
    diff = _compute_diff(result["rows"])
    upload.status = ReferenceFileUpload.Status.PENDING_VALIDATION
    upload.validation_report = result
    upload.diff_summary = diff
    upload.row_count = len(result["rows"])
    upload.save(update_fields=["status", "validation_report", "diff_summary", "row_count"])
    return {**result, "diff": diff}


def activate_file(upload: ReferenceFileUpload, activator_sub: str) -> None:
    """
    Maker-checker : archive l'actif courant, crée les nouvelles ValueChains.
    Lève ValueError si les préconditions ne sont pas remplies.
    """
    if upload.status != ReferenceFileUpload.Status.PENDING_VALIDATION:
        raise ValueError(
            f"Ce fichier ne peut pas être activé (statut actuel : {upload.status}). "
            "Seuls les fichiers en statut «pending_validation» peuvent être activés."
        )
    if upload.uploaded_by == activator_sub:
        raise ValueError(
            "Le même utilisateur ne peut pas téléverser ET activer un référentiel "
            "(principe maker-checker : l'activation doit être faite par un second administrateur)."
        )

    parsed_rows = upload.validation_report.get("rows", [])
    if not parsed_rows:
        raise ValueError(
            "Aucune donnée parsée disponible. Re-uploadez le fichier pour régénérer le rapport."
        )

    with transaction.atomic():
        # Archiver l'actif courant du même type
        ReferenceFileUpload.objects.filter(
            file_type=upload.file_type,
            status=ReferenceFileUpload.Status.ACTIVE,
        ).update(status=ReferenceFileUpload.Status.ARCHIVED)

        # Créer les nouvelles ValueChains
        ValueChain.objects.bulk_create([
            ValueChain(source_file=upload, **row)
            for row in parsed_rows
        ])

        # Activer
        upload.status = ReferenceFileUpload.Status.ACTIVE
        upload.activated_by = activator_sub
        upload.activated_at = timezone.now()
        upload.save(update_fields=["status", "activated_by", "activated_at"])

    # Invalider le cache dropdown
    cache.delete(CACHE_KEY_ACTIVE)
