from django.urls import path
from credits import views

urlpatterns = [
    # Tableau de bord (Étape 7)
    path("dashboard/", views.credits_dashboard),
    # Préremplissage & utilitaires
    path("application/prefill/", views.prefill_application),
    # Les 14 chaînes de valeur (`referentiel.chains`) : niveau au-dessus de la
    # filière (`ValueChain`) dans le formulaire de demande — cf. `list_value_chain_families`.
    path("value-chain-families/", views.list_value_chain_families),
    path("needs-sheet/parse/", views.parse_needs_sheet_view),
    # Saisie manuelle : GET sert les rubriques du template actif, POST enregistre
    # la saisie par le MÊME pipeline que le dépôt Excel.
    path("needs-sheet/saisie/", views.needs_sheet_saisie_options),
    path("needs-sheet/saisie/enregistrer/", views.needs_sheet_saisie),
    path("needs-sheet-template/", views.download_needs_sheet_template),
    path("simulate/", views.simulate_scoring),
    # Liste & détail
    path("applications/", views.list_applications),
    path("applications/<str:code>/", views.application_detail),
    # Scoring
    path("applications/<str:code>/score/", views.score_application),
    # Machine à états (Étape 5)
    path("applications/<str:code>/submit/", views.submit_application),
    path("applications/<str:code>/start-analysis/", views.start_analysis),
    path("applications/<str:code>/approve/", views.approve_application),
    path("applications/<str:code>/reject/", views.reject_application),
    path("applications/<str:code>/adjourn/", views.adjourn_application),
    path("applications/<str:code>/reopen-analysis/", views.reopen_analysis),
    # Clôture (ACTIVE → CLOSED) : seule porte de la boucle d'apprentissage (P10).
    path("applications/<str:code>/close/", views.close_application),
    path("applications/<str:code>/client-consent/", views.client_consent),
    path("applications/<str:code>/renew-consent/", views.renew_client_consent),
    # Décaissement (Étape 6)
    path("applications/<str:code>/disbursement/", views.disbursement_detail),
    path("applications/<str:code>/disbursement/request/", views.request_disbursement_view),
    path("applications/<str:code>/disbursement/confirm/", views.confirm_disbursement_view),
    path("applications/<str:code>/disbursement/cancel/", views.cancel_disbursement_view),
    # Demandes de caution — surface du GARANT, pas du dossier (SPEC §2.5).
    # Volontairement hors de `applications/<code>/` : le garant n'a pas accès au
    # dossier, il a accès à SON engagement.
    path("guarantee-requests/", views.list_guarantee_requests),
    path("guarantee-requests/<int:guarantee_id>/consent/", views.consent_guarantee_request),
    # Propositions de caution — le client propose, l'agent valide.
    # `guarantee-proposals/` (le demandeur) est symétrique de
    # `guarantee-requests/` (le garant) : deux surfaces personnelles, deux
    # publics. `queue/` est celle du personnel ; elle est déclarée avant la route
    # à identifiant pour rester lisible, même si `<int:…>` ne peut pas la capter.
    path("guarantee-proposals/", views.list_my_guarantee_proposals),
    path("guarantee-proposals/queue/", views.guarantee_proposal_queue),
    path("guarantee-proposals/<int:proposal_id>/validate/",
         views.validate_guarantee_proposal),
    path("guarantee-proposals/<int:proposal_id>/refuse/",
         views.refuse_guarantee_proposal),
    # Garanties (Étape 4)
    path("applications/<str:code>/guarantees/", views.list_guarantees),
    path("applications/<str:code>/guarantees/savings/", views.place_savings_guarantee),
    path("applications/<str:code>/guarantees/moral/", views.register_moral_guarantee),
    path("applications/<str:code>/guarantee-proposals/",
         views.application_guarantee_proposals),
    path("applications/<str:code>/guarantee-proposals/candidates/",
         views.guarantee_proposal_candidates),
    path("applications/<str:code>/guarantees/asset/", views.place_asset_guarantee_view),
    path("applications/<str:code>/guarantees/<int:guarantee_id>/confirm/", views.confirm_guarantee),
    path("applications/<str:code>/guarantees/<int:guarantee_id>/release/", views.release_guarantee),
    # Analyse documentaire (Partie H)
    path("applications/<str:code>/analysis-report/", views.analysis_report),
    # Moteur d'analyse technico-économique (SPEC Moteur)
    # Convention du module `applications/<code>/…`, PAS `admin/demandes/<ref>/…`
    # de la SPEC : elle adresse des modèles qui n'existent pas ici.
    # `analyse/justifier/` avant `analyse/` n'est pas nécessaire (chemins
    # distincts, pas de préfixe capturant), mais l'ordre suit la lecture métier.
    path("applications/<str:code>/analyse/", views.analyse_detail),
    path("applications/<str:code>/analyse/justifier/", views.analyse_justifier),
    path("applications/<str:code>/reanalyser/", views.reanalyser),
    path("applications/<str:code>/analyse-resume/", views.analyse_resume),
    # Comité de crédit — décision collégiale à quorum (CONTRAT §2)
    path("applications/<str:code>/committee-votes/", views.committee_votes),
    path("applications/<str:code>/committee-vote/", views.committee_vote),
    # Barèmes de score éditables par le comité (CONTRAT §5)
    # `<code>` (DSCR, ECART_TECHNIQUE…) plutôt que `<id>` : le code est l'identité
    # stable et unique du barème, et celle que sert la liste. Écart au contrat
    # signalé dans le rapport de lot.
    path("baremes/", views.list_baremes),
    path("baremes/revisions/<int:revision_id>/activate/", views.bareme_activate),
    path("baremes/<str:code>/", views.bareme_detail),
    path("baremes/<str:code>/preview/", views.bareme_preview),
]
