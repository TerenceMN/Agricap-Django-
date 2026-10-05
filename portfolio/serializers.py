"""
Sérialisation → dictionnaires au FORMAT ATTENDU par le frontend (Module Crédits
Agricoles). On expose les libellés français des statuts (badges/filtres du SPA) et
des nombres (float) plutôt que des Decimal en chaîne.
"""
from __future__ import annotations

from decimal import Decimal

from .models import Loan, LoanTransaction
from common.money import en_float


def loan_row(loan: Loan) -> dict:
    """Ligne de la table admin (colonnes ID/Date/Bénéficiaire/…)."""
    return {
        "id": loan.reference,
        "date": loan.date.isoformat() if loan.date else "",
        "operator": loan.operator,
        "type": loan.category,
        "amountRequested": en_float(loan.amount_requested),
        "amountApproved": en_float(loan.amount_approved),
        "amountDisbursed": en_float(loan.disbursed),
        "currency": loan.currency,
        "duration": loan.duration_months,
        # Les DEUX unités sont servies, chacune nommée : l'écran ne peut plus
        # afficher un taux annuel dans une case libellée « %/mois » (ni l'inverse).
        "rate": en_float(loan.rate),
        "rateUnit": "percent_per_month",
        "annualRate": en_float(loan.annual_rate) if loan.annual_rate is not None else None,
        "annualRateUnit": "percent_per_year",
        "deferralMonths": loan.deferral_months,
        "deferralMode": loan.deferral_mode,
        "dueDate": loan.due_date.isoformat() if loan.due_date else "",
        "manager": loan.manager,
        "investor": loan.investor,
        "source": loan.source,
        "status": loan.get_status_display(),          # libellé FR (badge + filtre)
        "statusCode": loan.status,
        "score": loan.score,
        "guarantee": loan.guarantee,
        "progress": loan.progress,
        "frequency": loan.frequency,
        "startDate": loan.start_date.isoformat() if loan.start_date else "",
        "outstanding": en_float(loan.outstanding),
        "repaid": en_float(loan.repaid),
        # Traçabilité vers la demande de crédit.
        "applicationCode": loan.application.code if loan.application_id else "",
        "decision": ((loan.application.score_result or {}).get("eligible", False)
                     and "FAVORABLE" or "") if loan.application_id else "",
        "analysisStatut": loan.application.status if loan.application_id else "",
    }


def transaction_row(t: LoanTransaction) -> dict:
    return {
        "id": t.pk,
        "date": t.date.isoformat() if t.date else "",
        "kind": t.kind,
        "type": t.label or t.get_kind_display(),
        "amount": None if t.amount is None else en_float(t.amount),
        "currency": t.currency,
        "originalAmount": None if t.original_amount is None else en_float(t.original_amount),
        "originalCurrency": t.original_currency,
        "paymentMethod": t.payment_method,
        "ref": t.reference,
        "status": t.get_status_display(),
        "verifiedBy": t.verified_by,
        "subwalletId": t.subwallet_id,
    }


def transactions_with_balance(loan: Loan) -> list[dict]:
    """Journal + solde restant cumulé (running balance), dans l'ordre chronologique."""
    rows = []
    balance = Decimal("0")
    for t in loan.transactions.all():
        if t.amount is not None:
            balance += t.amount
        row = transaction_row(t)
        row["balance"] = en_float(balance)
        rows.append(row)
    return rows


def config_payload(loan: Loan) -> dict:
    """Config courante (taux/maturité/statut) + historique d'audit."""
    return {
        "currentConfig": {
            "rate": en_float(loan.rate),
            "rateUnit": "percent_per_month",
            "annualRate": en_float(loan.annual_rate) if loan.annual_rate is not None else None,
            "annualRateUnit": "percent_per_year",
            "duration": loan.duration_months,
            "deferralMonths": loan.deferral_months,
            "deferralMode": loan.deferral_mode,
            "frequency": loan.frequency,
            "status": loan.get_status_display(),
            "statusCode": loan.status,
            "startDate": loan.start_date.isoformat() if loan.start_date else "",
        },
        "history": [
            {
                "date": h.created_at.isoformat(),
                "action": h.action,
                "user": h.user,
                "details": h.details,
            }
            for h in loan.config_history.all()
        ],
    }


def note_row(n) -> dict:
    return {"id": n.pk, "author": n.author, "text": n.text, "date": n.created_at.isoformat()}


def subwallet_row(sw) -> dict:
    return {
        "id": sw.pk, "moduleKey": sw.module_key, "label": sw.label,
        "allocatedAmount": en_float(sw.allocated_amount), "balance": en_float(sw.balance),
    }


def guarantee_row(g) -> dict:
    return {
        "id": g.pk, "type": g.type, "label": g.label, "description": g.description,
        "value": en_float(g.value) if g.value is not None else None,
    }
