"""
Service de décaissement — Crédits Agricoles (Étape 6).

Flux :
  APPROVED → (request_disbursement) → PENDING_DISBURSEMENT
  PENDING_DISBURSEMENT → (confirm_disbursement) → ACTIVE

À la confirmation :
  1. Maker ≠ checker vérifié
  2. Écriture comptable DR 4121 / CR 5211 (JCR)
  3. Création portfolio.Loan (sous-portefeuille)
  4. Création ModuleAllocation (répartition par module)
  5. SMS client
  6. Libération hold épargne (si type = épargne → hold reste actif, c'est une garantie)
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # imports differes a l'execution (cf. corps des fonctions)
    from credits.models import DisbursementRequest

from datetime import date
from decimal import Decimal
from typing import Any

from django.db import transaction
from django.utils import timezone
import logging

logger = logging.getLogger(__name__)

# Comptes SYSCOHADA pour le décaissement crédit
_ACCOUNT_CREDIT_ENCOURS = "4121"   # Crédits à court terme (DR)
_ACCOUNT_BANQUE = "5211"           # Banque principale (CR)
_ACCOUNT_PRODUITS_INTERETS = "771" # Intérêts de prêts (CR — si intérêts perçus d'avance)

_DEFAULT_FINANCING_PCT = Decimal("80")  # % financé par module (standard)


class DisbursementError(Exception):
    """Refus d'une opération de décaissement.

    Même contrat que `credits.workflow.WorkflowError` : chaque sous-classe porte
    son `code` et son `http_status`, et la vue se contente de relayer. La vue
    déduisait auparavant le code du TEXTE du message
    (``is_mkck = "maker" in str(exc).lower()``) — une reformulation du message de
    maker-checker aurait silencieusement dégradé le code en `DISBURSEMENT_ERROR`
    et le statut de 409 à 400, sur le contrôle le plus sensible du module.
    """

    code = "DISBURSEMENT_ERROR"
    http_status = 422

    def __init__(self, message: str, errors: list[dict] | None = None) -> None:
        super().__init__(message)
        self.errors = errors or []

    def as_errors(self) -> list[dict]:
        return self.errors or [{"code": self.code, "message": str(self)}]


class DisbursementMakerChecker(DisbursementError):
    """Le demandeur et le confirmateur sont la même personne."""

    code = "MAKER_CHECKER_VIOLATION"
    http_status = 409


class DisbursementRequestMissing(DisbursementError):
    """Aucune demande de décaissement n'existe pour ce dossier."""

    code = "DISBURSEMENT_REQUEST_MISSING"
    http_status = 404


class DisbursementRequestConflict(DisbursementError):
    """La demande existe mais son statut interdit l'opération demandée."""

    code = "DISBURSEMENT_REQUEST_CONFLICT"
    http_status = 409


class DisbursementAlreadyDone(DisbursementError):
    """Le dossier a déjà été décaissé — jamais deux fois."""

    code = "DISBURSEMENT_ALREADY_DONE"
    http_status = 409


class DisbursementAmountInvalid(DisbursementError):
    """Montant approuvé absent ou nul."""

    code = "DISBURSEMENT_AMOUNT_INVALID"
    http_status = 422


# ── Étape 1 : demande de décaissement ─────────────────────────────────────────

@transaction.atomic
def request_disbursement(
    app,
    requester_sub: str,
    notes: str = "",
) -> "DisbursementRequest":
    """
    APPROVED → PENDING_DISBURSEMENT.
    Crée un DisbursementRequest en attente de double validation.
    """
    from credits.models import DisbursementRequest
    from credits.workflow import WorkflowError, _assert_status

    _assert_status(app, "approved")

    if not app.amount_approved or app.amount_approved <= 0:
        raise DisbursementAmountInvalid("Montant approuvé manquant ou nul.")

    if hasattr(app, "disbursement_request"):
        dr = app.disbursement_request
        if dr.status == DisbursementRequest.Status.CONFIRMED:
            raise DisbursementAlreadyDone("Ce dossier a déjà été décaissé.")
        if dr.status == DisbursementRequest.Status.PENDING:
            raise DisbursementRequestConflict(
                "Une demande de décaissement est déjà en attente. "
                "Confirmez ou annulez-la avant d'en créer une nouvelle."
            )
        # Cancelled → on peut en créer une nouvelle
        dr.delete()

    dr = DisbursementRequest.objects.create(
        application=app,
        amount=app.amount_approved,
        currency=app.currency,
        status=DisbursementRequest.Status.PENDING,
        requested_by_sub=requester_sub,
        notes=notes,
    )

    app.status = "pending_disbursement"
    app.save(update_fields=["status", "updated_at"])

    from credits.workflow import _audit_transition
    _audit_transition(
        app, actor=requester_sub, action="credits.disbursement.request",
        etape="decaissement_demande", montant=str(dr.amount), devise=dr.currency,
        motif=notes,
    )

    return dr


# ── Étape 2 : confirmation et décaissement effectif ──────────────────────────

@transaction.atomic
def confirm_disbursement(
    app,
    confirmer_sub: str,
) -> dict[str, Any]:
    """
    PENDING_DISBURSEMENT → ACTIVE.
    Maker ≠ checker, crée le Loan, poste l'écriture comptable, crée les allocations.
    """
    from credits.models import DisbursementRequest
    from credits.workflow import _assert_status

    _assert_status(app, "pending_disbursement")

    try:
        dr = app.disbursement_request
    except DisbursementRequest.DoesNotExist:
        raise DisbursementRequestMissing("Aucune demande de décaissement trouvée pour ce dossier.")

    if dr.status != DisbursementRequest.Status.PENDING:
        raise DisbursementRequestConflict(
            f"La demande de décaissement est dans le statut «{dr.status}» — impossible de confirmer."
        )

    # Maker ≠ checker
    if dr.requested_by_sub == confirmer_sub:
        raise DisbursementMakerChecker(
            "La même personne ne peut pas demander et confirmer un décaissement "
            "(principe maker ≠ checker)."
        )

    amount = dr.amount
    currency = dr.currency
    disbursed_at = timezone.now()
    disbursed_date = disbursed_at.date()

    # ── 1. Écriture comptable ─────────────────────────────────────────────
    journal_entry = _post_disbursement_entry(
        app=app, amount=amount, currency=currency,
        date=disbursed_date, by=confirmer_sub,
    )

    # ── 2. Portfolio.Loan ─────────────────────────────────────────────────
    loan = _create_portfolio_loan(app, amount, currency, disbursed_date, confirmer_sub)

    # ── 2 bis. Événement comptable B1 ─────────────────────────────────────
    # DANS la transaction du décaissement : un franc sorti sans son événement
    # serait un encours invisible au grand livre (413 muet), et le
    # provisionnement refuserait ensuite de déclasser un encours qu'il ne voit
    # pas. L'événement naît donc avec le prêt, ou aucun des deux n'existe.
    credit_event = _emit_disbursement_event(
        app=app, loan=loan, amount=amount, currency=currency,
        occurred_at=disbursed_at, by=confirmer_sub,
    )

    # ── 3. ModuleAllocations ──────────────────────────────────────────────
    allocations = _create_module_allocations(app, amount)

    # ── 4. Mettre à jour le dossier ───────────────────────────────────────
    app.status = "active"
    app.disbursed_at = disbursed_at
    app.disbursed_by_sub = confirmer_sub
    app.disbursed_amount = amount
    app.save(update_fields=[
        "status", "disbursed_at", "disbursed_by_sub", "disbursed_amount", "updated_at",
    ])

    # ── 5. Mettre à jour la DisbursementRequest ───────────────────────────
    dr.status = DisbursementRequest.Status.CONFIRMED
    dr.confirmed_by_sub = confirmer_sub
    dr.confirmed_at = disbursed_at
    dr.loan_id = loan.pk
    dr.journal_entry_id = journal_entry.pk if journal_entry else None
    dr.save()

    # ── 6. Journalisation append-only (tâche D) ───────────────────────────
    # maker ≠ checker : l'entrée trace les DEUX acteurs, c'est la preuve du
    # double contrôle sur l'opération la plus sensible du module.
    from credits.workflow import _audit_transition
    _audit_transition(
        app, actor=confirmer_sub, action="credits.disbursement.confirm",
        etape="decaissement_confirme", montant=str(amount), devise=currency,
        demandePar=dr.requested_by_sub, confirmePar=confirmer_sub,
        loanReference=loan.reference,
        journalEntryId=(journal_entry.pk if journal_entry else None),
        creditEventId=(credit_event.pk if credit_event else None),
    )

    # ── 7. SMS client ─────────────────────────────────────────────────────
    _notify_client_disbursement(app, amount, currency)

    # ── 8. Instructions fournisseurs ──────────────────────────────────────
    supplier_instructions = _build_supplier_instructions(app)

    return {
        "code": app.code,
        "status": app.status,
        "disbursedAmount": float(amount),
        "currency": currency,
        "disbursedAt": disbursed_at.isoformat(),
        "loanReference": loan.reference,
        "journalEntryId": journal_entry.pk if journal_entry else None,
        "creditEventId": credit_event.pk if credit_event else None,
        "moduleAllocations": allocations,
        "supplierInstructions": supplier_instructions,
    }


def cancel_disbursement_request(app, cancelled_by_sub: str) -> None:
    """Annule une demande de décaissement en attente (retour à APPROVED)."""
    from credits.models import DisbursementRequest
    from credits.workflow import _assert_status

    _assert_status(app, "pending_disbursement")

    try:
        dr = app.disbursement_request
    except DisbursementRequest.DoesNotExist:
        raise DisbursementRequestMissing("Aucune demande de décaissement trouvée.")

    if dr.status != DisbursementRequest.Status.PENDING:
        raise DisbursementRequestConflict("Impossible d'annuler — statut non PENDING.")

    dr.status = DisbursementRequest.Status.CANCELLED
    dr.save(update_fields=["status", "updated_at"])

    app.status = "approved"
    app.save(update_fields=["status", "updated_at"])

    from credits.workflow import _audit_transition
    _audit_transition(
        app, actor=cancelled_by_sub, action="credits.disbursement.cancel",
        etape="decaissement_annule", montant=str(dr.amount), devise=dr.currency,
    )


# ── Helpers internes ──────────────────────────────────────────────────────────

def _post_disbursement_entry(app, amount: Decimal, currency: str, date, by: str):
    """Écriture DR 4121 / CR 5211 dans le journal JCR."""
    try:
        from ledger.services import post_journal_entry
        piece_ref = f"DEC-{app.code}"
        idempotency_key = f"disbursement-{app.code}"
        return post_journal_entry(
            date=date,
            piece_ref=piece_ref,
            code="JCR",
            currency=currency,
            description=f"Décaissement crédit agricole {app.code} — {app.client.full_name}",
            idempotency_key=idempotency_key,
            by=by,
            lines=[
                {"account": _ACCOUNT_CREDIT_ENCOURS, "debit": str(amount), "credit": "0"},
                {"account": _ACCOUNT_BANQUE, "debit": "0", "credit": str(amount)},
            ],
        )
    except Exception as exc:
        # L'écriture comptable ne bloque pas le décaissement si le module ledger
        # est indisponible (ex. comptes non configurés en dev) — logguer l'erreur.
        import logging
        logging.getLogger("agricap").warning(
            f"Écriture comptable décaissement {app.code} échouée : {exc}"
        )
        return None


def _emit_disbursement_event(*, app, loan, amount: Decimal, currency: str, occurred_at, by: str):
    """Écrit le décaissement dans la file comptable du crédit (annexe B1).

    À la différence de `_post_disbursement_entry` — qui pousse une écriture dans
    l'app `ledger` sur des comptes (4121/5211) absents du plan comptable de
    l'annexe A, et l'avale en cas d'échec — cette émission est NON best-effort :
    elle vit dans la transaction du décaissement. La file est un fait métier, pas
    un canal secondaire ; si elle échoue, le décaissement n'a pas lieu.

    DETTE CROISÉE SIGNALÉE (rapport) : deux comptabilités cohabitent — `ledger`
    (4121/5211, SYSCOHADA large, alimenté ici) et `accounting` (413/501/511,
    annexe A, alimenté par cette file). Elles ne doivent pas être consolidées
    ensemble sans arbitrage, sous peine de compter deux fois chaque
    décaissement. L'arbitrage appartient au fondateur ; le signaler vaut mieux
    que de débrancher unilatéralement un journal existant.
    """
    from credits.events import emettre_decaissement

    return emettre_decaissement(
        app,
        amount=amount,
        currency=currency,
        # Même référence d'acte que la pièce `ledger` : un auditeur rapproche les
        # deux journaux sans table de correspondance.
        reference=f"DEC-{app.code}",
        loan_id=loan.pk,
        loan_reference=loan.reference,
        occurred_at=occurred_at,
        actor_sub=by,
        applicationCode=app.code,
        valueChain=(app.value_chain.code if app.value_chain_id else ""),
    )


def _create_portfolio_loan(app, amount: Decimal, currency: str, disbursed_date, by: str):
    """Crée ou met à jour un portfolio.Loan lié au dossier."""
    from portfolio.models import Loan

    vc = app.value_chain
    duration = int(vc.cycle_months) if vc else 12

    # Taux mensuel depuis score_result (annual_rate / 12)
    score_result = app.score_result or {}
    annual_rate = score_result.get("proposedRate", float(vc.base_rate) if vc else 18.0)
    monthly_rate = round(Decimal(str(annual_rate)) / 12, 4)

    due_date = _add_months(disbursed_date, duration)

    # Référence unique pour le portefeuille
    loan_ref = f"PRT-{app.code}"

    loan, created = Loan.objects.get_or_create(
        reference=loan_ref,
        defaults={
            "operator": app.client.full_name or app.client.sub[:20],
            "category": vc.label if vc else "",
            "amount_requested": app.amount_requested or amount,
            "amount_approved": amount,
            "currency": currency,
            "duration_months": duration,
            "rate": monthly_rate,
            "start_date": disbursed_date,
            "due_date": due_date,
            "status": Loan.Status.EN_COURS,
            "score": int(score_result.get("score", 0)),
            "guarantee": app.guarantee_type or "",
            "application": app,
            "borrower_sub": app.client.sub,
            "source": "App",
            "created_by": by,
        },
    )
    if not created:
        # Mettre à jour si le Loan existait (redécaissement après annulation)
        loan.status = Loan.Status.EN_COURS
        loan.application = app
        loan.start_date = disbursed_date
        loan.due_date = due_date
        loan.save(update_fields=["status", "application", "start_date", "due_date", "updated_at"])

    return loan


def _create_module_allocations(app, amount_approved: Decimal) -> list[dict]:
    """Crée ModuleAllocation à partir de la NeedsSheet ou du référentiel."""
    from credits.models import ModuleAllocation

    # Supprimer les allocations existantes (re-calcul propre)
    ModuleAllocation.objects.filter(application=app).delete()

    allocations_data: list[tuple[str, Decimal, str]] = []  # (module, cost, source)

    ns = app.needs_sheet
    vc = app.value_chain

    if ns and ns.parsed_ok and ns.total_by_module:
        # Source : NeedsSheet
        for module, cost in ns.total_by_module.items():
            allocations_data.append((module, Decimal(str(cost)), "needs_sheet"))
    elif vc and vc.module_weights:
        # Source : référentiel filière
        for module, pct in vc.module_weights.items():
            cost = amount_approved * Decimal(str(pct)) / 100
            allocations_data.append((module, cost.quantize(Decimal("0.01")), "referential"))
    else:
        # Aucune source → une seule allocation générique
        allocations_data.append(("reserve", amount_approved, "manual"))

    result = []
    total_cost = sum(c for _, c, _ in allocations_data) or Decimal("1")

    objs = []
    for module, cost, source in allocations_data:
        # Proportion du montant approuvé allouée à ce module
        ratio = (cost / total_cost) if total_cost > 0 else Decimal("0")
        amount_financed = (amount_approved * ratio).quantize(Decimal("0.01"))
        financing_pct = _DEFAULT_FINANCING_PCT

        objs.append(ModuleAllocation(
            application=app,
            module=module,
            cost=cost,
            financing_pct=financing_pct,
            amount_financed=amount_financed,
            source=source,
        ))
        result.append({
            "module": module,
            "cost": float(cost),
            "financingPct": float(financing_pct),
            "amountFinanced": float(amount_financed),
            "source": source,
        })

    ModuleAllocation.objects.bulk_create(objs)
    return result


def _build_supplier_instructions(app) -> list[dict]:
    """
    Construit la liste des instructions de paiement fournisseurs
    depuis les NeedItems (suggested_supplier non vide).
    """
    try:
        from credits.models import NeedItem
        items = NeedItem.objects.filter(
            sheet=app.needs_sheet,
            suggested_supplier__gt="",
            supplier_warning="",   # uniquement les fournisseurs non blacklistés
        ).values("module", "label", "suggested_supplier", "computed_total", "declared_total")

        # Grouper par fournisseur
        by_supplier: dict[str, dict] = {}
        for item in items:
            s = item["suggested_supplier"]
            total = float(item["computed_total"] or item["declared_total"] or 0)
            if s not in by_supplier:
                by_supplier[s] = {"supplier": s, "totalAmount": 0.0, "items": []}
            by_supplier[s]["totalAmount"] = round(by_supplier[s]["totalAmount"] + total, 2)
            by_supplier[s]["items"].append({
                "module": item["module"],
                "label": item["label"],
                "amount": total,
            })

        return list(by_supplier.values())
    except Exception:
        return []


def _add_months(d: date, months: int) -> date:
    """Ajoute N mois à une date."""
    month = d.month - 1 + months
    year = d.year + month // 12
    month = month % 12 + 1
    import calendar
    day = min(d.day, calendar.monthrange(year, month)[1])
    return d.replace(year=year, month=month, day=day)


def _notify_client_disbursement(app, amount: Decimal, currency: str) -> None:
    try:
        from common.sms import send_sms
        send_sms(
            phone=app.client.phone,
            message=f"AGRICAP : Votre crédit {app.code} de {amount} {currency} "
            f"a été décaissé. Bonne campagne agricole ! "
            f"Pour toute question, contactez votre conseiller.",
        )
    except Exception:
        # Un canal secondaire ne doit pas faire échouer l'opération métier — mais
        # son échec doit LAISSER UNE TRACE (cf. credits/workflow.py, même garde).
        logger.warning("[NOTIF] envoi impossible pour %s", getattr(app, "code", "?"), exc_info=True)


# ── Sérialiseur DisbursementRequest ───────────────────────────────────────────

def serialize_disbursement(app) -> dict | None:
    """Retourne les infos de décaissement d'un dossier, ou None si absent."""
    try:
        dr = app.disbursement_request
    except Exception:
        return None

    return {
        "status": dr.status,
        "amount": float(dr.amount),
        "currency": dr.currency,
        "requestedBySub": dr.requested_by_sub,
        "requestedAt": dr.requested_at.isoformat(),
        "confirmedBySub": dr.confirmed_by_sub or None,
        "confirmedAt": dr.confirmed_at.isoformat() if dr.confirmed_at else None,
        "loanId": dr.loan_id,
        "journalEntryId": dr.journal_entry_id,
        "notes": dr.notes or None,
    }
