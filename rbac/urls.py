from django.urls import path

from . import views

urlpatterns = [
    path("me", views.me),
    path("roles", views.roles),
    path("roles/<str:role_id>", views.role_detail),
    path("supervisors", views.supervisors),
    # Recherche par nom : les écrans d'assignation ne demandent plus un `sub`.
    path("directory", views.directory_search),
    path("users", views.users),
    path("users/<str:sub>", views.user_detail),
    path("users/<str:sub>/action", views.user_action),
]
