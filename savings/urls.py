from django.urls import path

from . import views

urlpatterns = [
    path("plans/mine", views.my_plans),
    # Détail/modification et clôture d'un plan (back-office). Déclarés AVANT les
    # sous-routes : `plans/<int:plan_id>` ne peut pas capter `plans/mine`, qui
    # n'est pas un entier, mais l'ordre reste celui du fichier — on le garde
    # lisible plutôt que dépendant d'une subtilité de résolution.
    path("plans/<int:plan_id>", views.plan_detail),
    path("plans/<int:plan_id>/closure", views.plan_closure),
    path("plans/<int:plan_id>/deposit", views.plan_deposit),
    path("plans/<int:plan_id>/withdraw", views.plan_withdraw),
    path("plans/<int:plan_id>/rate-config", views.plan_rate_config),
    path("plans/<int:plan_id>/adjustment", views.plan_adjustment),
    path("plans", views.all_plans),
    path("groups", views.groups),
    path("groups/mine", views.my_groups),
    path("groups/assign", views.assign_group),
    path("groups/<int:group_id>", views.group_detail),
    path("groups/<int:group_id>/audit", views.group_audit),
    path("groups/<int:group_id>/requests", views.group_integration_requests),
    path("groups/<int:group_id>/requests/join", views.request_group_integration),
    path("groups/requests/<int:request_id>/decide", views.decide_group_integration),
    path("requests/mine", views.my_group_requests),
    path("requests", views.all_group_requests),
]
