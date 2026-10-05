"""Logement des écarts d'arrondi — le point de passage unique.

Toute opération qui arrondit un montant issu d'une division (conversion de change,
intérêt, quote-part) appelle `arrondir_et_loger()`. Elle reçoit le montant au centime,
et le sous-centime part au compte d'écarts de la devise, avec de quoi reconstituer le
calcul.

Ce module ne calcule RIEN : l'arrondi vit dans `common/money.py`, la conversion dans
`fx/services.py`. Il ne fait que raccorder l'un à l'autre et tenir le journal — un
troisième endroit qui saurait arrondir serait un troisième endroit à corriger.
"""
from __future__ import annotations

from decimal import Decimal

from django.db import transaction
from django.db.models import F

from common.money import ZERO, arrondir, arrondir_residu

from .models import RoundingResidueAccount, RoundingResidueEntry


def compte_de(devise: str) -> RoundingResidueAccount:
    """Compte d'écarts de la devise, créé à la première utilisation.

    Création paresseuse plutôt que fixture : un compte d'écarts pour une devise qui n'a
    jamais servi n'a aucun sens à afficher, et une fixture figerait la liste des devises
    alors que le référentiel FX en accepte d'autres.
    """
    compte, _ = RoundingResidueAccount.objects.get_or_create(currency=(devise or "").upper())
    return compte


def loger_residu(*, devise: str, exact, arrondi, residu, source: str,
                 entity_type: str = "", entity_id: str = "", actor_sub: str = "",
                 detail: dict | None = None) -> RoundingResidueEntry | None:
    """Écrit une ligne de résidu et met à jour le cumul. `None` si le résidu est nul.

    Un résidu nul n'écrit RIEN : l'immense majorité des opérations tombent juste, et une
    ligne à zéro par opération noierait le journal sous du bruit — l'auditeur cherche les
    écarts, pas la preuve qu'il n'y en a pas eu.

    Le cumul est mis à jour par `F()` : deux conversions concurrentes sur la même devise
    ne doivent pas s'écraser l'une l'autre (lecture-modification-écriture en Python
    perdrait un résidu sur deux sous charge).
    """
    residu_stocke = arrondir_residu(residu)
    if residu_stocke == ZERO:
        return None

    compte = compte_de(devise)
    with transaction.atomic():
        ligne = RoundingResidueEntry.objects.create(
            account=compte,
            amount=residu_stocke,
            exact_amount=arrondir_residu(exact),
            rounded_amount=Decimal(arrondi),
            source=source,
            entity_type=entity_type,
            entity_id=str(entity_id or ""),
            actor_sub=actor_sub or "",
            detail=detail or {},
        )
        RoundingResidueAccount.objects.filter(pk=compte.pk).update(
            balance=F("balance") + residu_stocke,
            entry_count=F("entry_count") + 1,
        )
    return ligne


def arrondir_et_loger(valeur, *, devise: str, source: str, entity_type: str = "",
                      entity_id: str = "", actor_sub: str = "",
                      detail: dict | None = None) -> Decimal:
    """Arrondit au centime ET loge le sous-centime. Rend le montant utilisable.

    C'est la fonction que doivent appeler les chemins qui DÉPLACENT de l'argent. Les
    chemins de LECTURE (agrégats, simulations, affichage) utilisent `common.money.arrondir`
    directement : convertir pour afficher ne crée aucun écart à conserver, et écrire au
    journal à chaque rafraîchissement d'un tableau de bord le rendrait illisible.
    """
    montant, residu = arrondir(valeur)
    loger_residu(devise=devise, exact=valeur, arrondi=montant, residu=residu, source=source,
                 entity_type=entity_type, entity_id=entity_id, actor_sub=actor_sub,
                 detail=detail)
    return montant


def etat_des_comptes() -> list[dict]:
    """Photo des comptes d'écarts, pour l'écran d'audit."""
    return [
        {
            "currency": c.currency,
            "balance": str(c.balance),
            "centimesEntiers": str(c.centimes_entiers),
            "entryCount": c.entry_count,
            "updatedAt": c.updated_at.isoformat(),
        }
        for c in RoundingResidueAccount.objects.all()
    ]
