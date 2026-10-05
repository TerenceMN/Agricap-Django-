from django.urls import path

from . import views

urlpatterns = [
    path("", views.suppliers),
    path("<int:supplier_id>/action", views.supplier_action),
]
