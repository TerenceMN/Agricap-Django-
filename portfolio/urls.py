from django.urls import path

from . import views

urlpatterns = [
    path("mine", views.mine),
    path("mine/<str:ref>", views.mine_detail),
    path("mine/<str:ref>/pay", views.mine_pay),
    path("mine/<str:ref>/rebalance", views.mine_rebalance),
    path("loans", views.loans),
    path("loans/from-application/<str:code>", views.loan_from_application),
    path("loans/<str:ref>", views.loan_detail),
    path("loans/<str:ref>/config", views.loan_config),
    path("loans/<str:ref>/schedule", views.loan_schedule),
    path("loans/<str:ref>/transactions", views.loan_transactions),
    path("loans/<str:ref>/notes", views.loan_notes),
    path("loans/<str:ref>/action", views.loan_action),
    path("summary", views.summary),
    path("alerts", views.alerts),
    path("calendar", views.calendar),
]
