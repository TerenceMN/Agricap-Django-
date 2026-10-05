"""Fonctions de sérialisation plates (pas de classes DRF `Serializer`) — même convention
que `portfolio/serializers.py`. Utilisées à la fois par `services.py` (snapshot
d'idempotence) et `views.py` (réponse succès), pour garantir qu'un rejeu renvoie
EXACTEMENT la même forme que la réponse d'origine."""
from __future__ import annotations

from common.choices import FlowStatus

from .models import CashRegisterSession, FundTransfer, PaymentOrder, PaymentOrderEvent, RegularizationOrder, \
    TreasuryAccount, WalletMovement, WithdrawalRequest


def transfer_row(t: FundTransfer) -> dict:
    return {"detail": "Transfert effectué.", "transferId": t.pk, "amount": float(t.amount)}


def movement_row(m: WalletMovement, *, verb: str) -> dict:
    return {"detail": verb, "movementId": m.pk, "amount": float(m.amount)}


def withdrawal_request_row(r: WithdrawalRequest) -> dict:
    from .withdrawal_tiers import _required_approvals, _threshold_for
    approvals_count = r.approvals.filter(decision="APPROVED").count() if r.pk else 0
    needed = _required_approvals(r.amount, _threshold_for(r.wallet.currency))
    return {
        "detail": "Retrait effectué." if r.status == FlowStatus.POSTED else "Retrait en attente de validation.",
        "requestId": r.pk, "amount": float(r.amount), "status": r.status, "autoValidated": r.auto_validated,
        "requiredApprovals": needed, "approvalsCount": approvals_count,
        "movementId": r.movement_id,
        # Jambe externe (Mobile Money / banque) : canal + référence de l'ordre de décaissement
        # Makuta créé au règlement. `channel` vide/`agent` = retrait interne (aucun versement
        # fournisseur, `payoutOrderRef` nul).
        "channel": r.channel or None, "counterparty": r.counterparty or None,
        "payoutOrderRef": r.payout_order.reference if r.payout_order_id else None,
    }


def regularization_order_row(o: RegularizationOrder) -> dict:
    from .regularization import _required_approvals, _threshold_for
    approvals_count = o.approvals.filter(decision="APPROVED").count() if o.pk else 0
    needed = _required_approvals(o.amount, _threshold_for(o.wallet.currency))
    return {
        "detail": "Crédit de régularisation effectué." if o.status == FlowStatus.POSTED
        else "Ordre de régularisation en attente de validation.",
        "orderId": o.pk, "amount": float(o.amount), "status": o.status, "autoValidated": o.auto_validated,
        "requiredApprovals": needed, "approvalsCount": approvals_count,
        "movementId": o.movement_id, "ticketId": o.ticket_id,
    }


#: Message affichable par statut d'ordre de paiement. Le front NE DOIT PAS déduire l'issue
#: d'un code HTTP : un ordre indéterminé se répond en 200 avec `status: INDETERMINATE`, parce
#: qu'il n'y a ni erreur du client, ni succès — il y a une incertitude à afficher comme telle.
PAYMENT_STATUS_DETAIL = {
    PaymentOrder.Status.PENDING: "Ordre de paiement créé — pas encore transmis au fournisseur.",
    PaymentOrder.Status.SENT: "Ordre transmis au fournisseur — réponse en attente.",
    PaymentOrder.Status.AWAITING_CONFIRMATION: "Le fournisseur a accusé réception — issue pas encore connue.",
    PaymentOrder.Status.INDETERMINATE: "Issue inconnue : la liaison a été coupée après l'envoi. "
                                        "L'opération est en cours de vérification auprès du fournisseur.",
    PaymentOrder.Status.CONFIRMED: "Paiement confirmé par le fournisseur.",
    PaymentOrder.Status.REFUSED: "Paiement refusé par le fournisseur.",
    PaymentOrder.Status.CANCELLED: "Ordre de paiement annulé avant tout envoi.",
}


def payment_order_row(o: PaymentOrder) -> dict:
    """Vue CLIENT/STAFF d'un ordre. Les montants sortent en **chaînes** (`str(Decimal)`) et
    non en `float` : un ordre de paiement est la pièce qui sera comparée au relevé de
    l'opérateur, et `float` y introduit des écarts au centime (principe 4)."""
    return {
        "reference": o.reference, "referenceId": o.reference_id,
        "status": o.status,
        "detail": PAYMENT_STATUS_DETAIL.get(o.status, ""),
        "direction": o.direction,
        "operation": o.operation,
        "amount": str(o.amount),
        "currency": o.currency,
        "counterparty": o.counterparty,
        "walletId": o.wallet_id,
        "treasuryAccountCode": o.treasury_account.code if o.treasury_account_id else None,
        "providerReference": o.provider_reference or None,
        "movementId": o.movement_id,
        "reversalMovementId": o.reversal_movement_id,
        # Vrai tant que l'issue n'est pas connue : c'est CE drapeau qui doit empêcher le
        # front de proposer « réessayer » — un rejeu à l'aveugle paie deux fois.
        "awaitingReconciliation": o.status in PaymentOrder.OPEN_STATUSES,
        "failureDetail": o.failure_detail or None,
        "createdAt": o.created_at.isoformat() if o.created_at else None,
        "sentAt": o.sent_at.isoformat() if o.sent_at else None,
        "settledAt": o.settled_at.isoformat() if o.settled_at else None,
        "createdBy": o.created_by,
    }


def payment_event_row(e: PaymentOrderEvent) -> dict:
    return {
        "id": e.pk, "kind": e.kind, "source": e.source, "fromStatus": e.from_status or None,
        "toStatus": e.to_status or None, "actor": e.actor or None, "motive": e.motive or None,
        "payload": e.payload or {}, "at": e.at.isoformat() if e.at else None,
    }


def account_row(a: TreasuryAccount) -> dict:
    return {
        "id": a.pk, "code": a.code, "name": a.name, "kind": a.kind,
        "agencyId": a.agency_id, "currency": a.currency, "balance": float(a.balance),
        "initialAmount": float(a.initial_amount), "manager": a.manager_sub, "scope": a.scope,
        "riskLevel": a.risk_level, "status": a.status, "createdAt": a.created_at.isoformat(),
        "dailyCeiling": float(a.daily_ceiling) if a.daily_ceiling is not None else None,
        "partnerId": a.partner_id, "partnerName": a.partner.name if a.partner_id else None,
    }


def session_row(s: CashRegisterSession) -> dict:
    return {
        "id": s.pk, "accountCode": s.account.code, "status": s.status,
        "openedBy": s.opened_by, "openingCount": float(s.opening_count),
        "openingBalanceExpected": float(s.opening_balance_expected), "openedAt": s.opened_at.isoformat(),
        "cashInTotal": float(s.cash_in_total),
        "closedBy": s.closed_by,
        "closingCount": float(s.closing_count) if s.closing_count is not None else None,
        "closingBalanceExpected": float(s.closing_balance_expected) if s.closing_balance_expected is not None
        else None,
        "discrepancy": float(s.discrepancy) if s.discrepancy is not None else None,
        "closedAt": s.closed_at.isoformat() if s.closed_at else None,
    }
