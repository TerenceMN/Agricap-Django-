from django.urls import path

from . import views

urlpatterns = [
    path("projects", views.projects),
    # Dossiers rattachables — la liste qui manquait au formulaire de projet.
    path("credit-applications/linkable", views.linkable_credit_applications),
    # AVANT `projects/<str:code>` : sans cela, « next-code » serait pris pour un
    # code de projet et l'aperçu répondrait 404.
    path("projects/next-code", views.project_next_code),
    path("projects/<str:code>", views.project_detail),
    path("projects/<str:code>/action", views.project_action),
    path("projects/<str:code>/credit-application", views.project_link_credit_application),
    path("projects/<str:code>/transitions", views.project_transitions),
    # Retirés (août 2026) avec le scoring et le comité d'investissement :
    # `analysis`, `technical-analysis`, `financial-analysis`, `approve-analysis`,
    # `committee*`, `clear-conditions`. Ce module ne note plus un projet et ne le
    # fait plus approuver par une instance : il publie une pièce jointe, et
    # l'investisseur décide en souscrivant.
    path("projects/<str:code>/expert-valuation", views.project_expert_valuation),
    path("projects/<str:code>/close-fundraising", views.project_close_fundraising),
    path("projects/<str:code>/disburse", views.project_disburse),
    path("projects/<str:code>/record-return", views.project_record_return),
    path("projects/<str:code>/cancel", views.project_cancel),
    # Pièces du dossier — le document que l'investisseur lit pour décider. C'est
    # tout ce qui subsiste de l'ancien bloc « analyses ».
    path("projects/<str:code>/analysis-attachments", views.project_analysis_attachments),
    path("projects/<str:code>/analysis-attachments/<int:piece_id>",
         views.project_analysis_attachment),
    path("offers", views.offers),
    path("offers/open", views.offers_open),
    path("offers/<int:offer_id>/close", views.offer_close),
    path("offers/<int:offer_id>/distribute", views.offer_distribute),
    path("offers/<int:offer_id>/collateral", views.offer_collateral),
    path("investors", views.investors),
    path("investors/me", views.my_investor_profile),
    path("investors/<int:investor_id>/action", views.investor_action),
    path("subscriptions", views.subscriptions),
    path("subscriptions/mine", views.my_subscriptions),
    path("subscriptions/<int:subscription_id>/settle", views.subscription_settle),
    path("subscriptions/<int:subscription_id>/cancel", views.subscription_cancel),
    path("movements", views.movements),
    path("schedules", views.schedules),
    path("sub-portfolios", views.sub_portfolios),
    path("sub-portfolios/<int:sub_id>/target-allocation", views.sub_portfolio_target_allocation),
    path("sub-portfolios/<int:sub_id>/allocation", views.sub_portfolio_allocation),
    path("observations", views.observations),
    path("questions", views.questions),
    path("questions/<int:question_id>/answer", views.question_answer),
    path("performance-reports", views.performance_reports),
    path("obligations", views.obligations),
    path("obligations/<int:position_id>/withdraw", views.obligation_withdraw),
    path("obligations/<int:position_id>/convert", views.obligation_convert),
    path("obligations/<int:position_id>/withdrawals", views.obligation_withdrawals),
    path("obligations/<int:position_id>/conversions", views.obligation_conversions),
    path("secondary-market", views.secondary_market),
    path("dashboard-metrics", views.dashboard_metrics),
    path("portfolio-allocation", views.my_portfolio_allocation),
    path("metrics/mine", views.my_metrics),
    path("metrics/portfolio", views.portfolio_metrics),
    path("pipeline", views.pipeline),
    path("accounting-events", views.accounting_events),
    # Classement d'une souscription dans une poche — au moment de la souscription
    # (`POST /subscriptions`, champ `subPortfolioId`) ou après coup, ici.
    path("subscriptions/<int:subscription_id>/sub-portfolio", views.subscription_sub_portfolio),
    # Rééquilibrage : l'ordre s'instruit comme une souscription (réservation puis
    # encaissement constaté), jamais comme un déplacement de solde.
    path("sub-portfolios/<int:sub_id>/rebalancing-orders", views.sub_portfolio_rebalancing),
    # Séries de référence : lecture des séries ACTIVES, déclaration et activation
    # maker ≠ checker. Les points arrivent par `import_benchmark_series`.
    path("benchmarks", views.benchmark_series),
    path("benchmarks/<str:code>/activate", views.benchmark_series_activate),
    # Référentiel ESG (capacité `config`) et mesures de projet.
    path("esg/criteria", views.esg_criteria),
    # L'activation porte sur un JEU de critères : une pondération se vote entière.
    path("esg/criteria/activate", views.esg_criteria_activate),
    path("esg/measures/<int:measure_id>/verify", views.esg_measure_verify),
    path("projects/<str:code>/esg", views.project_esg),
]
