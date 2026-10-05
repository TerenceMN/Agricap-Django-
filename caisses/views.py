"""API des caisses/comptes de trésorerie (Wallets.jsx, Treasury.jsx) et portefeuilles
clients (ClientWallet.jsx).

Deux populations d'endpoints qu'il ne faut jamais confondre :

* la **trésorerie de l'institution** (`TreasuryAccount`, séances de caisse) — soldes des
  caisses et coffres par agence, plafonds journaliers, gérants. Interne : `IsStaff` cumulé
  à la capacité. `HasCapability("read")` seul ne protégeait rien, les rôles clients le
  portant tous ;
* le **portefeuille du client** (`ClientWallet`) — les vues `*_mine`, gardées par
  `IsAuthenticated` et filtrées par `user=request.user`. Elles restent inchangées : c'est
  l'argent de l'appelant, et le filtre est dans la requête, pas dans un identifiant d'URL.
"""
from __future__ import annotations

from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

from django.conf import settings

from accounts.permissions import IsStaff
from audit.services import record as audit_record
from common import idempotency, makuta
from common.money import est_au_centime
from common.makuta import MakutaConfigurationError
from common.parsing import to_decimal, to_int
from rbac.permissions import CapaciteSelonMethode, HasCapability
from rbac.role_registry import get_role

#: Taille de page par défaut de `GET /accounts?meta=1` (borne haute `MAX_PAGE`) — aligné sur
#: le patron de pagination du back-office comptable (`accounting.views._pagination`).
DEFAULT_PAGE = 50
MAX_PAGE = 200


def _pagination(request) -> tuple[int, int]:
    """Borne (limit, offset) d'une lecture paginée — même règle que `accounting.views`
    (principe 6 : ne pas réinventer un troisième parseur de pagination)."""
    limit = min(to_int(request.GET.get("limit"), DEFAULT_PAGE) or DEFAULT_PAGE, MAX_PAGE)
    offset = max(to_int(request.GET.get("offset"), 0), 0)
    return limit, offset

from . import cash_register, channels, partner_link, payments, regularization, serializers, services, \
    withdrawal_tiers
from .models import CashRegisterSession, ClientWallet, PaymentOrder, RegularizationOrder, TreasuryAccount, \
    WithdrawalRequest


def _require(request, capability: str) -> bool:
    return bool(getattr(get_role(getattr(request.user, "role", "")), capability, False))


@api_view(["GET", "POST"])
@permission_classes([IsStaff, CapaciteSelonMethode(GET="read", POST="create")])
def accounts(request):
    if request.method == "GET":
        qs = TreasuryAccount.objects.all()
        agency_id = request.GET.get("agency")
        if agency_id:
            qs = qs.filter(agency_id=agency_id)
        # Pagination ADDITIVE et rétro-compatible (patron `?meta=1` d'`audit.views.entries`).
        # Sans `?meta=1`, la réponse reste un TABLEAU BRUT : `Caisses.jsx`/`caissesWire.ts` et
        # `Wallets.jsx` consomment cette forme telle quelle, elle ne doit pas changer. Avec
        # `?meta=1` (+ `?limit=&offset=`), on enveloppe et on expose `totalRows` DANS LE CORPS
        # (jamais un header `X-Total-Rows` : illisible en JS sans Access-Control-Expose-Headers,
        # ce qui casse en prod derrière un autre proxy). Le filtre `?agency=` reste actif ici.
        if request.GET.get("meta") in ("1", "true", "yes"):
            total = qs.count()
            limit, offset = _pagination(request)
            return Response({
                "results": [serializers.account_row(a) for a in qs[offset:offset + limit]],
                "totalRows": total,
                "limit": limit,
                "offset": offset,
            })
        return Response([serializers.account_row(a) for a in qs])
    if not _require(request, "create"):
        return Response({"detail": "Capacité requise : create."}, status=403)
    data = request.data or {}
    account = services.create_treasury_account(
        code=data.get("code", ""), name=data.get("name", ""), kind=data.get("kind", "CAISSE"),
        currency=data.get("currency", "USD"), agency_id=data.get("agencyId"),
        manager_sub=data.get("manager", ""), initial_amount=data.get("initialAmount", "0"),
        by=getattr(request.user, "sub", ""),
    )
    return Response(serializers.account_row(account), status=201)


@api_view(["GET", "PATCH"])
@permission_classes([IsStaff, CapaciteSelonMethode(GET="read", PATCH="create")])
def account_detail(request, code):
    account = TreasuryAccount.objects.filter(code=code).first()
    if not account:
        return Response({"detail": "Compte introuvable."}, status=404)
    if request.method == "PATCH":
        if not _require(request, "create"):
            return Response({"detail": "Capacité requise : create."}, status=403)
        data = request.data or {}
        for field, model_field in (("name", "name"), ("manager", "manager_sub"), ("scope", "scope"),
                                    ("riskLevel", "risk_level"), ("status", "status")):
            if field in data:
                setattr(account, model_field, data[field])
        account.save()
    return Response(serializers.account_row(account))


@api_view(["POST"])
@permission_classes([IsStaff, HasCapability("validate")])
def account_action(request, code):
    # `IsStaff` cumulé à `validate` (jamais `validate` seul) : AGIR sur une caisse ne peut pas
    # être plus permissif que la LIRE. `accounts`/`account_register_sessions` exigent
    # `[IsStaff, ...]` ; sans `IsStaff` ici, un rôle non-staff porteur de `validate` (possible
    # via un `RoleOverride` de type Client) pourrait transférer/geler une caisse qu'il ne peut
    # même pas consulter. Les rôles caisse légitimes (`gest_caisse`…) portent `is_staff_role`.
    account = TreasuryAccount.objects.filter(code=code).first()
    if not account:
        return Response({"detail": "Compte introuvable."}, status=404)
    data = request.data or {}
    action = data.get("action")
    by = getattr(request.user, "sub", "")
    if action == "transfer":
        to_account = TreasuryAccount.objects.filter(code=data.get("toCode")).first()
        if not to_account:
            return Response({"detail": "Compte destination introuvable."}, status=404)
        key = data.get("idempotencyKey")
        if not key:
            return Response({"detail": "idempotencyKey requis."}, status=400)
        try:
            transfer = services.transfer_funds(
                from_account_id=account.pk, to_account_id=to_account.pk,
                amount=data.get("amount", "0"), reason=data.get("reason", ""),
                idempotency_key=key, by=by,
            )
        except idempotency.IdempotentReplay as exc:
            return idempotency.replay_response(exc)
        return Response(serializers.transfer_row(transfer))
    if action == "add_flow":
        key = data.get("idempotencyKey")
        if not key:
            return Response({"detail": "idempotencyKey requis."}, status=400)
        try:
            account = services.adjust_account(
                account_id=account.pk, amount=data.get("amount", "0"), direction=data.get("direction", "in"),
                reason=data.get("reason", ""), idempotency_key=key, by=by,
            )
        except idempotency.IdempotentReplay as exc:
            return idempotency.replay_response(exc)
        return Response(serializers.account_row(account))
    if action == "register_open":
        session = cash_register.open_session(account=account, opening_count=data.get("openingCount", "0"), by=by)
        return Response(serializers.session_row(session), status=201)
    if action == "register_close":
        session = cash_register._open_session_for(account)
        if not session:
            return Response({"detail": "Aucune séance de caisse ouverte pour ce compte."}, status=404)
        session = cash_register.close_session(session=session, closing_count=data.get("closingCount", "0"), by=by)
        return Response(serializers.session_row(session))
    if action == "set_daily_ceiling":
        account.daily_ceiling = data.get("dailyCeiling") or None
        account.save(update_fields=["daily_ceiling", "updated_at"])
        return Response(serializers.account_row(account))
    if action == "link_partner":
        account = partner_link.link_partner(account=account, partner_id=data.get("partnerId"), by=by)
        return Response(serializers.account_row(account))
    if action == "sync_partner":
        result = partner_link.sync_account_partner(account=account, by=by)
        return Response(result)
    if action == "block":
        account.status = TreasuryAccount.Status.BLOQUE
        account.save(update_fields=["status", "updated_at"])
    elif action == "archive":
        account.status = TreasuryAccount.Status.ARCHIVE
        account.save(update_fields=["status", "updated_at"])
    elif action == "reassign":
        account.manager_sub = data.get("manager", account.manager_sub)
        account.save(update_fields=["manager_sub", "updated_at"])
    else:
        return Response({"detail": f"Action inconnue : {action}"}, status=400)
    return Response(serializers.account_row(account))


@api_view(["GET"])
@permission_classes([IsStaff, HasCapability("read")])
def account_register_sessions(request, code):
    account = TreasuryAccount.objects.filter(code=code).first()
    if not account:
        return Response({"detail": "Compte introuvable."}, status=404)
    sessions = CashRegisterSession.objects.select_related("account").filter(account=account)[:100]
    return Response([serializers.session_row(s) for s in sessions])


def _wallet_row(w: ClientWallet) -> dict:
    return {"id": w.pk, "currency": w.currency, "balance": float(w.balance), "status": w.status}


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def my_wallets(request):
    wallets = ClientWallet.objects.filter(user=request.user)
    return Response([_wallet_row(w) for w in wallets])


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def my_movements(request):
    return Response(services.wallet_movements(user=request.user))


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def my_withdrawal_requests(request):
    """Visibilité client sur ses propres demandes de retrait (Support.jsx « Crédit forcé »
    n'a pas d'équivalent ici — c'est `withdrawal_requests` côté staff qui reste le seul
    accès pour approuver/rejeter). Sans cet endpoint, un retrait au-dessus du palier auto
    disparaîtrait de la vue du client jusqu'à son approbation (il ne devient un
    `WalletMovement` — donc visible via `my_movements` — qu'une fois posté)."""
    requests_qs = WithdrawalRequest.objects.filter(wallet__user=request.user).select_related("wallet")
    return Response([serializers.withdrawal_request_row(r) for r in requests_qs])


@api_view(["GET"])
@permission_classes([HasCapability("validate")])
def wallet_for_user(request, sub):
    """Résout (et crée si besoin) le portefeuille d'un client par son `sub` IdP — utilisé
    par le flux de régularisation (Support.jsx « Crédit forcé ») : un agent connaît le
    ticket/le client, pas l'id interne de `ClientWallet`."""
    from accounts.models import FintechUser
    user = FintechUser.objects.filter(sub=sub).first()
    if not user:
        return Response({"detail": "Utilisateur introuvable."}, status=404)
    currency = request.GET.get("currency", "USD")
    wallet = _get_or_create_wallet(user, currency)
    return Response({**_wallet_row(wallet), "userSub": sub})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def my_convert(request):
    data = request.data or {}
    key = data.get("idempotencyKey")
    if not key:
        return Response({"detail": "idempotencyKey requis."}, status=400)
    try:
        result = services.convert_wallet(
            user=request.user, from_currency=data.get("from", "USD"), to_currency=data.get("to", "CDF"),
            amount=data.get("amount", "0"), idempotency_key=key, by=getattr(request.user, "sub", ""),
        )
    except idempotency.IdempotentReplay as exc:
        return idempotency.replay_response(exc)
    return Response(result)


def _get_or_create_wallet(user, currency: str) -> ClientWallet:
    wallet, _ = ClientWallet.objects.get_or_create(user=user, currency=currency)
    return wallet


def _deposit_kind(payload: dict) -> dict:
    """Discriminant de la réponse de dépôt (contrat front). Un `payment_order_row` porte
    `reference` ; un `movement_row` n'a que `movementId`. On dérive `kind` de la forme, pour
    que le rejeu idempotent (qui rend le snapshot brut) porte le même discriminant que la
    réponse d'origine."""
    kind = "payment_order" if "reference" in payload else "movement"
    return {"kind": kind, **payload}


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def my_deposit(request):
    """Dépôt sur le portefeuille du demandeur — routé par CANAL (décision « une seule porte »).

    * **interne** (`agent` / vide : espèces, agence) → mouvement crédité directement, comme
      avant. Réponse `{"kind": "movement", ...}`.
    * **externe** (`mobile_money` / `bank`) → **aucun crédit tant que le fournisseur n'a pas
      confirmé.** On crée un ordre d'ENCAISSEMENT (Makuta), on le transmet si Makuta est
      configuré, et le portefeuille n'est crédité qu'à la confirmation (rappel entrant /
      réconciliation). Réponse `{"kind": "payment_order", ...}` : le front affiche « en
      attente de confirmation », jamais « dépôt effectué »."""
    data = request.data or {}
    key = data.get("idempotencyKey")
    if not key:
        return Response({"detail": "idempotencyKey requis."}, status=400)
    channel = data.get("channel", "") or ""
    if not channels.is_known(channel):
        return Response({"code": "unknown_channel", "message": f"Canal de dépôt inconnu : {channel}."},
                        status=422)
    wallet = _get_or_create_wallet(request.user, data.get("currency", "USD"))
    by = getattr(request.user, "sub", "")

    if channels.is_external(channel):
        counterparty = data.get("counterparty", "") or ""
        if not counterparty:
            return Response({"code": "counterparty_required",
                             "message": "Le numéro Mobile Money / compte source est requis pour "
                                        "un dépôt externe."}, status=422)
        try:
            order = payments.create_payment_order(
                wallet_id=wallet.pk, direction=PaymentOrder.Direction.COLLECTION,
                operation=channels.collect_operation(channel), amount=data.get("amount", "0"),
                counterparty=counterparty, idempotency_key=key, by=by,
            )
        except idempotency.IdempotentReplay as exc:
            return Response(_deposit_kind(exc.record.response_snapshot), status=200)
        except MakutaConfigurationError:
            # Catalogue d'opérations non fourni par Wolf Technologies : on refuse proprement
            # (503) SANS créditer le portefeuille — jamais un crédit sur un dépôt non reçu.
            #
            # Sauf en SIMULATION explicite (`MAKUTA_SIMULATION`, développement seulement,
            # ignoré hors `DEBUG`) : l'encaissement est alors joué comme un dépôt interne
            # pour que les écrans restent développables. Le mouvement est MARQUÉ simulé,
            # dans la réponse comme au journal d'audit — un crédit sans contrepartie doit
            # se reconnaître au premier coup d'œil, y compris des mois plus tard.
            if not getattr(settings, "MAKUTA_SIMULATION", False):
                return Response({"code": "external_deposit_unavailable",
                                 "message": "Le dépôt par Mobile Money / banque est momentanément "
                                            "indisponible. Réessayez plus tard ou déposez en agence."},
                                status=503)
            try:
                movement = services.deposit(
                    wallet_id=wallet.pk, amount=data.get("amount", "0"), channel=channel,
                    idempotency_key=key, by=by,
                )
            except idempotency.IdempotentReplay as exc:
                return Response(_deposit_kind(exc.record.response_snapshot), status=200)
            audit_record(actor=by, action="caisses.deposit.simulated", entity_type="WalletMovement",
                         entity_id=str(movement.pk),
                         details={"channel": channel, "counterparty": counterparty,
                                  "amount": str(movement.amount),
                                  "raison": "MAKUTA_SIMULATION — encaissement externe simulé, "
                                            "aucun argent n'a transité par le fournisseur."})
            return Response({
                **_deposit_kind(serializers.movement_row(movement, verb="Dépôt simulé — aucun argent reçu.")),
                "simulated": True,
                "simulationNotice": (
                    "Dépôt SIMULÉ : la passerelle de paiement n'est pas configurée et le "
                    "mode développement est actif. Aucun argent n'a été reçu."
                ),
            }, status=201)
        if makuta.is_configured():
            order = payments.dispatch_payment_order(reference=order.reference, by=by)
        return Response(_deposit_kind(serializers.payment_order_row(order)), status=201)

    try:
        movement = services.deposit(
            wallet_id=wallet.pk, amount=data.get("amount", "0"), channel=channel,
            idempotency_key=key, by=by,
        )
    except idempotency.IdempotentReplay as exc:
        return Response(_deposit_kind(exc.record.response_snapshot), status=200)
    return Response(_deposit_kind(serializers.movement_row(movement, verb="Dépôt effectué.")))


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def my_withdraw(request):
    """Passe par le workflow à paliers (`withdrawal_tiers`) plutôt que par
    `services.withdraw()` (débit inconditionnel) : un retrait sous le seuil auto est traité
    immédiatement (même effet qu'avant), au-dessus il crée une demande PENDING_VALIDATION,
    débitée seulement une fois le palier manager/quorum atteint.

    Le canal reste optionnel — entrée inchangée pour un retrait interne (espèces/agence). Un
    canal externe (`mobile_money`/`bank`) + une contrepartie déclenchent, **au règlement**, un
    ordre de décaissement Makuta (visible via les endpoints `payments`). Le versement externe
    ne part jamais avant l'approbation humaine (P2)."""
    data = request.data or {}
    key = data.get("idempotencyKey")
    if not key:
        return Response({"detail": "idempotencyKey requis."}, status=400)
    channel = data.get("channel", "") or ""
    if not channels.is_known(channel):
        return Response({"code": "unknown_channel", "message": f"Canal de retrait inconnu : {channel}."},
                        status=422)
    counterparty = data.get("counterparty", "") or ""
    if channels.is_external(channel) and not counterparty:
        return Response({"code": "counterparty_required",
                         "message": "Le numéro Mobile Money / compte destinataire est requis "
                                    "pour un retrait externe."}, status=422)
    wallet = _get_or_create_wallet(request.user, data.get("currency", "USD"))
    try:
        request_obj = withdrawal_tiers.create_withdrawal_request(
            wallet_id=wallet.pk, amount=data.get("amount", "0"),
            idempotency_key=key, by=getattr(request.user, "sub", ""),
            channel=channel, counterparty=counterparty,
        )
    except idempotency.IdempotentReplay as exc:
        return idempotency.replay_response(exc)
    except MakutaConfigurationError:
        return Response({"code": "external_withdrawal_unavailable",
                         "message": "Le retrait par Mobile Money / banque est momentanément "
                                    "indisponible. Réessayez plus tard ou retirez en agence."},
                        status=503)
    return Response(serializers.withdrawal_request_row(request_obj))


@api_view(["GET"])
@permission_classes([HasCapability("validate")])
def withdrawal_requests(request):
    qs = WithdrawalRequest.objects.select_related("wallet").prefetch_related("approvals").all()[:500]
    status = request.GET.get("status")
    if status:
        qs = qs.filter(status=status)
    return Response([serializers.withdrawal_request_row(r) for r in qs])


@api_view(["POST"])
@permission_classes([HasCapability("validate")])
def withdrawal_request_approve(request, request_id):
    role = get_role(getattr(request.user, "role", ""))
    req = withdrawal_tiers.approve(
        request_id=request_id, approver_sub=getattr(request.user, "sub", ""), approver_role=role.id,
        otp_code=(request.data or {}).get("otpCode"),
    )
    return Response(serializers.withdrawal_request_row(req))


@api_view(["POST"])
@permission_classes([HasCapability("validate")])
def withdrawal_request_reject(request, request_id):
    role = get_role(getattr(request.user, "role", ""))
    req = withdrawal_tiers.reject(
        request_id=request_id, approver_sub=getattr(request.user, "sub", ""), approver_role=role.id,
        reason=(request.data or {}).get("reason", ""),
    )
    return Response(serializers.withdrawal_request_row(req))


@api_view(["POST"])
@permission_classes([HasCapability("validate")])
def withdrawal_otp_request(request, request_id):
    challenge = withdrawal_tiers.request_step_up_otp(request_id=request_id,
                                                       approver_sub=getattr(request.user, "sub", ""))
    return Response({"challengeId": challenge.pk, "expiresAt": challenge.expires_at.isoformat()})


@api_view(["POST"])
@permission_classes([HasCapability("validate")])
def withdrawal_otp_verify(request, request_id):
    data = request.data or {}
    ok = withdrawal_tiers.verify_step_up_otp(challenge_id=data.get("challengeId", ""), code=data.get("code", ""))
    return Response({"verified": ok})


@api_view(["GET", "POST"])
@permission_classes([HasCapability("validate")])
def regularization_orders(request):
    if request.method == "GET":
        qs = RegularizationOrder.objects.select_related("wallet").prefetch_related("approvals").all()[:500]
        status = request.GET.get("status")
        if status:
            qs = qs.filter(status=status)
        return Response([serializers.regularization_order_row(o) for o in qs])
    data = request.data or {}
    key = data.get("idempotencyKey")
    if not key:
        return Response({"detail": "idempotencyKey requis."}, status=400)
    wallet_id = data.get("walletId")
    if not wallet_id:
        return Response({"detail": "walletId requis."}, status=400)
    try:
        order = regularization.create_regularization_order(
            wallet_id=wallet_id, amount=data.get("amount", "0"), reason=data.get("reason", ""),
            ticket_id=data.get("ticketId"), idempotency_key=key, by=getattr(request.user, "sub", ""),
        )
    except idempotency.IdempotentReplay as exc:
        return idempotency.replay_response(exc)
    return Response(serializers.regularization_order_row(order), status=201)


@api_view(["POST"])
@permission_classes([HasCapability("validate")])
def regularization_order_approve(request, order_id):
    role = get_role(getattr(request.user, "role", ""))
    order = regularization.approve(
        order_id=order_id, approver_sub=getattr(request.user, "sub", ""), approver_role=role.id,
        otp_code=(request.data or {}).get("otpCode"),
    )
    return Response(serializers.regularization_order_row(order))


@api_view(["POST"])
@permission_classes([HasCapability("validate")])
def regularization_order_reject(request, order_id):
    role = get_role(getattr(request.user, "role", ""))
    order = regularization.reject(
        order_id=order_id, approver_sub=getattr(request.user, "sub", ""), approver_role=role.id,
        reason=(request.data or {}).get("reason", ""),
    )
    return Response(serializers.regularization_order_row(order))


@api_view(["POST"])
@permission_classes([HasCapability("validate")])
def regularization_otp_request(request, order_id):
    challenge = regularization.request_step_up_otp(order_id=order_id, approver_sub=getattr(request.user, "sub", ""))
    return Response({"challengeId": challenge.pk, "expiresAt": challenge.expires_at.isoformat()})


@api_view(["POST"])
@permission_classes([HasCapability("validate")])
def regularization_otp_verify(request, order_id):
    data = request.data or {}
    ok = regularization.verify_step_up_otp(challenge_id=data.get("challengeId", ""), code=data.get("code", ""))
    return Response({"verified": ok})


# ═══════════════════════════════════════ ORDRES DE PAIEMENT (fournisseur Makuta)

def _payment_staff(request) -> bool:
    """Vue « supervision des paiements ». Volontairement PAS `read` : la capacité `read` est
    portée par les rôles CLIENT (`agri_op`, `invest`) — s'en servir ici exposerait à chaque
    client la file des ordres de toute la coopérative."""
    role = get_role(getattr(request.user, "role", ""))
    return bool(role.validate or role.audit or role.config)


def _payment_order_or_403(request, reference: str):
    """Un client ne voit que SES ordres ; le staff paiements les voit tous. Renvoie
    `(order, response_erreur)` — 404 indifférencié pour un ordre d'autrui, pour ne pas
    transformer l'endpoint en oracle d'existence de références."""
    order = PaymentOrder.objects.select_related("wallet", "treasury_account").filter(
        reference=reference).first()
    if not order:
        return None, Response({"detail": "Ordre de paiement introuvable."}, status=404)
    if order.wallet.user_id == getattr(request.user, "pk", None):
        return order, None
    if _payment_staff(request):
        return order, None
    return None, Response({"detail": "Ordre de paiement introuvable."}, status=404)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def my_payment_orders(request):
    """Crée un ordre de paiement sur le portefeuille du demandeur.

    `send: true` enchaîne l'envoi — mais en DEUX appels de service distincts, la création
    étant committée avant que le réseau ne soit sollicité. Un échec d'envoi laisse donc
    toujours un ordre exploitable en base."""
    data = request.data or {}
    key = data.get("idempotencyKey")
    if not key:
        return Response({"detail": "idempotencyKey requis."}, status=400)
    wallet = _get_or_create_wallet(request.user, data.get("currency", "USD"))
    by = getattr(request.user, "sub", "")
    try:
        order = payments.create_payment_order(
            wallet_id=wallet.pk, direction=data.get("direction", PaymentOrder.Direction.COLLECTION),
            operation=data.get("operation", ""), amount=data.get("amount", "0"),
            counterparty=data.get("counterparty", ""),
            treasury_account_code=data.get("treasuryAccountCode", ""),
            metadata=data.get("metadata") or {}, idempotency_key=key, by=by,
        )
    except idempotency.IdempotentReplay as exc:
        return idempotency.replay_response(exc)
    if data.get("send"):
        order = payments.dispatch_payment_order(reference=order.reference, by=by)
        return Response(serializers.payment_order_row(order))
    return Response(serializers.payment_order_row(order), status=201)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def my_payment_order_list(request):
    qs = PaymentOrder.objects.select_related("wallet", "treasury_account").filter(
        wallet__user=request.user)
    status = request.GET.get("status")
    if status:
        qs = qs.filter(status=status)
    return Response([serializers.payment_order_row(o) for o in qs[:200]])


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def payment_orders(request):
    if not _payment_staff(request):
        return Response({"detail": "Capacité requise : validate ou audit."}, status=403)
    qs = payments.open_orders(status=request.GET.get("status"),
                              direction=request.GET.get("direction"))
    return Response([serializers.payment_order_row(o) for o in qs])


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def payment_orders_indeterminate(request):
    """File de réconciliation : tout ordre dont l'issue n'est pas connue. C'est l'outil que
    le principe 2 exige — un humain regarde cette liste et décide ; rien ne s'y résout seul."""
    if not _payment_staff(request):
        return Response({"detail": "Capacité requise : validate ou audit."}, status=403)
    orders = payments.indeterminate_orders()
    return Response({
        "count": len(orders),
        "orders": [serializers.payment_order_row(o) for o in orders],
        "consigne": "Ces ordres ont peut-être abouti chez le fournisseur. Les relire "
                    "(réconciliation), jamais les rejouer.",
    })


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def payment_order_detail(request, reference):
    order, error = _payment_order_or_403(request, reference)
    if error:
        return error
    row = serializers.payment_order_row(order)
    # Le journal complet (motifs, réponses fournisseur) est une pièce d'audit interne :
    # il ne part pas vers le client (principe 7 — asymétrie d'information).
    if _payment_staff(request):
        row["events"] = [serializers.payment_event_row(e) for e in order.events.all()]
    return Response(row)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def payment_order_send(request, reference):
    order, error = _payment_order_or_403(request, reference)
    if error:
        return error
    order = payments.dispatch_payment_order(reference=order.reference,
                                            by=getattr(request.user, "sub", ""))
    return Response(serializers.payment_order_row(order))


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def payment_order_cancel(request, reference):
    order, error = _payment_order_or_403(request, reference)
    if error:
        return error
    order = payments.cancel_payment_order(
        reference=order.reference, motive=(request.data or {}).get("motive", ""),
        by=getattr(request.user, "sub", ""),
    )
    return Response(serializers.payment_order_row(order))


@api_view(["POST"])
@permission_classes([HasCapability("validate")])
def payment_order_reconcile(request, reference):
    order = payments.reconcile_payment_order(
        reference=reference, motive=(request.data or {}).get("motive", ""),
        by=getattr(request.user, "sub", ""),
    )
    return Response(serializers.payment_order_row(order))


@api_view(["POST"])
@permission_classes([HasCapability("validate")])
def payment_order_force_settle(request, reference):
    """Clôture manuelle sur preuve externe — décision humaine assumée, motif circonstancié
    obligatoire. Réservée aux cas où la relecture de statut n'est pas disponible."""
    data = request.data or {}
    order = payments.force_settle(
        reference=reference, outcome=data.get("outcome", ""), motive=data.get("motive", ""),
        by=getattr(request.user, "sub", ""),
    )
    return Response(serializers.payment_order_row(order))


@api_view(["POST"])
@authentication_classes([])
@permission_classes([AllowAny])
def payment_callback(request):
    """Point d'entrée du rappel fournisseur — **ouvert au réseau, fermé à la confiance**.

    Il n'est ouvert que parce qu'un rappel arrive sans session ; il ne croit personne : sans
    clé publique Makuta configurée, tout est refusé (503), et avec une clé, seule une
    signature valide sur les OCTETS BRUTS du corps donne accès à la suite. Aucun rappel ne
    peut créditer quoi que ce soit sans franchir ces deux verrous.

    Tant que Wolf Technologies n'a pas fourni sa clé publique, son format de rappel et le
    nom de son en-tête de signature, cet endpoint refuse tout — c'est le comportement
    correct, pas une régression.
    """
    ip = request.META.get("REMOTE_ADDR", "")
    signature = request.META.get(
        "HTTP_" + payments.callback_signature_header().upper().replace("-", "_"), "",
    )
    try:
        order = payments.handle_callback(raw_body=request.body, signature=signature, ip=ip)
    except payments.CallbackRejected as exc:
        payments.log_rejected_callback(reason=exc.message, ip=ip)
        return Response({"detail": exc.message, "code": "makuta_callback_rejected"},
                        status=exc.http_status)
    return Response({"detail": "Rappel pris en compte.", "reference": order.reference,
                     "status": order.status})


# ── Écarts d'arrondi (principe 4 bis) ────────────────────────────────────────────────


@api_view(["GET"])
@permission_classes([IsStaff, HasCapability("audit")])
def rounding_residues(request):
    """Comptes d'écarts d'arrondi et leur journal — LECTURE SEULE ABSOLUE.

    Capacité `audit` et non `read` : c'est l'écran d'un auditeur ou d'un comptable, pas
    une donnée d'exploitation. Aucune route d'écriture n'existe et n'existera : les
    écarts ne s'écrivent que depuis le calcul qui les produit (`caisses.rounding`), et le
    journal est append-only jusque dans le modèle.

    `totalRows` accompagne toute liste plafonnée (§4.6) : un auditeur qui croit voir tout
    alors qu'il voit 200 lignes tire de fausses conclusions.
    """
    from .models import RoundingResidueEntry
    from .rounding import etat_des_comptes

    plafond = min(to_int(request.GET.get("limit"), 200) or 200, 500)
    lignes = RoundingResidueEntry.objects.select_related("account")

    devise = (request.GET.get("currency") or "").upper()
    if devise:
        lignes = lignes.filter(account__currency=devise)
    source = request.GET.get("source") or ""
    if source:
        lignes = lignes.filter(source=source)

    total = lignes.count()
    return Response({
        "accounts": etat_des_comptes(),
        "sources": [{"code": c, "label": lib} for c, lib in RoundingResidueEntry.Source.choices],
        "totalRows": total,
        "returned": min(total, plafond),
        "truncated": total > plafond,
        "cap": plafond,
        "entries": [
            {
                "id": e.pk,
                "currency": e.account.currency,
                "amount": str(e.amount),
                "exactAmount": str(e.exact_amount),
                "roundedAmount": str(e.rounded_amount),
                "source": e.source,
                "sourceLabel": e.get_source_display(),
                "entityType": e.entity_type,
                "entityId": e.entity_id,
                "actorSub": e.actor_sub,
                "detail": e.detail,
                "createdAt": e.created_at.isoformat(),
            }
            for e in lignes[:plafond]
        ],
    })


# --- Limites de dépôt et de retrait -----------------------------------------


@api_view(["GET", "PUT"])
@permission_classes([IsStaff, HasCapability("config")])
def transfer_limits(request):
    """Plafonds de dépôt et de retrait — lecture et paramétrage.

    Deux familles, distinctes et souvent confondues :

    * **Paliers KYC** — ce qu'un client peut DÉTENIR (plafond de solde, qui refuse un
      dépôt) et RETIRER dans le mois. Ils dépendent des documents validés, pas du montant.
    * **Paliers de validation du retrait** — à partir de quel montant un retrait cesse
      d'être automatique et demande un valideur, puis plusieurs. Ils dépendent du
      montant, pas du client.

    Capacité `config` : ce sont des règles de gouvernance, pas des actes d'exploitation.
    """
    from compliance.kyc_levels import BALANCE_CAPS, LEVEL_LIMITS, plafonds
    from compliance.models import KycTierLimit

    from .models import WithdrawalThreshold

    if request.method == "GET":
        paliers = []
        for tier in sorted(BALANCE_CAPS):
            ligne = KycTierLimit.objects.filter(tier=tier).first()
            solde, mois = plafonds(tier)
            paliers.append({
                "tier": tier, "balanceCap": float(solde), "monthlyLimit": float(mois),
                "label": ligne.label if ligne else "",
                # `source` dit d'où vient le chiffre : un plafond de repli n'a été voté
                # par personne, et l'écran doit le distinguer d'un plafond paramétré.
                "source": "database" if ligne else "code_fallback",
                "updatedBy": ligne.updated_by if ligne else "",
                "updatedAt": ligne.updated_at.isoformat() if ligne else None,
            })
        return Response({
            "kycTiers": paliers,
            "withdrawalThresholds": [
                {"currency": t.currency, "autoLimit": float(t.auto_limit),
                 "managerLimit": float(t.manager_limit)}
                for t in WithdrawalThreshold.objects.all()
            ],
            "fallbackNotice": (
                "Un palier au source « code_fallback » applique une constante du code que "
                "personne n'a votée. Paramétrez-le pour qu'il devienne une décision."
                if any(p["source"] == "code_fallback" for p in paliers) else ""
            ),
        })

    data = request.data or {}
    by = getattr(request.user, "sub", "")
    errors = []

    for palier in data.get("kycTiers", []) or []:
        tier = (palier.get("tier") or "").strip()
        if tier not in BALANCE_CAPS:
            errors.append({"code": "TIER_UNKNOWN", "message": f"Palier inconnu : « {tier} »."})
            continue
        solde = to_decimal(palier.get("balanceCap"), default="-1")
        mois = to_decimal(palier.get("monthlyLimit"), default="-1")
        if solde < 0 or mois < 0:
            errors.append({"code": "LIMIT_NEGATIVE",
                            "message": f"Palier {tier} : les plafonds ne peuvent pas être négatifs."})
            continue
        if not est_au_centime(palier.get("balanceCap")) or not est_au_centime(palier.get("monthlyLimit")):
            errors.append({"code": "LIMIT_PRECISION",
                            "message": f"Palier {tier} : les plafonds s'expriment au centime."})
            continue
        KycTierLimit.objects.update_or_create(
            tier=tier,
            defaults={"balance_cap": solde, "monthly_limit": mois,
                      "label": (palier.get("label") or "").strip()[:80], "updated_by": by},
        )

    for seuil in data.get("withdrawalThresholds", []) or []:
        devise = (seuil.get("currency") or "").strip().upper()
        auto = to_decimal(seuil.get("autoLimit"), default="-1")
        manager = to_decimal(seuil.get("managerLimit"), default="-1")
        if auto < 0 or manager < 0:
            errors.append({"code": "THRESHOLD_NEGATIVE",
                            "message": f"{devise} : les seuils ne peuvent pas être négatifs."})
            continue
        # Un seuil automatique au-dessus du seuil « manager » viderait le palier
        # intermédiaire de son sens : tout passerait en automatique.
        if auto > manager:
            errors.append({"code": "THRESHOLD_ORDER",
                            "message": f"{devise} : le seuil automatique ({auto}) doit rester "
                                       f"inférieur ou égal au seuil manager ({manager})."})
            continue
        WithdrawalThreshold.objects.update_or_create(
            currency=devise, defaults={"auto_limit": auto, "manager_limit": manager})

    if errors:
        return Response({"detail": errors[0]["message"], "errors": errors}, status=422)

    audit_record(actor=by, action="caisses.transfer_limits.update", entity_type="Config",
                 entity_id="transfer_limits", details=data)
    return Response({"detail": "Limites enregistrées."})
