"""Plan d'évolution de catégorie réseau (Agencies.jsx « Plan d'évolution ») — checklist de
prérequis à cocher avant que le type réel de l'agence ne change, remplaçant le changement
instantané `services.evolve_type` (conservé tel quel pour compatibilité ascendante, déjà
câblé sur Agencies.jsx — voir la note dans `models.py`)."""
from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from audit.services import record as audit_record
from common.exceptions import ConflictError, ValidationFailed

from .models import Agency, EvolutionPlan, EvolutionPlanItem

DEFAULT_CHECKLIST = [
    "Conformité réglementaire de la nouvelle catégorie vérifiée",
    "Capacité de trésorerie adaptée validée",
    "Personnel suffisant en poste",
    "Infrastructure et sécurité adaptées à la nouvelle catégorie",
]


@transaction.atomic
def start_evolution_plan(*, agency: Agency, to_type: str, reason: str = "", by: str = "") -> EvolutionPlan:
    valid_types = {choice for choice, _ in Agency.Type.choices}
    if to_type not in valid_types:
        raise ValidationFailed(f"Type d'agence invalide : {to_type}.")
    if to_type == agency.type:
        raise ValidationFailed("L'agence est déjà de ce type.")
    if EvolutionPlan.objects.filter(agency=agency, status=EvolutionPlan.Status.IN_PROGRESS).exists():
        raise ConflictError("Un plan d'évolution est déjà en cours pour cette agence.")

    plan = EvolutionPlan.objects.create(agency=agency, from_type=agency.type, to_type=to_type,
                                         reason=reason, created_by=by)
    EvolutionPlanItem.objects.bulk_create([
        EvolutionPlanItem(plan=plan, label=label, order=i) for i, label in enumerate(DEFAULT_CHECKLIST)
    ])
    audit_record(actor=by, action="agency.evolution_plan.start", entity_type="EvolutionPlan",
                 entity_id=str(plan.pk), details={"from": plan.from_type, "to": plan.to_type, "reason": reason})
    return plan


@transaction.atomic
def check_evolution_item(*, item: EvolutionPlanItem, by: str = "") -> EvolutionPlanItem:
    if item.plan.status != EvolutionPlan.Status.IN_PROGRESS:
        raise ConflictError("Ce plan d'évolution n'est plus actif.")
    if item.is_done:
        return item  # déjà coché — idempotent, pas une erreur
    item.is_done = True
    item.done_by = by
    item.done_at = timezone.now()
    item.save(update_fields=["is_done", "done_by", "done_at"])
    audit_record(actor=by, action="agency.evolution_plan.check_item", entity_type="EvolutionPlanItem",
                 entity_id=str(item.pk), details={"label": item.label, "planId": item.plan_id})
    return item


@transaction.atomic
def complete_evolution_plan(*, plan: EvolutionPlan, by: str = "") -> Agency:
    if plan.status != EvolutionPlan.Status.IN_PROGRESS:
        raise ConflictError("Ce plan d'évolution n'est plus actif.")
    pending = plan.items.filter(is_done=False).count()
    if pending:
        raise ConflictError(f"{pending} étape(s) du plan restent à cocher avant de finaliser l'évolution.")

    agency = plan.agency
    previous_type = agency.type
    agency.type = plan.to_type
    agency.save(update_fields=["type", "updated_at"])
    plan.status = EvolutionPlan.Status.COMPLETED
    plan.completed_at = timezone.now()
    plan.save(update_fields=["status", "completed_at"])
    audit_record(actor=by, action="agency.evolve_type", entity_type="Agency", entity_id=agency.code,
                 details={"from": previous_type, "to": plan.to_type, "reason": plan.reason, "planId": plan.pk})
    return agency


@transaction.atomic
def cancel_evolution_plan(*, plan: EvolutionPlan, reason: str = "", by: str = "") -> EvolutionPlan:
    if plan.status != EvolutionPlan.Status.IN_PROGRESS:
        raise ConflictError("Ce plan d'évolution n'est plus actif.")
    plan.status = EvolutionPlan.Status.CANCELLED
    plan.save(update_fields=["status"])
    audit_record(actor=by, action="agency.evolution_plan.cancel", entity_type="EvolutionPlan",
                 entity_id=str(plan.pk), details={"reason": reason})
    return plan
