from django.urls import path

from . import views

urlpatterns = [
    path("mine", views.my_notifications),
    path("<int:notification_id>/read", views.mark_read),
]
