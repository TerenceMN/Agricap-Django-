"""Tâches Celery pour le module Support : relances en-attente-client et clôture automatique."""
from __future__ import annotations

import logging

logger = logging.getLogger("agricap")

try:
    from celery import shared_task
    _CELERY = True
except ImportError:
    _CELERY = False
    def shared_task(*args, **kwargs):  # noqa: ANN001
        def decorator(fn):
            return fn
        return decorator if args and callable(args[0]) else decorator


@shared_task(name="support.remind_await_j2", bind=True, max_retries=3, ignore_result=True)
def remind_await_client_j2(self, ticket_id: int) -> None:
    from .models import Ticket
    from . import workflow
    ticket = Ticket.objects.filter(pk=ticket_id).select_related("user").first()
    if not ticket or ticket.status != Ticket.Status.EN_ATTENTE_CLIENT:
        return
    short = ticket.await_client_question[:60] + ("…" if len(ticket.await_client_question) > 60 else "")
    workflow._sys_msg(
        ticket,
        f"Petit rappel concernant votre dossier {ticket.public_id} : "
        f"nous attendons {short} pour continuer le traitement.",
        action_source="remind_j2",
    )
    logger.info("AWAIT_REMIND_J2 ticket=%s", ticket_id)


@shared_task(name="support.remind_await_j5", bind=True, max_retries=3, ignore_result=True)
def remind_await_client_j5(self, ticket_id: int) -> None:
    from .models import Ticket
    from . import workflow
    ticket = Ticket.objects.filter(pk=ticket_id).select_related("user").first()
    if not ticket or ticket.status != Ticket.Status.EN_ATTENTE_CLIENT:
        return
    workflow._sys_msg(
        ticket,
        f"Dernier rappel : sans réponse de votre part d'ici 48 h, "
        f"votre dossier {ticket.public_id} sera clos. Vous pourrez toujours en ouvrir un nouveau.",
        action_source="remind_j5",
    )
    workflow._sms(
        ticket,
        f"AGRICAP Support : dernier rappel dossier {ticket.public_id}. "
        f"Répondez sous 48 h ou il sera clos automatiquement.",
    )
    logger.info("AWAIT_REMIND_J5 ticket=%s", ticket_id)


@shared_task(name="support.autoclose_j7", bind=True, max_retries=3, ignore_result=True)
def autoclose_await_client_j7(self, ticket_id: int) -> None:
    from django.utils import timezone
    from .models import Ticket
    from . import workflow
    ticket = Ticket.objects.filter(pk=ticket_id).select_related("user").first()
    if not ticket or ticket.status != Ticket.Status.EN_ATTENTE_CLIENT:
        return

    now = timezone.now()
    if ticket.sla_paused_at:
        pause_secs = int((now - ticket.sla_paused_at).total_seconds())
        ticket.sla_accumulated_pause_seconds += pause_secs
        ticket.sla_paused_at = None

    ticket.status = Ticket.Status.REJETE
    ticket.reject_type = Ticket.RejectType.INFO_INSUFFISANTES
    ticket.rejected_reason = "Clôture automatique après 7 jours sans réponse du client."
    ticket.resolved_at = now
    ticket.awaiting_since = None
    ticket.await_task_j2_id = ""
    ticket.await_task_j5_id = ""
    ticket.await_task_j7_id = ""
    ticket.save()

    workflow._sys_msg(
        ticket,
        f"Sans réponse de votre part après nos relances, votre dossier {ticket.public_id} a été clos. "
        f"Vous pouvez ouvrir un nouveau dossier à tout moment depuis l'application.",
        action_source="autoclose_j7",
    )
    workflow._sys_msg(
        ticket,
        f"Clôture automatique J+7 sans réponse (relances J+2 et J+5 effectuées).",
        is_internal=True,
        action_source="autoclose_j7",
    )
    logger.info("AWAIT_AUTOCLOSE_J7 ticket=%s", ticket_id)
