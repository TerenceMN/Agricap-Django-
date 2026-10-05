"""Services FX — gouvernance du taux de change (HAZINA principe 5).

Deux règles structurent tout ce module :

1. **Le taux appliqué à une conversion est figé et stocké au moment de l'opération**, jamais
   recalculé a posteriori (Annexe A du cahier de conception). D'où `convert_with_provenance`
   et `to_usd`, qui rendent AVEC le montant la provenance du taux (identifiant, palier,
   usage, source, date d'effet, fraîcheur) — un appelant qui journalise cette provenance
   satisfait le principe 4 de MKOPO (« toute conversion CDF/USD journalise le taux utilisé
   et sa date ») sans avoir à connaître le modèle.

2. **Aucune retombée silencieuse.** Il n'existe aucun taux par défaut dans ce module. Si le
   taux du jour n'a pas été saisi, la lecture stricte (`taux_du_jour`) refuse, et la lecture
   tolérante (`current_rate` / `resolve_rate`) renvoie le dernier taux actif EN LE MARQUANT
   périmé (`stale`, `stalenessDays`) et en loggant. Le seul chiffre en dur toléré ici est le
   SEUIL de maker-checker, quand `InstitutionConfig` est muet — et il s'annonce (principe 8).
"""
from __future__ import annotations

import logging
import re
import time
import warnings
from datetime import date as date_cls
from decimal import Decimal, InvalidOperation

import requests
import urllib3
from django.db import transaction
from django.utils import timezone

from audit.services import record as audit_record
from common.exceptions import ConflictError, NotFoundError, ValidationFailed
from common.money import ZERO, arrondir
from common.parsing import to_decimal

from .models import ExchangeRate

logger = logging.getLogger("fx.bcc_sync")
gov_logger = logging.getLogger("fx.gouvernance")

MIN_CLIENT_MARGIN_VS_BCC = Decimal("0.015")  # 1.5%, marge minimale client/staff vs BCC

#: Seuil de secours (en %) au-delà duquel un taux exige un second acteur. N'a d'effet que si
#: `InstitutionConfig.raw['fx_seuil_variation_pct']` est absent ou illisible — et son emploi
#: est alors loggé (principe 8, exception « valeurs par défaut de secours »).
DEFAULT_VARIATION_THRESHOLD_PCT = Decimal("2.0")

PCT = Decimal("0.0001")  # quantum des pourcentages stockés (4 décimales)


# ═══════════════════════════════════════════════ PARAMÈTRE DE GOUVERNANCE (principe 8)

def variation_threshold_pct() -> Decimal:
    """Écart maximal (en %) vs le taux de référence tolérable sans second acteur.

    Source normale : `InstitutionConfig.raw['fx_seuil_variation_pct']` (feuille de
    gouvernance). À défaut : `settings.FX_VARIATION_THRESHOLD_PCT`, puis
    `DEFAULT_VARIATION_THRESHOLD_PCT`, avec warning — un seuil de maker-checker codé en dur
    en silence, c'est un contrôle que le comité croit avoir et qu'il n'a pas.
    """
    try:
        from referentiel.models import InstitutionConfig
        raw = (InstitutionConfig.active().raw or {})
        brut = raw.get("fx_seuil_variation_pct", raw.get("seuil_variation_taux_change"))
        if brut not in (None, ""):
            seuil = Decimal(str(brut).replace(",", "."))
            if seuil > 0:
                return seuil
            gov_logger.warning("Seuil FX configuré <= 0 (%s) — ignoré.", brut)
    except Exception as exc:  # noqa: BLE001 — la config ne doit jamais casser une saisie
        gov_logger.warning("Seuil FX : InstitutionConfig illisible (%s).", exc)

    from django.conf import settings
    seuil = to_decimal(getattr(settings, "FX_VARIATION_THRESHOLD_PCT", None) or
                       DEFAULT_VARIATION_THRESHOLD_PCT, str(DEFAULT_VARIATION_THRESHOLD_PCT))
    if seuil <= 0:
        seuil = DEFAULT_VARIATION_THRESHOLD_PCT
    gov_logger.warning(
        "Seuil d'écart FX absent d'InstitutionConfig — repli sur %s %% (à paramétrer dans "
        "la feuille de gouvernance, clé `fx_seuil_variation_pct`).", seuil,
    )
    return seuil


# ═══════════════════════════════════════════════════════════════ LECTURE DES TAUX

def _active_qs(*, tier: str, currency: str, usage: str):
    return ExchangeRate.objects.filter(
        tier=tier, currency=currency, usage=usage, status=ExchangeRate.Status.ACTIF,
    )


def reference_rate(*, tier: str, currency: str, usage: str, effective_date: date_cls
                   ) -> ExchangeRate | None:
    """Taux contre lequel se mesure l'écart d'une nouvelle saisie.

    C'est le taux ACTIF du jour visé s'il en existe un (on CORRIGE un taux déjà publié),
    sinon le dernier taux actif antérieur (« la veille » au sens du principe 5). Mesurer une
    correction contre la veille plutôt que contre le taux corrigé laisserait passer sans
    contrôle un remplacement massif du taux du jour lui-même.
    """
    same_day = _active_qs(tier=tier, currency=currency, usage=usage).filter(
        effective_date=effective_date).first()
    if same_day:
        return same_day
    return (_active_qs(tier=tier, currency=currency, usage=usage)
            .filter(effective_date__lt=effective_date)
            .order_by("-effective_date", "-version").first())


def current_rate(*, tier: str, currency: str, on: date_cls | None = None,
                 usage: str = ExchangeRate.Usage.OPERATIONNEL) -> ExchangeRate | None:
    """Dernier taux ACTIF à la date demandée (lecture tolérante, rétro-compatible).

    Un taux `EN_ATTENTE` n'est JAMAIS servi : tant qu'un second acteur ne l'a pas validé,
    il n'existe pas pour les conversions. Si le taux rendu est antérieur à la date demandée,
    un warning est loggé — la retombée sur la veille est tracée, jamais silencieuse. Les
    appelants qui doivent RESTITUER cette information utilisent `resolve_rate`.
    """
    qs = _active_qs(tier=tier, currency=currency, usage=usage)
    if on:
        qs = qs.filter(effective_date__lte=on)
    rate = qs.order_by("-effective_date", "-version").first()
    if rate is None:
        return None
    asked = on or timezone.localdate()
    if rate.effective_date < asked:
        gov_logger.warning(
            "Aucun taux %s/%s/%s au %s : repli sur celui du %s (%s jour(s) d'ancienneté).",
            tier, currency, usage, asked, rate.effective_date, (asked - rate.effective_date).days,
        )
    return rate


def rate_provenance(rate: ExchangeRate, *, asked_for: date_cls | None = None) -> dict:
    """Provenance journalisable d'un taux — ce qu'un appelant doit stocker à côté du montant
    converti pour qu'un auditeur reconstitue le calcul deux ans après."""
    asked = asked_for or rate.effective_date
    staleness = (asked - rate.effective_date).days
    return {
        "rateId": rate.pk,
        "tier": rate.tier,
        "currency": rate.currency,
        "usage": rate.usage,
        "source": rate.source,
        "sourceReference": rate.source_reference,
        "buy": str(rate.buy_rate),
        "sell": str(rate.sell_rate),
        "effectiveDate": rate.effective_date.isoformat(),
        "askedFor": asked.isoformat(),
        "version": rate.version,
        "status": rate.status,
        "stale": staleness > 0,
        "stalenessDays": max(staleness, 0),
        "validatedBy": rate.validated_by,
    }


def resolve_rate(*, tier: str, currency: str, on: date_cls | None = None,
                 usage: str = ExchangeRate.Usage.OPERATIONNEL,
                 max_staleness_days: int | None = None) -> tuple[ExchangeRate, dict]:
    """Taux applicable + sa provenance. Lève `NotFoundError` s'il n'y en a aucun — un
    montant converti sans taux gouverné n'a pas à exister.

    `max_staleness_days` : au-delà, la retombée n'est plus acceptable et devient une erreur
    (à utiliser par les traitements où un taux périmé fausserait une décision).
    """
    asked = on or timezone.localdate()
    rate = current_rate(tier=tier, currency=currency, on=asked, usage=usage)
    if rate is None:
        raise NotFoundError(
            f"Aucun taux {tier}/{currency}/{usage} actif au {asked}. "
            "Saisissez (et faites valider) le taux avant toute conversion."
        )
    meta = rate_provenance(rate, asked_for=asked)
    if max_staleness_days is not None and meta["stalenessDays"] > max_staleness_days:
        raise NotFoundError(
            f"Le dernier taux {tier}/{currency}/{usage} date du {rate.effective_date} "
            f"({meta['stalenessDays']} jour(s)) : au-delà de la tolérance de "
            f"{max_staleness_days} jour(s). Saisissez le taux du jour."
        )
    return rate, meta


def taux_du_jour(*, date_taux: date_cls, usage: str = ExchangeRate.Usage.OPERATIONNEL,
                 tier: str = ExchangeRate.Tier.BCC,
                 currency: str = ExchangeRate.Currency.USD) -> ExchangeRate:
    """Lecture STRICTE : le taux de CETTE date et de CET usage, ou rien.

    Homonyme volontaire de `accounting.fx.taux_du_jour` : les deux modules doivent converger
    vers une seule source (cf. rapport). Aucune retombée sur la veille ici — c'est la
    fonction qu'utilisent les traitements comptables (clôture, arrêté), pour lesquels
    « le taux d'hier » n'est pas une approximation acceptable mais une erreur d'arrêté.
    """
    rate = _active_qs(tier=tier, currency=currency, usage=usage).filter(
        effective_date=date_taux).order_by("-version").first()
    if rate is None:
        en_attente = ExchangeRate.objects.filter(
            tier=tier, currency=currency, usage=usage, effective_date=date_taux,
            status=ExchangeRate.Status.EN_ATTENTE).exists()
        detail = (" Un taux est saisi mais EN ATTENTE de validation par un second acteur."
                  if en_attente else "")
        raise NotFoundError(
            f"Aucun taux {usage} {tier}/{currency} actif pour le {date_taux}.{detail}"
        )
    return rate


def closing_rate(*, currency: str = ExchangeRate.Currency.USD, on: date_cls,
                 tier: str = ExchangeRate.Tier.BCC) -> ExchangeRate:
    """Taux de CLÔTURE d'une date — celui que tout état financier consolidé doit référencer
    (principe 5). Strict par construction : un bilan ne s'arrête pas « à peu près »."""
    return taux_du_jour(date_taux=on, usage=ExchangeRate.Usage.CLOTURE, tier=tier,
                        currency=currency)


def pending_rates(*, tier: str = "", currency: str = "", usage: str = "") -> list[ExchangeRate]:
    """Corbeille des taux en attente d'un second acteur."""
    qs = ExchangeRate.objects.filter(status=ExchangeRate.Status.EN_ATTENTE)
    if tier:
        qs = qs.filter(tier=tier)
    if currency:
        qs = qs.filter(currency=currency)
    if usage:
        qs = qs.filter(usage=usage)
    return list(qs.order_by("-effective_date", "tier", "currency"))


# ═══════════════════════════════════════════════════════════ SAISIE ET VALIDATION

def _variation_pct(*, buy: Decimal, sell: Decimal, ref: ExchangeRate) -> Decimal:
    """Écart relatif maximal (%) entre un couple (achat, vente) et un taux de référence.

    On retient le MAX des deux écarts plutôt que l'écart du seul cours vendeur : un taux
    dont seule la jambe acheteuse s'effondre déforme la marge institutionnelle autant qu'un
    déplacement du cours pivot, et doit donc déclencher le même contrôle.
    """
    ecarts = []
    for neuf, ancien in ((buy, ref.buy_rate), (sell, ref.sell_rate)):
        if ancien:
            ecarts.append(abs(neuf - Decimal(ancien)) / Decimal(ancien) * Decimal("100"))
    return max(ecarts).quantize(PCT) if ecarts else Decimal("0").quantize(PCT)


@transaction.atomic
def set_rate(*, tier: str, currency: str, buy: Decimal | str, sell: Decimal | str,
             effective_date: date_cls, by: str = "",
             usage: str = ExchangeRate.Usage.OPERATIONNEL,
             source: str = ExchangeRate.Source.MANUELLE, source_reference: str = "",
             reason: str = "") -> ExchangeRate:
    """Publie un taux — en append-only, jamais en écrasement.

    * valeurs identiques au taux actif du même jour → aucune nouvelle version (idempotent) ;
    * écart ≤ seuil (`InstitutionConfig`) → le taux naît `ACTIF` et remplace la version
      précédente du même jour, qui passe `REMPLACE` et reste lisible ;
    * écart > seuil → le taux naît `EN_ATTENTE`, n'est servi à AUCUNE conversion, et exige
      `validate_rate` par un acteur différent, avec motif.
    """
    buy = to_decimal(buy)
    sell = to_decimal(sell)
    if buy <= 0:
        raise ValidationFailed("Le taux d'achat doit être strictement positif.")
    if sell <= buy:
        raise ValidationFailed("Le taux de vente doit être strictement supérieur au taux d'achat.")
    if usage not in ExchangeRate.Usage.values:
        raise ValidationFailed(f"Usage inconnu : {usage} (attendu OPERATIONNEL ou CLOTURE).")
    if source not in ExchangeRate.Source.values:
        raise ValidationFailed(f"Source inconnue : {source} (attendu BCC, MANUELLE ou AGREGATEUR).")

    if tier in (ExchangeRate.Tier.STAFF, ExchangeRate.Tier.CLIENT):
        bcc = _active_qs(tier=ExchangeRate.Tier.BCC, currency=currency, usage=usage).filter(
            effective_date=effective_date).first()
        if bcc and sell < bcc.sell_rate * (1 + MIN_CLIENT_MARGIN_VS_BCC):
            raise ValidationFailed(
                "Le taux {} doit avoir une marge d'au moins 1.5% au-dessus du taux BCC.".format(tier)
            )

    key = dict(tier=tier, currency=currency, usage=usage, effective_date=effective_date)
    en_cours = ExchangeRate.objects.select_for_update().filter(**key)
    actif_du_jour = en_cours.filter(status=ExchangeRate.Status.ACTIF).first()

    if (actif_du_jour and actif_du_jour.buy_rate == buy and actif_du_jour.sell_rate == sell
            and actif_du_jour.source == source):
        return actif_du_jour  # rien de neuf : pas de version cosmétique dans l'historique

    if en_cours.filter(status=ExchangeRate.Status.EN_ATTENTE).exists():
        raise ConflictError(
            "Un taux {}/{}/{} du {} est déjà en attente de validation : faites-le valider "
            "ou rejeter avant d'en proposer un autre.".format(tier, currency, usage, effective_date)
        )

    ref = reference_rate(tier=tier, currency=currency, usage=usage, effective_date=effective_date)
    seuil = variation_threshold_pct()
    ecart = _variation_pct(buy=buy, sell=sell, ref=ref) if ref else None
    besoin_checker = ecart is not None and ecart > seuil

    if besoin_checker and not (reason or "").strip():
        raise ValidationFailed(
            "Écart de {} % vs le taux du {} (seuil {} %) : un motif est obligatoire pour "
            "soumettre ce taux à validation.".format(ecart, ref.effective_date, seuil)
        )

    version = (en_cours.order_by("-version").values_list("version", flat=True).first() or 0) + 1
    statut = ExchangeRate.Status.EN_ATTENTE if besoin_checker else ExchangeRate.Status.ACTIF

    # L'ordre compte : la contrainte d'unicité n'autorise qu'un seul ACTIF par clé.
    if statut == ExchangeRate.Status.ACTIF and actif_du_jour:
        actif_du_jour.status = ExchangeRate.Status.REMPLACE
        actif_du_jour.superseded_at = timezone.now()
        actif_du_jour.save(update_fields=["status", "superseded_at"])

    rate = ExchangeRate.objects.create(
        tier=tier, currency=currency, usage=usage, buy_rate=buy, sell_rate=sell,
        effective_date=effective_date, source=source, source_reference=source_reference,
        status=statut, version=version, supersedes=actif_du_jour,
        variation_pct=ecart, threshold_pct=seuil, reference_rate=ref,
        reason=reason, created_by=by,
    )

    audit_record(
        actor=by, action="fx.set_rate", entity_type="ExchangeRate", entity_id=str(rate.pk),
        details={
            "tier": tier, "currency": currency, "usage": usage, "buy": str(buy), "sell": str(sell),
            "effectiveDate": effective_date.isoformat(), "source": source,
            "version": version, "status": statut,
            "variationPct": str(ecart) if ecart is not None else None,
            "thresholdPct": str(seuil),
            "referenceRateId": ref.pk if ref else None,
            "supersedesId": actif_du_jour.pk if actif_du_jour else None,
            "reason": reason,
        },
    )
    if besoin_checker:
        gov_logger.warning(
            "Taux %s/%s/%s du %s : écart de %s %% > seuil %s %% — validation par un second "
            "acteur requise (taux non servi tant qu'il est EN_ATTENTE).",
            tier, currency, usage, effective_date, ecart, seuil,
        )
    return rate


@transaction.atomic
def validate_rate(*, rate_id: int, by: str, reason: str = "", approve: bool = True
                  ) -> ExchangeRate:
    """Second acteur (maker ≠ checker) sur un taux en attente.

    Le motif est exigé dans les DEUX sens : approuver un écart de 5 % sans dire pourquoi
    n'est pas une validation, c'est un clic.
    """
    rate = ExchangeRate.objects.select_for_update().filter(pk=rate_id).first()
    if rate is None:
        raise NotFoundError("Taux introuvable.")
    if rate.status != ExchangeRate.Status.EN_ATTENTE:
        raise ConflictError(
            f"Ce taux n'est pas en attente de validation (statut {rate.status})."
        )
    if not (by or "").strip():
        raise ValidationFailed("Un valideur identifié est requis (maker ≠ checker).")
    if by == rate.created_by:
        raise ValidationFailed(
            "Maker ≠ checker : le taux a été saisi par ce même acteur ; "
            "la validation doit venir d'un second acteur."
        )
    if not (reason or "").strip():
        raise ValidationFailed("Un motif est obligatoire pour valider ou rejeter un taux.")

    now = timezone.now()
    if approve:
        actif = _active_qs(tier=rate.tier, currency=rate.currency, usage=rate.usage).filter(
            effective_date=rate.effective_date).first()
        if actif:
            actif.status = ExchangeRate.Status.REMPLACE
            actif.superseded_at = now
            actif.save(update_fields=["status", "superseded_at"])
        rate.status = ExchangeRate.Status.ACTIF
    else:
        rate.status = ExchangeRate.Status.REJETE
    rate.validated_by = by
    rate.validated_at = now
    rate.validation_reason = reason
    rate.save(update_fields=["status", "validated_by", "validated_at", "validation_reason"])

    audit_record(
        actor=by, action="fx.validate_rate" if approve else "fx.reject_rate",
        entity_type="ExchangeRate", entity_id=str(rate.pk),
        details={
            "tier": rate.tier, "currency": rate.currency, "usage": rate.usage,
            "effectiveDate": rate.effective_date.isoformat(), "version": rate.version,
            "variationPct": str(rate.variation_pct) if rate.variation_pct is not None else None,
            "thresholdPct": str(rate.threshold_pct) if rate.threshold_pct is not None else None,
            "maker": rate.created_by, "checker": by, "reason": reason,
            "decision": "approved" if approve else "rejected",
        },
    )
    return rate


# ═══════════════════════════════════════════════════════════════════ CONVERSIONS

def convert(*, amount: Decimal | str, from_currency: str, to_currency: str, tier: str = "CLIENT",
            on: date_cls | None = None,
            usage: str = ExchangeRate.Usage.OPERATIONNEL) -> Decimal:
    """Le CDF est le pivot implicite : `from_currency`/`to_currency` valent "CDF" ou une des
    devises étrangères cotées (`ExchangeRate.Currency`)."""
    return convert_with_provenance(
        amount=amount, from_currency=from_currency, to_currency=to_currency,
        tier=tier, on=on, usage=usage,
    )[0]


def convertir_avec_residu(
    *, amount: Decimal | str, from_currency: str, to_currency: str, tier: str = "CLIENT",
    on: date_cls | None = None, usage: str = ExchangeRate.Usage.OPERATIONNEL,
    max_staleness_days: int | None = None,
) -> tuple[Decimal, Decimal, dict | None]:
    """`(montant au centime, résidu signé, provenance du taux)` — implémentation unique.

    Une conversion ne tombe presque jamais juste : diviser par un taux à six décimales
    produit un montant à décimales sans fin. Le montant servi est arrondi au centime
    (`common.money.arrondir`, `ROUND_HALF_UP`), et le sous-centime est RENDU plutôt
    qu'écrasé — c'est l'appelant qui déplace de l'argent, donc c'est à lui de loger
    l'écart au compte prévu (`caisses.rounding.loger_residu`).

    Deux défauts corrigés au passage, sur les trois sites de quantize qui vivaient ici :
    `.quantize(Decimal("0.01"))` sans mode d'arrondi appliquait `ROUND_HALF_EVEN`,
    l'arrondi bancaire du contexte par défaut, alors que le projet impose `ROUND_HALF_UP`
    (principe 4) ; et l'écart était perdu sans trace.
    """
    amount = to_decimal(amount)
    if from_currency == to_currency:
        return amount, ZERO, None
    if from_currency == "CDF":
        rate, meta = resolve_rate(tier=tier, currency=to_currency, on=on, usage=usage,
                                  max_staleness_days=max_staleness_days)
        montant, residu = arrondir(amount / rate.sell_rate)
        return montant, residu, meta
    if to_currency == "CDF":
        rate, meta = resolve_rate(tier=tier, currency=from_currency, on=on, usage=usage,
                                  max_staleness_days=max_staleness_days)
        montant, residu = arrondir(amount * rate.buy_rate)
        return montant, residu, meta
    raise ValidationFailed("Conversion directe entre deux devises étrangères non supportée (passer par CDF).")


def convert_with_provenance(
    *, amount: Decimal | str, from_currency: str, to_currency: str, tier: str = "CLIENT",
    on: date_cls | None = None, usage: str = ExchangeRate.Usage.OPERATIONNEL,
    max_staleness_days: int | None = None,
) -> tuple[Decimal, dict | None]:
    """Conversion + provenance du taux appliqué.

    C'est la porte d'entrée des modules qui doivent JOURNALISER leur conversion (contrôle de
    plafond de délégation, KPI multi-devises, écriture comptable) : le second membre du
    tuple est un dict sérialisable prêt à être stocké dans un journal d'audit.

    Signature INCHANGÉE (une trentaine d'appelants) : elle délègue à
    `convertir_avec_residu` et laisse tomber le résidu. C'est légitime pour un appelant
    de LECTURE — un agrégat, un contrôle de plafond, un affichage ne déplacent aucun
    argent, donc ne créent aucun écart à conserver. Un appelant qui, lui, crédite ou
    débite doit prendre `convertir_avec_residu` et loger l'écart.
    """
    montant, _residu, meta = convertir_avec_residu(
        amount=amount, from_currency=from_currency, to_currency=to_currency, tier=tier,
        on=on, usage=usage, max_staleness_days=max_staleness_days,
    )
    return montant, meta


def to_usd(amount: Decimal | str, currency: str, *, on: date_cls | None = None,
           tier: str = ExchangeRate.Tier.BCC,
           usage: str = ExchangeRate.Usage.OPERATIONNEL,
           max_staleness_days: int | None = None) -> tuple[Decimal, dict | None]:
    """Contre-valeur USD d'un montant, AVEC la provenance du taux.

    Point d'entrée destiné aux contrôles monétaires des autres modules (plafond de
    délégation de `credits`, agrégats du tableau de bord) : il n'existe aucune valeur de
    secours ici. Sans taux gouverné, `NotFoundError` — à l'appelant de dire à l'utilisateur
    que la décision ne peut pas être prise plutôt que de la prendre sur un chiffre inventé.

    Devises acceptées : "USD" (identité), "CDF" (pivot local), ou toute devise cotée.
    """
    montant, _residu, meta = to_usd_avec_residu(
        amount, currency, on=on, tier=tier, usage=usage, max_staleness_days=max_staleness_days,
    )
    return montant, meta


def to_usd_avec_residu(amount: Decimal | str, currency: str, *, on: date_cls | None = None,
                       tier: str = ExchangeRate.Tier.BCC,
                       usage: str = ExchangeRate.Usage.OPERATIONNEL,
                       max_staleness_days: int | None = None,
                       ) -> tuple[Decimal, Decimal, dict | None]:
    """`(contre-valeur USD au centime, résidu signé, provenance)`.

    À employer par tout chemin qui DÉPLACE de l'argent ; `to_usd` reste le point d'entrée
    des lectures. Sur un passage par devise tierce (X → CDF → USD), le résidu rendu est
    celui de la SECONDE jambe : c'est elle qui produit le montant servi. Le résidu de la
    première jambe est logé séparément par l'appelant s'il a matérialisé le montant CDF
    intermédiaire — sinon ce montant n'a jamais existé comme argent et n'a pas d'écart à
    conserver.
    """
    amount = to_decimal(amount)
    if currency == "USD":
        return amount, ZERO, None
    if currency == "CDF":
        return convertir_avec_residu(
            amount=amount, from_currency="CDF", to_currency="USD", tier=tier, on=on,
            usage=usage, max_staleness_days=max_staleness_days,
        )
    # Devise tierce : CDF est le pivot (X → CDF → USD), les deux taux sont journalisés.
    cdf, _residu_source, meta_source = convertir_avec_residu(
        amount=amount, from_currency=currency, to_currency="CDF", tier=tier, on=on, usage=usage,
        max_staleness_days=max_staleness_days,
    )
    usd, residu_usd, meta_usd = convertir_avec_residu(
        amount=cdf, from_currency="CDF", to_currency="USD", tier=tier, on=on, usage=usage,
        max_staleness_days=max_staleness_days,
    )
    return usd, residu_usd, {"legs": [meta_source, meta_usd]}


# ══════════════════════════════════════════════════════════ SYNCHRONISATION BCC

# BCC ne publie plus qu'un « Cours indicatif » unique par devise (colonnes acheteur/vendeur
# masquées côté source, cf. commentaire HTML de leur page) — `ExchangeRate` exige pourtant
# sell > buy. On applique un spread synthétique minime, symétrique autour du cours réel
# publié, uniquement pour satisfaire cette contrainte de schéma ; le cours BCC réel reste le
# point central (buy+sell)/2.
BCC_SYNTHETIC_SPREAD = Decimal("0.0005")  # 0.05%
BCC_RATES_URL = "https://www.bcc.cd/operations-et-marches/domaine-operationnel/operations-de-change/cours-de-change"
BCC_MAX_ATTEMPTS = 3
BCC_RETRY_DELAY_SECONDS = 2
BCC_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
# Bornes de plausibilité du « Cours indicatif » (CDF pour 1 unité de devise) — protège contre
# un parsing corrompu (page restructurée, valeur capturée par erreur) plutôt que d'enregistrer
# silencieusement un chiffre absurde comme taux officiel.
BCC_MIN_PLAUSIBLE_RATE = Decimal("0.01")
BCC_MAX_PLAUSIBLE_RATE = Decimal("1000000")
_BCC_ROW_RE = re.compile(
    r"<td>\s*([A-Z]{3})\s*</td>\s*<td>[^<]*</td>\s*(?:<!--.*?-->\s*)?<td>\s*([\d\s.,]+)\s*</td>",
    re.DOTALL,
)


def _parse_bcc_number(raw: str) -> Decimal:
    # Format BCC : espace = séparateur de milliers, virgule = séparateur décimal (ex. "2 252,2900").
    cleaned = raw.replace("\xa0", " ").replace(" ", "").replace(",", ".")
    return Decimal(cleaned)


def _fetch_bcc_page() -> str:
    """GET avec re-essais (le site BCC est parfois lent/instable) — `verify=False` car son
    certificat TLS ne se vérifie pas proprement (constaté), et un User-Agent de navigateur
    au cas où le site filtrerait les clients sans en-tête reconnu."""
    last_error: Exception | ValidationFailed | None = None
    for attempt in range(1, BCC_MAX_ATTEMPTS + 1):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", urllib3.exceptions.InsecureRequestWarning)
            try:
                resp = requests.get(BCC_RATES_URL, timeout=15, verify=False,
                                     headers={"User-Agent": BCC_USER_AGENT})
            except requests.RequestException as exc:
                last_error = exc
                logger.warning("Tentative %s/%s échouée (réseau) : %s", attempt, BCC_MAX_ATTEMPTS, exc)
                if attempt < BCC_MAX_ATTEMPTS:
                    time.sleep(BCC_RETRY_DELAY_SECONDS)
                continue
        if resp.status_code == 200:
            return resp.text
        last_error = ValidationFailed(f"BCC a répondu {resp.status_code}.")
        logger.warning("Tentative %s/%s : statut HTTP %s", attempt, BCC_MAX_ATTEMPTS, resp.status_code)
        if attempt < BCC_MAX_ATTEMPTS:
            time.sleep(BCC_RETRY_DELAY_SECONDS)
    raise ValidationFailed(f"BCC injoignable après {BCC_MAX_ATTEMPTS} tentatives : {last_error}")


def fetch_bcc_rates(*, by: str = "") -> list[ExchangeRate]:
    """Récupère le « Cours indicatif » du jour publié par la BCC (page HTML publique, pas
    d'API) et l'enregistre comme taux BCC OPÉRATIONNEL pour les devises couvertes par
    `ExchangeRate.Currency`. Robustesse : re-essais réseau, tolérance aux lignes/devises
    individuellement illisibles ou hors bornes plausibles (une devise en échec n'annule pas
    les autres), et le formulaire manuel (`set_rate`) reste utilisable si tout échoue —
    aucune donnée n'est jamais écrite tant que le parsing n'a pas produit au moins une
    valeur exploitable.

    Gouvernance : un mouvement BCC au-delà du seuil (`InstitutionConfig`) ne s'applique PAS
    tout seul parce qu'il vient d'une source officielle — il est enregistré `EN_ATTENTE` et
    attend un second acteur. Un décrochage brutal du franc est exactement le moment où une
    institution doit décider consciemment du taux qu'elle applique.

    Le taux de CLÔTURE n'est jamais dérivé de cette synchronisation : arrêter une position
    est un acte comptable délibéré, pas la conséquence d'un job.
    """
    html = _fetch_bcc_page()

    found: dict[str, Decimal] = {}
    for code, raw_value in _BCC_ROW_RE.findall(html):
        try:
            value = _parse_bcc_number(raw_value)
        except (InvalidOperation, ValueError):
            logger.warning("Valeur illisible pour %s (%r) — ignorée.", code, raw_value)
            continue
        if not (BCC_MIN_PLAUSIBLE_RATE <= value <= BCC_MAX_PLAUSIBLE_RATE):
            logger.warning("Valeur hors bornes plausibles pour %s (%s) — ignorée.", code, value)
            continue
        found[code] = value

    covered = [c for c in ExchangeRate.Currency.values if c in found]
    if not covered:
        raise ValidationFailed(
            "Aucune devise exploitable dans la page BCC (format de page changé, ou valeurs hors bornes)."
        )

    today = timezone.localdate()
    rates: list[ExchangeRate] = []
    errors: list[str] = []
    for code in covered:
        indicatif = found[code]
        buy = (indicatif * (1 - BCC_SYNTHETIC_SPREAD)).quantize(Decimal("0.000001"))
        sell = (indicatif * (1 + BCC_SYNTHETIC_SPREAD)).quantize(Decimal("0.000001"))
        try:
            rates.append(set_rate(
                tier=ExchangeRate.Tier.BCC, currency=code, buy=buy, sell=sell,
                effective_date=today, by=by, usage=ExchangeRate.Usage.OPERATIONNEL,
                source=ExchangeRate.Source.BCC, source_reference=BCC_RATES_URL,
                reason="Synchronisation automatique du cours indicatif publié par la BCC.",
            ))
        except Exception as exc:  # une devise en échec (DB, contrainte...) ne bloque pas les autres
            logger.warning("Échec enregistrement %s : %s", code, exc)
            errors.append(f"{code}: {exc}")

    if not rates:
        raise ValidationFailed("Aucun taux BCC n'a pu être enregistré : " + "; ".join(errors))
    en_attente = [r.currency for r in rates if r.status == ExchangeRate.Status.EN_ATTENTE]
    if en_attente:
        logger.warning(
            "Synchronisation BCC : %s en attente de validation (écart > seuil) — "
            "ces taux ne sont servis à aucune conversion tant qu'un second acteur "
            "ne les a pas validés.", ", ".join(en_attente),
        )
    logger.info("Synchronisation BCC : %s devise(s) traitée(s) (%s).",
                len(rates), ", ".join(r.currency for r in rates))
    return rates
