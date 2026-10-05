"""Health checks + circuit breaker pour les partenaires API (ApiPartners.jsx). Pas de
véritable intégration opérateur (M-Pesa/Orange Money/Airtel/banque) : sans identifiants
réels, un test HTTP simple sur `base_url` reste honnête — pas de résultat simulé quand rien
n'est configuré."""
from __future__ import annotations

import time

import requests
from django.db import transaction
from django.utils import timezone

from audit.services import record as audit_record
from common.exceptions import ConflictError, ValidationFailed

from .models import Partner, PartnerHealthCheck, PartnerSyncLog

CIRCUIT_FAILURE_THRESHOLD = 5
CIRCUIT_HALF_OPEN_SECONDS = 60
HEALTH_CHECK_TIMEOUT_SECONDS = 5


def _check_circuit(partner: Partner) -> None:
    if partner.circuit_state != Partner.CircuitState.OPEN:
        return
    elapsed = (timezone.now() - partner.circuit_opened_at).total_seconds() if partner.circuit_opened_at else 9999
    if elapsed < CIRCUIT_HALF_OPEN_SECONDS:
        raise ConflictError(
            f"Disjoncteur ouvert pour {partner.name} — réessayez dans "
            f"{int(CIRCUIT_HALF_OPEN_SECONDS - elapsed)}s."
        )
    partner.circuit_state = Partner.CircuitState.HALF_OPEN
    partner.save(update_fields=["circuit_state"])


@transaction.atomic
def _record_outcome(partner: Partner, *, ok: bool) -> None:
    if ok:
        partner.consecutive_failures = 0
        partner.circuit_state = Partner.CircuitState.CLOSED
        partner.circuit_opened_at = None
        partner.status = Partner.Status.ACTIF
    else:
        partner.consecutive_failures += 1
        if partner.consecutive_failures >= CIRCUIT_FAILURE_THRESHOLD:
            partner.circuit_state = Partner.CircuitState.OPEN
            partner.circuit_opened_at = timezone.now()
            partner.status = Partner.Status.DECONNECTE
    partner.save(update_fields=["consecutive_failures", "circuit_state", "circuit_opened_at", "status"])


def check_health(*, partner: Partner, by: str = "") -> PartnerHealthCheck:
    """Health check réel (Test) — n'écrit PAS `last_sync` (contrairement à `sync_partner`),
    juste un sondage de disponibilité."""
    if not partner.base_url:
        raise ValidationFailed(
            f"Aucune URL configurée pour {partner.name} — renseignez `base_url` avant de tester."
        )
    _check_circuit(partner)

    started = time.monotonic()
    try:
        response = requests.get(partner.base_url, timeout=HEALTH_CHECK_TIMEOUT_SECONDS)
        latency_ms = int((time.monotonic() - started) * 1000)
        ok = response.status_code < 400
        check = PartnerHealthCheck.objects.create(
            partner=partner, ok=ok, latency_ms=latency_ms, http_status=response.status_code,
            error_text="" if ok else f"HTTP {response.status_code}",
        )
    except requests.RequestException as exc:
        check = PartnerHealthCheck.objects.create(partner=partner, ok=False, error_text=str(exc)[:255])
        ok = False

    _record_outcome(partner, ok=ok)
    audit_record(actor=by, action="partner.test", entity_type="Partner", entity_id=str(partner.pk),
                 details={"name": partner.name, "ok": ok})
    return check


def sync_partner(*, partner: Partner, by: str = "") -> PartnerSyncLog:
    """Synchronisation — respecte le même disjoncteur que `check_health` (une synchro ne
    doit pas non plus bombarder un partenaire déjà en panne). Sans `base_url` configurée,
    dégrade en synchro "logique" (statut mis à jour, pas d'appel réseau réel) — matches le
    comportement pré-existant pour ne pas casser les partenaires jamais configurés."""
    if partner.base_url:
        _check_circuit(partner)
        started = time.monotonic()
        try:
            response = requests.get(partner.base_url, timeout=HEALTH_CHECK_TIMEOUT_SECONDS)
            ok = response.status_code < 400
            log = PartnerSyncLog.objects.create(
                partner=partner, status=PartnerSyncLog.Status.SUCCESS if ok else PartnerSyncLog.Status.FAILED,
                error_text="" if ok else f"HTTP {response.status_code}",
            )
        except requests.RequestException as exc:
            log = PartnerSyncLog.objects.create(partner=partner, status=PartnerSyncLog.Status.FAILED,
                                                 error_text=str(exc)[:255])
            ok = False
        _record_outcome(partner, ok=ok)
    else:
        log = PartnerSyncLog.objects.create(partner=partner, status=PartnerSyncLog.Status.SUCCESS)
        partner.status = Partner.Status.ACTIF
        partner.save(update_fields=["status"])

    if log.status == PartnerSyncLog.Status.SUCCESS:
        partner.last_sync = timezone.now()
        partner.save(update_fields=["last_sync"])

    audit_record(actor=by, action="partner.sync", entity_type="Partner", entity_id=str(partner.pk),
                 details={"name": partner.name, "status": log.status})
    return log


def configure_partner(*, partner: Partner, base_url: str = "", type_: str | None = None, by: str = "") -> Partner:
    partner.base_url = base_url
    if type_ is not None:
        partner.type = type_
    partner.save(update_fields=["base_url", "type"])
    audit_record(actor=by, action="partner.configure", entity_type="Partner", entity_id=str(partner.pk),
                 details={"baseUrl": base_url})
    return partner
