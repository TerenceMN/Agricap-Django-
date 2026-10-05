"""Cycle de vie des actifs gageables.

Toute transition passe par ce module — jamais d'écriture directe de `status`
depuis une vue, et surtout jamais depuis un payload client (l'ancien PATCH
laissait le client fixer son propre statut, ce qui vidait la garantie de sa
substance).

Transitions autorisées :
    declare  → verifie   (agent terrain, fixe la valeur retenue)
    declare  → rejete    (agent terrain, motif obligatoire)
    verifie  → gage      (confirmation d'une garantie, verrou atomique)
    libere   → gage      (ré-engagement d'un actif libéré)
    gage     → libere    (libération de la garantie)
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # imports differes a l'execution (cf. corps des fonctions)
    from assets.models import Asset

from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.utils import timezone


class AssetError(Exception):
    """Transition invalide ou règle métier violée."""


class AssetAlreadyPledged(AssetError):
    """L'actif est déjà nanti sur un autre dossier."""


def _decote() -> Decimal:
    """Décote institutionnelle appliquée à la valeur vérifiée.

    Lue depuis `InstitutionConfig` (principe 8 : les règles vivent en base).
    Repli sur 30 % avec warning si aucune configuration n'est chargée.
    """
    try:
        from referentiel.models import InstitutionConfig
        cfg = InstitutionConfig.active()
        if cfg is not None and cfg.decote_garantie is not None:
            return Decimal(str(cfg.decote_garantie))
    except Exception:
        pass

    import logging
    logging.getLogger(__name__).warning(
        "InstitutionConfig indisponible — décote de garantie par défaut (30 %%) appliquée."
    )
    return Decimal(str(getattr(settings, "CREDIT_FALLBACK_GUARANTEE_HAIRCUT", "0.30")))


def valeur_apres_decote(valeur_verifiee: Decimal) -> Decimal:
    """Valeur retenue = valeur vérifiée × (1 − décote), arrondie au centime."""
    montant = Decimal(str(valeur_verifiee)) * (Decimal("1") - _decote())
    return montant.quantize(Decimal("0.01"))


# ── Vérification terrain ──────────────────────────────────────────────────────

@transaction.atomic
def verify_asset(asset, verifier_sub: str, valeur_verifiee: Decimal,
                 documents: list | None = None) -> None:
    """`declare` → `verifie`. L'agent constate l'actif et fixe sa valeur.

    `valeur_verifiee` est la valeur constatée sur le terrain ; la valeur retenue
    en découle par application de la décote institutionnelle.
    """
    if asset.status not in (asset.Status.DECLARE, asset.Status.REJETE):
        raise AssetError(
            f"Un actif au statut « {asset.status} » ne peut pas être vérifié."
        )
    if asset.type == asset.Type.AUTRE:
        raise AssetError(
            "Un actif de catégorie « autre » n'est pas gageable : "
            "précisez sa catégorie avant vérification."
        )

    montant = Decimal(str(valeur_verifiee))
    if montant <= 0:
        raise AssetError("La valeur vérifiée doit être strictement positive.")

    asset.valeur_retenue = valeur_apres_decote(montant)
    asset.status = asset.Status.VERIFIE
    asset.verifie_par_sub = verifier_sub
    asset.verifie_le = timezone.now()
    asset.motif_rejet = ""
    if documents:
        asset.documents = list(documents)
    asset.save(update_fields=[
        "valeur_retenue", "status", "verifie_par_sub", "verifie_le",
        "motif_rejet", "documents", "updated_at",
    ])


@transaction.atomic
def reject_asset(asset, verifier_sub: str, motif: str) -> None:
    """`declare` → `rejete`. Motif obligatoire — une décision sans motif n'en est pas une."""
    if asset.status != asset.Status.DECLARE:
        raise AssetError(
            f"Un actif au statut « {asset.status} » ne peut pas être rejeté."
        )
    if not (motif or "").strip():
        raise AssetError("Le motif de rejet est obligatoire.")

    asset.status = asset.Status.REJETE
    asset.motif_rejet = motif.strip()
    asset.verifie_par_sub = verifier_sub
    asset.verifie_le = timezone.now()
    asset.valeur_retenue = None
    asset.save(update_fields=[
        "status", "motif_rejet", "verifie_par_sub", "verifie_le",
        "valeur_retenue", "updated_at",
    ])


# ── Gage / libération ─────────────────────────────────────────────────────────

@transaction.atomic
def pledge_asset(asset_id: int, application) -> "Asset":
    """`verifie|libere` → `gage`, sous verrou.

    Le `select_for_update` est le cœur de la protection contre le double gage :
    deux dossiers confirmant simultanément une garantie sur le même actif sont
    sérialisés, et le second constate que l'actif est déjà nanti.
    """
    from assets.models import Asset

    asset = Asset.objects.select_for_update().get(pk=asset_id)

    if asset.gage_application_id is not None:
        if asset.gage_application_id == application.pk:
            return asset  # idempotent : déjà gagé sur CE dossier
        raise AssetAlreadyPledged(
            f"L'actif « {asset.name} » est déjà nanti sur un autre dossier."
        )
    if asset.status not in (asset.Status.VERIFIE, asset.Status.LIBERE):
        raise AssetError(
            f"Seul un actif vérifié peut être nanti (statut actuel : « {asset.status} »)."
        )
    if not asset.valeur_retenue or asset.valeur_retenue <= 0:
        raise AssetError("L'actif n'a pas de valeur retenue : vérification incomplète.")

    asset.status = asset.Status.GAGE
    asset.gage_application = application
    asset.save(update_fields=["status", "gage_application", "updated_at"])
    return asset


def invalidate_verification(asset) -> bool:
    """Renvoie un actif modifié en file de vérification. `True` si l'état a changé.

    Un actif `verifie` OU `libere` est `is_pledgeable` et porte une
    `valeur_retenue` certifiée par un agent. Dès que le client touche à sa
    description, cette certification ne porte plus sur le même bien : elle doit
    tomber, sinon un gage levé puis l'actif redésigné resterait mobilisable avec
    la valeur d'un objet qui n'existe plus.

    Ne modifie PAS un actif `gage` (la vue refuse la modification en amont) ni un
    actif `declare`/`rejete` (rien à invalider). N'écrit pas en base : l'appelant
    enregistre, pour rester dans une seule sauvegarde.
    """
    from assets.models import Asset

    if asset.status not in (Asset.Status.VERIFIE, Asset.Status.LIBERE):
        return False

    asset.status = Asset.Status.DECLARE
    asset.valeur_retenue = None
    asset.verifie_par_sub = ""
    asset.verifie_le = None
    return True


@transaction.atomic
def release_asset(asset) -> None:
    """`gage` → `libere`. L'actif redevient mobilisable, valeur retenue conservée."""
    from assets.models import Asset

    locked = Asset.objects.select_for_update().get(pk=asset.pk)
    if locked.status != Asset.Status.GAGE:
        return  # déjà libéré — opération idempotente

    locked.status = Asset.Status.LIBERE
    locked.gage_application = None
    locked.save(update_fields=["status", "gage_application", "updated_at"])
