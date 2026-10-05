from django.urls import path

from . import views

urlpatterns = [
    path("ranges", views.ranges),
    path("chains", views.chains),
    path("config", views.config),
    path("versions", views.versions),
]
