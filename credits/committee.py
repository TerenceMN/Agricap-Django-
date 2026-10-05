"""
Comité de crédit — décision collégiale à quorum (CONVENTIONS.md §7.1.4, CONTRAT §2).

Les dossiers dont le montant demandé dépasse le plafond de délégation d'agence ne
se décident pas par une approbation individuelle : ils passent en comité. Chaque
membre vote (`CommitteeVote`, append-only), le quorum est paramétrable via
`InstitutionConfig` (principe 8), et la décision collégiale — dès que le quorum
d'un sens est atteint — se matérialise par une transition de la machine à états
`credits.workflow` (jamais une écriture directe de `status`, §5).

Le procès-verbal est la séquence des votes en base, doublée d'une entrée de
journal d'audit à chaque vote et à la résolution (principe 3). maker ≠ checker :
celui qui a soumis ou initié le dossier ne siège pas dessus, et l'approbateur
final (dernier votant) est nécessairement distinct du soumetteur — ce que
`workflow.approve` re-vérifie.
"""
from __future__ import annotations

import logging
from decimal import Decimal

from django.conf import settings
from django.db import IntegrityError, transaction

logger = logging.getLogger(__name__)

#: Quorum de secours si rien n'est configuré (principe 8, valeur de repli loggée).
DEFAULT_QUORUM = 3


# ── Exceptions (convention `credits.workflow.WorkflowError`) ──────────────────

class CommitteeError(Exception):
    """Refus d'une opération de comité. `code` et `http_status` portés par la classe."""

    code = "COMMITTEE_ERROR"
    http_status = 422

    def __init__(self, message: str, errors: list[dict] | None = None) -> None:
        super().__init__(message)
        self.errors = errors or []

    def as_errors(self) -> list[dict]:
        return self.errors or [{"code": self.code, "message": str(self)}]


class CommitteeNotRequired(CommitteeError):
    """Dossier sous le plafond de délégation : il se décide sans comité."""

    code = "COMMITTEE_NOT_REQUIRED"


class CommitteeStateError(CommitteeError):
    """Le statut du dossier n'autorise pas de vote de comité."""

    code = "COMMITTEE_STATE_INVALID"
    http_status = 409


class CommitteeMakerChecker(CommitteeError):
    """Le votant a soumis ou initié le dossier (maker ≠ checker)."""

    code = "MAKER_CHECKER_VIOLATION"
    http_status = 409


class CommitteeAlreadyVoted(CommitteeError):
    """Ce membre a déjà voté sur ce dossier — un vote par membre (append-only)."""

    code = "COMMITTEE_ALREADY_VOTED"
    http_status = 409


class CommitteeInvalidDecision(CommitteeError):
    """Sens de vote absent/inconnu, ou motif manquant."""

    code = "COMMITTEE_DECISION_INVALID"


# ── Paramètres (principe 8) ───────────────────────────────────────────────────

def committee_quorum() -> int:
    """Quorum du comité, paramétrable via `InstitutionConfig` (principe 8).

    Source normale : la feuille de gouvernance (feuille 16) chargée dans
    `InstitutionConfig.raw['quorum_comite']`. À défaut, repli sur
    `settings.CREDIT_COMMITTEE_QUORUM` puis `DEFAULT_QUORUM`, avec warning loggé —
    l'exception « valeurs par défaut de secours » du principe 8. On ne code jamais
    un quorum en dur silencieux : un comité qui décide sous un quorum non voulu
    n'est pas opposable.
    """
    try:
        from referentiel.models import InstitutionConfig
        cfg = InstitutionConfig.active()
        raw = cfg.raw or {}
        brut = raw.get("quorum_comite", raw.get("quorum"))
        if brut not in (None, ""):
            q = int(brut)
            if q >= 1:
                return q
            logger.warning("Quorum comité configuré < 1 (%s) — ignoré.", brut)
    except Exception as exc:  # noqa: BLE001 — la config ne doit jamais casser le vote
        logger.warning("Quorum comité : InstitutionConfig illisible (%s).", exc)

    q = int(getattr(settings, "CREDIT_COMMITTEE_QUORUM", DEFAULT_QUORUM) or DEFAULT_QUORUM)
    q = q if q >= 1 else DEFAULT_QUORUM
    logger.warning(
        "Quorum comité absent d'InstitutionConfig — repli sur %s (à paramétrer "
        "dans la feuille de gouvernance `quorum_comite`).", q,
    )
    return q


def committee_threshold_usd() -> float:
    """Plafond de délégation d'agence au-delà duquel le comité est requis (USD)."""
    limits = getattr(settings, "CREDIT_DELEGATION_USD", {}) or {}
    return float(limits.get("gest_zone") or 25_000)


def _amount_usd(app) -> float:
    from credits.workflow import _to_usd
    montant = app.amount_requested or Decimal("0")
    return _to_usd(Decimal(str(montant)), app.currency)


def requires_committee(app) -> bool:
    """True si le montant demandé dépasse le plafond de délégation d'agence."""
    return _amount_usd(app) >= committee_threshold_usd()


# ── Lecture (procès-verbal) ───────────────────────────────────────────────────

def _tally(app):
    from credits.models import CommitteeVote

    votes = list(app.committee_votes.all())
    approve = sum(1 for v in votes if v.decision == CommitteeVote.Decision.APPROVE)
    reject = sum(1 for v in votes if v.decision == CommitteeVote.Decision.REJECT)
    return votes, approve, reject


def _resolution(app, approve: int, reject: int, quorum: int):
    """(resolved, decision) — d'après le décompte ET le statut atteint du dossier."""
    if approve >= quorum:
        return True, "approve"
    if reject >= quorum:
        return True, "reject"
    if app.status in ("approved", "pending_disbursement", "active"):
        return True, "approve"
    if app.status == "rejected":
        return True, "reject"
    return False, None


def _serialize_vote(v) -> dict:
    return {
        "voter": v.voter_sub,
        "decision": v.decision,
        "comment": v.comment,
        "conditions": v.conditions or None,
        "votedAt": v.created_at.isoformat(),
    }


def votes_summary(app) -> dict:
    """Payload `GET committee-votes/` — quorum, votes, décompte, résolution."""
    votes, approve, reject = _tally(app)
    quorum = committee_quorum()
    resolved, decision = _resolution(app, approve, reject, quorum)
    return {
        "applicationCode": app.code,
        "quorum": quorum,
        "requiresCommittee": requires_committee(app),
        "thresholdUsd": committee_threshold_usd(),
        "votes": [_serialize_vote(v) for v in votes],
        "tally": {"approve": approve, "reject": reject},
        "resolved": resolved,
        "decision": decision,
    }


# ── Vote et résolution ────────────────────────────────────────────────────────

@transaction.atomic
def cast_vote(app, *, voter_sub: str, decision: str, comment: str = "",
              conditions: str = "", voter_roles: list[str] | None = None) -> dict:
    """Enregistre un vote de comité et, si le quorum est atteint, résout le dossier.

    Append-only (principe 3). Un membre ne vote qu'une fois. Dès qu'un sens atteint
    le quorum, la transition passe par `workflow.approve` / `workflow.reject`.
    """
    from credits.models import CommitteeVote

    decision = (decision or "").strip().lower()
    if decision not in (CommitteeVote.Decision.APPROVE, CommitteeVote.Decision.REJECT):
        raise CommitteeInvalidDecision(
            "Sens de vote invalide : attendu « approve » ou « reject »."
        )
    if not (comment or "").strip():
        raise CommitteeInvalidDecision(
            "Chaque vote du comité exige un motif : le commentaire est obligatoire."
        )

    if app.status != "in_analysis":
        raise CommitteeStateError(
            "Le comité ne vote que sur un dossier en analyse (statut courant : "
            f"« {app.status} »)."
        )
    if not requires_committee(app):
        raise CommitteeNotRequired(
            "Ce dossier est sous le plafond de délégation d'agence : il se décide "
            "par approbation simple, pas en comité de crédit."
        )

    # maker ≠ checker : ni le soumetteur ni l'initiateur ne votent la décision.
    if voter_sub and voter_sub in {app.submitted_by_sub, app.initiated_by_sub}:
        raise CommitteeMakerChecker(
            "Vous avez soumis ou initié ce dossier : vous ne pouvez pas voter sa "
            "décision en comité (maker ≠ checker)."
        )

    quorum = committee_quorum()

    # Un vote déjà posé par ce membre → 409, sans corrompre la transaction (savepoint).
    try:
        with transaction.atomic():
            vote = CommitteeVote.objects.create(
                application=app,
                voter_sub=voter_sub or "",
                decision=decision,
                comment=comment.strip(),
                conditions=(conditions or "").strip(),
                quorum_at_vote=quorum,
            )
    except IntegrityError as exc:
        raise CommitteeAlreadyVoted(
            "Vous avez déjà voté sur ce dossier : un membre ne vote qu'une fois "
            "(le procès-verbal est append-only)."
        ) from exc

    from credits.workflow import _audit_transition
    _audit_transition(
        app, actor=voter_sub or "", action="credits.committee.vote",
        etape="comite_vote", decision=decision, motif=comment.strip(),
        conditions=(conditions or "").strip() or None, quorum=quorum,
    )

    votes, approve, reject = _tally(app)
    resolved = False
    outcome = None
    if approve >= quorum:
        _resolve_approve(app, votes, voter_sub, voter_roles)
        resolved, outcome = True, "approve"
    elif reject >= quorum:
        _resolve_reject(app, votes, voter_sub)
        resolved, outcome = True, "reject"

    return {
        "vote": _serialize_vote(vote),
        "tally": {"approve": approve, "reject": reject},
        "quorum": quorum,
        "resolved": resolved,
        "decision": outcome,
    }


def _proces_verbal(votes, outcome: str) -> str:
    entete = "APPROBATION" if outcome == "approve" else "REJET"
    lignes = [f"Décision du comité de crédit : {entete} (quorum atteint)."]
    for v in votes:
        cond = f" — conditions : {v.conditions}" if v.conditions else ""
        lignes.append(f"• {v.voter_sub} : {v.decision} — {v.comment}{cond}")
    return "\n".join(lignes)


def _audit_pv(app, votes, outcome: str, actor: str) -> None:
    """Entrée de journal du procès-verbal — la trace collégiale de la décision."""
    from credits.workflow import _audit_transition
    _audit_transition(
        app, actor=actor or "", action="credits.committee.resolved",
        etape="comite_decision", decision=outcome, quorum=committee_quorum(),
        procesVerbal=[_serialize_vote(v) for v in votes],
    )


def _resolve_approve(app, votes, voter_sub, voter_roles) -> None:
    from credits.workflow import approve as wf_approve

    pv = _proces_verbal(votes, "approve")
    montant = app.amount_approved or app.amount_requested or Decimal("0")
    wf_approve(
        app,
        approver_sub=voter_sub or "",
        amount_approved=Decimal(str(montant)),
        comment=pv,
        approver_roles=voter_roles or [],
    )
    _audit_pv(app, votes, "approve", voter_sub)


def _resolve_reject(app, votes, voter_sub) -> None:
    from credits.workflow import reject as wf_reject

    pv = _proces_verbal(votes, "reject")
    wf_reject(
        app,
        rejector_sub=voter_sub or "",
        reason_code="autre",
        comment=pv,
    )
    _audit_pv(app, votes, "reject", voter_sub)
