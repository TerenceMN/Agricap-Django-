"""
Service de gestion des garanties (Étape 4).

Quatre types canoniques :
  epargne   — blocage d'un montant sur SavingsPlan (hold comptable)
  morale    — caution solidaire d'un tiers avec confirmation J+7
  materiel  — gage sur un actif mobilier vérifié (assets.Asset)
  foncier   — hypothèque sur un actif immobilier vérifié (assets.Asset)

Principe 9 — une garantie est opposable ou n'est pas. Concrètement :
  - le type posé doit figurer dans `ValueChain.eligible_guarantees` de la
    filière du dossier (`assert_type_eligible`) ;
  - un gage porte sur un actif **vérifié par un agent**, libre de tout autre
    gage, et pour sa **valeur retenue** après décote — jamais sa valeur déclarée ;
  - le double gage est empêché par un verrou atomique posé à la confirmation.

Le module ne modifie pas les modèles du module savings — il crée uniquement
des enregistrements CreditGuarantee dans le module credits.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # imports differes a l'execution (cf. corps des fonctions)
    from credits.models import CreditGuarantee

import uuid
from decimal import Decimal
from typing import Any

from django.db import transaction
from django.utils import timezone
import logging

logger = logging.getLogger(__name__)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _hold_ref() -> str:
    return f"HOLD-{uuid.uuid4().hex[:8].upper()}"


def _available_savings_balance(savings_plan, currency: str) -> Decimal:
    """
    Solde disponible = balance − somme des holds actifs sur ce plan.
    On reste dans le module credits pour ne pas coupler au module savings.
    """
    from credits.models import CreditGuarantee
    active_holds = CreditGuarantee.objects.filter(
        savings_plan=savings_plan,
        guarantee_type=CreditGuarantee.GuaranteeType.EPARGNE,
        status__in=[
            CreditGuarantee.Status.ACTIVE,
            CreditGuarantee.Status.PENDING,
        ],
    )
    held = sum(g.hold_amount or Decimal(0) for g in active_holds)
    return (savings_plan.balance or Decimal(0)) - held


# ── Garantie épargne ───────────────────────────────────────────────────────────

class InsufficientSavingsError(Exception):
    pass


class GuaranteeError(Exception):
    """Refus de pose/confirmation d'une garantie.

    Chaque sous-classe porte son `code` : c'est lui qui remonte au front, pas le
    texte du message. Une reformulation d'un message ne doit jamais changer le
    comportement d'un client — le front ne doit avoir aucune raison de deviner la
    règle par la signature de la phrase.
    """

    code = "GUARANTEE_ERROR"


@transaction.atomic
def place_savings_hold(
    application,
    savings_plan_id: int,
    amount: Decimal,
    registered_by_sub: str,
    notes: str = "",
) -> "CreditGuarantee":
    """
    Bloque `amount` sur le plan d'épargne `savings_plan_id` pour le dossier.

    Règles :
      - Le plan doit appartenir au client du dossier
      - Le solde disponible (hors holds actifs) doit être ≥ amount
      - Un dossier ne peut avoir qu'un seul hold actif à la fois
    """
    from savings.models import SavingsPlan
    from credits.models import CreditGuarantee

    try:
        plan = SavingsPlan.objects.get(pk=savings_plan_id, user=application.client)
    except SavingsPlan.DoesNotExist:
        raise GuaranteeError("Plan d'épargne introuvable ou n'appartient pas au client.")

    if plan.status != SavingsPlan.Status.ACTIF:
        raise GuaranteeError("Le plan d'épargne n'est pas actif.")

    assert_type_eligible(application, CreditGuarantee.GuaranteeType.EPARGNE)

    available = _available_savings_balance(plan, plan.currency)
    if available < amount:
        raise InsufficientSavingsError(
            f"Solde disponible insuffisant : {available:.2f} {plan.currency} "
            f"(demandé : {amount:.2f} {plan.currency}, dont "
            f"{plan.balance - available:.2f} déjà bloqués)."
        )

    # Libérer un éventuel hold précédent sur ce dossier
    existing = CreditGuarantee.objects.filter(
        application=application,
        guarantee_type=CreditGuarantee.GuaranteeType.EPARGNE,
        status__in=[CreditGuarantee.Status.ACTIVE, CreditGuarantee.Status.PENDING],
    ).first()
    if existing:
        _do_release(existing)

    guarantee = CreditGuarantee.objects.create(
        application=application,
        guarantee_type=CreditGuarantee.GuaranteeType.EPARGNE,
        status=CreditGuarantee.Status.ACTIVE,
        savings_plan=plan,
        hold_amount=amount,
        hold_currency=plan.currency,
        hold_reference=_hold_ref(),
        hold_placed_at=timezone.now(),
        registered_by_sub=registered_by_sub,
        notes=notes,
    )

    # Mettre à jour le champ guarantee_type sur le dossier
    application.guarantee_type = "epargne"
    application.save(update_fields=["guarantee_type", "updated_at"])

    return guarantee


def release_savings_hold(guarantee) -> None:
    """Libère un bloc d'épargne (rejet, annulation, expiration)."""
    if guarantee.guarantee_type != "epargne":
        raise GuaranteeError("Cette garantie n'est pas de type épargne.")
    if guarantee.status == guarantee.Status.RELEASED:
        return
    _do_release(guarantee)


def _do_release(guarantee) -> None:
    guarantee.status = guarantee.Status.RELEASED
    guarantee.hold_released_at = timezone.now()
    guarantee.save(update_fields=["status", "hold_released_at", "updated_at"])

    # Un gage sur actif libère aussi l'actif sous-jacent
    if guarantee.asset_id:
        from assets.services import release_asset
        release_asset(guarantee.asset)


# ── Éligibilité du type de garantie par filière ───────────────────────────────

class GuaranteeTypeNotEligible(GuaranteeError):
    """Type de garantie non admis pour la filière du dossier."""

    code = "GUARANTEE_TYPE_NOT_ELIGIBLE"


def assert_type_eligible(application, guarantee_type: str) -> None:
    """Vérifie que le type figure dans `ValueChain.eligible_guarantees`.

    Le référentiel filière portait déjà ce champ sans que rien ne le contrôle :
    n'importe quel type pouvait être posé sur n'importe quelle filière. Le
    contrôle est fait ici, à la pose, et re-vérifié à la soumission (défense en
    profondeur — un dossier ancien peut contenir un type devenu inéligible
    après mise à jour du référentiel).

    Filière absente du dossier : on n'invente pas de règle, on laisse passer —
    le scoring signalera l'absence de filière par ailleurs.
    """
    chain = getattr(application, "value_chain", None)
    if chain is None:
        return

    eligible = list(getattr(chain, "eligible_guarantees", None) or [])
    if not eligible:
        return  # référentiel non renseigné : pas de restriction inventée

    if guarantee_type not in eligible:
        raise GuaranteeTypeNotEligible(
            f"La garantie « {guarantee_type} » n'est pas admise pour la filière "
            f"{chain.label} (types admis : {', '.join(eligible)})."
        )


# ── Garantie sur actif (materiel / foncier) ───────────────────────────────────

class AssetNotOwned(GuaranteeError):
    """L'actif n'existe pas ou n'appartient pas au client du dossier."""

    code = "ASSET_NOT_OWNED"


class AssetNotVerified(GuaranteeError):
    """L'actif n'a pas été contrôlé par un agent de terrain."""

    code = "ASSET_NOT_VERIFIED"


class AssetAlreadyPledged(GuaranteeError):
    """L'actif est déjà nanti sur un autre dossier."""

    code = "ASSET_ALREADY_PLEDGED"


class AssetCategoryMismatch(GuaranteeError):
    """La catégorie de l'actif ne correspond à aucun type de garantie."""

    code = "ASSET_CATEGORY_MISMATCH"


class AssetNoRetainedValue(GuaranteeError):
    """L'actif n'a pas de valeur retenue : la vérification est incomplète."""

    code = "ASSET_NO_RETAINED_VALUE"


@transaction.atomic
def place_asset_guarantee(
    application,
    asset_id: int,
    registered_by_sub: str,
) -> "CreditGuarantee":
    """Pose un gage sur un actif vérifié du client.

    Cinq règles bloquantes, dans cet ordre — chacune renvoie un code distinct
    pour que le front puisse guider le client plutôt qu'afficher « erreur » :
      1. l'actif existe et appartient au client du dossier   ASSET_NOT_OWNED
      2. il est vérifié et libre de gage                     ASSET_NOT_VERIFIED / ASSET_ALREADY_PLEDGED
      3. sa catégorie donne un type de garantie              ASSET_CATEGORY_MISMATCH
      4. ce type est admis pour la filière                   GUARANTEE_TYPE_NOT_ELIGIBLE
      5. il porte une valeur retenue                         ASSET_NO_RETAINED_VALUE

    La garantie est créée en PENDING : le gage effectif de l'actif n'intervient
    qu'à la confirmation par un agent (`confirm_asset_guarantee`), qui pose le
    verrou atomique contre le double gage.
    """
    from assets.models import Asset
    from credits.models import CreditGuarantee

    # 1. Propriété
    asset = Asset.objects.filter(pk=asset_id).first()
    if asset is None or asset.user_id != application.client_id:
        raise AssetNotOwned(
            "Actif introuvable ou n'appartenant pas au client du dossier."
        )

    # 2. Statut
    if asset.gage_application_id is not None:
        raise AssetAlreadyPledged(
            f"L'actif « {asset.name} » est déjà nanti sur un autre dossier."
        )
    if asset.status not in (Asset.Status.VERIFIE, Asset.Status.LIBERE):
        raise AssetNotVerified(
            f"L'actif « {asset.name} » n'a pas été vérifié par un agent. "
            "Un actif déclaré ne peut pas servir de garantie."
        )

    # 3. Catégorie → type de garantie
    guarantee_type = asset.guarantee_type
    if not guarantee_type:
        raise AssetCategoryMismatch(
            f"La catégorie « {asset.type} » ne correspond à aucun type de garantie."
        )

    # 4. Éligibilité filière
    assert_type_eligible(application, guarantee_type)

    # 5. Valeur retenue
    if not asset.valeur_retenue or asset.valeur_retenue <= 0:
        raise AssetNoRetainedValue(
            "L'actif n'a pas de valeur retenue : la vérification est incomplète."
        )

    # Un même actif ne peut pas être proposé deux fois sur le même dossier
    existing = CreditGuarantee.objects.filter(
        application=application, asset=asset,
        status__in=[CreditGuarantee.Status.PENDING, CreditGuarantee.Status.ACTIVE],
    ).first()
    if existing:
        return existing

    return CreditGuarantee.objects.create(
        application=application,
        guarantee_type=guarantee_type,
        status=CreditGuarantee.Status.PENDING,
        asset=asset,
        covered_amount=asset.valeur_retenue,
        hold_currency=asset.currency,
        registered_by_sub=registered_by_sub,
    )


@transaction.atomic
def confirm_asset_guarantee(guarantee, confirmer_sub: str):
    """Confirme un gage : l'actif passe effectivement en `gage`, sous verrou.

    C'est ici — et pas à la pose — que le double gage est empêché : deux
    dossiers qui confirment simultanément sur le même actif sont sérialisés par
    le `select_for_update` de `pledge_asset`.
    """
    from assets.services import AssetAlreadyPledged, AssetError, pledge_asset
    from credits.models import CreditGuarantee

    if guarantee.guarantee_type not in CreditGuarantee.ASSET_BACKED_TYPES:
        raise GuaranteeError("Cette garantie n'est pas un gage sur actif.")
    if guarantee.status == CreditGuarantee.Status.ACTIVE:
        return guarantee
    if guarantee.status == CreditGuarantee.Status.RELEASED:
        raise GuaranteeError("Garantie déjà levée.")

    try:
        asset = pledge_asset(guarantee.asset_id, guarantee.application)
    except AssetAlreadyPledged as exc:
        raise GuaranteeError(str(exc)) from exc
    except AssetError as exc:
        raise GuaranteeError(str(exc)) from exc

    guarantee.status = CreditGuarantee.Status.ACTIVE
    guarantee.covered_amount = asset.valeur_retenue
    guarantee.confirmed_by_sub = confirmer_sub
    guarantee.confirmed_at = timezone.now()
    guarantee.save(update_fields=[
        "status", "covered_amount", "confirmed_by_sub", "confirmed_at", "updated_at",
    ])
    return guarantee


# ── Caution solidaire (garantie morale) ───────────────────────────────────────
#
# Délai historique de confirmation par un AGENT, conservé pour les cautions
# déclaratives d'avant le consentement opposable. La fenêtre qui compte
# désormais est celle du GARANT (`credits.guarantor.consent_window_hours`).
MORAL_GUARANTEE_EXPIRY_DAYS = 7


@transaction.atomic
def register_moral_guarantee(
    application,
    guarantor_name: str,
    guarantor_phone: str,
    guarantor_id_number: str,
    registered_by_sub: str,
    guarantor_sub: str = "",
    montant_couvert: Decimal | None = None,
    notes: str = "",
) -> "CreditGuarantee":
    """Désigne un garant — la caution reste inopposable tant qu'il n'a pas consenti.

    Ce qui change par rapport à la version déclarative : le garant doit être un
    utilisateur AGRICAP identifié (`guarantor_sub`), il doit passer les sept
    contrôles de `credits.guarantor`, et la caution naît en `PENDING_CONSENT`
    avec une fenêtre de consentement — pas en `PENDING` avec une simple attente
    de contre-signature d'agent.

    Les trois champs déclaratifs restent requis : ils portent la pièce
    d'identité relevée en agence, qui reste la trace physique de l'engagement.
    """
    from credits.guarantor import (
        GuarantorUnknown, assert_can_guarantee, consent_window_hours,
    )
    from credits.models import CreditGuarantee

    if not guarantor_name.strip():
        raise GuaranteeError("Le nom du garant est requis.")
    if not guarantor_phone.strip():
        raise GuaranteeError("Le téléphone du garant est requis.")
    if not guarantor_id_number.strip():
        raise GuaranteeError("Le numéro d'identité du garant est requis.")

    assert_type_eligible(application, CreditGuarantee.GuaranteeType.MORALE)

    guarantor = _resolve_guarantor(guarantor_sub)
    if guarantor is None:
        raise GuarantorUnknown(
            "Le garant doit disposer d'un compte AGRICAP : sans compte, il ne "
            "peut pas consentir lui-même, et la caution reste déclarative."
        )

    montant = Decimal(str(
        montant_couvert
        if montant_couvert is not None
        else (application.amount_approved or application.amount_requested or 0)
    ))
    assert_can_guarantee(application, guarantor, montant)

    # Une nouvelle désignation éteint la précédente : on ne laisse jamais deux
    # cautions vivantes sur un même dossier, sans quoi la couverture serait
    # comptée deux fois et deux garants s'estimeraient engagés pour le tout.
    CreditGuarantee.objects.filter(
        application=application,
        guarantee_type=CreditGuarantee.GuaranteeType.MORALE,
        status__in=[
            CreditGuarantee.Status.PENDING,
            CreditGuarantee.Status.PENDING_CONSENT,
            CreditGuarantee.Status.CONSENTED,
            CreditGuarantee.Status.ACTIVE,
        ],
    ).update(status=CreditGuarantee.Status.RELEASED, updated_at=timezone.now())

    now = timezone.now()
    window = consent_window_hours()

    guarantee = CreditGuarantee.objects.create(
        application=application,
        guarantee_type=CreditGuarantee.GuaranteeType.MORALE,
        status=CreditGuarantee.Status.PENDING_CONSENT,
        guarantor=guarantor,
        guarantor_sub=guarantor_sub,
        guarantor_name=guarantor_name,
        guarantor_phone=guarantor_phone,
        guarantor_id_number=guarantor_id_number,
        covered_amount=montant,
        hold_currency=application.currency,
        consent_expires_at=now + timezone.timedelta(hours=window),
        expires_at=now + timezone.timedelta(days=MORAL_GUARANTEE_EXPIRY_DAYS),
        registered_by_sub=registered_by_sub,
        notes=notes,
    )

    application.guarantee_type = "morale"
    application.save(update_fields=["guarantee_type", "updated_at"])

    _audit(
        actor=registered_by_sub, action="credit.guarantee.guarantor_designated",
        guarantee=guarantee,
        details={"guarantorSub": str(guarantor.pk), "coveredAmount": str(montant),
                 "consentWindowHours": window},
    )

    _notify_guarantor(guarantee)
    return guarantee


def _resolve_guarantor(guarantor_sub: str):
    if not (guarantor_sub or "").strip():
        return None
    from accounts.models import FintechUser
    return FintechUser.objects.filter(pk=guarantor_sub.strip()).first()


def record_guarantor_consent(
    guarantee,
    responder_sub: str,
    accept: bool,
    channel: str = "app",
    ip: str | None = None,
) -> "CreditGuarantee":
    """Le garant accepte ou refuse sa caution — acte unique et horodaté.

    Calqué sur `workflow.record_client_consent` : même contrôle d'identité (seul
    le bénéficiaire de l'acte le pose), même contrôle de fenêtre, même
    horodatage. Ce qui s'y ajoute est la re-vérification intégrale de la
    capacité d'engagement : entre la désignation et le clic, le garant a pu
    s'engager ailleurs, tomber en défaut ou quitter le groupe. L'engagement se
    forme ici, donc c'est ici que les règles doivent tenir.

    La fonction n'est **pas** atomique dans son ensemble, volontairement : les
    contrôles doivent pouvoir écrire (le passage en `expired` d'une demande dont
    la fenêtre est dépassée) puis lever une exception. Sous un `@atomic` global,
    ce `raise` annulait l'écriture d'expiration — la demande restait
    éternellement `pending_consent`, et chaque nouvelle tentative reconstatait la
    même expiration sans jamais la matérialiser. L'atomicité est posée là où elle
    a un sens : autour de la transition ET de sa journalisation, indissociables
    (une preuve de consentement sans entrée d'audit, ou l'inverse, ne vaut rien).
    """
    from credits.guarantor import (
        GuarantorAlreadyAnswered, GuarantorConsentExpired, GuarantorNotDesignated,
        InvalidGuaranteeState, assert_can_guarantee, consent_window_hours,
    )
    from credits.models import CreditGuarantee

    if guarantee.guarantee_type != CreditGuarantee.GuaranteeType.MORALE:
        raise InvalidGuaranteeState("Cette garantie n'est pas une caution solidaire.")

    if not guarantee.guarantor_id or str(guarantee.guarantor_id) != str(responder_sub):
        raise GuarantorNotDesignated(
            "Seul le garant désigné peut consentir à cette caution."
        )

    if guarantee.status in (
        CreditGuarantee.Status.CONSENTED,
        CreditGuarantee.Status.DECLINED,
    ):
        raise GuarantorAlreadyAnswered(
            "Vous avez déjà répondu à cette demande de caution. Un consentement "
            "ne se rejoue pas : une nouvelle désignation est nécessaire."
        )

    if guarantee.status != CreditGuarantee.Status.PENDING_CONSENT:
        raise InvalidGuaranteeState(
            f"Cette demande de caution n'attend plus de réponse "
            f"(statut « {guarantee.get_status_display()} »)."
        )

    if guarantee.is_consent_expired:
        # La lecture constate l'expiration : on ne laisse pas une demande morte
        # traîner en `pending_consent` jusqu'au passage de la tâche périodique.
        guarantee.status = CreditGuarantee.Status.EXPIRED
        guarantee.save(update_fields=["status", "updated_at"])
        raise GuarantorConsentExpired(
            f"Le délai de réponse a expiré le "
            f"{guarantee.consent_expires_at.strftime('%d/%m/%Y à %H:%M')}. "
            "Le demandeur doit vous désigner à nouveau."
        )

    if accept:
        # Re-vérification complète : la capacité au moment de l'engagement, pas
        # au moment de la demande. `exclude_pk` évite que la caution en cours
        # d'acceptation ne se compte elle-même dans le cumul et ne se refuse.
        assert_can_guarantee(
            guarantee.application, guarantee.guarantor,
            guarantee.covered_amount, exclude_pk=guarantee.pk,
        )

    now = timezone.now()
    with transaction.atomic():
        guarantee.status = (
            CreditGuarantee.Status.CONSENTED if accept
            else CreditGuarantee.Status.DECLINED
        )
        guarantee.consent_meta = {
            "decision": "accepted" if accept else "declined",
            "at": now.isoformat(),
            "channel": channel,
            "ip": ip,
            "bySub": str(responder_sub),
            "coveredAmount": str(guarantee.covered_amount or 0),
            "currency": guarantee.hold_currency or guarantee.application.currency,
            "consentWindowHours": consent_window_hours(),
            "consentExpiresAt": (
                guarantee.consent_expires_at.isoformat()
                if guarantee.consent_expires_at else None
            ),
        }
        guarantee.save(update_fields=["status", "consent_meta", "updated_at"])

        _audit(
            actor=str(responder_sub),
            action="credit.guarantee.consent_accepted" if accept
            else "credit.guarantee.consent_declined",
            guarantee=guarantee, ip=ip,
            details={"channel": channel,
                     "coveredAmount": str(guarantee.covered_amount or 0)},
        )
    return guarantee


def confirm_moral_guarantee(
    guarantee, confirmer_sub: str
) -> "CreditGuarantee":
    """Constitution de la caution par l'agent — `consented` → `active`.

    C'est le `constituted` de la SPEC : l'agent acte que la caution consentie est
    formalisée (pièces relevées, engagement signé). Il ne peut plus se substituer
    au garant : sans consentement préalable, il n'y a rien à constituer.

    Les cautions déclaratives antérieures (statut `pending`, sans garant lié)
    conservent l'ancien chemin — on ne réécrit pas l'historique (principe 3).
    """
    from credits.guarantor import GuarantorConsentMissing
    from credits.models import CreditGuarantee

    if guarantee.guarantee_type != CreditGuarantee.GuaranteeType.MORALE:
        raise GuaranteeError("Cette garantie n'est pas de type caution morale.")
    if guarantee.status == CreditGuarantee.Status.ACTIVE and guarantee.confirmed_at:
        raise GuaranteeError("Déjà confirmée.")
    if guarantee.status == CreditGuarantee.Status.RELEASED:
        raise GuaranteeError("Garantie déjà levée.")
    if guarantee.status == CreditGuarantee.Status.DECLINED:
        raise GuarantorConsentMissing(
            "Le garant a refusé cette caution : elle ne peut pas être constituée."
        )

    if guarantee.status == CreditGuarantee.Status.PENDING_CONSENT:
        if guarantee.is_consent_expired:
            guarantee.status = CreditGuarantee.Status.EXPIRED
            guarantee.save(update_fields=["status", "updated_at"])
        raise GuarantorConsentMissing(
            "Le garant n'a pas encore consenti : une caution ne se constitue pas "
            "sans l'accord explicite de la personne qu'elle engage."
        )

    if guarantee.status == CreditGuarantee.Status.EXPIRED:
        raise GuarantorConsentMissing(
            "La fenêtre de consentement du garant a expiré : le garant doit être "
            "désigné à nouveau."
        )

    # — Chemin historique : caution déclarative d'avant le consentement opposable
    if guarantee.status == CreditGuarantee.Status.PENDING:
        if guarantee.is_expired:
            guarantee.status = CreditGuarantee.Status.EXPIRED
            guarantee.save(update_fields=["status", "updated_at"])
            raise GuaranteeError(
                f"Délai de confirmation expiré le "
                f"{guarantee.expires_at.strftime('%d/%m/%Y')}."
            )
        if guarantee.guarantor_sub and guarantee.guarantor_sub != confirmer_sub:
            raise GuaranteeError(
                "Le confirmateur doit être le garant enregistré "
                f"({guarantee.guarantor_name})."
            )

    guarantee.status = CreditGuarantee.Status.ACTIVE
    guarantee.confirmed_by_sub = confirmer_sub
    guarantee.confirmed_at = timezone.now()
    guarantee.save(update_fields=["status", "confirmed_by_sub", "confirmed_at", "updated_at"])

    _audit(actor=confirmer_sub, action="credit.guarantee.constituted",
           guarantee=guarantee)
    return guarantee


def expire_pending_moral_guarantees() -> int:
    """Passe en EXPIRED les cautions dont un délai est dépassé.

    Deux fenêtres, deux motifs d'expiration : le consentement du garant
    (`consent_expires_at`, statut `pending_consent`) et la confirmation par
    l'agent des cautions déclaratives historiques (`expires_at`, statut
    `pending`). À appeler depuis une tâche périodique quotidienne.
    """
    from credits.models import CreditGuarantee
    now = timezone.now()

    consentements = CreditGuarantee.objects.filter(
        guarantee_type=CreditGuarantee.GuaranteeType.MORALE,
        status=CreditGuarantee.Status.PENDING_CONSENT,
        consent_expires_at__lt=now,
    ).update(status=CreditGuarantee.Status.EXPIRED, updated_at=now)

    declaratives = CreditGuarantee.objects.filter(
        guarantee_type=CreditGuarantee.GuaranteeType.MORALE,
        status=CreditGuarantee.Status.PENDING,
        expires_at__lt=now,
    ).update(status=CreditGuarantee.Status.EXPIRED, updated_at=now)

    return consentements + declaratives


# ── Journalisation ────────────────────────────────────────────────────────────

def _audit(*, actor: str, action: str, guarantee, details: dict | None = None,
           ip: str | None = None) -> None:
    """Trace une transition de garantie (SPEC §2.7).

    Volontairement non « best-effort » : contrairement au SMS, une transition de
    caution non journalisée est une perte de preuve. L'appel vit dans la
    transaction atomique de l'appelant — si l'audit échoue, la transition est
    annulée avec lui.
    """
    from audit.services import record
    payload = {
        "applicationCode": guarantee.application.code,
        "guaranteeType": guarantee.guarantee_type,
        "status": guarantee.status,
    }
    payload.update(details or {})
    record(
        actor=actor or "", action=action, entity_type="CreditGuarantee",
        entity_id=guarantee.pk, details=payload, ip=ip,
    )


# ── Notification du garant ────────────────────────────────────────────────────

#: Écran garant côté front. Le chemin est porté dans le CORPS de la notification
#: parce que `notifications.Notification` n'a pas de champ d'URL — voir la note
#: du fragment de lot : ajouter ce champ est une décision qui engage une app
#: partagée, pas un effet de bord de ce lot.
GUARANTEE_REQUESTS_PATH = "/guarantee-requests"


def _notify_guarantor(guarantee) -> None:
    """Prévient le garant sur les deux canaux, avec le chemin de l'écran.

    Signalé par l'agent front : rien ne pointait vers l'écran garant. Une
    notification sans chemin laisse la fenêtre de 72 h expirer **faute d'accès**,
    pas faute de décision — le consentement devient alors un obstacle
    administratif au lieu d'être un acte.
    """
    _notify_guarantor_inapp(guarantee)
    _notify_guarantor_sms(guarantee)


def _notify_guarantor_inapp(guarantee) -> None:
    """Dépose la demande dans la boîte de notifications du garant.

    Volontairement **non** best-effort, contrairement au SMS : c'est une écriture
    dans la même base et la même transaction que la désignation. Un garant qui
    n'est pas notifié ne peut pas consentir, donc une caution silencieusement
    non notifiée est une caution qui expirera — mieux vaut que la désignation
    échoue franchement que de créer un engagement que personne ne verra.
    """
    from notifications.models import Notification

    app = guarantee.application
    deadline = guarantee.consent_expires_at or guarantee.expires_at
    echeance = deadline.strftime("%d/%m/%Y à %H:%M") if deadline else "prochainement"

    Notification.objects.create(
        user=guarantee.guarantor,
        title="Demande de caution solidaire",
        body=(
            f"{app.client.full_name} vous désigne comme garant de son dossier de "
            f"crédit {app.code}, à hauteur de {guarantee.covered_amount} "
            f"{guarantee.hold_currency}. En cas de défaut de sa part, vous vous "
            f"engagez solidairement à hauteur de ce montant.\n\n"
            f"Vous devez accepter ou refuser avant le {echeance}. "
            f"Sans réponse de votre part, la demande expire et vous n'êtes engagé "
            f"à rien.\n\n"
            f"Répondre : {GUARANTEE_REQUESTS_PATH}"
        ),
    )


def _notify_guarantor_sms(guarantee) -> None:
    """Envoie un SMS au garant (best-effort, échec silencieux).

    Best-effort assumé : le SMS dépend d'un tiers (Dream Digital) et d'un réseau.
    Son échec ne doit pas annuler une désignation, la notification in-app faisant
    foi comme canal de rattrapage.
    """
    try:
        from common.sms import send_sms
        phone = guarantee.guarantor_phone
        app = guarantee.application
        deadline = guarantee.consent_expires_at or guarantee.expires_at
        expires = deadline.strftime("%d/%m/%Y à %H:%M")
        message = (
            f"AGRICAP : {app.client.full_name} vous désigne comme garant du "
            f"dossier crédit {app.code}, à hauteur de "
            f"{guarantee.covered_amount} {guarantee.hold_currency}. "
            f"Acceptez ou refusez avant le {expires} dans l'application "
            f"(rubrique « Demandes de caution ») ou auprès de votre agence. "
            f"Sans réponse, la demande expire et ne vous engage à rien."
        )
        send_sms(phone=phone, message=message)
    except Exception:
        # Un canal secondaire ne doit pas faire échouer l'opération métier — mais
        # son échec doit LAISSER UNE TRACE (cf. credits/workflow.py, même garde).
        logger.warning("[NOTIF] envoi impossible pour %s", getattr(app, "code", "?"), exc_info=True)


# ── Vue « demandes de caution » du garant ─────────────────────────────────────

def guarantee_requests_for(user, status: str = ""):
    """Les cautions dont `user` est le garant désigné — et rien d'autre.

    Le filtre porte sur la FK `guarantor`, jamais sur `guarantor_sub` : ce
    dernier est une chaîne déclarative que n'importe quel enregistrement
    historique peut porter sans qu'aucun compte n'y corresponde.
    """
    from credits.models import CreditGuarantee

    qs = (
        CreditGuarantee.objects
        .filter(guarantor=user, guarantee_type=CreditGuarantee.GuaranteeType.MORALE)
        .select_related("application__client", "application__value_chain")
    )
    if status:
        qs = qs.filter(status=status)
    # Les demandes qui appellent une action d'abord, puis les plus urgentes.
    return qs.order_by(
        models_case_pending_first(), "consent_expires_at", "-created_at",
    )


def models_case_pending_first():
    """Tri : `pending_consent` en tête, le reste ensuite."""
    from django.db.models import Case, IntegerField, Value, When
    from credits.models import CreditGuarantee
    return Case(
        When(status=CreditGuarantee.Status.PENDING_CONSENT, then=Value(0)),
        default=Value(1), output_field=IntegerField(),
    ).asc()


def serialize_guarantee_request(guarantee) -> dict[str, Any]:
    """Forme servie au GARANT — contrat figé (`docs/status-fragments/lot6-backend.md`).

    Principe 7 appliqué à un tiers : le garant voit son engagement (qui, combien,
    jusqu'à quand) et le lien de groupe qui le justifie. Il ne voit ni la décote,
    ni sa contribution à la couverture, ni le score du demandeur, ni ses propres
    plafonds d'engagement — ce sont les règles du moteur, et un garant qui les
    connaît est un garant qui peut aider à les contourner.
    """
    from credits.guarantor import shared_groups

    app = guarantee.application
    client = app.client
    chain = app.value_chain
    consent = guarantee.consent_meta or {}
    montant = app.amount_approved or app.amount_requested

    return {
        "id": guarantee.pk,
        "applicationCode": app.code,
        "status": guarantee.status,
        "applicant": {
            "displayName": client.full_name or client.pk,
            "sharedGroups": [
                {"id": g.pk, "name": g.name, "type": g.type}
                for g in shared_groups(client, guarantee.guarantor)
            ],
        },
        "valueChain": {"code": chain.code, "label": chain.label} if chain else None,
        "loanAmount": float(montant) if montant is not None else None,
        "loanCurrency": app.currency,
        "coveredAmount": (
            float(guarantee.covered_amount)
            if guarantee.covered_amount is not None else None
        ),
        "coveredCurrency": guarantee.hold_currency or app.currency,
        "consentExpiresAt": (
            guarantee.consent_expires_at.isoformat()
            if guarantee.consent_expires_at else None
        ),
        "consentedAt": (
            consent.get("at") if consent.get("decision") == "accepted" else None
        ),
        "declinedAt": (
            consent.get("at") if consent.get("decision") == "declined" else None
        ),
        "isExpired": guarantee.is_consent_expired,
        "createdAt": guarantee.created_at.isoformat(),
    }


# ── Vue synthèse des garanties d'un dossier ───────────────────────────────────

#: Nombre de caractères de fin conservés par le masquage d'une pièce d'identité.
#: Assez pour qu'un agent rapproche une pièce qu'il a déjà vue, trop peu pour la
#: reconstituer.
PIECE_VISIBLE = 4


def masquer_piece_identite(numero: str) -> str:
    """Masque un numéro de pièce d'identité en n'en laissant que la fin.

    « CD-CNI-99887766 » → « ••••7766 ». Un numéro plus court que la fenêtre
    visible est masqué en ENTIER : une pièce de cinq caractères dont on
    montrerait les quatre derniers ne serait pas masquée, elle serait publiée.
    """
    valeur = (numero or "").strip()
    if not valeur:
        return ""
    if len(valeur) <= PIECE_VISIBLE:
        return "•" * len(valeur)
    return "•" * PIECE_VISIBLE + valeur[-PIECE_VISIBLE:]


def get_guarantee_summary(application, *, pour_staff: bool = False) -> dict[str, Any]:
    """Résumé des garanties d'un dossier — masqué par défaut.

    `pour_staff=False` (défaut) sert le TITULAIRE du dossier. Le cloisonnement
    d'accès est correct depuis le lot sécurité — seuls le titulaire et le
    personnel atteignent cette vue — mais le titulaire y lisait la pièce
    d'identité COMPLÈTE de son garant : la donnée personnelle d'un TIERS,
    saisie par un agent (`register_moral_guarantee` exige `CAN_INSTRUCT`), et
    dont le demandeur n'a aucun usage. Un numéro de CNI ne sert qu'à une chose
    dans ce dossier : prouver qu'un agent a vu la pièce. Cette preuve appartient
    à l'instruction, pas au demandeur.

    Le défaut est donc le masquage, et non l'inverse : une vue qui oublierait de
    préciser son audience doit se tromper du côté prudent. `guarantorIdProvided`
    conserve l'information UTILE au titulaire — la pièce a bien été renseignée,
    la caution est donc complète — sans en livrer le contenu.

    À BRANCHER (hors périmètre) : les endpoints staff doivent passer
    `pour_staff=True`. Ils vivent dans `credits/views.py`, tenu par un autre
    agent ; la liste exacte est dans le rapport de lot. Tant que ce n'est pas
    fait, un agent voit la pièce masquée — une gêne visible et réversible en une
    ligne, préférable à une fuite invisible.
    """
    from credits.models import CreditGuarantee

    guarantees = list(
        application.guarantees.order_by("-created_at")
    )

    result: dict[str, Any] = {
        "count": len(guarantees),
        "guaranteeType": application.guarantee_type or None,
        "items": [],
    }

    # Couverture = somme des montants RETENUS des garanties ACTIVES uniquement.
    # Une garantie en attente de confirmation — ou de consentement — ne couvre
    # rien. Une caution morale n'y entre qu'après sa décote (`retained_coverage`).
    couverture = Decimal("0")

    for g in guarantees:
        item: dict = {
            "id": g.pk,
            "type": g.guarantee_type,
            "status": g.status,
            "coveredAmount": float(g.covered_amount) if g.covered_amount is not None else None,
            "createdAt": g.created_at.isoformat(),
        }
        if g.status == CreditGuarantee.Status.ACTIVE:
            couverture += g.retained_coverage

        if g.guarantee_type in CreditGuarantee.ASSET_BACKED_TYPES and g.asset_id:
            item.update({
                "asset": {
                    "id": g.asset_id,
                    "name": g.asset.name,
                    "category": g.asset.type,
                    "declaredValue": float(g.asset.value),
                    "retainedValue": (
                        float(g.asset.valeur_retenue)
                        if g.asset.valeur_retenue is not None else None
                    ),
                    "currency": g.asset.currency,
                    "status": g.asset.status,
                    "verifiedAt": (
                        g.asset.verifie_le.isoformat() if g.asset.verifie_le else None
                    ),
                },
                "confirmedAt": g.confirmed_at.isoformat() if g.confirmed_at else None,
            })
        if g.guarantee_type == CreditGuarantee.GuaranteeType.EPARGNE:
            item.update({
                "holdAmount": float(g.hold_amount or 0),
                "holdCurrency": g.hold_currency,
                "holdReference": g.hold_reference,
                "holdPlacedAt": g.hold_placed_at.isoformat() if g.hold_placed_at else None,
                "holdReleasedAt": g.hold_released_at.isoformat() if g.hold_released_at else None,
                "availableBalance": (
                    float(_available_savings_balance(g.savings_plan, g.hold_currency))
                    if g.savings_plan and g.status != CreditGuarantee.Status.RELEASED
                    else None
                ),
            })
        elif g.guarantee_type == CreditGuarantee.GuaranteeType.MORALE:
            consent = g.consent_meta or {}
            item.update({
                "guarantorName": g.guarantor_name,
                "guarantorPhone": g.guarantor_phone,
                # Pièce d'identité d'un TIERS : en clair pour l'instruction,
                # masquée pour le titulaire du dossier.
                "guarantorIdNumber": (
                    g.guarantor_id_number if pour_staff
                    else masquer_piece_identite(g.guarantor_id_number)
                ),
                "guarantorIdProvided": bool((g.guarantor_id_number or "").strip()),
                "guarantorSub": str(g.guarantor_id) if g.guarantor_id else None,
                "confirmedAt": g.confirmed_at.isoformat() if g.confirmed_at else None,
                "expiresAt": g.expires_at.isoformat() if g.expires_at else None,
                "isExpired": g.is_expired,
                # Consentement du garant — c'est ce qui rend la caution opposable.
                "consentExpiresAt": (
                    g.consent_expires_at.isoformat() if g.consent_expires_at else None
                ),
                "isConsentExpired": g.is_consent_expired,
                "consentedAt": (
                    consent.get("at") if consent.get("decision") == "accepted" else None
                ),
                "declinedAt": (
                    consent.get("at") if consent.get("decision") == "declined" else None
                ),
                "consentChannel": consent.get("channel"),
                # Contribution réelle à la couverture, après décote : la lire ici
                # évite que l'analyste ne recalcule 30 % de tête, et que le front
                # ne le calcule du tout (aucun chiffre métier côté client).
                "retainedCoverage": float(g.retained_coverage),
                "daysLeft": (
                    max(0, (g.consent_expires_at - timezone.now()).days)
                    if g.consent_expires_at
                    and g.status == CreditGuarantee.Status.PENDING_CONSENT
                    else None
                ),
            })
        result["items"].append(item)

    # Défense en profondeur : un dossier fraîchement créé peut porter un `float`
    # dans `amount_requested` si un appelant l'a passé tel quel à `create()`
    # (Django ne convertit qu'au rechargement). `Decimal / float` lève un
    # TypeError et faisait tomber la réponse entière en 500. La cause est
    # corrigée à la source dans `views.py` ; ce garde évite qu'un futur appelant
    # ne rouvre la même brèche depuis un autre chemin.
    montant = application.amount_approved or application.amount_requested
    if montant is not None and not isinstance(montant, Decimal):
        montant = Decimal(str(montant))

    result["coverage"] = {
        "retainedTotal": float(couverture),
        "currency": application.currency,
        "requestedAmount": float(montant) if montant is not None else None,
        # Ratio calculé sur les valeurs retenues après décote, jamais déclarées
        "ratio": (
            round(float(couverture / montant), 3)
            if montant else None
        ),
        "activeCount": sum(
            1 for g in guarantees if g.status == CreditGuarantee.Status.ACTIVE
        ),
    }
    return result
