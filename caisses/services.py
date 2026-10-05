"""Services monétaires des caisses/portefeuilles clients — rigueur stricte : Decimal,
`transaction.atomic` + verrouillage ordonné (`select_for_update`, no-op sur SQLite mais
prépare Postgres — voir note dans le plan), clé d'idempotence obligatoire, audit."""
from __future__ import annotations

from decimal import Decimal

from django.db import transaction
from django.db.models import F, Sum

from audit.services import record as audit_record
from common import idempotency
from common.exceptions import ConflictError, InsufficientFundsError, NotFoundError, PermissionDeniedError, \
    ValidationFailed
from common.parsing import to_decimal

from . import serializers
from .cash_register import _open_session_for
from .models import CashRegisterSession, ClientWallet, FundTransfer, TreasuryAccount, WalletMovement


def _enforce_staff_ceiling(*, by: str, amount: Decimal) -> None:
    """Plafond individuel par opération (`rbac.StaffProfile.per_operation_ceiling`) — borne
    ce qu'UN staff peut exécuter seul sur `transfer_funds`/`adjust_account` (exécution à un
    seul acteur, sans workflow multi-parties). `by` vide (appel système/test sans acteur) ou
    profil sans plafond configuré -> aucune restriction (comportement historique)."""
    if not by:
        return
    from rbac.models import StaffProfile
    profile = StaffProfile.objects.filter(user_id=by).first()
    if not profile or profile.per_operation_ceiling is None:
        return
    if amount > profile.per_operation_ceiling:
        raise PermissionDeniedError(
            f"Ce montant ({amount}) dépasse votre plafond individuel par opération "
            f"({profile.per_operation_ceiling}) — faites appel à un supérieur."
        )


@transaction.atomic
def create_treasury_account(*, code: str, name: str, kind: str = TreasuryAccount.Kind.CAISSE,
                             currency: str = TreasuryAccount.Currency.USD,
                             agency_id: int | None = None, manager_sub: str = "",
                             initial_amount: Decimal | str = "0", by: str = "") -> TreasuryAccount:
    if not code or not name:
        raise ValidationFailed("Code et nom du compte requis.")
    amount = to_decimal(initial_amount)
    if amount < 0:
        raise ValidationFailed("Le montant initial ne peut pas être négatif.")
    if TreasuryAccount.objects.filter(code=code).exists():
        raise ValidationFailed(f"Le code compte « {code} » existe déjà.")
    account = TreasuryAccount.objects.create(
        code=code, name=name, kind=kind, currency=currency, agency_id=agency_id,
        manager_sub=manager_sub, initial_amount=amount, balance=amount, created_by=by,
    )
    audit_record(actor=by, action="caisses.create_account", entity_type="TreasuryAccount",
                 entity_id=account.code, details={"initial_amount": str(amount)})
    return account


@transaction.atomic
def transfer_funds(*, from_account_id: int, to_account_id: int, amount: Decimal | str,
                    reason: str = "", idempotency_key: str, by: str = "") -> FundTransfer:
    amount = to_decimal(amount)
    if amount <= 0:
        raise ValidationFailed("Le montant du transfert doit être strictement positif.")
    if from_account_id == to_account_id:
        raise ValidationFailed("Les comptes source et destination doivent être distincts.")
    _enforce_staff_ceiling(by=by, amount=amount)

    rec = idempotency.begin(
        scope="caisses.transfer", key=idempotency_key,
        params={"from": from_account_id, "to": to_account_id, "amount": str(amount)}, by=by,
    )

    # Ordre de verrouillage déterministe (trié par pk) : casse le deadlock AB-BA.
    ids = sorted((from_account_id, to_account_id))
    locked = {
        a.pk: a for a in TreasuryAccount.objects.select_for_update().filter(pk__in=ids).order_by("pk")
    }
    source = locked.get(from_account_id)
    destination = locked.get(to_account_id)
    if not source or not destination:
        raise NotFoundError("Compte de trésorerie introuvable.")
    if source.currency != destination.currency:
        raise ValidationFailed("Transfert impossible entre devises différentes (utiliser fx.convert d'abord).")
    if source.balance < amount:
        raise InsufficientFundsError(account_id=source.pk)

    transfer = FundTransfer.objects.create(
        from_account=source, to_account=destination, amount=amount, currency=source.currency,
        reason=reason, idempotency_key=idempotency_key, created_by=by,
    )
    TreasuryAccount.objects.filter(pk=source.pk).update(balance=F("balance") - amount)
    TreasuryAccount.objects.filter(pk=destination.pk).update(balance=F("balance") + amount)

    audit_record(actor=by, action="caisses.transfer", entity_type="FundTransfer", entity_id=str(transfer.pk),
                 details={"from": source.code, "to": destination.code, "amount": str(amount)})
    idempotency.complete(rec, response=serializers.transfer_row(transfer),
                          entity_type="FundTransfer", entity_id=str(transfer.pk))
    return transfer


def _get_or_create_wallet(user, currency: str) -> ClientWallet:
    wallet, _ = ClientWallet.objects.get_or_create(user=user, currency=currency)
    return wallet


@transaction.atomic
def deposit(*, wallet_id: int, amount: Decimal | str, channel: str = "", idempotency_key: str,
            by: str = "") -> WalletMovement:
    from compliance.kyc_levels import balance_cap_for

    amount = to_decimal(amount)
    if amount <= 0:
        raise ValidationFailed("Le montant du dépôt doit être strictement positif.")
    rec = idempotency.begin(scope="caisses.deposit", key=idempotency_key,
                             params={"wallet": wallet_id, "amount": str(amount)}, by=by)
    wallet = ClientWallet.objects.select_for_update().filter(pk=wallet_id).first()
    if not wallet:
        raise NotFoundError("Portefeuille introuvable.")
    cap = balance_cap_for(user=wallet.user)
    if wallet.balance + amount > cap:
        raise ValidationFailed(
            f"Ce dépôt dépasserait le plafond de solde autorisé pour votre palier KYC "
            f"({cap} {wallet.currency})."
        )
    movement = WalletMovement.objects.create(wallet=wallet, kind=WalletMovement.Kind.DEPOSIT, amount=amount)
    ClientWallet.objects.filter(pk=wallet.pk).update(balance=F("balance") + amount)
    audit_record(actor=by, action="caisses.deposit", entity_type="WalletMovement", entity_id=str(movement.pk),
                 details={"amount": str(amount), "channel": channel})
    idempotency.complete(rec, response=serializers.movement_row(movement, verb="Dépôt effectué."),
                          entity_type="WalletMovement", entity_id=str(movement.pk))
    return movement


@transaction.atomic
def withdraw(*, wallet_id: int, amount: Decimal | str, idempotency_key: str, by: str = "") -> WalletMovement:
    amount = to_decimal(amount)
    if amount <= 0:
        raise ValidationFailed("Le montant du retrait doit être strictement positif.")
    rec = idempotency.begin(scope="caisses.withdraw", key=idempotency_key,
                             params={"wallet": wallet_id, "amount": str(amount)}, by=by)
    wallet = ClientWallet.objects.select_for_update().filter(pk=wallet_id).first()
    if not wallet:
        raise NotFoundError("Portefeuille introuvable.")
    if wallet.balance < amount:
        raise InsufficientFundsError(account_id=wallet.pk)
    movement = WalletMovement.objects.create(wallet=wallet, kind=WalletMovement.Kind.WITHDRAW, amount=amount)
    ClientWallet.objects.filter(pk=wallet.pk).update(balance=F("balance") - amount)
    audit_record(actor=by, action="caisses.withdraw", entity_type="WalletMovement", entity_id=str(movement.pk),
                 details={"amount": str(amount)})
    idempotency.complete(rec, response=serializers.movement_row(movement, verb="Retrait effectué."),
                          entity_type="WalletMovement", entity_id=str(movement.pk))
    return movement


@transaction.atomic
def adjust_account(*, account_id: int, amount: Decimal | str, direction: str, reason: str = "",
                    idempotency_key: str, by: str = "") -> TreasuryAccount:
    """Injection/retrait manuel de fonds sur un compte de trésorerie (Wallets.jsx « Ajouter
    flux ») — pas un transfert entre deux comptes internes, une écriture externe (dépôt
    partenaire, ajustement de caisse)."""
    amount = to_decimal(amount)
    if amount <= 0:
        raise ValidationFailed("Le montant doit être strictement positif.")
    if direction not in ("in", "out"):
        raise ValidationFailed("direction doit être 'in' ou 'out'.")
    _enforce_staff_ceiling(by=by, amount=amount)

    rec = idempotency.begin(
        scope="caisses.adjust", key=idempotency_key,
        params={"account": account_id, "amount": str(amount), "direction": direction}, by=by,
    )

    account = TreasuryAccount.objects.select_for_update().filter(pk=account_id).first()
    if not account:
        raise NotFoundError("Compte introuvable.")
    if account.status == TreasuryAccount.Status.BLOQUE:
        raise ConflictError("Ce compte est bloqué (gelé) — aucun ajustement n'est autorisé.")
    if direction == "out" and account.balance < amount:
        raise InsufficientFundsError(account_id=account.pk)

    register_session = _open_session_for(account) if account.kind == TreasuryAccount.Kind.CAISSE else None
    if direction == "in" and register_session and account.daily_ceiling is not None:
        if register_session.cash_in_total + amount > account.daily_ceiling:
            raise ValidationFailed(
                f"Plafond journalier de caisse dépassé ({register_session.cash_in_total + amount} > "
                f"{account.daily_ceiling} {account.currency})."
            )

    delta = amount if direction == "in" else -amount
    TreasuryAccount.objects.filter(pk=account.pk).update(balance=F("balance") + delta)
    account.refresh_from_db()
    if direction == "in" and register_session:
        CashRegisterSession.objects.filter(pk=register_session.pk).update(cash_in_total=F("cash_in_total") + amount)

    audit_record(actor=by, action="caisses.adjust", entity_type="TreasuryAccount", entity_id=account.code,
                 details={"amount": str(amount), "direction": direction, "reason": reason})
    idempotency.complete(rec, response=serializers.account_row(account),
                          entity_type="TreasuryAccount", entity_id=account.code)
    return account


@transaction.atomic
def convert_wallet(*, user, from_currency: str, to_currency: str, amount: Decimal | str,
                    idempotency_key: str, by: str = "") -> dict:
    """Change (FX) client — délègue le taux figé à `fx.services.convert` (jamais recalculé
    a posteriori), débite/crédite les deux portefeuilles dans la même transaction."""
    from fx.services import convert as fx_convert

    amount = to_decimal(amount)
    if amount <= 0:
        raise ValidationFailed("Le montant à convertir doit être strictement positif.")
    if from_currency == to_currency:
        raise ValidationFailed("Devises source et cible identiques.")

    rec = idempotency.begin(
        scope="caisses.convert", key=idempotency_key,
        params={"user": user.sub, "from": from_currency, "to": to_currency, "amount": str(amount)}, by=by,
    )

    source_wallet = ClientWallet.objects.select_for_update().filter(user=user, currency=from_currency).first()
    if not source_wallet or source_wallet.balance < amount:
        raise InsufficientFundsError(account_id=source_wallet.pk if source_wallet else None)
    target_wallet, _ = ClientWallet.objects.get_or_create(user=user, currency=to_currency)

    converted_amount = fx_convert(amount=amount, from_currency=from_currency, to_currency=to_currency, tier="CLIENT")

    ClientWallet.objects.filter(pk=source_wallet.pk).update(balance=F("balance") - amount)
    ClientWallet.objects.filter(pk=target_wallet.pk).update(balance=F("balance") + converted_amount)
    WalletMovement.objects.create(wallet=source_wallet, kind=WalletMovement.Kind.FX_SELL, amount=amount)
    WalletMovement.objects.create(wallet=target_wallet, kind=WalletMovement.Kind.FX_BUY, amount=converted_amount)

    audit_record(actor=by, action="caisses.convert", entity_type="ClientWallet", entity_id=str(source_wallet.pk),
                 details={"from": from_currency, "to": to_currency, "amount": str(amount),
                          "result": str(converted_amount)})

    response = {"detail": "Conversion effectuée.", "amount": float(amount), "result": float(converted_amount),
                "fromCurrency": from_currency, "toCurrency": to_currency}
    idempotency.complete(rec, response=response, entity_type="ClientWallet", entity_id=str(source_wallet.pk))
    return response


#: Préfixe de la référence lisible d'un mouvement de portefeuille.
#: Voir `wallet_movements` pour le raisonnement.
MOVEMENT_REFERENCE_PREFIX = "MVT"


def movement_reference(movement_id: int) -> str:
    """Référence LISIBLE d'un mouvement — `MVT-000004`.

    Ce n'est pas un second identifiant : c'est la mise en forme du seul qui
    existe (la clé primaire). Une vraie référence persistée serait préférable,
    mais elle demanderait une colonne et une reprise de l'historique ; en
    attendant, formater la clé donne au client quelque chose de citable sans
    créer une seconde vérité qui pourrait diverger.

    Pourquoi c'est nécessaire : l'écran servait `m.pk` brut sous l'intitulé
    « ID ». Un client lisait donc « 4 » — un numéro qui ressemble à un rang de
    ligne, que personne n'ose citer au téléphone et qu'un agent ne sait pas
    rechercher. Le zéro-padding et le préfixe en font un identifiant.
    """
    return f"{MOVEMENT_REFERENCE_PREFIX}-{movement_id:06d}"


def _movement_origin(m: WalletMovement) -> dict:
    """Opération à l'origine du mouvement, quand elle est retrouvable.

    Un `WalletMovement` ne porte PAS sa cause métier : son seul qualificatif est
    `kind` (DEPOSIT / WITHDRAW / FX_BUY…). Mais trois opérations pointent vers
    lui, et c'est par là qu'on remonte : un ordre de paiement, une demande de
    retrait, un ordre de régularisation.

    L'intérêt est concret : pour un flux externe, la référence qui compte n'est
    pas la nôtre, c'est celle du FOURNISSEUR — c'est elle que le client donnera
    à son opérateur Mobile Money, et elle seule permet de rapprocher les deux
    côtés. La servir ici évite au client de la demander à l'agence.

    Rend `{}` quand aucune opération ne réclame ce mouvement : un dépôt d'agent
    n'en a pas, et inventer une origine serait pire que de n'en afficher aucune.
    """
    ordre = m.payment_order.all()[:1]
    if ordre:
        o = ordre[0]
        return {
            "origin": "payment_order",
            "originReference": o.reference,
            "providerReference": o.provider_reference or None,
            "counterparty": o.counterparty or None,
        }
    retrait = m.withdrawal_request.all()[:1]
    if retrait:
        return {"origin": "withdrawal_request", "originReference": str(retrait[0].pk)}
    regul = m.regularization_order.all()[:1]
    if regul:
        return {"origin": "regularization_order", "originReference": str(regul[0].pk)}
    return {}


def wallet_movements(*, user) -> list[dict]:
    """Historique des mouvements (tous portefeuilles/devises) de l'utilisateur — lecture
    seule, utilisé par l'onglet « Vue d'ensemble » de ClientWallet.jsx et par la page
    « Historique des transactions ».

    `reference` s'ajoute à `id` sans le remplacer : `id` reste la clé technique dont
    le front se sert pour ses listes, `reference` est ce qu'on montre et ce qu'on cite.
    Les trois `prefetch_related` évitent un N+1 sur la résolution d'origine — sans eux,
    un historique de 200 lignes déclencherait 600 requêtes.
    """
    movements = (
        WalletMovement.objects
        .filter(wallet__user=user)
        .select_related("wallet")
        .prefetch_related("payment_order", "withdrawal_request", "regularization_order")
        .order_by("-created_at")
    )
    return [
        {
            "id": m.pk,
            # `referenceId` est la référence MÉTIER structurée (AG-CD-…), posée à
            # la création et figée. `reference` reste le repli lisible dérivé de
            # la clé pour les lignes antérieures à la colonne, tant que la reprise
            # d'historique (`backfill_references`) n'a pas tourné.
            "referenceId": m.reference_id,
            "reference": m.reference_id or movement_reference(m.pk),
            "date": m.created_at.isoformat(), "type": m.kind, "currency": m.wallet.currency,
            "amount": float(m.amount), "status": m.status,
            **_movement_origin(m),
        }
        for m in movements
    ]


def agency_balance(*, agency_id: int) -> Decimal:
    """Lecture seule — agrégat live utilisé par `agencies.views._row()` (pas de champ
    balance dupliqué sur `Agency`)."""
    total = TreasuryAccount.objects.filter(agency_id=agency_id).aggregate(total=Sum("balance"))["total"]
    return total or Decimal("0")
