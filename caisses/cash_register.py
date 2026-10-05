"""Discipline de caisse journalière (Treasury.jsx/Wallets.jsx, comptes `kind=CAISSE`) —
ouverture avec comptage initial, clôture avec comptage final comparé au solde système
(`TreasuryAccount.balance`, déjà tenu à jour en temps réel par chaque mouvement — pas besoin
d'un journal séparé des mouvements du jour). Un écart au-delà de la tolérance gèle le compte
(`status=BLOQUE`) plutôt que de rester une simple valeur affichée sans conséquence."""
from __future__ import annotations

import logging
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from audit.services import record as audit_record
from common.exceptions import ConflictError, ValidationFailed
from common.parsing import to_decimal

from .models import CaisseConfig, CashRegisterSession, TreasuryAccount

logger = logging.getLogger("agricap")

#: Valeur de repli de la tolérance d'écart de caisse — appliquée UNIQUEMENT tant qu'aucune
#: `CaisseConfig` n'est saisie, et alors avec un warning loggé (exception principe 8). Elle
#: reprend à l'identique l'ancien seuil codé en dur, pour que le comportement soit strictement
#: inchangé jusqu'à ce que le comité configure une valeur.
_DEFAULT_DISCREPANCY_TOLERANCE = Decimal("1")


def discrepancy_tolerance() -> Decimal:
    """Tolérance d'écart de caisse, lue en base (principe 8 : le comité la règle sans
    redéploiement). Aucune `CaisseConfig` en base → repli sur `_DEFAULT_DISCREPANCY_TOLERANCE`
    AVEC warning loggé : le repli est explicite, jamais silencieux."""
    config = CaisseConfig.objects.order_by("-created_at").first()
    if config is not None:
        return config.discrepancy_tolerance
    logger.warning(
        "Aucune CaisseConfig en base — repli sur la tolérance d'écart de caisse par défaut "
        "(%s). Configurer le seuil (comité) pour lever ce repli.",
        _DEFAULT_DISCREPANCY_TOLERANCE,
    )
    return _DEFAULT_DISCREPANCY_TOLERANCE


def _open_session_for(account: TreasuryAccount) -> CashRegisterSession | None:
    return CashRegisterSession.objects.filter(account=account, status=CashRegisterSession.Status.OPEN).first()


@transaction.atomic
def open_session(*, account: TreasuryAccount, opening_count: Decimal | str, by: str = "") -> CashRegisterSession:
    if account.kind != TreasuryAccount.Kind.CAISSE:
        raise ValidationFailed("Seuls les comptes de type Caisse suivent une séance de billetage.")
    if _open_session_for(account):
        raise ConflictError("Une séance de caisse est déjà ouverte pour ce compte.")
    opening_count = to_decimal(opening_count)
    session = CashRegisterSession.objects.create(
        account=account, opened_by=by, opening_count=opening_count, opening_balance_expected=account.balance,
    )
    audit_record(actor=by, action="caisses.register.open", entity_type="CashRegisterSession",
                 entity_id=str(session.pk), details={"openingCount": str(opening_count),
                                                       "expected": str(account.balance)})
    return session


@transaction.atomic
def close_session(*, session: CashRegisterSession, closing_count: Decimal | str, by: str = "") -> CashRegisterSession:
    if session.status != CashRegisterSession.Status.OPEN:
        raise ConflictError("Cette séance de caisse est déjà clôturée.")
    account = TreasuryAccount.objects.select_for_update().get(pk=session.account_id)
    closing_count = to_decimal(closing_count)
    discrepancy = closing_count - account.balance

    session.closed_by = by
    session.closing_count = closing_count
    session.closing_balance_expected = account.balance
    session.discrepancy = discrepancy
    session.closed_at = timezone.now()

    if abs(discrepancy) > discrepancy_tolerance():
        session.status = CashRegisterSession.Status.DISCREPANCY
        account.status = TreasuryAccount.Status.BLOQUE
        account.save(update_fields=["status", "updated_at"])
        audit_record(actor=by, action="caisses.register.discrepancy_freeze", entity_type="TreasuryAccount",
                     entity_id=account.code, details={"discrepancy": str(discrepancy)})
    else:
        session.status = CashRegisterSession.Status.CLOSED

    session.save(update_fields=["status", "closed_by", "closing_count", "closing_balance_expected",
                                 "discrepancy", "closed_at"])
    audit_record(actor=by, action="caisses.register.close", entity_type="CashRegisterSession",
                 entity_id=str(session.pk), details={"closingCount": str(closing_count),
                                                       "discrepancy": str(discrepancy)})
    return session
