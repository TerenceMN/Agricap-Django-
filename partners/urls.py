from django.urls import path

from . import views

urlpatterns = [
    path("", views.partners),
    path("<int:partner_id>", views.partner_configure),
    path("<int:partner_id>/sync", views.partner_sync),
    path("<int:partner_id>/test", views.partner_test),
    path("<int:partner_id>/logs", views.partner_logs),
]
