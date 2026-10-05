from django.urls import path

from . import views

urlpatterns = [
    path("entries", views.entries),
    path("export", views.export),
]
