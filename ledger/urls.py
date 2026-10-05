from django.urls import path

from . import views

urlpatterns = [
    path("accounts", views.accounts),
    path("accounts/<str:code>/lines", views.account_lines),
    path("entries", views.entries),
    path("entries/<int:entry_id>/reverse", views.entry_reverse),
    path("trial-balance", views.trial_balance_view),
    path("statements/<str:kind>", views.statements),
]
