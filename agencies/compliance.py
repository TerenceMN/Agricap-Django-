"""Score de conformité PAR AGENCE (Agencies.jsx `complianceScore`) — distinct du score
réseau global (`analytics.compute_compliance_score`, moyenne sur tout le réseau) : agrège
des signaux propres à UNE agence (ponctualité des rapprochements, transactions en attente en
retard, trésorerie saine, historique d'incidents de suspension/fermeture). Calculé à la
demande (pas de planificateur dans ce projet — même principe que `analytics.services`/
`support.sla`), persiste `Agency.compliance_score` (champ existant, jusqu'ici modifiable
seulement à la main via PATCH, jamais calculé automatiquement) + un instantané d'historique."""
from __future__ import annotations

from datetime import timedelta

from django.utils import timezone

from .models import Agency, AgencyComplianceSnapshot, AgencyReactivation, AgencyReconciliation

# Composantes calculables avec les données réellement présentes et rattachées à une agence
# (`TreasuryAccount.agency`, `Transaction.agency`, `AgencyReconciliation.agency`,
# `AgencyReactivation.agency`) — pas de "KYC agence" (les clients ne sont pas rattachés à
# une agence dans le modèle actuel, cf. `analytics.services` qui calcule ce composant au
# niveau réseau, pas par agence).
COMPONENT_WEIGHTS = {
    "RAPPRO_PONCTUALITE": 0.35,
    "TRANSACTIONS_RETARD": 0.30,
    "TRESORERIE_SAINE": 0.20,
    "HISTORIQUE_INCIDENTS": 0.15,
}

_RECONCILIATION_WINDOW_DAYS = 30
_RECONCILIATION_PUNCTUAL_HOURS = 24
_INCIDENT_WINDOW_DAYS = 90
_SNAPSHOT_MIN_INTERVAL_HOURS = 1


def _rapprochement_score(agency: Agency) -> float | None:
    since = timezone.now() - timedelta(days=_RECONCILIATION_WINDOW_DAYS)
    completed = AgencyReconciliation.objects.filter(
        agency=agency, status=AgencyReconciliation.Status.COMPLETED, closed_at__gte=since,
    )
    total = completed.count()
    if not total:
        return None
    punctual = sum(
        1 for r in completed
        if r.closed_at and (r.closed_at - r.opened_at) <= timedelta(hours=_RECONCILIATION_PUNCTUAL_HOURS)
    )
    return round(punctual / total * 100, 1)


def _transactions_retard_score(agency: Agency) -> float | None:
    from common.choices import FlowStatus
    from transactions.models import Transaction
    from transactions.services import _threshold_for

    pending = Transaction.objects.filter(agency=agency, status=FlowStatus.PENDING_VALIDATION)
    total = pending.count()
    if not total:
        return None
    now = timezone.now()
    overdue = sum(
        1 for tx in pending
        if now - tx.created_at > timedelta(hours=_threshold_for(tx.operation_type).manual_timeout_hours)
    )
    return round((1 - overdue / total) * 100, 1)


def _tresorerie_score(agency: Agency) -> float | None:
    from caisses.models import TreasuryAccount

    accounts = TreasuryAccount.objects.filter(agency=agency)
    total = accounts.count()
    if not total:
        return None
    healthy = accounts.exclude(status=TreasuryAccount.Status.BLOQUE).count()
    return round(healthy / total * 100, 1)


def _historique_incidents_score(agency: Agency) -> float | None:
    """Chaque réactivation passée (déverrouillage/réouverture) implique qu'une suspension ou
    fermeture a eu lieu — toujours calculable (0 réactivation = 100%), contrairement aux
    autres composantes qui dépendent de données pouvant être absentes."""
    since = timezone.now() - timedelta(days=_INCIDENT_WINDOW_DAYS)
    incidents = AgencyReactivation.objects.filter(agency=agency, created_at__gte=since).count()
    return max(0.0, 100.0 - 20.0 * incidents)


_COMPONENT_FUNCS = {
    "RAPPRO_PONCTUALITE": ("Rapprochements clôturés < 24h", _rapprochement_score),
    "TRANSACTIONS_RETARD": ("Transactions en attente non en retard", _transactions_retard_score),
    "TRESORERIE_SAINE": ("Comptes de trésorerie non bloqués", _tresorerie_score),
    "HISTORIQUE_INCIDENTS": ("Absence d'incidents (90j)", _historique_incidents_score),
}


def compute_agency_compliance_score(*, agency: Agency, persist: bool = True) -> dict:
    components = []
    for code, (label, fn) in _COMPONENT_FUNCS.items():
        components.append({"code": code, "label": label, "weight": COMPONENT_WEIGHTS[code], "score": fn(agency)})

    available = [c for c in components if c["score"] is not None]
    if not available:
        score = None
    else:
        weight_sum = sum(c["weight"] for c in available)
        score = round(sum(c["score"] * c["weight"] for c in available) / weight_sum, 1)

    if persist and score is not None:
        recent = AgencyComplianceSnapshot.objects.filter(
            agency=agency, computed_at__gte=timezone.now() - timedelta(hours=_SNAPSHOT_MIN_INTERVAL_HOURS),
        ).exists()
        if not recent:
            AgencyComplianceSnapshot.objects.create(agency=agency, score=score, components=components)
        Agency.objects.filter(pk=agency.pk).update(compliance_score=score)
        agency.compliance_score = score

    return {"score": score, "components": components}
