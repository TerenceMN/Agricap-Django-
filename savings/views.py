"""API épargne (Savings.jsx + admin/savings/*) — CRUD léger, toujours audité.

Trois cercles de visibilité, et la ligne passe par le PROPRIÉTAIRE de la donnée :

1. **Mes données** (`my_plans`, `my_groups`, `plan_deposit`…) — `IsAuthenticated` et filtre
   `user=request.user` dans la requête. Inchangé : c'est l'épargne de l'appelant.
2. **Le catalogue des groupes** (`GET /groups`) — un membre a besoin de la liste des
   coopératives/AVEC pour demander à en rejoindre une (modale « Rejoindre un groupe »).
   Il reste donc en `read`, mais servi par un sérialiseur RÉDUIT : ni le solde du groupe,
   ni le nom de ses membres. Ces deux champs concernent des tiers et l'institution.
3. **Le back-office épargne** (`all_plans`, détail et journal d'un groupe) — soldes de
   tous les titulaires, adhésions nominatives, journal d'audit : `IsStaff` cumulé.
   `HasCapability("read")` seul ne filtrait rien, les rôles clients le portant tous.

Écrire sur un groupe (créer, renommer, changer le taux) relève de la capacité
`cooperatives` — « administrer le réseau mutualiste », déjà utilisée par `assign_group` —
et non de `create`, que le rôle `invest` porte : un investisseur pouvait créer une
coopérative d'épargne et en fixer le taux.
"""
from __future__ import annotations

import datetime
import uuid
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal

from django.db import transaction
from django.db.models import F
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from accounts.models import FintechUser
from accounts.permissions import IsStaff
from audit.services import record as audit_record
from common import idempotency
from common.exceptions import InsufficientFundsError, ValidationFailed
from common.money import est_au_centime
from common.parsing import to_date, to_decimal
from rbac.permissions import CapaciteSelonMethode, HasCapability

from . import events
from .models import (
    GroupIntegrationRequest, SavingsAdjustment, SavingsDeposit, SavingsEvent, SavingsGroup,
    SavingsGroupMember, SavingsPlan, SavingsRateChange, SavingsWithdrawal,
)

#: Plafond de taux annuel — le seul seuil métier de ce module. Vécu côté serveur (les
#: modales le vérifiaient côté client, où il n'engageait rien). 6 % = plancher de refus,
#: pas une valeur cachée : il est renvoyé dans le refus pour que l'écran l'explique.
MAX_ANNUAL_RATE = Decimal("6")

#: Nombre d'occurrences projetées par la simulation de croissance (côté serveur).
PROJECTION_ROWS = 10

#: Fréquence de versement → pas en jours, pour la projection. Miroir serveur du
#: `freqMap` que la modale calculait côté navigateur (interdit §5).
_FREQUENCY_DAYS = {
    "hebdomadaire": 7, "bimensuel": 15, "mensuel": 30, "trimestriel": 90, "annuel": 365,
}


def _devises_supportees() -> set[str]:
    """Devises dans lesquelles un plan d'épargne peut exister.

    Ce ne sont pas « les devises de l'épargne » : ce sont celles où un PORTEFEUILLE peut
    exister (`caisses.TreasuryAccount.Currency`), puisque depuis « une seule porte » tout
    dépôt débite le wallet et tout retrait le crédite. La liste est LUE chez le propriétaire
    du concept plutôt que recopiée ici (principe 6, une seule nomenclature par concept).

    Sans ce contrôle, `currency` arrivait brut du client : un plan en « XYZ » était créé,
    n'acceptait plus jamais un dépôt (aucun portefeuille dans cette devise, donc
    `WALLET_MISSING` à perpétuité), et surtout — si un portefeuille venait un jour à exister
    dans cette devise — produisait des `SavingsEvent` que le plan comptable ne sait pas
    nommer. Ces événements resteraient en file indéfiniment et gonfleraient à jamais
    « l'écart connu des états financiers » que la comptabilité déclare désormais au bilan.
    Une devise non nommable pollue l'écart au lieu de le mesurer.
    """
    from caisses.models import TreasuryAccount

    return set(TreasuryAccount.Currency.values)


def _monthly_rate(annual: Decimal) -> Decimal:
    """Taux mensuel équivalent = annuel / 12, quantize 0.0001. CALCUL SERVEUR : c'est
    la ligne que le front faisait en `(val/12)` — désormais la seule source."""
    return (annual / Decimal("12")).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


def _plan_row(p: SavingsPlan) -> dict:
    return {
        "id": p.pk, "name": p.name, "objectiveType": p.objective_type, "type": p.plan_type,
        "objectif": float(p.objectif), "balance": float(p.balance), "status": p.status,
        "currency": p.currency, "accruedInterest": float(p.accrued_interest),
        "interestRate": float(p.interest_rate),
        # Taux mensuel et statut de taux servis par le serveur — le front n'en calcule
        # ni n'en déduit aucun (§5).
        "monthlyRate": float(p.monthly_rate),
        "rateStatus": p.rate_status,
        "frequency": p.frequency,
        "periodicDeposit": float(p.periodic_deposit),
    }


#: Champs DESCRIPTIFS d'un plan — les seuls qu'une modification peut toucher.
#:
#: Ce qui n'y est PAS, et pourquoi : `balance` ne bouge que par un mouvement
#: d'argent tracé (dépôt, retrait, ajustement) ; `interest_rate` relève de
#: `plan_rate_config`, qui journalise chaque changement de taux en
#: `SavingsRateChange` ; `currency` fige l'appariement avec le portefeuille et
#: rendrait un solde existant inintelligible ; `user` transférerait l'épargne
#: d'un titulaire à un autre. Une modification ouverte à tout le modèle aurait
#: rouvert ces quatre portes d'un coup.
CHAMPS_MODIFIABLES = {
    "name": "name",
    "objectiveType": "objective_type",
    "type": "plan_type",
    "objectif": "objectif",
}


def _creer_plan(titulaire, data: dict, *, par: str):
    """Crée un plan pour `titulaire`. **Unique chemin de création** (principe 6).

    Le client (`POST plans/mine`) et le back-office (`POST plans`) passent tous
    deux par ici : sans cela, l'ouverture d'un compte par un agent n'aurait pas
    subi le contrôle de devise, et un plan en devise sans portefeuille aurait pu
    naître par la porte admin — invisible jusqu'au premier dépôt.

    Renvoie `(plan, None)` ou `(None, réponse d'erreur 422)`.
    """
    currency = (data.get("currency") or "USD").strip().upper()
    if currency not in _devises_supportees():
        return None, Response(
            {"detail": f"Devise « {currency} » non supportée : un plan d'épargne n'existe "
                       f"que dans une devise où un portefeuille existe "
                       f"({', '.join(sorted(_devises_supportees()))}).",
             "errors": [{"code": "CURRENCY_UNKNOWN",
                         "message": f"Devise non supportée : {currency}."}]},
            status=422,
        )
    plan = SavingsPlan.objects.create(
        user=titulaire, name=data.get("name", ""),
        objective_type=data.get("objectiveType", "autre"),
        plan_type=data.get("type", "campagne"), objectif=data.get("objectif", "0"),
        currency=currency,
    )
    audit_record(actor=par, action="savings.plan.create",
                 entity_type="SavingsPlan", entity_id=str(plan.pk),
                 details={"titulaire": str(titulaire.pk), "devise": currency,
                          "ouvertPar": "titulaire" if par == str(titulaire.pk) else "agent"})
    return plan, None


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def my_plans(request):
    if request.method == "GET":
        return Response([_plan_row(p) for p in SavingsPlan.objects.filter(user=request.user)])
    plan, erreur = _creer_plan(request.user, request.data or {},
                               par=getattr(request.user, "sub", ""))
    return erreur or Response(_plan_row(plan), status=201)


@api_view(["GET", "POST"])
@permission_classes([IsStaff, CapaciteSelonMethode(GET="read", POST="create")])
def all_plans(request):
    """Vue admin (AdminSavingsTable) — tous les plans, tous titulaires.

    Sert aussi le `sub` du titulaire et ses adhésions de groupe, pour que l'écran
    n'ait plus à déduire l'affectation d'un `localStorage` (l'ancien
    `admin_savings_groups` était un référentiel de groupes fantôme, côté navigateur).

    POST — ouverture d'un compte POUR UN TITULAIRE par un agent. Le bouton
    « Ouvrir Compte » du back-office existait sans destinataire : il appelait un
    gestionnaire d'actions qui affichait « Fonctionnalité non implémentée », et
    `POST plans/mine` n'aurait pas convenu — il crée le plan pour l'APPELANT,
    donc au nom de l'agent et non du client.
    """
    if request.method == "POST":
        data = request.data or {}
        sub = (data.get("holderSub") or "").strip()
        if not sub:
            return Response(
                {"detail": "Le titulaire du compte est obligatoire : un plan d'épargne "
                           "appartient à quelqu'un.",
                 "errors": [{"code": "HOLDER_REQUIRED",
                             "message": "Renseignez « holderSub » (identifiant du titulaire)."}]},
                status=422,
            )
        titulaire = FintechUser.objects.filter(pk=sub).first()
        if titulaire is None:
            return Response(
                {"detail": f"Aucun utilisateur ne correspond à « {sub} ».",
                 "errors": [{"code": "HOLDER_NOT_FOUND",
                             "message": "Titulaire introuvable — vérifiez la sélection."}]},
                status=422,
            )
        plan, erreur = _creer_plan(titulaire, data,
                                   par=getattr(request.user, "sub", ""))
        if erreur:
            return erreur
        return Response({**_plan_row(plan),
                         "holder": titulaire.full_name or titulaire.email,
                         "holderSub": titulaire.pk, "holderGroups": []}, status=201)

    plans = SavingsPlan.objects.select_related("user").all()
    # Adhésions préchargées une fois (pas de N+1) : titulaire → noms de groupes.
    holder_groups: dict = {}
    for m in SavingsGroupMember.objects.select_related("group", "user").all():
        holder_groups.setdefault(m.user_id, []).append(m.group.name)
    return Response([
        {**_plan_row(p), "holder": p.user.full_name or p.user.email,
         "holderSub": p.user_id, "holderGroups": holder_groups.get(p.user_id, [])}
        for p in plans
    ])


@api_view(["GET", "PATCH"])
@permission_classes([IsStaff, CapaciteSelonMethode(GET="read", PATCH="config")])
def plan_detail(request, plan_id):
    """GET : un plan avec son titulaire. PATCH : ses champs DESCRIPTIFS seulement.

    Le menu « Modifier » du back-office n'avait aucun endpoint : il affichait un
    toast « Fonctionnalité non implémentée ». Le voici, volontairement étroit —
    `CHAMPS_MODIFIABLES` énumère ce qui peut bouger, et tout le reste est refusé
    plutôt qu'ignoré en silence. Un champ non modifiable envoyé par erreur doit
    faire un bruit, sinon l'appelant croit avoir changé un taux qu'il n'a pas
    changé.
    """
    plan = SavingsPlan.objects.select_related("user").filter(pk=plan_id).first()
    if not plan:
        return Response({"detail": "Plan introuvable.", "code": "PLAN_NOT_FOUND"}, status=404)

    def _row():
        return {**_plan_row(plan), "holder": plan.user.full_name or plan.user.email,
                "holderSub": plan.user_id}

    if request.method == "GET":
        return Response(_row())

    data = request.data or {}
    refuses = [c for c in data if c not in CHAMPS_MODIFIABLES]
    if refuses:
        return Response(
            {"detail": "Certains champs ne se modifient pas ici.",
             "errors": [{
                 "code": "CHAMP_NON_MODIFIABLE",
                 "message": (
                     f"« {c} » ne se modifie pas par cette route. Le solde ne bouge que "
                     f"par un mouvement d'argent tracé (dépôt, retrait, ajustement), le "
                     f"taux par « rate-config » qui journalise chaque changement, et ni "
                     f"la devise ni le titulaire ne changent sur un plan existant."),
             } for c in sorted(refuses)]},
            status=422,
        )

    avant = {api: getattr(plan, champ) for api, champ in CHAMPS_MODIFIABLES.items()}
    for cle, champ in CHAMPS_MODIFIABLES.items():
        if cle in data:
            setattr(plan, champ, data[cle])

    if not str(plan.name or "").strip():
        return Response(
            {"detail": "Le nom de l'objectif est obligatoire.",
             "errors": [{"code": "NAME_REQUIRED",
                         "message": "Un objectif d'épargne sans nom n'est pas identifiable "
                                    "par son titulaire."}]},
            status=422,
        )

    plan.save(update_fields=[*CHAMPS_MODIFIABLES.values(), "updated_at"]
              if hasattr(plan, "updated_at") else list(CHAMPS_MODIFIABLES.values()))

    apres = {api: getattr(plan, champ) for api, champ in CHAMPS_MODIFIABLES.items()}
    audit_record(
        actor=getattr(request.user, "sub", ""), action="savings.plan.update",
        entity_type="SavingsPlan", entity_id=str(plan.pk),
        # Le DIFF, et non l'état final : c'est l'écart qui se relit deux ans après.
        details={"modifications": {c: {"avant": str(avant[c]), "apres": str(apres[c])}
                                   for c in CHAMPS_MODIFIABLES if avant[c] != apres[c]}},
    )
    return Response(_row())


@api_view(["POST"])
@permission_classes([IsStaff, HasCapability("config")])
def plan_closure(request, plan_id):
    """Clôture d'un plan — l'acte qui remplace la « suppression » du back-office.

    Le menu de ligne proposait « Supprimer ». Il n'existait aucun endpoint, et il
    ne doit pas en exister : un plan d'épargne porte des dépôts, des retraits et
    des intérêts courus. L'effacer détruirait la contrepartie d'écritures déjà
    passées et ferait disparaître l'historique d'un titulaire (CONVENTIONS.md §9 —
    « tu ne supprimes jamais de données financières »). Le modèle porte d'ailleurs
    déjà l'état voulu : `Status.CLOTURE`.

    Clôturer laisse le plan consultable et auditable, et lui ferme les dépôts.

    Refus si le solde n'est pas nul : clôturer un plan qui détient encore de
    l'argent le rendrait inaccessible à son titulaire sans que cet argent ait
    bougé nulle part. Le retrait doit précéder la clôture, et il laisse sa trace.
    """
    plan = SavingsPlan.objects.select_related("user").filter(pk=plan_id).first()
    if not plan:
        return Response({"detail": "Plan introuvable.", "code": "PLAN_NOT_FOUND"}, status=404)

    if plan.status == SavingsPlan.Status.CLOTURE:
        return Response(
            {"detail": "Ce plan est déjà clôturé.",
             "errors": [{"code": "PLAN_DEJA_CLOTURE",
                         "message": "Aucune action : le plan est déjà clôturé."}]},
            status=409,
        )

    # `plan.balance or 0` aurait été un piège : `Decimal("0.00")` est FALSY, donc
    # un solde nul devenait l'entier 0 et la trace d'audit perdait son échelle
    # (« 0 » au lieu de « 0.00 »). Sur un montant, l'échelle EST une information :
    # elle dit en quelle précision la somme a été arrêtée.
    solde = Decimal(str(plan.balance if plan.balance is not None else "0.00"))
    if solde > 0:
        return Response(
            {"detail": "Clôture refusée : le plan détient encore un solde.",
             "errors": [{
                 "code": "SOLDE_NON_NUL",
                 "message": (
                     f"Le plan porte encore {solde} {plan.currency}. Effectuez le retrait "
                     f"vers le portefeuille du titulaire avant de clôturer — la clôture "
                     f"ne déplace aucun argent et laisserait ce solde inaccessible."),
             }]},
            status=422,
        )

    motif = str((request.data or {}).get("motif", "")).strip()
    if not motif:
        return Response(
            {"detail": "Le motif de clôture est obligatoire.",
             "errors": [{"code": "MOTIF_REQUIS",
                         "message": "Une clôture sans motif n'est pas relisible par un "
                                    "auditeur ni explicable au titulaire."}]},
            status=422,
        )

    plan.status = SavingsPlan.Status.CLOTURE
    plan.save(update_fields=["status"])
    audit_record(
        actor=getattr(request.user, "sub", ""), action="savings.plan.close",
        entity_type="SavingsPlan", entity_id=str(plan.pk),
        details={"titulaire": str(plan.user_id), "motif": motif,
                 "soldeALaCloture": str(solde), "devise": plan.currency},
    )
    return Response({**_plan_row(plan), "holder": plan.user.full_name or plan.user.email,
                     "holderSub": plan.user_id})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def plan_deposit(request, plan_id):
    """Dépôt sur un plan d'épargne — DÉBITE le portefeuille du client (flux interne).

    Décision du fondateur, « une seule porte » : le wallet est le seul point de contact
    avec l'extérieur ; tout autre flux d'argent est interne et débite le WALLET du client.
    Un dépôt d'épargne n'invente donc pas d'argent — il déplace du cash du portefeuille
    vers le plan, via le SEUL service de débit du système (`caisses.services.withdraw`,
    déjà employé par les obligations : pas de second chemin de débit, principe 6). Le
    `channel` reste informatif (par où l'argent est entré dans le système à l'origine) ;
    la source réelle du dépôt est toujours le portefeuille. Solde insuffisant ou
    portefeuille absent → refus 422 structuré, sans inscription partielle.

    Les contrôles d'amont ne sont PAS un doublon d'écran : `to_decimal` est tolérant (il
    rend `0` sur une saisie illisible et accepte les négatifs), si bien qu'un `amount` de
    `-500` débitait le plan — un retrait déguisé en dépôt. Ils sont collectés AVANT le
    débit et le refus est structuré `{code, message}` (principe 5) pour que l'écran déplie
    chaque cause au lieu d'afficher « Erreur 422 ».
    """
    plan = SavingsPlan.objects.filter(pk=plan_id, user=request.user).first()
    if not plan:
        return Response({"detail": "Plan introuvable.", "code": "PLAN_NOT_FOUND"}, status=404)

    data = request.data or {}
    errors: list[dict] = []

    raw_amount = data.get("amount")
    amount = to_decimal(raw_amount, default="-1")  # -1 = sentinelle « illisible »
    if amount <= 0:
        errors.append({
            "code": "AMOUNT_INVALID",
            "message": "Le montant du dépôt doit être un nombre strictement positif.",
        })
    elif not est_au_centime(raw_amount):
        # Principe 4 bis : un montant client au-delà du centime est REFUSÉ, pas arrondi.
        # Cette ligne arrondissait en silence : un client saisissant 10,005 voyait
        # 10,01 débité de son portefeuille sans que rien ne le lui dise. Un demi-centime
        # n'est pas grand-chose ; l'apprendre par son relevé plutôt que par l'écran, si.
        errors.append({
            "code": "AMOUNT_PRECISION",
            "message": (f"Le montant {raw_amount} porte plus de deux décimales. "
                        f"Les montants s'expriment au centime : saisissez 10,00 et non 10,005."),
        })

    channel = data.get("channel", SavingsPlan.Channel.AGENT)
    if channel not in SavingsPlan.Channel.values:
        errors.append({
            "code": "CHANNEL_UNKNOWN",
            "message": f"Canal de dépôt inconnu : {channel}.",
        })

    if plan.status != SavingsPlan.Status.ACTIF:
        errors.append({
            "code": "PLAN_CLOSED",
            "message": "Ce plan est clôturé : il n'accepte plus de dépôt.",
        })

    if errors:
        return Response({"detail": errors[0]["message"], "errors": errors}, status=422)

    # Le dépôt débite le portefeuille du client dans la devise du plan. Aucune création
    # de portefeuille implicite : sans wallet dans cette devise, il n'y a pas de cash à
    # déplacer — on le dit plutôt que d'inventer une source.
    from caisses.models import ClientWallet
    from caisses.services import withdraw as caisses_withdraw

    wallet = ClientWallet.objects.filter(user=request.user, currency=plan.currency).first()
    if wallet is None:
        return Response(
            {"detail": f"Aucun portefeuille {plan.currency} : alimentez d'abord votre "
                       "portefeuille avant d'épargner.",
             "errors": [{"code": "WALLET_MISSING",
                         "message": f"Aucun portefeuille {plan.currency} sur ce compte."}]},
            status=422,
        )

    # Idempotence optionnelle : une clé fournie protège d'un double-clic (le débit et
    # l'inscription ne se rejouent pas) ; sans clé, chaque requête est une opération
    # distincte (comportement historique préservé). Le débit + l'inscription vivent dans
    # UNE transaction : un échec après le débit annule tout, jamais de dépôt sans débit.
    key = (data.get("idempotencyKey") or "").strip() or uuid.uuid4().hex
    debit_key = f"savings-deposit:{plan.pk}:{key}"
    try:
        with transaction.atomic():
            movement = caisses_withdraw(wallet_id=wallet.pk, amount=amount,
                                         idempotency_key=debit_key,
                                         by=getattr(request.user, "sub", ""))
            deposit = SavingsDeposit.objects.create(plan=plan, amount=amount, channel=channel)
            SavingsPlan.objects.filter(pk=plan.pk).update(balance=F("balance") + amount)
            # L'événement comptable (B8) naît ICI, dans la transaction de l'acte : le débit
            # du wallet, l'inscription au plan et l'événement committent ensemble ou pas du
            # tout. Un dépôt sans son événement serait un écart comptable invisible.
            events.emettre(
                SavingsEvent.Type.SAVINGS_DEPOSITED, plan=plan, amount=amount,
                actor_sub=getattr(request.user, "sub", ""),
                depositId=deposit.pk, walletMovementId=movement.pk,
                walletId=wallet.pk, canalDeclare=channel,
            )
            audit_record(actor=getattr(request.user, "sub", ""), action="savings.plan.deposit",
                         entity_type="SavingsPlan", entity_id=str(plan.pk),
                         details={"amount": str(amount), "channel": channel,
                                  "walletMovementId": movement.pk,
                                  "currency": plan.currency})
    except idempotency.IdempotentReplay:
        # Requête déjà traitée à l'identique : on rejoue l'état courant sans re-débiter
        # ni ré-inscrire (le premier passage a tout committé, débit compris).
        plan.refresh_from_db()
        return Response(_plan_row(plan))
    except InsufficientFundsError:
        return Response(
            {"detail": f"Solde insuffisant : {wallet.balance} {plan.currency} disponibles "
                       f"pour un dépôt de {amount} {plan.currency}. Aucun dépôt effectué.",
             "errors": [{"code": "WALLET_INSUFFICIENT_FUNDS",
                         "message": f"Solde insuffisant ({wallet.balance} {plan.currency}) "
                                    f"pour un dépôt de {amount} {plan.currency}."}]},
            status=422,
        )
    plan.refresh_from_db()
    return Response(_plan_row(plan))


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def plan_withdraw(request, plan_id):
    """Retrait d'épargne — CRÉDITE le portefeuille du client (flux interne, miroir du dépôt).

    Symétrique exact de `plan_deposit` sous la règle « une seule porte » : sortir de
    l'épargne, ce n'est pas sortir de l'institution, c'est revenir au portefeuille. Le
    crédit passe par le SEUL service de crédit du système (`caisses.services.deposit`),
    jamais par une écriture directe dans les modèles de `caisses` (principe 6).

    L'endpoint n'existait pas : l'épargne était une porte à sens unique — l'argent entrait
    dans un plan et aucun chemin ne l'en faisait ressortir. Le retrait est ce chemin, et il
    est ce qui rend l'écriture B9 possible : sans acte métier, pas d'événement (« ne devine
    aucune écriture »).

    Deux contrôles portent le risque :

    * **Le solde du plan**, relu SOUS VERROU (`select_for_update`) dans la transaction —
      sans quoi deux retraits concurrents de 60 sur un solde de 100 passeraient tous deux
      leur contrôle avant que l'un ne débite, et le plan finirait négatif.
    * **Le plafond de solde KYC du portefeuille** (`caisses.services.deposit`) : un retrait
      qui ferait dépasser le plafond du wallet est refusé en entier, jamais tronqué.

    Le statut du plan n'est PAS un motif de refus, contrairement au dépôt : un plan clôturé
    n'accepte plus d'argent, mais l'argent qu'il détient doit toujours pouvoir revenir à son
    titulaire. Refuser serait piéger les fonds.
    """
    plan = SavingsPlan.objects.filter(pk=plan_id, user=request.user).first()
    if not plan:
        return Response({"detail": "Plan introuvable.", "code": "PLAN_NOT_FOUND"}, status=404)

    data = request.data or {}
    errors: list[dict] = []

    raw_amount = data.get("amount")
    amount = to_decimal(raw_amount, default="-1")  # -1 = sentinelle « illisible »
    if amount <= 0:
        errors.append({
            "code": "AMOUNT_INVALID",
            "message": "Le montant du retrait doit être un nombre strictement positif.",
        })
    elif not est_au_centime(raw_amount):
        # Principe 4 bis — même règle qu'au dépôt, et pour la même raison : un retrait
        # arrondi au centime supérieur sortirait du plan un demi-centime que le client
        # n'a pas demandé.
        errors.append({
            "code": "AMOUNT_PRECISION",
            "message": (f"Le montant {raw_amount} porte plus de deux décimales. "
                        f"Les montants s'expriment au centime : saisissez 10,00 et non 10,005."),
        })

    channel = data.get("channel", SavingsPlan.Channel.AGENT)
    if channel not in SavingsPlan.Channel.values:
        errors.append({
            "code": "CHANNEL_UNKNOWN",
            "message": f"Canal de retrait inconnu : {channel}.",
        })

    if errors:
        return Response({"detail": errors[0]["message"], "errors": errors}, status=422)

    from caisses.models import ClientWallet
    from caisses.services import deposit as caisses_deposit

    # Aucune création implicite de portefeuille : un plan dont le solde est positif a
    # forcément été alimenté depuis un wallet de cette devise, qui existe donc.
    wallet = ClientWallet.objects.filter(user=request.user, currency=plan.currency).first()
    if wallet is None:
        return Response(
            {"detail": f"Aucun portefeuille {plan.currency} pour recevoir ce retrait.",
             "errors": [{"code": "WALLET_MISSING",
                         "message": f"Aucun portefeuille {plan.currency} sur ce compte."}]},
            status=422,
        )

    key = (data.get("idempotencyKey") or "").strip() or uuid.uuid4().hex
    credit_key = f"savings-withdraw:{plan.pk}:{key}"
    actor = getattr(request.user, "sub", "")
    try:
        with transaction.atomic():
            verrou = SavingsPlan.objects.select_for_update().get(pk=plan.pk)
            if verrou.balance < amount:
                raise InsufficientFundsError(
                    f"Solde d'épargne insuffisant : {verrou.balance} {plan.currency} "
                    f"disponibles pour un retrait de {amount} {plan.currency}.",
                )
            movement = caisses_deposit(wallet_id=wallet.pk, amount=amount, channel=channel,
                                        idempotency_key=credit_key, by=actor)
            withdrawal = SavingsWithdrawal.objects.create(plan=plan, amount=amount, channel=channel)
            SavingsPlan.objects.filter(pk=plan.pk).update(balance=F("balance") - amount)
            # Événement comptable (B9) — même transaction que le mouvement et l'inscription.
            events.emettre(
                SavingsEvent.Type.SAVINGS_WITHDRAWN, plan=plan, amount=amount, actor_sub=actor,
                withdrawalId=withdrawal.pk, walletMovementId=movement.pk,
                walletId=wallet.pk, canalDeclare=channel,
            )
            audit_record(actor=actor, action="savings.plan.withdraw",
                         entity_type="SavingsPlan", entity_id=str(plan.pk),
                         details={"amount": str(amount), "channel": channel,
                                  "walletMovementId": movement.pk,
                                  "currency": plan.currency})
    except idempotency.IdempotentReplay:
        plan.refresh_from_db()
        return Response(_plan_row(plan))
    except InsufficientFundsError as exc:
        return Response(
            {"detail": f"{exc} Aucun retrait effectué.",
             "errors": [{"code": "SAVINGS_INSUFFICIENT_BALANCE", "message": str(exc)}]},
            status=422,
        )
    except ValidationFailed as exc:
        # Plafond de solde KYC du portefeuille (`caisses.services.deposit`) : le retrait est
        # refusé en entier — on ne rend jamais une partie d'un retrait demandé.
        return Response(
            {"detail": f"{exc} Aucun retrait effectué.",
             "errors": [{"code": "WALLET_CAP_EXCEEDED", "message": str(exc)}]},
            status=422,
        )
    plan.refresh_from_db()
    return Response(_plan_row(plan))


# ─────────────────────────── Configuration de taux (admin) ───────────────────────────
#
# GAP Critique de l'audit : `SavingsRateModal`/`SavingsAdjustmentModal` écrivaient la
# config de taux, les ajustements ET l'audit dans `localStorage`, et calculaient le taux
# mensuel côté client (`val/12`). Il n'existait AUCUN endpoint. Ces vues rapatrient tout :
# écriture atomique en base, taux mensuel calculé serveur, journal d'audit serveur
# (AuditEntry) + historique append-only (SavingsRateChange), consultable par la modale.


def _rate_change_row(c: SavingsRateChange) -> dict:
    return {
        "id": c.pk, "annualRate": float(c.annual_rate), "monthlyRate": float(c.monthly_rate),
        "status": c.status, "action": c.action, "effectiveDate": c.effective_date.isoformat(),
        "reason": c.reason, "actor": c.actor, "date": c.created_at.isoformat(),
    }


def _rate_config_payload(plan: SavingsPlan) -> dict:
    """Configuration de taux COURANTE d'un plan (valeurs vives, servies par le serveur)
    + historique append-only. Aucun taux mensuel n'est laissé au client à calculer."""
    return {
        "planId": plan.pk,
        "annualRate": float(plan.interest_rate),
        "monthlyRate": float(plan.monthly_rate),
        "status": plan.rate_status,
        "maxAnnualRate": float(MAX_ANNUAL_RATE),
        "history": [_rate_change_row(c) for c in plan.rate_changes.all()],
    }


@api_view(["GET", "POST"])
@permission_classes([HasCapability("config")])
def plan_rate_config(request, plan_id):
    """GET : config de taux courante + historique. POST : applique un changement.

    Le taux mensuel n'est JAMAIS lu du corps : il est recalculé (`annuel / 12`). Le
    plafond de 6 % est vérifié ici (les modales le vérifiaient côté client, sans effet
    opposable). Écriture atomique : la ligne d'historique, la mise à jour du plan et
    l'audit committent ensemble ou pas du tout (P3 append-only + §5 écriture atomique)."""
    plan = SavingsPlan.objects.filter(pk=plan_id).first()
    if not plan:
        return Response({"detail": "Plan introuvable.", "code": "PLAN_NOT_FOUND"}, status=404)

    if request.method == "GET":
        return Response(_rate_config_payload(plan))

    data = request.data or {}
    action = data.get("action", SavingsRateChange.Action.RATE_UPDATE)
    if action not in SavingsRateChange.Action.values:
        return Response({"detail": f"Action inconnue : {action}.",
                         "errors": [{"code": "ACTION_UNKNOWN",
                                     "message": f"Action de taux inconnue : {action}."}]}, status=422)

    errors: list[dict] = []
    # Selon l'action, on calcule l'état cible. Le blocage force le taux à 0 (pas de
    # rémunération) ; la suspension/réactivation conserve le taux courant.
    if action == SavingsRateChange.Action.BLOCK:
        annual, status = Decimal("0"), SavingsPlan.RateStatus.BLOQUE
    elif action == SavingsRateChange.Action.SUSPEND:
        annual, status = plan.interest_rate, SavingsPlan.RateStatus.SUSPENDU
    elif action == SavingsRateChange.Action.RESUME:
        annual, status = plan.interest_rate, SavingsPlan.RateStatus.ACTIF
    else:  # RATE_UPDATE
        annual = to_decimal(data.get("annualRate"), default="-1")
        status = SavingsPlan.RateStatus.ACTIF
        if annual < 0:
            errors.append({"code": "RATE_NEGATIVE", "message": "Le taux ne peut pas être négatif."})
        elif annual > MAX_ANNUAL_RATE:
            errors.append({"code": "RATE_ABOVE_MAX",
                           "message": f"Le taux ne peut excéder {MAX_ANNUAL_RATE} %."})

    if errors:
        return Response({"detail": errors[0]["message"], "errors": errors}, status=422)

    annual = annual.quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)
    monthly = _monthly_rate(annual)
    effective = to_date(data.get("effectiveDate")) or datetime.date.today()
    reason = (data.get("reason") or "").strip()
    actor = getattr(request.user, "sub", "")

    with transaction.atomic():
        change = SavingsRateChange.objects.create(
            plan=plan, annual_rate=annual, monthly_rate=monthly, status=status,
            effective_date=effective, action=action, reason=reason, actor=actor,
        )
        SavingsPlan.objects.filter(pk=plan.pk).update(
            interest_rate=annual, monthly_rate=monthly, rate_status=status)
        audit_record(actor=actor, action="savings.plan.rate_change",
                     entity_type="SavingsPlan", entity_id=str(plan.pk),
                     details={"action": action, "annualRate": str(annual),
                              "monthlyRate": str(monthly), "status": status,
                              "effectiveDate": effective.isoformat(), "reason": reason,
                              "changeId": change.pk})
    plan.refresh_from_db()
    return Response(_rate_config_payload(plan))


# ─────────────────────────── Ajustement des modalités (admin) ───────────────────────────


def _growth_projection(balance: Decimal, periodic: Decimal, frequency: str,
                       target: Decimal) -> list[dict]:
    """Simulation de croissance PROJETÉE CÔTÉ SERVEUR (l'ancienne modale la calculait en
    JS). Dépôts réguliers, sans intérêt ni retrait — projection assumée comme telle. Les
    dates avancent d'un pas fixe par fréquence ; on s'arrête à l'atteinte de la cible."""
    if periodic <= 0:
        return []
    rows: list[dict] = []
    step = _FREQUENCY_DAYS.get(frequency, 30)
    current = datetime.date.today()
    projected = balance
    for i in range(1, PROJECTION_ROWS + 1):
        current = current + datetime.timedelta(days=step)
        projected = (projected + periodic).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        rows.append({"num": i, "date": current.isoformat(),
                     "deposit": float(periodic), "projected": float(projected)})
        if target > 0 and projected >= target:
            break
    return rows


def _adjustment_metrics(plan: SavingsPlan) -> dict:
    """Métriques dérivées de l'ajustement — CALCULÉES SERVEUR (reste à épargner, nombre de
    dépôts nécessaires). `depositsNeeded` est `null` sans versement périodique : on ne rend
    pas « ∞ » comme un nombre, on dit qu'il n'est pas calculable."""
    target = plan.objectif
    balance = plan.balance
    periodic = plan.periodic_deposit
    remaining = max(Decimal("0"), target - balance)
    deposits_needed = None
    if periodic > 0:
        deposits_needed = int((remaining / periodic).to_integral_value(rounding=ROUND_CEILING))
    projection = _growth_projection(balance, periodic, plan.frequency, target)
    maturity = projection[-1]["date"] if deposits_needed and projection else None
    return {
        "remaining": float(remaining),
        "depositsNeeded": deposits_needed,
        "projectedMaturity": maturity,
        "projection": projection,
    }


def _adjustment_row(a: SavingsAdjustment) -> dict:
    return {
        "id": a.pk, "targetAmount": float(a.target_amount), "depositMode": a.deposit_mode,
        "frequency": a.frequency, "periodicDeposit": float(a.periodic_deposit),
        "reason": a.reason, "actor": a.actor, "date": a.created_at.isoformat(),
    }


def _adjustment_payload(plan: SavingsPlan) -> dict:
    return {
        "planId": plan.pk,
        "targetAmount": float(plan.objectif),
        "currentBalance": float(plan.balance),
        "depositMode": plan.deposit_channel,
        "frequency": plan.frequency,
        "periodicDeposit": float(plan.periodic_deposit),
        "currency": plan.currency,
        "metrics": _adjustment_metrics(plan),
        "history": [_adjustment_row(a) for a in plan.adjustments.all()],
    }


@api_view(["GET", "POST"])
@permission_classes([HasCapability("config")])
def plan_adjustment(request, plan_id):
    """GET : modalités courantes + métriques/projection serveur + historique. POST :
    persiste un ajustement de MODALITÉS. Le solde n'est jamais modifié ici : il ne bouge
    que par un mouvement d'argent tracé (conservation, §4)."""
    plan = SavingsPlan.objects.filter(pk=plan_id).first()
    if not plan:
        return Response({"detail": "Plan introuvable.", "code": "PLAN_NOT_FOUND"}, status=404)

    if request.method == "GET":
        return Response(_adjustment_payload(plan))

    data = request.data or {}
    errors: list[dict] = []

    # Champ absent = « garder la valeur courante » (comme fréquence/mode plus bas) ; on ne
    # valide donc que ce qui est explicitement fourni. Sentinelle `-1` pour distinguer une
    # saisie négative d'une absence.
    target = to_decimal(data.get("targetAmount"), default=str(plan.objectif)) \
        if "targetAmount" in data else plan.objectif
    if target < 0:
        errors.append({"code": "TARGET_INVALID", "message": "L'objectif cible doit être positif ou nul."})
    periodic = to_decimal(data.get("periodicDeposit"), default="-1") \
        if "periodicDeposit" in data else plan.periodic_deposit
    if periodic < 0:
        errors.append({"code": "PERIODIC_INVALID",
                       "message": "Le versement périodique doit être positif ou nul."})
    frequency = data.get("frequency", plan.frequency)
    if frequency not in SavingsPlan.DepositFrequency.values:
        errors.append({"code": "FREQUENCY_UNKNOWN", "message": f"Fréquence inconnue : {frequency}."})
    deposit_mode = data.get("depositMode", plan.deposit_channel)
    if deposit_mode not in SavingsPlan.Channel.values:
        errors.append({"code": "MODE_UNKNOWN", "message": f"Mode de dépôt inconnu : {deposit_mode}."})

    if errors:
        return Response({"detail": errors[0]["message"], "errors": errors}, status=422)

    target = target.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    periodic = periodic.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    reason = (data.get("reason") or "").strip()
    actor = getattr(request.user, "sub", "")

    with transaction.atomic():
        adj = SavingsAdjustment.objects.create(
            plan=plan, target_amount=target, deposit_mode=deposit_mode,
            frequency=frequency, periodic_deposit=periodic, reason=reason, actor=actor,
        )
        SavingsPlan.objects.filter(pk=plan.pk).update(
            objectif=target, frequency=frequency, deposit_channel=deposit_mode,
            periodic_deposit=periodic)
        audit_record(actor=actor, action="savings.plan.adjustment",
                     entity_type="SavingsPlan", entity_id=str(plan.pk),
                     details={"targetAmount": str(target), "frequency": frequency,
                              "depositMode": deposit_mode, "periodicDeposit": str(periodic),
                              "reason": reason, "adjustmentId": adj.pk})
    plan.refresh_from_db()
    return Response(_adjustment_payload(plan))


# ─────────────────────────── Affectation de groupe (admin) ───────────────────────────


@api_view(["POST"])
@permission_classes([HasCapability("cooperatives")])
def assign_group(request):
    """Affecte un titulaire (par `sub`) à un groupe, ou le désaffecte (`groupId` vide/`none`).

    Remplace l'ancien `admin_savings_groups` de `localStorage`, qui stockait les membres
    par NOM dans le navigateur : une affectation qui ne survivait pas à un vidage de cache
    et qu'aucun autre poste ne voyait. L'adhésion est désormais l'unique source
    (`SavingsGroupMember`), exclusive (un titulaire = au plus un groupe d'épargne à ce
    stade), et l'affectation est auditée côté serveur."""
    data = request.data or {}
    user_sub = (data.get("userSub") or "").strip()
    if not user_sub:
        return Response({"detail": "Titulaire (userSub) requis.",
                         "errors": [{"code": "USER_REQUIRED",
                                     "message": "Titulaire (userSub) requis."}]}, status=422)

    from accounts.models import FintechUser
    user = FintechUser.objects.filter(sub=user_sub).first()
    if not user:
        return Response({"detail": "Titulaire introuvable.", "code": "USER_NOT_FOUND"}, status=404)

    raw_group = data.get("groupId")
    target_group = None
    if raw_group not in (None, "", "none"):
        target_group = SavingsGroup.objects.filter(pk=raw_group).first()
        if not target_group:
            return Response({"detail": "Groupe introuvable.", "code": "GROUP_NOT_FOUND"}, status=404)

    actor = getattr(request.user, "sub", "")
    with transaction.atomic():
        # Affectation exclusive : on retire de tout groupe d'abord, puis on ajoute.
        SavingsGroupMember.objects.filter(user=user).delete()
        if target_group is not None:
            SavingsGroupMember.objects.get_or_create(group=target_group, user=user)
        audit_record(actor=actor, action="savings.group.assign_member",
                     entity_type="SavingsGroup",
                     entity_id=str(target_group.pk) if target_group else "",
                     details={"userSub": user_sub,
                              "groupId": target_group.pk if target_group else None,
                              "groupName": target_group.name if target_group else None})
    return Response({
        "userSub": user_sub,
        "groupId": target_group.pk if target_group else None,
        "groupName": target_group.name if target_group else None,
    })


def _group_row(g: SavingsGroup) -> dict:
    return {
        "id": g.pk, "name": g.name, "type": g.type, "description": g.description,
        "rate": float(g.rate), "frequency": g.frequency, "balance": float(g.balance),
        "membersCount": g.members.count(),
        "members": [m.user.full_name or m.user.email for m in g.members.select_related("user").all()],
        "status": "Actif",  # pas de cycle de vie suspendu/fermé pour les groupes à ce stade
    }


def _group_detail_payload(g: SavingsGroup) -> dict:
    """Fiche détaillée d'un groupe (gap #6) — SUPERSET de `_group_row`, servie au panneau
    de détail. Ajoute l'historique des membres (date d'adhésion) et le journal des demandes
    d'intégration. On ne FABRIQUE pas de « cotisations individuelles » : aucun mouvement
    d'argent n'est aujourd'hui rattaché à un groupe, donc afficher un montant par membre
    serait un chiffre inventé (§4.6). Le champ existe, honnêtement à null, tant que le
    modèle de cotisation de groupe n'existe pas."""
    members = [
        {"sub": m.user_id, "name": m.user.full_name or m.user.email,
         "joinedAt": m.joined_at.isoformat(), "contribution": None}
        for m in g.members.select_related("user").order_by("joined_at")
    ]
    requests = [
        {"id": r.pk, "userName": r.user.full_name or r.user.email, "reason": r.reason,
         "status": r.status, "date": r.created_at.isoformat()}
        for r in g.integration_requests.select_related("user").all()
    ]
    return {
        **_group_row(g),
        "adminSub": g.admin_sub,
        "createdAt": g.created_at.isoformat(),
        "memberHistory": members,
        "requests": requests,
        "contributionsTracked": False,
    }


def _group_public_row(g: SavingsGroup) -> dict:
    """Ligne servie à un MEMBRE : de quoi choisir un groupe à rejoindre, rien de plus.

    Deux champs de `_group_row` sont retirés, et ce sont les deux qui ne lui appartiennent
    pas : `balance` — l'encours du groupe, un agrégat de l'institution — et `members` —
    l'identité nominative de tiers, que nul n'a consenti à publier. `membersCount` reste :
    la taille d'un groupe est ce qu'on veut savoir avant d'y adhérer, et elle ne désigne
    personne.
    """
    return {k: v for k, v in _group_row(g).items() if k not in ("balance", "members")}


@api_view(["GET", "POST"])
@permission_classes([CapaciteSelonMethode(GET="read", POST="cooperatives")])
def groups(request):
    """GET : le catalogue des groupes, réduit pour un membre (cf. `_group_public_row`).
    POST : CRÉER un groupe et fixer son taux — capacité `cooperatives`, jamais `create`
    (que le rôle `invest` porte, ce qui mettait la création d'une coopérative d'épargne
    à la portée d'un investisseur)."""
    if request.method == "GET":
        ligne = _group_row if getattr(request.user, "is_staff_role", False) else _group_public_row
        return Response([ligne(g) for g in SavingsGroup.objects.all()])
    data = request.data or {}
    group = SavingsGroup.objects.create(
        name=data.get("name", ""), type=data.get("type", "AVEC"), description=data.get("description", ""),
        rate=data.get("rate", "6.0"), frequency=data.get("frequency", "mensuel"),
        admin_sub=getattr(request.user, "sub", ""),
    )
    audit_record(actor=getattr(request.user, "sub", ""), action="savings.group.create",
                 entity_type="SavingsGroup", entity_id=str(group.pk))
    return Response(_group_row(group), status=201)


@api_view(["GET", "PATCH", "DELETE"])
@permission_classes([IsStaff, CapaciteSelonMethode(GET="read", PATCH="cooperatives",
                                                   DELETE="config")])
def group_detail(request, group_id):
    """Fiche complète d'un groupe — historique nominatif des adhésions et journal des
    demandes d'intégration. Interne : un membre a `my_groups` (ses adhésions) et le
    catalogue réduit de `GET /groups`. Ici, il lirait qui d'autre a demandé à entrer dans
    le groupe, et pourquoi."""
    group = SavingsGroup.objects.filter(pk=group_id).first()
    if not group:
        return Response({"detail": "Groupe introuvable."}, status=404)
    if request.method == "DELETE":
        group.delete()
        return Response({"detail": "Groupe supprimé."})
    if request.method == "PATCH":
        data = request.data or {}
        for field, model_field in (("name", "name"), ("description", "description"), ("rate", "rate"),
                                    ("frequency", "frequency")):
            if field in data:
                setattr(group, model_field, data[field])
        group.save()
        audit_record(actor=getattr(request.user, "sub", ""), action="savings.group.update",
                     entity_type="SavingsGroup", entity_id=str(group.pk),
                     details={"rate": str(group.rate), "frequency": group.frequency})
    # GET/PATCH renvoient la fiche détaillée (superset de la ligne de liste) : le panneau
    # de détail (gap #6) et la modale de gestion lisent la même source.
    return Response(_group_detail_payload(group))


@api_view(["GET"])
@permission_classes([IsStaff, HasCapability("read")])
def group_audit(request, group_id):
    """Journal d'audit SERVEUR d'un groupe (création, mise à jour de taux, affectations,
    décisions d'intégration). Remplace `group_audit_${id}` de `localStorage`, qui vivait
    dans un seul navigateur. Lecture du journal append-only partagé (`AuditEntry`)."""
    group = SavingsGroup.objects.filter(pk=group_id).first()
    if not group:
        return Response({"detail": "Groupe introuvable."}, status=404)
    from audit.models import AuditEntry

    entries = AuditEntry.objects.filter(
        entity_type="SavingsGroup", entity_id=str(group_id),
    ).order_by("-created_at")[:100]
    return Response([
        {"id": e.pk, "action": e.action, "actor": e.actor, "details": e.details,
         "date": e.created_at.isoformat()}
        for e in entries
    ])


@api_view(["GET"])
@permission_classes([HasCapability("validate")])
def all_group_requests(request):
    """Vue admin (AdminGroupsTable) — toutes les demandes d'intégration, tous groupes."""
    reqs = GroupIntegrationRequest.objects.select_related("group", "user").all()
    return Response([
        {"id": r.pk, "groupId": r.group_id, "groupName": r.group.name,
         "userName": r.user.full_name or r.user.email, "reason": r.reason, "status": r.status,
         "date": r.created_at.isoformat()}
        for r in reqs
    ])


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def my_groups(request):
    memberships = SavingsGroupMember.objects.filter(user=request.user).select_related("group")
    return Response([
        {"id": m.group.pk, "name": m.group.name, "type": m.group.type, "rate": float(m.group.rate),
         "frequency": m.group.frequency, "balance": float(m.group.balance)}
        for m in memberships
    ])


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def my_group_requests(request):
    reqs = GroupIntegrationRequest.objects.filter(user=request.user).select_related("group")
    return Response([
        {"id": r.pk, "groupName": r.group.name, "reason": r.reason, "status": r.status,
         "date": r.created_at.isoformat()}
        for r in reqs
    ])


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def request_group_integration(request, group_id):
    group = SavingsGroup.objects.filter(pk=group_id).first()
    if not group:
        return Response({"detail": "Groupe introuvable."}, status=404)
    req = GroupIntegrationRequest.objects.create(
        group=group, user=request.user, reason=(request.data or {}).get("reason", ""),
    )
    return Response({"id": req.pk, "status": req.status}, status=201)


@api_view(["GET"])
@permission_classes([HasCapability("validate")])
def group_integration_requests(request, group_id):
    reqs = GroupIntegrationRequest.objects.filter(group_id=group_id)
    return Response([{"id": r.pk, "userSub": r.user_id, "reason": r.reason, "status": r.status} for r in reqs])


@api_view(["POST"])
@permission_classes([HasCapability("validate")])
def decide_group_integration(request, request_id):
    req = GroupIntegrationRequest.objects.filter(pk=request_id).first()
    if not req:
        return Response({"detail": "Demande introuvable."}, status=404)
    decision = (request.data or {}).get("decision")
    if decision not in ("approved", "rejected"):
        return Response({"detail": "decision doit être 'approved' ou 'rejected'."}, status=400)
    req.status = decision
    req.save(update_fields=["status"])
    if decision == "approved":
        SavingsGroupMember.objects.get_or_create(group=req.group, user=req.user)
    audit_record(actor=getattr(request.user, "sub", ""), action="savings.group.integration_decision",
                 entity_type="GroupIntegrationRequest", entity_id=str(req.pk), details={"decision": decision})
    return Response({"id": req.pk, "status": req.status})
