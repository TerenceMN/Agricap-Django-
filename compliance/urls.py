from django.urls import path

from . import views

urlpatterns = [
    path("kyc", views.kyc_profiles),
    path("kyc/mine", views.my_kyc),
    path("kyc/<str:user_sub>/validate", views.validate_kyc),
    path("documents/mine", views.my_documents),
    path("documents/<int:doc_id>/review", views.document_review),
]
