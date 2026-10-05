"""Hiérarchie d'exceptions métier, app-agnostique. Les apps monétaires (caisses, ledger,
transactions, investments) lèvent ces exceptions plutôt que de renvoyer None/False ou de
construire une Response() à la main — `config.exceptions.agricap_exception_handler` les
mappe vers le bon code HTTP (voir Annexe C du cahier de conception)."""
from __future__ import annotations


class BusinessError(Exception):
    code: str = "business_error"
    http_status: int = 400

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code

    def __str__(self) -> str:
        return self.message


class ValidationFailed(BusinessError):
    code = "validation_failed"
    http_status = 400


class NotFoundError(BusinessError):
    code = "not_found"
    http_status = 404


class ConflictError(BusinessError):
    code = "conflict"
    http_status = 409


class IdempotencyConflictError(ConflictError):
    code = "idempotency_conflict"


class InsufficientFundsError(ConflictError):
    code = "insufficient_funds"

    def __init__(self, message: str = "Solde insuffisant.", *, account_id=None) -> None:
        super().__init__(message)
        self.account_id = account_id


class QuorumNotMetError(ConflictError):
    code = "quorum_not_met"


class StepUpRequiredError(BusinessError):
    """Une validation d'un rang exigeant un step-up OTP a été tentée sans code (ou avec un
    code non vérifié) — HTTP 428 Precondition Required."""
    code = "step_up_required"
    http_status = 428


class PermissionDeniedError(BusinessError):
    """Échec ABAC (au-delà de IsAuthenticated/HasCapability) — ex. agence hors périmètre,
    hors plage horaire autorisée."""
    code = "permission_denied"
    http_status = 403
