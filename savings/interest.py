"""Arrêté mensuel des intérêts d'épargne.

CE QUI N'EXISTAIT PAS
---------------------
`SavingsPlan.accrued_interest` était servi à l'écran (`savings/views.py`) et **n'était
écrit nulle part** : le champ existait depuis la migration initiale, personne ne
l'alimentait. Tout client voyait donc « intérêts courus : 0,00 » quelle que soit
l'ancienneté de son plan. Ce module est le moteur manquant, pas un câblage.

POURQUOI IL RELÈVE DU PRINCIPE 4 bis
------------------------------------
`solde × taux mensuel` ne tombe presque jamais au centime : 233 344,10 CDF à 0,375 %
donne 875,04037…, et 30,00 USD à 0,375 % donne 0,1125. C'est exactement le lieu où le
sous-centime naît, mois après mois, sur chaque plan. Il part au compte d'écarts
(`caisses.rounding`), avec le plan et la période en référence.

IDEMPOTENCE
-----------
Un arrêté est identifié par `(plan, période AAAA-MM)`, sous contrainte d'unicité. Relancer
la commande sur une période déjà arrêtée ne recrée rien et ne double aucun intérêt — un
moteur d'intérêts qu'on n'ose pas relancer après un incident est un moteur qu'on
n'exécutera pas au bon moment.
"""
from __future__ import annotations

from decimal import Decimal

from django.db import IntegrityError, transaction
from django.db.models import F
from django.utils import timezone

from audit.services import record as audit_record
from caisses.models import RoundingResidueEntry
from caisses.rounding import arrondir_et_loger
from common.money import ZERO

from .models import SavingsInterestAccrual, SavingsPlan

#: Base des taux : `monthly_rate` est un POURCENTAGE mensuel (0,375 = 0,375 %/mois),
#: pas un coefficient. Le diviseur vit ici plutôt qu'en dur dans le calcul.
CENT_POUR_CENT = Decimal("100")


def periode_de(jour=None) -> str:
    """Clé de période « AAAA-MM » de la date donnée (aujourd'hui par défaut)."""
    jour = jour or timezone.localdate()
    return f"{jour.year:04d}-{jour.month:02d}"


def taux_applicable(plan: SavingsPlan) -> Decimal:
    """Taux mensuel retenu pour l'arrêté, selon la gouvernance du taux.

    `BLOQUE` cesse de produire des intérêts (taux 0) sans clôturer le plan ; `SUSPENDU`
    gèle le taux — il continue donc de courir à sa valeur figée. C'est la sémantique
    déjà écrite dans `SavingsPlan.RateStatus` : elle est LUE ici, pas réinventée.
    """
    if plan.rate_status == SavingsPlan.RateStatus.BLOQUE:
        return ZERO
    return plan.monthly_rate or ZERO


@transaction.atomic
def arreter_plan(plan: SavingsPlan, *, periode: str, actor_sub: str = "") -> SavingsInterestAccrual | None:
    """Arrête les intérêts d'un plan pour une période. `None` si déjà arrêté ou sans effet.

    L'intérêt est calculé sur le solde À L'INSTANT DE L'ARRÊTÉ, pas sur un solde moyen :
    c'est la convention la plus simple à expliquer à un épargnant, et la seule que les
    données actuelles permettent (aucun historique de solde journalier n'est tenu). Ce
    choix est écrit ici pour qu'il se conteste plutôt qu'il se découvre.
    """
    taux = taux_applicable(plan)
    base = plan.balance or ZERO
    exact = base * taux / CENT_POUR_CENT

    if exact == ZERO:
        return None

    interet = arrondir_et_loger(
        exact, devise=plan.currency,
        source=RoundingResidueEntry.Source.SAVINGS_INTEREST,
        entity_type="SavingsPlan", entity_id=str(plan.pk), actor_sub=actor_sub,
        detail={"periode": periode, "base": str(base), "tauxMensuelPct": str(taux)},
    )

    try:
        arrete = SavingsInterestAccrual.objects.create(
            plan=plan, period=periode, amount=interet, base_balance=base,
            monthly_rate=taux, currency=plan.currency, created_by=actor_sub or "",
        )
    except IntegrityError:
        # Période déjà arrêtée : la contrainte d'unicité a joué. La transaction est
        # annulée, résidu compris — un arrêté refusé ne laisse pas d'écart derrière lui.
        return None

    SavingsPlan.objects.filter(pk=plan.pk).update(
        accrued_interest=F("accrued_interest") + interet,
    )
    audit_record(actor=actor_sub or "system", action="savings.interest.accrue",
                 entity_type="SavingsPlan", entity_id=str(plan.pk),
                 details={"periode": periode, "montant": str(interet),
                          "base": str(base), "tauxMensuelPct": str(taux),
                          "devise": plan.currency})
    return arrete


def arreter_tous(*, periode: str | None = None, actor_sub: str = "") -> dict:
    """Arrêté de tous les plans ACTIFS pour une période. Rend un rapport chiffré.

    Les plans clôturés sont exclus : un plan fermé ne rémunère plus. Le rapport porte les
    effectifs ET le détail des sauts, parce qu'un arrêté qui annonce « 12 plans traités »
    sans dire combien ont été ignorés, et pourquoi, ne se vérifie pas.
    """
    periode = periode or periode_de()
    rapport = {"periode": periode, "traites": 0, "ignores": 0, "total": ZERO,
               "parDevise": {}, "sauts": []}

    for plan in SavingsPlan.objects.filter(status=SavingsPlan.Status.ACTIF).iterator():
        arrete = arreter_plan(plan, periode=periode, actor_sub=actor_sub)
        if arrete is None:
            rapport["ignores"] += 1
            rapport["sauts"].append({
                "planId": plan.pk,
                "motif": ("déjà arrêté" if SavingsInterestAccrual.objects.filter(
                    plan=plan, period=periode).exists() else "intérêt nul"),
            })
            continue
        rapport["traites"] += 1
        rapport["total"] += arrete.amount
        rapport["parDevise"][plan.currency] = (
            rapport["parDevise"].get(plan.currency, ZERO) + arrete.amount
        )

    rapport["total"] = str(rapport["total"])
    rapport["parDevise"] = {k: str(v) for k, v in rapport["parDevise"].items()}
    return rapport
