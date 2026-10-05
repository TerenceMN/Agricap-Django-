"""Agrégation lecture seule cross-apps (Analytics.jsx, Dashboard.jsx,
MultiCurrencyDashboard.jsx) — aucun modèle autoritaire propre (à l'exception
d'un instantané de score de conformité, cf. `models.py`), cette app ne fait que lire
les autres."""
from __future__ import annotations

from django.db.models import Sum
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response

from accounts.permissions import IsStaff
from rbac.permissions import HasCapability

from . import services


@api_view(["GET"])
@permission_classes([IsStaff, HasCapability("read")])
def overview(request):
    from agencies.models import Agency
    from caisses.models import TreasuryAccount
    from transactions.models import Transaction

    return Response({
        "activeAgencies": Agency.objects.filter(status=Agency.Status.ACTIF).count(),
        "suspendedAgencies": Agency.objects.filter(status=Agency.Status.SUSPENDU).count(),
        "treasuryTotalUSD": float(
            TreasuryAccount.objects.filter(currency="USD").aggregate(total=Sum("balance"))["total"] or 0
        ),
        "pendingTransactions": Transaction.objects.filter(status="pending_validation").count(),
        "postedTransactions": Transaction.objects.filter(status="posted").count(),
    })


@api_view(["GET"])
@permission_classes([IsStaff, HasCapability("read")])
def compliance_score(request):
    return Response(services.compute_compliance_score())


@api_view(["GET"])
@permission_classes([IsStaff, HasCapability("read")])
def multi_currency(request):
    """`GET /api/analytics/multi-currency?agency=<id|all>&period=<today|week|month|quarter|year>`

    Garde `read` — et surtout PAS `config` : cette vue sert un COMPTE agrégé de clients,
    pas l'annuaire nominatif (que sert `rbac`). Exiger `config` pour afficher un compteur,
    c'est refuser un chiffre anodin à tout le personnel qui n'administre pas le
    référentiel — et c'est ce qui faisait afficher « N/D » à l'écran.

    Un paramètre invalide est une erreur 400 structurée, jamais un repli silencieux sur la
    valeur par défaut : lire « toutes agences » quand on a demandé l'agence 7 est pire que
    ne rien lire.
    """
    agency, period_key, errors = services.validate_dashboard_params(
        agency=request.GET.get("agency", "all"),
        period=request.GET.get("period", "month"),
    )
    if errors:
        return Response({"detail": errors[0]["message"], "errors": errors}, status=400)
    return Response(services.multi_currency_dashboard(agency=agency, period_key=period_key))
