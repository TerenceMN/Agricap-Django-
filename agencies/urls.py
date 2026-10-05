from django.urls import path

from . import views

urlpatterns = [
    path("", views.agencies),
    # Routes littérales AVANT `<str:code>` (sinon "reconciliations"/"action-requests"
    # serait capturé comme un code d'agence par le pattern générique ci-dessous).
    path("reconciliations", views.reconciliations),
    path("reconciliations/<int:reconciliation_id>/assign", views.reconciliation_assign),
    path("reconciliations/<int:reconciliation_id>/complete", views.reconciliation_complete),
    path("action-requests", views.action_requests),
    path("action-requests/<int:request_id>/request-code", views.action_request_code),
    path("action-requests/<int:request_id>/verify-code", views.action_verify_code),
    path("action-requests/<int:request_id>/approve", views.action_request_approve),
    path("action-requests/<int:request_id>/reject", views.action_request_reject),
    path("action-requests/<int:request_id>/cancel", views.action_request_cancel),
    path("action-requests/<int:request_id>/notify-approvers", views.action_request_notify),
    path("approver-configs", views.approver_configs),
    path("approver-configs/<int:config_id>", views.approver_config_detail),
    path("approver-configs/<int:config_id>/phone", views.approver_config_phone),
    path("evolution-plans/<int:plan_id>/items/<int:item_id>/check", views.evolution_plan_check_item),
    path("evolution-plans/<int:plan_id>/complete", views.evolution_plan_complete),
    path("evolution-plans/<int:plan_id>/cancel", views.evolution_plan_cancel),
    path("<str:code>", views.agency_detail),
    path("<str:code>/action", views.agency_action),
    path("<str:code>/reconciliation", views.agency_reconciliation),
    path("<str:code>/status-history", views.agency_status_history),
    path("<str:code>/audit", views.agency_audit),
    path("<str:code>/compliance-score", views.agency_compliance_score),
    path("<str:code>/evolution-plans", views.agency_evolution_plans),
]
