"""Moteur SLA des tickets (Support.jsx) — délais par priorité, horloge de résolution
suspendue tant que `waiting_on='client'`, escalade automatique sur dépassement de la
première réponse. Calculé À LA DEMANDE (pas de planificateur dans ce projet), même
principe que `transactions.services.overdue_pending_count`/`alerts.evaluate_and_sync_alerts`."""
from __future__ import annotations

from datetime import timedelta

from django.utils import timezone

from .models import Ticket

# Délais en heures : (première réponse, résolution) — §6.2 annexe Support & Client.
SLA_MATRIX = {
    Ticket.Priority.CRITIQUE: (1, 8),
    Ticket.Priority.URGENT: (4, 24),
    Ticket.Priority.NORMAL: (24, 72),
    Ticket.Priority.FAIBLE: (48, 168),
}

_OPEN_STATUSES = (Ticket.Status.OUVERT, Ticket.Status.EN_TRAITEMENT, Ticket.Status.ESCALADE)
_LEVEL_ORDER = [Ticket.Level.L1, Ticket.Level.L2, Ticket.Level.L3]


def compute_sla_deadlines(*, priority: str, created_at) -> tuple:
    first_response_hours, resolution_hours = SLA_MATRIX.get(priority, SLA_MATRIX[Ticket.Priority.NORMAL])
    return (
        created_at + timedelta(hours=first_response_hours),
        created_at + timedelta(hours=resolution_hours),
    )


def set_waiting_on(*, ticket: Ticket, value: str) -> Ticket:
    """Bascule l'horloge SLA. Passer à 'client' fige `sla_resolution_due` (pause) ; revenir
    à 'agent' décale l'échéance du temps réellement passé en pause, pas seulement l'annule."""
    if value == ticket.waiting_on:
        return ticket
    now = timezone.now()
    if value == Ticket.WaitingOn.CLIENT:
        ticket.sla_paused_at = now
    elif ticket.sla_paused_at and ticket.sla_resolution_due:
        paused_duration = now - ticket.sla_paused_at
        ticket.sla_resolution_due = ticket.sla_resolution_due + paused_duration
        ticket.sla_paused_at = None
    ticket.waiting_on = value
    ticket.save(update_fields=["waiting_on", "sla_paused_at", "sla_resolution_due"])
    return ticket


def check_sla_breaches() -> list[Ticket]:
    """Parcourt les tickets ouverts : marque les dépassements, escalade automatiquement au
    niveau supérieur sur dépassement de la première réponse (jamais au-delà de L3).
    Retourne les tickets modifiés (pour créer les alertes correspondantes)."""
    now = timezone.now()
    touched = []
    for ticket in Ticket.objects.filter(status__in=_OPEN_STATUSES):
        changed = False
        if (not ticket.first_response_at and ticket.sla_first_response_due
                and now > ticket.sla_first_response_due and not ticket.sla_breached_first_response):
            ticket.sla_breached_first_response = True
            current_index = _LEVEL_ORDER.index(ticket.level)
            if current_index < len(_LEVEL_ORDER) - 1:
                ticket.level = _LEVEL_ORDER[current_index + 1]
                ticket.status = Ticket.Status.ESCALADE
            changed = True
        if (ticket.waiting_on == Ticket.WaitingOn.AGENT and ticket.sla_resolution_due
                and now > ticket.sla_resolution_due and not ticket.sla_breached_resolution):
            ticket.sla_breached_resolution = True
            changed = True
        if changed:
            ticket.save()
            touched.append(ticket)
    return touched
