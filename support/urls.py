from django.urls import path

from . import views

urlpatterns = [
    # ── Dashboard ──────────────────────────────────────────────────────────────
    path("dashboard/stats", views.dashboard_stats),

    # ── Tickets ────────────────────────────────────────────────────────────────
    path("tickets", views.tickets),
    path("tickets/<int:ticket_id>", views.ticket_detail),
    path("tickets/<int:ticket_id>/messages", views.ticket_messages),
    path("tickets/<int:ticket_id>/assign", views.ticket_assign),
    path("tickets/<int:ticket_id>/claim", views.ticket_claim),
    path("tickets/<int:ticket_id>/escalate", views.ticket_escalate),
    path("tickets/<int:ticket_id>/resolve", views.ticket_resolve),
    path("tickets/<int:ticket_id>/reject", views.ticket_reject),
    path("tickets/<int:ticket_id>/reopen", views.ticket_reopen),
    path("tickets/<int:ticket_id>/waiting-on", views.ticket_waiting_on),
    path("tickets/<int:ticket_id>/rate", views.ticket_rate),
    path("tickets/<int:ticket_id>/verify-mobile-money", views.ticket_verify_mm),
    path("tickets/<int:ticket_id>/force-credit", views.ticket_force_credit),
    path("tickets/<int:ticket_id>/await-client", views.ticket_await_client),
    path("tickets/<int:ticket_id>/client-360", views.ticket_client_360),
    path("tickets/<int:ticket_id>/reveal-contact", views.ticket_reveal_contact),

    # ── Conversations investisseur/gestionnaire ────────────────────────────────
    path("conversations/mine", views.my_conversations),
    path("conversations", views.start_conversation),
    path("conversations/<int:conversation_id>/messages", views.conversation_messages),
    path("conversations/<int:conversation_id>/messages/send", views.send_message),
]
