from django.urls import path

from . import views

urlpatterns = [
    path("rules", views.alert_rules),
    path("rules/<int:rule_id>", views.alert_rule_detail),
    path("", views.alerts),
    path("<int:alert_id>/acknowledge", views.alert_acknowledge),
    path("<int:alert_id>/resolve", views.alert_resolve),
]
