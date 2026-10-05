"""Évaluation des règles d'alertes — appelée à la demande (`GET /api/alerts`, pas de
planificateur en tâche de fond dans ce projet) : calcule chaque métrique en direct,
matérialise une `Alert` ACTIVE si la condition est remplie et qu'aucune alerte ACTIVE
n'existe déjà pour cette (règle, source) — sinon ne fait rien (pas de doublon, cf.
contrainte DB `unique_active_alert_dedup_key` en filet de sécurité)."""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.utils import timezone

from audit.services import record as audit_record
from common.exceptions import ConflictError, ValidationFailed

from .models import Alert, AlertRule

ESCALATION_MINUTES = 60  # une alerte WARNING non acquittée depuis ce délai devient CRITICAL

_SECONDS_PER_HOUR = Decimal(3600)


def as_threshold(value) -> Decimal:
    """Coerce une valeur entrante (JSON, formulaire, code) en seuil `Decimal`.

    Le JSON n'a pas de type décimal : `{"threshold": 79.9}` arrive en `float`.
    On repasse par `str()` pour retrouver le nombre que l'administrateur a
    réellement saisi (79,9) plutôt que sa traduction binaire — principe 4.
    """
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value).strip().replace(",", "."))
    except (InvalidOperation, AttributeError, TypeError):
        raise ValidationFailed("Seuil invalide : un nombre est attendu.")


def _as_metric(value) -> Decimal:
    """Métrique mesurée → `Decimal`, pour la comparer au seuil sans flottant."""
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _hours_since(moment) -> Decimal:
    """Ancienneté en heures, exacte : elle se compare à un seuil `Decimal`."""
    return Decimal(str((timezone.now() - moment).total_seconds())) / _SECONDS_PER_HOUR


def _compare(value, operator: str, threshold: Decimal) -> bool:
    value = _as_metric(value)
    if operator == AlertRule.Operator.GT:
        return value > threshold
    if operator == AlertRule.Operator.GTE:
        return value >= threshold
    if operator == AlertRule.Operator.LT:
        return value < threshold
    if operator == AlertRule.Operator.LTE:
        return value <= threshold
    return False


def _raise_alert(*, rule: AlertRule, source_type: str, source_id, title: str, body: str) -> None:
    dedup_key = f"{rule.code}:{source_id}"
    if Alert.objects.filter(dedup_key=dedup_key, status=Alert.Status.ACTIVE).exists():
        return
    Alert.objects.create(rule=rule, severity=rule.severity, title=title, body=body,
                          source_type=source_type, source_id=str(source_id), dedup_key=dedup_key)
    if rule.notify_phone:
        try:
            from common.sms import send_sms
            send_sms(phone=rule.notify_phone, message=f"AGRICAP Alerte [{rule.severity}] : {title}")
        except Exception:  # noqa: BLE001 — notification best-effort, ne doit jamais bloquer l'alerte.
            pass


def _evaluate_agency_suspended(rule: AlertRule) -> None:
    from agencies.models import Agency
    from audit.models import AuditEntry
    for agency in Agency.objects.filter(status=Agency.Status.SUSPENDU):
        entry = AuditEntry.objects.filter(
            entity_type="Agency", entity_id=agency.code, action="agency.suspend",
        ).order_by("-created_at").first()
        since = entry.created_at if entry else agency.updated_at
        hours = _hours_since(since)
        if _compare(hours, rule.operator, rule.threshold):
            _raise_alert(rule=rule, source_type="Agency", source_id=agency.code,
                         title=f"{agency.code} suspendue depuis {int(hours)}h",
                         body=f"Agence {agency.name} suspendue ({agency.suspended_reason or 'motif non renseigné'}).")


def _evaluate_reconciliation_overdue(rule: AlertRule) -> None:
    from agencies.models import AgencyReconciliation
    pending = AgencyReconciliation.objects.filter(
        status__in=(AgencyReconciliation.Status.PENDING, AgencyReconciliation.Status.IN_PROGRESS),
    ).select_related("agency")
    for recon in pending:
        hours = _hours_since(recon.opened_at)
        if _compare(hours, rule.operator, rule.threshold):
            _raise_alert(rule=rule, source_type="AgencyReconciliation", source_id=recon.pk,
                         title=f"Rapprochement {recon.agency.code} ouvert depuis {int(hours)}h",
                         body=f"Période {recon.period_start}..{recon.period_end}, statut {recon.status}.")


def _evaluate_compliance_score_low(rule: AlertRule) -> None:
    from analytics.services import compute_compliance_score
    result = compute_compliance_score(persist=False)
    if result["score"] is None:
        return
    if _compare(result["score"], rule.operator, rule.threshold):
        _raise_alert(rule=rule, source_type="ComplianceScore", source_id="global",
                     title=f"Score de conformité à {result['score']}%",
                     body="Score de conformité global sous le seuil configuré.")


def _evaluate_transaction_overdue(rule: AlertRule) -> None:
    from transactions.services import overdue_pending_count
    count = overdue_pending_count()
    if _compare(count, rule.operator, rule.threshold):
        _raise_alert(rule=rule, source_type="Transaction", source_id="global",
                     title=f"{count} transaction(s) en attente en retard",
                     body="Transactions en attente de validation depuis plus longtemps que leur délai configuré.")


def _evaluate_partner_failures(rule: AlertRule) -> None:
    from partners.models import Partner
    for partner in Partner.objects.filter(consecutive_failures__gt=0):
        if _compare(partner.consecutive_failures, rule.operator, rule.threshold):
            _raise_alert(rule=rule, source_type="Partner", source_id=partner.pk,
                         title=f"{partner.name} : {partner.consecutive_failures} échec(s) consécutif(s)",
                         body=f"Disjoncteur : {partner.circuit_state}.")


def _evaluate_ticket_sla_breached(rule: AlertRule) -> None:
    from django.db.models import Q
    from support.models import Ticket
    from support.sla import _OPEN_STATUSES, check_sla_breaches
    check_sla_breaches()  # recalcule les dépassements/escalades avant de compter
    count = Ticket.objects.filter(status__in=_OPEN_STATUSES).filter(
        Q(sla_breached_first_response=True) | Q(sla_breached_resolution=True),
    ).count()
    if _compare(count, rule.operator, rule.threshold):
        _raise_alert(rule=rule, source_type="Ticket", source_id="global",
                     title=f"{count} ticket(s) en dépassement de SLA",
                     body="Tickets ouverts ayant dépassé leur délai de première réponse ou de résolution.")


_EVALUATORS = {
    AlertRule.Metric.AGENCY_SUSPENDED: _evaluate_agency_suspended,
    AlertRule.Metric.RECONCILIATION_OVERDUE: _evaluate_reconciliation_overdue,
    AlertRule.Metric.COMPLIANCE_SCORE_LOW: _evaluate_compliance_score_low,
    AlertRule.Metric.TRANSACTION_OVERDUE: _evaluate_transaction_overdue,
    AlertRule.Metric.PARTNER_FAILURES: _evaluate_partner_failures,
    AlertRule.Metric.TICKET_SLA_BREACHED: _evaluate_ticket_sla_breached,
}


def evaluate_and_sync_alerts() -> None:
    for rule in AlertRule.objects.filter(enabled=True):
        _EVALUATORS[rule.metric](rule)
    _escalate_unacknowledged_warnings()


def _escalate_unacknowledged_warnings() -> None:
    """Une alerte WARNING active non acquittée depuis `ESCALATION_MINUTES` devient
    CRITICAL — évalué à chaque appel plutôt que par un job planifié (aucune infrastructure
    de tâches planifiées dans ce projet)."""
    cutoff = timezone.now() - timedelta(minutes=ESCALATION_MINUTES)
    Alert.objects.filter(
        status=Alert.Status.ACTIVE, severity=AlertRule.Severity.WARNING, triggered_at__lte=cutoff,
    ).update(severity=AlertRule.Severity.CRITICAL)


def acknowledge_alert(*, alert: Alert, by: str = "") -> Alert:
    if alert.status != Alert.Status.ACTIVE:
        raise ConflictError("Seule une alerte active peut être acquittée.")
    alert.status = Alert.Status.ACKNOWLEDGED
    alert.acknowledged_by = by
    alert.acknowledged_at = timezone.now()
    alert.save(update_fields=["status", "acknowledged_by", "acknowledged_at"])
    audit_record(actor=by, action="alert.acknowledge", entity_type="Alert", entity_id=str(alert.pk))
    return alert


def resolve_alert(*, alert: Alert, note: str = "", by: str = "") -> Alert:
    if alert.status == Alert.Status.RESOLVED:
        raise ConflictError("Cette alerte est déjà résolue.")
    alert.status = Alert.Status.RESOLVED
    alert.resolved_by = by
    alert.resolved_at = timezone.now()
    alert.resolution_note = note
    alert.save(update_fields=["status", "resolved_by", "resolved_at", "resolution_note"])
    audit_record(actor=by, action="alert.resolve", entity_type="Alert", entity_id=str(alert.pk), details={"note": note})
    return alert


def create_alert_rule(*, code: str, name: str, metric: str, operator: str, threshold, severity: str,
                       description: str = "", notify_phone: str = "", by: str = "") -> AlertRule:
    if not code or not name:
        raise ValidationFailed("Code et nom de la règle requis.")
    if AlertRule.objects.filter(code=code).exists():
        raise ValidationFailed(f"Une règle avec le code « {code} » existe déjà.")
    rule = AlertRule.objects.create(code=code, name=name, description=description, metric=metric, operator=operator,
                                     threshold=as_threshold(threshold), severity=severity,
                                     notify_phone=notify_phone, created_by=by)
    audit_record(actor=by, action="alert_rule.create", entity_type="AlertRule", entity_id=str(rule.pk),
                 details={"code": code, "metric": metric})
    return rule


def update_alert_rule(*, rule: AlertRule, by: str = "", **fields) -> AlertRule:
    changed = []
    for field, value in fields.items():
        if value is not None:
            # Le seuil arrive du JSON en `float` : on le rétablit en `Decimal`
            # exact avant de l'écrire (principe 4).
            setattr(rule, field, as_threshold(value) if field == "threshold" else value)
            changed.append(field)
    if changed:
        rule.save(update_fields=changed)
    audit_record(actor=by, action="alert_rule.update", entity_type="AlertRule", entity_id=str(rule.pk),
                 details={"fields": changed})
    return rule
