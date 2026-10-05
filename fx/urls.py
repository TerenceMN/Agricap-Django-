from django.urls import path

from . import views

urlpatterns = [
    path("rates", views.rates),
    path("rates/current", views.current),
    path("rates/pending", views.pending),
    path("rates/sync-bcc", views.sync_bcc),
    path("rates/<int:pk>/validate", views.validate),
    path("convert", views.convert),
]
