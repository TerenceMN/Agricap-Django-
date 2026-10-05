"""
API du portefeuille de crédits (Module Crédits Agricoles, admin). Réservé au personnel
(admin/manager). Format des réponses aligné sur le frontend.
"""
from __future__ import annotations

from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework import status as http

from accounts.permissions import IsStaff
from . import serializers, services
from .models import Loan, LoanNote


def _get(ref: str) -> Loan | None:
    return Loan.objects.filter(reference=ref).first()


# --- Espace client (Credits.jsx) ---------------------------------------------------
def _get_mine(request, ref: str) -> Loan | None:
    loan = _get(ref)
    if not loan or loan.borrower_sub != getattr(request.user, "sub", ""):
        return None
    return loan


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def mine(request):
    sub = getattr(request.user, "sub", "")
    if request.method == "GET":
        return Response([services.client_loan_detail(loan) for loan in services.client_loans(sub)])
    loan = services.submit_client_application(request.data or {}, by=sub)
    return Response(services.client_loan_detail(loan), status=http.HTTP_201_CREATED)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def mine_detail(request, ref):
    loan = _get_mine(request, ref)
    if not loan:
        return Response({"detail": "Dossier introuvable."}, status=404)
    return Response(services.client_loan_detail(loan))


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def mine_pay(request, ref):
    loan = _get_mine(request, ref)
    if not loan:
        return Response({"detail": "Dossier introuvable."}, status=404)
    data = request.data or {}
    services.pay_from_subwallet(
        loan, data.get("subwalletId"), data.get("amount"), data.get("beneficiary", ""),
        data.get("description", ""), by=getattr(request.user, "sub", ""),
    )
    return Response(services.client_loan_detail(loan))


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def mine_rebalance(request, ref):
    loan = _get_mine(request, ref)
    if not loan:
        return Response({"detail": "Dossier introuvable."}, status=404)
    data = request.data or {}
    services.rebalance_subwallets(
        loan, data.get("fromId"), data.get("toId"), data.get("amount"),
        by=getattr(request.user, "sub", ""),
    )
    return Response(services.client_loan_detail(loan))


@api_view(["POST"])
@permission_classes([IsStaff])
def loan_from_application(request, code):
    """Rattache un dossier de GESTION à une demande ANALYSÉE par le moteur."""
    from credits.models import CreditApplication
    app = CreditApplication.objects.filter(code=code).first()
    if not app:
        return Response({"detail": "Dossier d'analyse introuvable."}, status=404)
    loan = services.create_from_application(app, by=getattr(request.user, "sub", ""))
    return Response({"detail": f"Dossier {loan.reference} rattaché.", **serializers.loan_row(loan)},
                    status=http.HTTP_201_CREATED)


@api_view(["GET", "POST"])
@permission_classes([IsStaff])
def loans(request):
    """GET : liste filtrable (?status=&search=). POST : création manuelle (« Ajouter »)."""
    if request.method == "GET":
        # Chaque demande analysée par le moteur apparaît ici (dossier de gestion créé au besoin).
        services.sync_from_applications(by=getattr(request.user, "sub", ""))
        qs = Loan.objects.select_related("application", "application__client", "application__value_chain")
        st = request.GET.get("status")
        if st and st.lower() != "all" and st.lower() != "tous":
            code = services.status_code(st, "")
            if code:
                qs = qs.filter(status=code)
        search = (request.GET.get("search") or "").strip().lower()
        rows = [serializers.loan_row(l) for l in qs]
        if search:
            rows = [r for r in rows if search in (r["operator"] + r["id"] + r["manager"]).lower()]
        return Response(rows)

    loan = services.create_loan(request.data or {}, by=getattr(request.user, "sub", ""))
    return Response({"detail": f"Dossier {loan.reference} créé.", **serializers.loan_row(loan)},
                    status=http.HTTP_201_CREATED)


@api_view(["GET", "PATCH", "DELETE"])
@permission_classes([IsStaff])
def loan_detail(request, ref):
    loan = _get(ref)
    if not loan:
        return Response({"detail": "Dossier introuvable."}, status=404)
    if request.method == "DELETE":
        loan.delete()
        return Response({"detail": "Dossier supprimé."})
    if request.method == "PATCH":
        loan = services.update_loan(loan, request.data or {})
    return Response({
        **serializers.loan_row(loan),
        "notes": [serializers.note_row(n) for n in loan.notes.all()],
        "config": serializers.config_payload(loan),
    })


@api_view(["GET", "POST"])
@permission_classes([IsStaff])
def loan_config(request, ref):
    """GET : config + historique + échéancier. POST : applique la config (audit)."""
    loan = _get(ref)
    if not loan:
        return Response({"detail": "Dossier introuvable."}, status=404)
    if request.method == "POST":
        action = request.data.get("action") or "Modification"
        sched = services.apply_config(loan, request.data or {}, by=getattr(request.user, "sub", ""), action=action)
        return Response({"detail": "Configuration enregistrée.",
                         **serializers.config_payload(loan), **sched})
    return Response({**serializers.config_payload(loan), **services.schedule_for(loan)})


@api_view(["GET"])
@permission_classes([IsStaff])
def loan_schedule(request, ref):
    loan = _get(ref)
    if not loan:
        return Response({"detail": "Dossier introuvable."}, status=404)
    return Response(services.schedule_for(loan))


@api_view(["GET", "POST"])
@permission_classes([IsStaff])
def loan_transactions(request, ref):
    loan = _get(ref)
    if not loan:
        return Response({"detail": "Dossier introuvable."}, status=404)
    if request.method == "POST":
        services.add_transaction(loan, request.data or {}, by=getattr(request.user, "sub", ""))
    return Response({"currency": loan.currency, "transactions": serializers.transactions_with_balance(loan)})


@api_view(["GET", "POST"])
@permission_classes([IsStaff])
def loan_notes(request, ref):
    loan = _get(ref)
    if not loan:
        return Response({"detail": "Dossier introuvable."}, status=404)
    if request.method == "POST":
        text = (request.data.get("text") or "").strip()
        if not text:
            return Response({"detail": "Note vide."}, status=400)
        LoanNote.objects.create(loan=loan, author=getattr(request.user, "sub", "") or "Admin", text=text)
    return Response([serializers.note_row(n) for n in loan.notes.all()])


@api_view(["POST"])
@permission_classes([IsStaff])
def loan_action(request, ref):
    """Actions du menu : reassign/extend/pause/block/resume/close/cancel/reminder/note/disburse/approve/default."""
    loan = _get(ref)
    if not loan:
        return Response({"detail": "Dossier introuvable."}, status=404)
    action = request.data.get("action") or ""
    result = services.run_action(loan, action, request.data or {}, by=getattr(request.user, "sub", ""))
    st = http.HTTP_200_OK if result.get("ok") else http.HTTP_400_BAD_REQUEST
    return Response({**result, "credit": serializers.loan_row(loan)}, status=st)


@api_view(["GET"])
@permission_classes([IsStaff])
def summary(request):
    return Response(services.summary())


@api_view(["GET"])
@permission_classes([IsStaff])
def alerts(request):
    return Response(services.alerts())


@api_view(["GET"])
@permission_classes([IsStaff])
def calendar(request):
    return Response(services.calendar_entries())
