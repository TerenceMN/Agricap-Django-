"""Fonctions de sérialisation plates (convention `portfolio/serializers.py`) — utilisées à
la fois par `services.py` (snapshot d'idempotence) et `views.py` (réponse succès)."""
from __future__ import annotations

from accounts import directory
from compliance import kyc_levels

from .models import Investor, Movement, Offer, Project, Subscription

#: Unité de CHAQUE taux servi par ces sérialiseurs — déclarée, jamais devinée.
#:
#: Le module stocke ses taux dans DEUX unités, et ce n'est pas un choix : c'est un
#: héritage. `Offer.coupon_rate` et `ObligationPosition.rate` sont en points de
#: pourcentage (9,000 = 9 %) ; `Offer.loan_to_value`, `BondWithdrawal.penalty_rate`,
#: `SecondaryMarketListing.fee_rate`, `InvestmentConfig.default_rate_alert` et
#: `concentration_threshold` sont des fractions (0,60 = 60 %). Deux conventions
#: cohabitent donc jusque dans la MÊME table (`Offer.coupon_rate` à 9,000 à côté de
#: `Offer.loan_to_value` à 0,600).
#:
#: Les uniformiser en sortie sans les uniformiser en base créerait une troisième
#: convention et ferait afficher 1 250 % là où on attend 12,5 % — c'est une migration
#: de données sur des montants réels, donc une décision, jamais un effet de bord de
#: sérialisation. En attendant qu'elle soit tranchée, **aucun taux ne sort sans son
#: unité** : le consommateur lit `units`, il ne parie plus.
UNIT_PERCENT = "percent"      # 9,0 = 9 %
UNIT_FRACTION = "fraction"    # 0,09 = 9 %

OFFER_RATE_UNITS = {"couponRate": UNIT_PERCENT}
COLLATERAL_RATE_UNITS = {"loanToValue": UNIT_FRACTION}
SUBSCRIPTION_RATE_UNITS = {"couponRate": UNIT_PERCENT}
OBLIGATION_RATE_UNITS = {"rate": UNIT_PERCENT}
WITHDRAWAL_RATE_UNITS = {"penaltyRate": UNIT_FRACTION}


#: Statuts à partir desquels l'identité de l'emprunteur devient lisible — pour les
#: SEULS souscripteurs encaissés du projet (décision du fondateur : révélation à
#: partir du décaissement P08). P13 (annulé) n'y figure pas : les souscriptions y ont
#: été remboursées, il n'y a plus de souscripteur à qui révéler quoi que ce soit.
IDENTITY_DISCLOSED_STATUSES = (
    Project.Status.P08, Project.Status.P09, Project.Status.P10,
    Project.Status.P11, Project.Status.P12,
)

IDENTITY_DISCLOSURE_RULE = (
    "L'identité du promoteur (nom, contact, localisation fine) est anonymisée pendant "
    "la levée (P01→P07). Elle devient lisible à partir du décaissement (P08), et "
    "uniquement pour les investisseurs dont une souscription a été encaissée sur ce "
    "projet."
)

ZONE_UNAVAILABLE_REASON = (
    "Zone large indisponible : ce projet n'est pas rattaché à un dossier de crédit, et "
    "la localisation saisie sur le projet est un texte libre dont la granularité "
    "(province ? commune ?) est inconnue — elle n'est donc pas servie pendant l'anonymat."
)


def _project_amounts(p: Project) -> dict:
    """Grandeurs financières du projet — publiques à tous les stades.

    L'avancement d'une levée n'est pas une information d'identité : un investisseur a
    le droit de savoir combien a été collecté sur ce qu'il envisage de financer.
    """
    return {
        "fundingTarget": float(p.funding_target),
        # `requestedAmount` = le montant DEMANDÉ dans le dossier de crédit ; il peut
        # différer de l'objectif de levée (co-financement, tranche).
        "requestedAmount": float(p.requested_amount),
        # `fundedAmount` = ENCAISSÉ (B10). Les réservations vivent sur l'offre.
        "fundedAmount": float(p.funded_amount), "progressPercent": p.progress_percent,
        "disbursedAmount": float(p.disbursed_amount), "returnedAmount": float(p.returned_amount),
        "distributedAmount": float(p.distributed_amount),
    }


def project_row(p: Project) -> dict:
    """Sérialiseur PERSONNEL — identité complète, provenance du dossier de crédit.

    Il n'est jamais servi à un client : le choix du sérialiseur se fait dans la vue,
    par rôle et par droit, jamais par un `if` d'affichage (CONVENTIONS.md §5).
    """
    return {
        "id": p.pk, "code": p.code, "title": p.title,
        # Filière et localisation LUES dans le dossier quand il existe (principe 6).
        "sector": p.value_chain_label, "location": p.fine_location,
        "zone": p.broad_zone,
        "promoter": p.borrower_name, "promoterContact": p.borrower_contact,
        "status": p.status,
        **_project_amounts(p),
        # `riskCategory` est un LIBELLÉ éditorial, pas une note : il décrit le
        # projet. `riskScore` et `globalScore` ont été retirés avec le moteur de
        # scoring — ce module ne produit plus de note, et en servir une figée
        # aurait laissé les écrans en afficher une périmée.
        "riskCategory": p.risk_category,
        "managerName": p.manager_name,
        "managerSub": p.manager_sub, "isInvestable": p.is_investable,
        # Provenance : quel dossier de crédit ce projet finance.
        "creditApplicationCode": (p.credit_application.code if p.credit_application_id else None),
        "identityDisclosed": True,
    }


def project_public_row(p: Project) -> dict:
    """Sérialiseur CLIENT anonymisé — filière, zone large, score, nature du risque.

    Ni nom, ni contact, ni localisation fine de l'emprunteur : les clés n'existent
    pas dans la réponse (elles ne sont pas vidées — une clé présente et vide invite
    l'écran à la remplir autrement).
    """
    row = {
        "id": p.pk, "code": p.code, "title": p.title,
        "sector": p.value_chain_label,
        "zone": p.broad_zone,
        "status": p.status,
        **_project_amounts(p),
        "riskCategory": p.risk_category,
        "isInvestable": p.is_investable,
        "identityDisclosed": False,
        "identityDisclosureRule": IDENTITY_DISCLOSURE_RULE,
    }
    if not p.broad_zone:
        row["zoneUnavailableReason"] = ZONE_UNAVAILABLE_REASON
    return row


def project_identified_row(p: Project) -> dict:
    """Sérialiseur SOUSCRIPTEUR — le client anonymisé, plus l'identité révélée.

    Servi aux seuls investisseurs qui ont une souscription ENCAISSÉE sur un projet
    décaissé (P08+). C'est l'ajout d'un bloc, pas une variante conditionnelle du
    sérialiseur client.
    """
    row = project_public_row(p)
    row.update({
        "promoter": p.borrower_name, "promoterContact": p.borrower_contact,
        "location": p.fine_location,
        "identityDisclosed": True,
    })
    return row


def _project_detail_extras(p: Project) -> dict:
    """Champs narratifs de la vue détail, communs à tous les sérialiseurs.

    Ils décrivent le PROJET (objectifs, analyse de risque, allocation des fonds,
    impact) et non l'emprunteur : ils sont servis y compris pendant l'anonymat, où
    « la nature du risque » fait explicitement partie de ce que l'investisseur doit
    connaître avant d'engager son argent.
    """
    row = {
        "objectives": p.objectives, "description": p.description, "riskAnalysis": p.risk_analysis,
        "fundAllocation": p.fund_allocation, "impactEsg": p.impact_esg, "imageUrl": p.image_url,
        "startDate": p.start_date.isoformat() if p.start_date else None,
        "expectedMaturity": p.expected_maturity.isoformat() if p.expected_maturity else None,
        # Valorisation d'expert : les trois champs voyagent ENSEMBLE. Servir la valeur
        # sans sa date permettrait à un écran d'afficher une expertise de 2023 comme si
        # elle datait d'aujourd'hui — exactement ce que l'Annexe D interdit.
        "expertValuation": float(p.expert_valuation) if p.expert_valuation is not None else None,
        "expertValuationDate": (p.expert_valuation_date.isoformat()
                                 if p.expert_valuation_date else None),
        "expertValuationSource": p.expert_valuation_source,
    }
    latest_offer = p.offers.order_by("-created_at").first()
    if latest_offer:
        row.update({
            "typeOfTitle": latest_offer.type_of_title, "couponRate": float(latest_offer.coupon_rate),
            "paymentFrequency": latest_offer.payment_frequency, "maturityMonths": latest_offer.maturity_months,
            "minTicket": float(latest_offer.min_ticket),
            "units": OFFER_RATE_UNITS,
        })
    return row


def project_detail_row(p: Project) -> dict:
    """Vue détail PERSONNEL (ProjectDetailsModal, ProjectQA…) — champs narratifs/JSON
    volontairement absents de la liste pour ne pas alourdir `GET /investments/projects`.
    Fusionne les conditions de la dernière offre (l'offre reste une entité séparée,
    gérée par `OffersManagement.jsx`)."""
    return {**project_row(p), **_project_detail_extras(p)}


def project_public_detail_row(p: Project) -> dict:
    """Vue détail CLIENT anonymisée."""
    return {**project_public_row(p), **_project_detail_extras(p)}


def project_identified_detail_row(p: Project) -> dict:
    """Vue détail SOUSCRIPTEUR d'un projet décaissé — identité révélée."""
    return {**project_identified_row(p), **_project_detail_extras(p)}


def offer_row(o: Offer) -> dict:
    return {
        "id": o.pk, "code": o.code, "projectId": o.project_id, "typeOfTitle": o.type_of_title,
        "couponRate": float(o.coupon_rate), "maturityMonths": o.maturity_months,
        "minTicket": float(o.min_ticket), "bondUnitValue": float(o.bond_unit_value),
        "minBonds": o.min_bonds, "maxBonds": o.max_bonds, "availableBonds": o.available_bonds,
        "fundingGoal": float(o.funding_goal),
        # Deux grandeurs distinctes, jamais confondues : engagements vs argent reçu.
        "reservedAmount": float(o.reserved_amount), "fundedAmount": float(o.funded_amount),
        "minFundingAmount": float(o.min_funding_amount),
        "oversubscriptionPolicy": o.oversubscription_policy,
        "subscriptionDeadline": o.subscription_deadline.isoformat() if o.subscription_deadline else None,
        "closedAt": o.closed_at.isoformat() if o.closed_at else None,
        "status": o.status,
        "units": OFFER_RATE_UNITS,
        # Une offre OUVERTE n'est pas pour autant VISIBLE : le catalogue investisseur
        # (`metrics.open_offers_summary`) ne sert que les projets en P06. Et l'offre doit
        # exister AVANT — le passage en P06 exige justement une offre ouverte. Une offre
        # publiée trop tôt est donc normale, mais rien ne le disait : côté back-office
        # elle s'affichait « Ouvert », côté investisseur le catalogue restait vide, et
        # aucun des deux écrans ne mentionnait l'autre.
        **_offer_visibility(o),
    }


def _offer_visibility(o: Offer) -> dict:
    """Visibilité RÉELLE de l'offre pour un investisseur, et sa cause."""
    if o.status != Offer.Status.OUVERT:
        return {"visibleToInvestors": False,
                "visibilityReason": f"Offre au statut « {o.get_status_display()} »."}
    if o.project.status != Project.Status.P06:
        return {
            "visibleToInvestors": False,
            "visibilityReason": (
                f"Projet en « {o.project.get_status_display()} » : le catalogue "
                f"investisseur ne sert que les projets en levée de fonds (P06). "
                f"Faites avancer le projet pour publier cette offre."
            ),
        }
    return {"visibleToInvestors": True, "visibilityReason": ""}


def investor_row(i: Investor, manager_names: dict[str, str] | None = None,
                 kyc_statuses: dict[str, str] | None = None) -> dict:
    """Ligne investisseur — le `sub` reste la clé, le nom n'est qu'un affichage.

    L'identité est LUE dans le miroir IdP (`accounts.FintechUser`), jamais recopiée
    dans `Investor` : une seule source par concept (principe 6). Nom et email peuvent
    être vides — un compte provisionné en JIT sans claim `name` n'a pas de nom — et le
    serveur ne fabrique alors rien : il sert la chaîne vide et laisse l'écran replier
    sur le `sub`. Inventer un nom à partir d'un identifiant technique produirait une
    identité qui n'existe nulle part.

    `manager_names` : annuaire `sub → nom` déjà résolu par l'appelant
    (`accounts.directory.names_by_sub`) pour le gestionnaire assigné, qui est un
    `CharField` et non une FK. En liste, le résoudre une fois pour tout le lot ; sur
    une ligne isolée, l'omettre coûte une requête, ce qui est le bon prix.

    Appelants en liste : sélectionner `user` (`select_related`), sinon une requête par
    ligne.
    """
    if manager_names is None:
        manager_names = directory.names_by_sub([i.assigned_manager_sub])
    # Le KYC est servi par `compliance`, seule app qui le maintienne. La colonne
    # `Investor.kyc_status` existe encore en base mais n'est écrite par personne :
    # la lire affichait « En attente » à vie, même après une validation réussie.
    if kyc_statuses is None:
        kyc_statuses = kyc_levels.statuts_par_sub([i.user_id])
    return {
        "id": i.pk, "userSub": i.user_id,
        "userName": i.user.full_name, "userEmail": i.user.email,
        "investorType": i.investor_type,
        "kycStatus": kyc_statuses.get(i.user_id, kyc_levels.STATUT_DEFAUT),
        "riskProfile": i.risk_profile, "status": i.status,
        "assignedManagerSub": i.assigned_manager_sub,
        # Absent de l'annuaire = gestionnaire inconnu du miroir IdP : chaîne vide, et
        # l'écran replie sur le `sub`. Jamais le `sub` déguisé en nom.
        "assignedManagerName": manager_names.get(i.assigned_manager_sub, ""),
    }


def subscription_row(s: Subscription) -> dict:
    return {
        "id": s.pk, "investorId": s.investor_id, "offerId": s.offer_id,
        # `amount` = RÉSERVÉ, `allocatedAmount` = servi après arbitrage de
        # sursouscription, `settledAmount` = réellement encaissé. Trois grandeurs, trois
        # champs : les fondre en un seul reviendrait à présenter une intention comme un
        # placement.
        "amount": float(s.amount), "allocatedAmount": float(s.allocated_amount),
        "settledAmount": float(s.settled_amount), "refundedAmount": float(s.refunded_amount),
        "bonds": s.bonds, "queueRank": s.queue_rank, "status": s.status,
        "paymentStatus": s.payment_status,
        "couponRate": float(s.coupon_rate_snapshot), "subscriptionDate": s.subscription_date.isoformat(),
        "reservedAt": s.reserved_at.isoformat() if s.reserved_at else None,
        "settledAt": s.settled_at.isoformat() if s.settled_at else None,
        "refundedAt": s.refunded_at.isoformat() if s.refunded_at else None,
        "nextPaymentDate": s.next_payment_date.isoformat() if s.next_payment_date else None,
        "totalReceived": float(s.total_received), "subPortfolioId": s.sub_portfolio_id,
        "units": SUBSCRIPTION_RATE_UNITS,
    }


def obligation_row(p) -> dict:
    """Position obligataire d'un investisseur.

    `offerCode` / `projectCode` / `subscriptionId` sont la PROVENANCE des termes et de
    l'argent : sans eux, `rate` et `couponAmount` seraient des nombres sans auteur —
    ce qu'ils étaient quand ils tombaient des valeurs par défaut du modèle.
    """
    return {
        "id": p.pk, "name": p.name, "couponAmount": float(p.coupon_amount),
        "investedAmount": float(p.invested_amount), "rate": float(p.rate),
        "termMonths": p.term_months, "status": p.status,
        "dateCreated": p.date_created.isoformat(),
        "offerId": p.offer_id,
        "offerCode": p.offer.code if p.offer_id else None,
        "projectCode": p.offer.project.code if p.offer_id else None,
        "paymentFrequency": p.offer.payment_frequency if p.offer_id else None,
        "subscriptionId": p.subscription_id,
        "settledAmount": (float(p.subscription.settled_amount) if p.subscription_id else None),
        "termsSource": ("investments.Offer" if p.offer_id else "AUCUNE (position antérieure "
                        "au rattachement obligatoire à une offre)"),
        "units": OBLIGATION_RATE_UNITS,
    }


def distribution_row(d) -> dict:
    return {
        "id": d.pk, "offerId": d.offer_id, "kind": d.kind, "totalAmount": float(d.total_amount),
        "currency": d.currency, "valueDate": d.value_date.isoformat(), "executedBy": d.executed_by,
        "lines": [
            {"subscriptionId": line.subscription_id, "investorId": line.investor_id,
             "share": float(line.share), "amount": float(line.amount)}
            for line in d.lines.all()
        ],
    }


def investment_event_row(e) -> dict:
    """Ligne de la file consommée par le moteur d'écritures (`accounting`)."""
    return {
        "id": e.pk, "eventType": e.event_type,
        "projectCode": e.project.code if e.project_id else None,
        "offerCode": e.offer.code if e.offer_id else None,
        "subscriptionId": e.subscription_id, "investorId": e.investor_id,
        "amount": float(e.amount), "currency": e.currency,
        "segregationAccount": e.segregation_account,
        "occurredAt": e.occurred_at.isoformat(), "actor": e.actor_sub, "payload": e.payload,
        "consumedAt": e.consumed_at.isoformat() if e.consumed_at else None,
        "journalReference": e.journal_reference,
    }


def movement_row(m: Movement) -> dict:
    return {
        "id": m.pk, "type": m.type, "investorId": m.investor_id, "projectId": m.project_id,
        "amount": float(m.amount), "currency": m.currency, "status": m.status,
        "geographicZone": m.geographic_zone, "dateTime": m.date_time.isoformat(),
    }


_TECHNIQUE_VIDE = {
    "landSize": None, "productionCapacity": None, "productionCycleMonths": None,
    "yieldForecast": None, "climateRisk": "", "mitigation": "",
}

_FINANCIERE_VIDE = {
    "investmentBreakdown": {}, "revenueForecast": {}, "costStructure": {},
    "cashflowProjection": {}, "ebitdaMargin": None, "dscr": None, "irr": None,
    "financialScore": None,
}


def collateral_row(c) -> dict:
    return {
        "offerId": c.offer_id, "debtType": c.debt_type, "guarantees": c.guarantees,
        "collateralValue": float(c.collateral_value), "loanToValue": float(c.loan_to_value),
        "units": COLLATERAL_RATE_UNITS,
    }


def performance_report_row(r) -> dict:
    return {
        "id": r.pk, "projectId": r.project_id, "reportingPeriod": r.reporting_period,
        "submissionDate": r.submission_date.isoformat(),
        # Montants, quantités et écarts sont stockés en `Decimal` (principe 4) ; la
        # conversion en nombre se fait ICI, à la frontière, comme pour tous les
        # montants du module — le contrat du front est inchangé.
        "actualRevenue": float(r.actual_revenue), "forecastRevenue": float(r.forecast_revenue),
        "actualCosts": float(r.actual_costs), "forecastCosts": float(r.forecast_costs),
        "actualProduction": float(r.actual_production),
        "forecastProduction": float(r.forecast_production),
        # Trois écarts CALCULÉS ET FIGÉS par le serveur. `deviationPercent` reste
        # l'écart de revenu (nom historique consommé tel quel). `unfavorable` dit le
        # sens : un écart de coûts positif est défavorable, un écart de revenu positif
        # ne l'est pas — l'écran n'a pas à connaître cette règle métier.
        "deviationPercent": float(r.deviation_percent),
        "revenueDeviationPercent": float(r.deviation_percent),
        "costDeviationPercent": float(r.cost_deviation_percent),
        "productionDeviationPercent": float(r.production_deviation_percent),
        "unfavorable": {
            "revenue": r.deviation_percent < 0,
            "costs": r.cost_deviation_percent > 0,
            "production": r.production_deviation_percent < 0,
        },
        "hasForecast": {
            "revenue": bool(r.forecast_revenue), "costs": bool(r.forecast_costs),
            "production": bool(r.forecast_production),
        },
        "deviationComments": r.deviation_comments,
        "validationStatus": r.validation_status, "validatedBy": r.validated_by,
        "validationDate": r.validation_date.isoformat() if r.validation_date else None,
    }


def analysis_attachment_row(a) -> dict:
    """Pièce justificative d'une analyse. `url` est absent quand le stockage n'en
    expose pas — l'écran affiche alors le nom sans lien mort."""
    return {
        "id": a.pk, "kind": a.kind, "kindLabel": a.get_kind_display(),
        "originalName": a.original_name, "label": a.label,
        "url": a.file.url if a.file else None,
        "uploadedBy": a.uploaded_by, "uploadedAt": a.uploaded_at.isoformat(),
    }
