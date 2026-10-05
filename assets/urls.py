from django.urls import path

from . import views

urlpatterns = [
    # Surface client
    path("mine", views.my_assets),
    path("mine/<int:asset_id>", views.asset_detail),
    # Surface agent terrain
    path("pending", views.pending_verification),
    # Doit précéder `<int:asset_id>/…` : sans cela, « history » serait candidat
    # à la capture d'identifiant sur des routes moins typées.
    path("history", views.verification_history),
    path("<int:asset_id>/verify", views.verify),
    path("<int:asset_id>/reject", views.reject),
]
