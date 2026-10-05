"""Paliers KYC (T1/T2/T3) dérivés des documents APPROUVÉS d'un utilisateur — remplace le
`kyc_level`/`monthly_limit` figés de `KycProfile` (valeurs par défaut statiques, jamais
recalculées) par une progression réelle : T1 (aucun document validé) -> T2 (pièce d'identité
validée) -> T3 (pièce d'identité + justificatif de domicile validés). Recalculé au moment de
chaque revue de document (`document_review`), pas sur un planificateur (aucune infrastructure
de tâches planifiées dans ce projet — même principe que `support.sla`)."""
from __future__ import annotations

import logging

from decimal import Decimal

from common.choices import FlowStatus

from .models import Document, KycProfile

#: Valeurs de SECOURS. Les plafonds réels vivent en base (`KycTierLimit`, principe 8) ;
#: ces constantes ne s'appliquent que si la table est vide, et jamais en silence.
LEVEL_LIMITS = {"T1": Decimal("500"), "T2": Decimal("5000"), "T3": Decimal("50000")}
BALANCE_CAPS = {"T1": Decimal("1000"), "T2": Decimal("10000"), "T3": Decimal("1000000")}
DEFAULT_LEVEL = "T1"

logger = logging.getLogger("agricap.gouvernance")


def plafonds(tier: str) -> tuple[Decimal, Decimal]:
    """`(plafond de solde, plafond mensuel)` du palier — base d'abord, code en repli."""
    from .models import KycTierLimit

    ligne = KycTierLimit.objects.filter(tier=tier).first()
    if ligne:
        return ligne.balance_cap, ligne.monthly_limit
    logger.warning(
        "Plafonds KYC du palier %s absents de la base — repli sur les constantes du code "
        "(solde %s, mois %s). Paramétrez-les dans « Limites de dépôt et de retrait ».",
        tier, BALANCE_CAPS.get(tier), LEVEL_LIMITS.get(tier),
    )
    return (BALANCE_CAPS.get(tier, BALANCE_CAPS[DEFAULT_LEVEL]),
            LEVEL_LIMITS.get(tier, LEVEL_LIMITS[DEFAULT_LEVEL]))


def compute_kyc_level(*, user) -> str:
    approved = Document.objects.filter(user=user, status=FlowStatus.APPROVED)
    has_id = approved.filter(type=Document.Type.ID_CARD).exists()
    has_address = approved.filter(type=Document.Type.PROOF_ADDRESS).exists()
    if has_id and has_address:
        return "T3"
    if has_id:
        return "T2"
    return DEFAULT_LEVEL


def sync_kyc_level(*, user) -> KycProfile:
    profile, _ = KycProfile.objects.get_or_create(user=user)
    level = compute_kyc_level(user=user)
    limit = plafonds(level)[1]
    if profile.kyc_level != level or profile.monthly_limit != limit:
        profile.kyc_level = level
        profile.monthly_limit = limit
        profile.save(update_fields=["kyc_level", "monthly_limit", "updated_at"])
    return profile


def monthly_limit_for(*, user) -> Decimal:
    profile = KycProfile.objects.filter(user=user).first()
    return profile.monthly_limit if profile else LEVEL_LIMITS[DEFAULT_LEVEL]


def balance_cap_for(*, user) -> Decimal:
    profile = KycProfile.objects.filter(user=user).first()
    level = profile.kyc_level if profile else DEFAULT_LEVEL
    return plafonds(level)[0]


def monthly_withdrawal_total(*, user, currency: str, now=None) -> Decimal:
    """Somme des retraits déjà effectués ce mois civil, tous portefeuilles de cette devise —
    utilisé pour vérifier le plafond KYC avant d'accepter un nouveau retrait."""
    from django.db.models import Sum
    from django.utils import timezone

    from caisses.models import WalletMovement

    now = now or timezone.now()
    start_of_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    total = WalletMovement.objects.filter(
        wallet__user=user, wallet__currency=currency, kind=WalletMovement.Kind.WITHDRAW,
        created_at__gte=start_of_month,
    ).aggregate(total=Sum("amount"))["total"]
    return total or Decimal("0")


# ── Statut KYC canonique ─────────────────────────────────────────────────────────────
#
# `KycProfile.Status` stocke des LIBELLÉS français comme codes (« Validé », « En attente »)
# — un héritage. Les écrans et `investments` parlent en codes (`VALIDE`, `EN_ATTENTE`).
# La correspondance vit ICI, dans l'app qui possède le KYC, et pas dans chaque lecteur.

_CODE_CANONIQUE = {
    KycProfile.Status.VALIDE: "VALIDE",
    KycProfile.Status.EN_ATTENTE: "EN_ATTENTE",
}

#: Sans profil KYC, l'utilisateur est en attente — jamais validé par défaut.
STATUT_DEFAUT = "EN_ATTENTE"


def statuts_par_sub(subs) -> dict[str, str]:
    """Statut KYC canonique de chaque `sub`, en UN aller-retour.

    `KycProfile` est la SEULE autorité sur le KYC. `investments.Investor.kyc_status`
    existe aussi en base mais n'est écrit par personne : il restait donc éternellement
    à « EN_ATTENTE », si bien que valider un KYC affichait un succès sans que la liste
    des investisseurs ne bouge jamais. Ce module est ce qui manquait pour que le lecteur
    et l'écrivain regardent la même colonne.
    """
    subs = {s for s in subs if s}
    if not subs:
        return {}
    return {
        p.user_id: _CODE_CANONIQUE.get(p.kyc_status, STATUT_DEFAUT)
        for p in KycProfile.objects.filter(user_id__in=subs)
    }
