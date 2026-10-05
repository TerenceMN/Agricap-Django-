from django.urls import path

from . import views

urlpatterns = [
    path("", views.transactions),
    path("<int:tx_id>", views.transaction_detail),
    path("<int:tx_id>/approve", views.transaction_approve),
    path("<int:tx_id>/reject", views.transaction_reject),
    path("<int:tx_id>/reverse", views.transaction_reverse),
    path("<int:tx_id>/otp/request", views.otp_request),
    path("<int:tx_id>/otp/verify", views.otp_verify),
    path("bulk-action", views.bulk_action),
    path("special-cases", views.special_cases),
    path("special-cases/<int:case_id>/escalate", views.special_case_escalate),
    path("thresholds", views.thresholds),
    path("supervision", views.supervision),
]
