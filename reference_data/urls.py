from django.urls import path

from . import views

urlpatterns = [
    path("upload/", views.upload_reference_file),
    path("uploads/", views.list_uploads),
    path("uploads/<int:upload_id>/activate/", views.activate_reference_file),
    path("value-chains/", views.list_value_chains),
]
