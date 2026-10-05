"""Routes de l'API comptable — montées sur `/api/accounting/` (cf. `config/urls.py`).

À NE PAS CONFONDRE avec `/api/ledger/`, qui expose l'autre socle comptable du projet
(plan SYSCOHADA, devise portée par la PIÈCE, déjà branché sur `credits/disbursement.py` et
`agencies/services.py`). Les deux coexistent volontairement : la convergence des deux plans
(137 = « Résultat » en SYSCOHADA vs « Provisions » ici) est une décision du fondateur, pas
un effet de bord d'un routage.
"""
from django.urls import path

from . import views

urlpatterns = [
    # --- Plan comptable -----------------------------------------------------
    path("comptes", views.comptes),
    path("comptes/demandes", views.demandes_compte),
    path("comptes/demandes/<int:demande_id>/decision", views.demande_compte_decision),
    path("comptes/<str:code>", views.compte_detail),
    path("comptes/<str:code>/activation", views.compte_activation),
    path("comptes/<str:code>/suppression", views.compte_suppression),

    # --- Catalogue d'écritures (annexe B) -----------------------------------
    path("catalogue", views.catalogue_schemas),

    # --- Pièces et lignes ---------------------------------------------------
    path("pieces", views.pieces),
    path("pieces/od", views.piece_od),
    path("pieces/<str:reference>", views.piece_detail),
    path("pieces/<str:reference>/validation", views.piece_validation),
    path("pieces/<str:reference>/contrepassation", views.piece_contrepassation),

    # --- Restitutions -------------------------------------------------------
    path("journaux", views.journaux),
    path("balance", views.balance),
    path("grand-livre", views.grand_livre),
    path("controles/integrite", views.controle_integrite),
    path("controles/fx", views.controle_fx),

    # --- Taux de change : LECTURE SEULE (la gouvernance vit dans `/api/fx/`) --
    path("taux", views.taux_change),
    path("taux/saisie", views.taux_saisie_interdite),

    # --- Provisionnement PAR ------------------------------------------------
    path("provisions/classes", views.classes_risque),
    path("provisions/classes/<str:code>", views.classe_risque_detail),
    path("provisions/classification", views.classification),
    path("provisions/arretes", views.arretes),
    path("provisions/classements", views.classements),

    # --- États financiers ---------------------------------------------------
    path("etats/bilan", views.etat_bilan),
    path("etats/resultat", views.etat_resultat),
    path("etats/consolide", views.etat_consolide),
]
