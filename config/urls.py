"""Routage central de l'API AGRICAP FINTECH."""
from django.conf import settings
from django.conf.urls.static import static
from django.urls import include, path

from accounts import views as accounts_views
from common import views as common_views


def health(_request):
    from django.http import JsonResponse
    return JsonResponse({"status": "ok", "service": "agricap-fintech-backend"})


urlpatterns = [
    # admin.site.urls retiré : login/permissions Django séparés de l'IdP et du
    # RBAC métier, exposé pour rien (l'appli a ses propres écrans backoffice).
    path("api/health", health),
    path("api/sms/test", common_views.sms_test),
    path("api/internal/sms/send", common_views.sms_relay),
    path("api/me", accounts_views.me),
    path("api/portfolio/", include("portfolio.urls")),
    path("api/dataio/", include("dataio.urls")),
    path("api/referentiel/", include("referentiel.urls")),
    path("api/audit/", include("audit.urls")),
    path("api/rbac/", include("rbac.urls")),
    path("api/agencies/", include("agencies.urls")),
    path("api/caisses/", include("caisses.urls")),
    path("api/ledger/", include("ledger.urls")),
    # Socle comptable bi-monnaie natif (devise portée par la LIGNE, annexes A/B/E) —
    # distinct de `ledger` (SYSCOHADA, devise portée par la pièce), qui reste branché.
    path("api/accounting/", include("accounting.urls")),
    path("api/fx/", include("fx.urls")),
    path("api/transactions/", include("transactions.urls")),
    path("api/investments/", include("investments.urls")),
    path("api/savings/", include("savings.urls")),
    path("api/assets/", include("assets.urls")),
    path("api/contracts/", include("contracts.urls")),
    path("api/suppliers/", include("suppliers.urls")),
    path("api/compliance/", include("compliance.urls")),
    path("api/support/", include("support.urls")),
    path("api/reference-data/", include("reference_data.urls")),
    path("api/credits/", include("credits.urls")),
    path("api/notifications/", include("notifications.urls")),
    path("api/partners/", include("partners.urls")),
    path("api/analytics/", include("analytics.urls")),
    path("api/alerts/", include("alerts.urls")),
]

# --- Documentation OpenAPI ---------------------------------------------------
# Servie UNIQUEMENT si `API_DOCS_ENABLED` : le schéma décrit des endpoints staff
# qui exposent barèmes, seuils et tolérances. Le publier apprendrait au demandeur
# comment fabriquer son score (principe 7). Aucune permission ne protège une
# route qu'on n'a pas montée — c'est l'inverse d'une case à cocher oubliée.
if getattr(settings, "API_DOCS_ENABLED", False):
    from drf_spectacular.views import (
        SpectacularAPIView, SpectacularRedocView, SpectacularSwaggerView,
    )

    urlpatterns += [
        path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
        path("api/docs/", SpectacularSwaggerView.as_view(url_name="schema"),
             name="swagger-ui"),
        path("api/redoc/", SpectacularRedocView.as_view(url_name="schema"),
             name="redoc"),
    ]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
