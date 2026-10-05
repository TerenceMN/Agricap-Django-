"""Cycle de vie des agences. `close()`/`reconciliation_report()` importent `caisses`/
`ledger` en différé (à l'intérieur des fonctions) — même pattern que `portfolio/views.py`
import de `credit.models` — pour ne pas créer de dépendance de chargement circulaire au
niveau module (`caisses`/`ledger` référencent `agencies.Agency` en FK, donc `agencies` ne
peut pas les importer au niveau module)."""
from __future__ import annotations

from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from audit.services import record as audit_record
from common.exceptions import ConflictError, ValidationFailed
from common.parsing import to_decimal

from .models import Agency, AgencyAlert, AgencyReactivation, AgencyReconciliation


@transaction.atomic
def create_agency(*, code: str, name: str, type_: str = Agency.Type.URBAINE, city: str = "",
                   province: str = "", manager_sub: str = "", by: str = "") -> Agency:
    if not code or not name:
        raise ValidationFailed("Code et nom de l'agence requis.")
    if Agency.objects.filter(code=code).exists():
        raise ValidationFailed(f"Le code agence « {code} » existe déjà.")
    agency = Agency.objects.create(
        code=code, name=name, type=type_, city=city, province=province,
        manager_sub=manager_sub, created_by=by,
    )
    audit_record(actor=by, action="agency.create", entity_type="Agency", entity_id=agency.code,
                 details={"name": name})
    return agency


@transaction.atomic
def suspend(*, agency: Agency, reason: str, by: str = "") -> Agency:
    agency.status = Agency.Status.SUSPENDU
    agency.suspended_reason = reason
    agency.save(update_fields=["status", "suspended_reason", "updated_at"])
    audit_record(actor=by, action="agency.suspend", entity_type="Agency", entity_id=agency.code,
                 details={"reason": reason})
    return agency


@transaction.atomic
def unlock_temporary(*, agency: Agency, reason: str, document, by: str = "") -> Agency:
    """Réactive une agence suspendue — exige une justification écrite ET une pièce à
    l'appui (contrairement à `suspend`/`close` qui n'exigent qu'un motif) : l'institution a
    délibérément suspendu l'agence, y revenir doit être prouvé, pas juste déclaré."""
    _require_reactivation_proof(reason, document)
    agency.status = Agency.Status.ACTIF
    agency.suspended_reason = ""
    agency.save(update_fields=["status", "suspended_reason", "updated_at"])
    reactivation = AgencyReactivation.objects.create(
        agency=agency, kind=AgencyReactivation.Kind.UNLOCK, reason=reason, document=document, created_by=by,
    )
    audit_record(actor=by, action="agency.unlock_temporary", entity_type="Agency", entity_id=agency.code,
                 details={"reason": reason, "document": reactivation.document.name})
    return agency


@transaction.atomic
def reopen(*, agency: Agency, reason: str, document, by: str = "") -> Agency:
    """Réouvre une agence fermée — même exigence de preuve que `unlock_temporary`."""
    if agency.status != Agency.Status.FERMEE:
        raise ValidationFailed("Seule une agence fermée peut être réouverte.")
    _require_reactivation_proof(reason, document)
    agency.status = Agency.Status.ACTIF
    agency.closed_reason = ""
    agency.save(update_fields=["status", "closed_reason", "updated_at"])
    reactivation = AgencyReactivation.objects.create(
        agency=agency, kind=AgencyReactivation.Kind.REOPEN, reason=reason, document=document, created_by=by,
    )
    audit_record(actor=by, action="agency.reopen", entity_type="Agency", entity_id=agency.code,
                 details={"reason": reason, "document": reactivation.document.name})
    return agency


def _require_reactivation_proof(reason: str, document) -> None:
    if not reason or not reason.strip():
        raise ValidationFailed("Une justification écrite est requise pour réactiver une agence.")
    if not document:
        raise ValidationFailed("Un document justificatif est requis pour réactiver une agence.")


@transaction.atomic
def evolve_type(*, agency: Agency, new_type: str, reason: str = "", by: str = "") -> Agency:
    """Fait évoluer une agence dans le réseau (ex. Point de service -> Rurale -> Urbaine ->
    Siège, ou l'inverse) — une décision de supervision distincte d'un simple PATCH de
    fiche, donc auditée séparément avec la valeur d'origine et le motif."""
    valid_types = {choice for choice, _ in Agency.Type.choices}
    if new_type not in valid_types:
        raise ValidationFailed(f"Type d'agence invalide : {new_type}.")
    if new_type == agency.type:
        raise ValidationFailed("L'agence est déjà de ce type.")
    previous_type = agency.type
    agency.type = new_type
    agency.save(update_fields=["type", "updated_at"])
    audit_record(actor=by, action="agency.evolve_type", entity_type="Agency", entity_id=agency.code,
                 details={"from": previous_type, "to": new_type, "reason": reason})
    return agency


@transaction.atomic
def close(*, agency: Agency, reason: str, by: str = "") -> Agency:
    from caisses.models import TreasuryAccount
    non_zero = TreasuryAccount.objects.filter(agency=agency).exclude(balance=Decimal("0")).exists()
    if non_zero:
        raise ConflictError(
            "Impossible de fermer l'agence : des comptes de trésorerie ont un solde non nul."
        )
    agency.status = Agency.Status.FERMEE
    agency.closed_reason = reason
    agency.save(update_fields=["status", "closed_reason", "updated_at"])
    audit_record(actor=by, action="agency.close", entity_type="Agency", entity_id=agency.code,
                 details={"reason": reason})
    return agency


def reconciliation_report(*, agency: Agency) -> dict:
    from ledger.services import trial_balance
    return trial_balance(scope=agency)


def add_alert(*, agency: Agency, level: str, message: str) -> AgencyAlert:
    return AgencyAlert.objects.create(agency=agency, level=level, message=message)


@transaction.atomic
def open_reconciliation(*, agency: Agency, period_start, period_end, is_final_closure: bool = False,
                         by: str = "") -> AgencyReconciliation:
    if period_end < period_start:
        raise ValidationFailed("La date de fin doit être postérieure à la date de début.")
    if agency.reconciliations.filter(
        status__in=(AgencyReconciliation.Status.PENDING, AgencyReconciliation.Status.IN_PROGRESS),
    ).exists():
        raise ConflictError("Un rapprochement est déjà ouvert pour cette agence.")
    recon = AgencyReconciliation.objects.create(
        agency=agency, period_start=period_start, period_end=period_end, is_final_closure=is_final_closure,
    )
    audit_record(actor=by, action="agency.reconciliation.open", entity_type="AgencyReconciliation",
                 entity_id=str(recon.pk), details={"agency": agency.code, "periodStart": str(period_start),
                                                    "periodEnd": str(period_end)})
    return recon


@transaction.atomic
def assign_reconciliation(*, reconciliation: AgencyReconciliation, assignee_sub: str, by: str = "") \
        -> AgencyReconciliation:
    if reconciliation.status == AgencyReconciliation.Status.COMPLETED:
        raise ConflictError("Ce rapprochement est déjà terminé.")
    reconciliation.assigned_to = assignee_sub
    reconciliation.status = AgencyReconciliation.Status.IN_PROGRESS
    reconciliation.save(update_fields=["assigned_to", "status"])
    audit_record(actor=by, action="agency.reconciliation.assign", entity_type="AgencyReconciliation",
                 entity_id=str(reconciliation.pk), details={"assignedTo": assignee_sub})
    return reconciliation


@transaction.atomic
def complete_reconciliation(*, reconciliation: AgencyReconciliation, delta_amount, currency: str,
                             notes: str = "", by: str = "") -> AgencyReconciliation:
    if reconciliation.status == AgencyReconciliation.Status.COMPLETED:
        raise ConflictError("Ce rapprochement est déjà terminé.")
    reconciliation.status = AgencyReconciliation.Status.COMPLETED
    reconciliation.delta_amount = to_decimal(delta_amount)
    reconciliation.currency = currency
    reconciliation.notes = notes
    reconciliation.closed_at = timezone.now()
    reconciliation.save(update_fields=["status", "delta_amount", "currency", "notes", "closed_at"])
    audit_record(actor=by, action="agency.reconciliation.complete", entity_type="AgencyReconciliation",
                 entity_id=str(reconciliation.pk),
                 details={"deltaAmount": str(reconciliation.delta_amount), "currency": currency})
    return reconciliation
