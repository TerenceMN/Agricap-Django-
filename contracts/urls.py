from django.urls import path

from . import views

urlpatterns = [
    path("mine", views.my_contracts),
    path("mine/<int:contract_id>/sign", views.sign_contract),
]
