"""Rattachement d'un compte Mobile Money (`TreasuryAccount.kind=MOBILE_MONEY`) à un
partenaire API (Treasury.jsx/Wallets.jsx / ApiPartners.jsx) — la synchronisation délègue
entièrement au disjoncteur/health-check déjà réel de `partners.services.sync_partner`
(pas de duplication : un compte Mobile Money isolé sans lien avec la connectivité réelle du
partenaire serait un statut purement cosmétique, comme l'étaient `Agency.compliance_score`
ou `TreasuryAccount.status` avant les correctifs correspondants)."""
from __future__ import annotations

from common.exceptions import ValidationFailed

from .models import TreasuryAccount


def link_partner(*, account: TreasuryAccount, partner_id: int | None, by: str = "") -> TreasuryAccount:
    if account.kind != TreasuryAccount.Kind.MOBILE_MONEY:
        raise ValidationFailed("Seuls les comptes Mobile Money peuvent être rattachés à un partenaire.")
    from audit.services import record as audit_record
    account.partner_id = partner_id
    account.save(update_fields=["partner", "updated_at"])
    audit_record(actor=by, action="caisses.account.link_partner", entity_type="TreasuryAccount",
                 entity_id=account.code, details={"partnerId": partner_id})
    return account


def sync_account_partner(*, account: TreasuryAccount, by: str = "") -> dict:
    """Ne modifie `account.status` qu'entre ACTIF et EN_OBSERVATION (jamais BLOQUE, réservé
    au gel sur écart de caisse — un succès de synchro partenaire ne doit pas lever à lui seul
    un gel décidé pour une raison différente)."""
    if account.kind != TreasuryAccount.Kind.MOBILE_MONEY:
        raise ValidationFailed("Seuls les comptes Mobile Money peuvent être synchronisés avec un partenaire.")
    if not account.partner_id:
        raise ValidationFailed("Ce compte n'est rattaché à aucun partenaire API.")

    from audit.services import record as audit_record
    from partners.models import PartnerSyncLog
    from partners.services import sync_partner

    log = sync_partner(partner=account.partner, by=by)
    if log.status == PartnerSyncLog.Status.SUCCESS:
        if account.status == TreasuryAccount.Status.EN_OBSERVATION:
            account.status = TreasuryAccount.Status.ACTIF
            account.save(update_fields=["status", "updated_at"])
    else:
        if account.status == TreasuryAccount.Status.ACTIF:
            account.status = TreasuryAccount.Status.EN_OBSERVATION
            account.save(update_fields=["status", "updated_at"])

    account.partner.refresh_from_db()
    audit_record(actor=by, action="caisses.account.sync_partner", entity_type="TreasuryAccount",
                 entity_id=account.code, details={"partner": account.partner.name, "syncStatus": log.status})
    return {
        "accountStatus": account.status, "partnerSyncStatus": log.status,
        "partnerCircuitState": account.partner.circuit_state,
    }
