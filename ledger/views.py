"""API du grand livre (`Accounting.jsx`).

Le grand livre n'a **aucune vue client** : il ne sert que des données de l'institution.
Chaque vue cumule donc `IsStaff` et une capacité explicite. Le garde de capacité seul ne
suffisait pas — `client`, `agri_op`, `invest` et `partner` portent tous `read=True` dans
`rbac.role_registry` : `HasCapability("read")` laissait n'importe quel membre consulter
la balance générale et les états financiers de la coopérative.

Plus grave, `read` gardait aussi les POST : poster une écriture au journal et créer un
compte du plan comptable étaient à la portée du même `read`. Un client pouvait écrire
dans la comptabilité. La capacité dépend désormais de la MÉTHODE : lire (`read`), saisir
une écriture (`create`, le maker), toucher au plan comptable (`config`, le référentiel).
"""
from __future__ import annotations

from datetime import date as date_cls

from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response

from accounts.permissions import IsStaff
from common import idempotency
from common.parsing import to_date
from rbac.permissions import CapaciteSelonMethode, HasCapability

from . import serializers, services
from .models import ChartAccount, JournalEntry


def _account_row(a: ChartAccount) -> dict:
    return {"code": a.code, "name": a.name, "classNo": a.class_no, "nature": a.nature,
            "isCoreActivity": a.is_core_activity, "currencies": a.currencies, "parent": a.parent_id}


@api_view(["GET", "POST"])
@permission_classes([IsStaff, CapaciteSelonMethode(GET="read", POST="config")])
def accounts(request):
    """GET : le plan comptable. POST : y AJOUTER un compte — un acte de référentiel,
    donc `config` (principe 8 : les règles vivent en base, mais elles s'y écrivent sous
    la main du comité, pas sous celle du premier porteur de `read`)."""
    if request.method == "GET":
        return Response([_account_row(a) for a in ChartAccount.objects.all()])
    data = request.data or {}
    account = ChartAccount.objects.create(
        code=data.get("code", ""), name=data.get("name", ""), class_no=data.get("classNo", 5),
        nature=data.get("nature", ChartAccount.Nature.ACTIF),
        currencies=data.get("currencies", []), parent_id=data.get("parentId"),
    )
    return Response(_account_row(account), status=201)


@api_view(["GET", "POST"])
@permission_classes([IsStaff, CapaciteSelonMethode(GET="read", POST="create")])
def entries(request):
    """GET : le journal. POST : POSTER une écriture — l'acte du maker (`create`), que
    `entry_reverse` (checker, `validate`) vient contrebalancer."""
    if request.method == "GET":
        qs = JournalEntry.objects.prefetch_related("lines__account").all()[:500]
        return Response([serializers.entry_row(e) for e in qs])
    data = request.data or {}
    key = data.get("idempotencyKey")
    if not key:
        return Response({"detail": "idempotencyKey requis."}, status=400)
    try:
        entry = services.post_journal_entry(
            date=to_date(data.get("date")) or date_cls.today(), piece_ref=data.get("pieceRef", ""),
            code=data.get("code", ""), currency=data.get("currency", "USD"),
            lines=data.get("lines", []), description=data.get("description", ""),
            idempotency_key=key, by=getattr(request.user, "sub", ""),
        )
    except idempotency.IdempotentReplay as exc:
        return idempotency.replay_response(exc)
    return Response(serializers.entry_row(entry), status=201)


@api_view(["POST"])
@permission_classes([IsStaff, HasCapability("validate")])
def entry_reverse(request, entry_id):
    entry = services.reverse_journal_entry(
        entry_id=entry_id, reason=(request.data or {}).get("reason", ""),
        by=getattr(request.user, "sub", ""),
    )
    return Response(serializers.entry_row(entry), status=201)


@api_view(["GET"])
@permission_classes([IsStaff, HasCapability("read")])
def account_lines(request, code):
    account = ChartAccount.objects.filter(code=code).first()
    if not account:
        return Response({"detail": "Compte introuvable."}, status=404)
    lines = account.lines.select_related("entry").order_by("entry__date", "id")
    balance = 0.0
    rows = []
    for line in lines:
        balance += float(line.debit) - float(line.credit)
        rows.append({
            "date": line.entry.date.isoformat(), "piece": line.entry.piece_ref,
            "label": line.entry.description, "debit": float(line.debit), "credit": float(line.credit),
            "balance": balance,
        })
    return Response(rows)


@api_view(["GET"])
@permission_classes([IsStaff, HasCapability("read")])
def trial_balance_view(request):
    return Response(services.trial_balance(as_of=to_date(request.GET.get("as_of"))))


@api_view(["GET"])
@permission_classes([IsStaff, HasCapability("read")])
def statements(request, kind):
    return Response(services.financial_statements(kind=kind, as_of=to_date(request.GET.get("as_of"))))
