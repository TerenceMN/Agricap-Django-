"""Le client propose un garant, l'agent valide — décision du fondateur, juillet 2026.

Pourquoi ce module existe
-------------------------
La désignation d'un garant (`credits.guarantees.register_moral_guarantee`) est
réservée au personnel (`CAN_INSTRUCT`), et aucun écran client ne l'appelait :
la caution solidaire était donc **inatteignable depuis l'application**, ce qui
expliquait mécaniquement que la boîte de réception du garant reste vide —
personne ne pouvait jamais rien lui demander.

Ouvrir cet endpoint au client aurait été la réponse courte, et la mauvaise :
une désignation notifie un tiers, ouvre une fenêtre de consentement de 72 h et
immobilise sa capacité d'engagement. Laissée au demandeur, elle permettrait
d'inonder n'importe qui de demandes de caution, et de sonder l'application pour
découvrir qui a de l'épargne.

D'où la forme retenue : **une proposition, qui n'est pas une désignation.**

  Client ──propose──> GuaranteeProposal ──agent valide──> register_moral_guarantee
                              │                                    │
                              │                          CreditGuarantee (pending_consent)
                              └──agent refuse──> refusée, conservée, journalisée

Ce que ce module NE fait pas
----------------------------
Il ne crée aucune `CreditGuarantee` lui-même, ne touche à aucun statut de
garantie, ne calcule aucune couverture. Une proposition ne pèse **rien** : ni
dans la couverture, ni dans le scoring, ni dans une décision. C'est le cœur du
principe 9 (« toute garantie est opposable ou n'est pas »), et
`tests_guarantee_proposals` échoue si une proposition non validée fait bouger la
couverture d'un seul centime.

La désignation, elle, reste unique : la validation appelle la fonction
existante. Il n'existe pas deux chemins vers une caution (principe 6).

Asymétrie d'information (principe 7)
------------------------------------
Les règles de capacité du garant portent sur la situation financière d'**une
autre personne** : son épargne, ses cautions actives, ses défauts. Ce module ne
les évalue jamais à la proposition — non par oubli, mais pour rendre la fuite
structurellement impossible : ce qui n'est pas calculé ne peut pas être divulgué.
Le demandeur n'apprend donc RIEN sur les finances de la personne qu'il propose.
L'agent, lui, obtient le diagnostic complet dans sa file de validation
(`serialize_for_staff`), et c'est lui — pas le système — qui refuse.

Deux sérialiseurs, jamais un `if` d'affichage : `serialize_for_applicant` et
`serialize_for_staff` n'ont aucun champ en commun par accident.
"""
from __future__ import annotations

import logging
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from django.conf import settings
from django.db import transaction
from django.utils import timezone

logger = logging.getLogger(__name__)

CENT = Decimal("0.01")


# ── Exceptions ────────────────────────────────────────────────────────────────

class ProposalError(Exception):
    """Refus d'un acte sur une proposition de caution.

    Convention reprise telle quelle de `credits.workflow.WorkflowError` et de
    `credits.guarantor.GuarantorError` : `code` en MAJUSCULES consommé par le
    front, `http_status` porté par la règle et non par la vue, `as_errors()` pour
    la réponse structurée du principe 5. Aucune vue ne réécrit un code.
    """

    code = "PROPOSAL_ERROR"
    http_status = 422

    def __init__(self, message: str, errors: list[dict] | None = None) -> None:
        super().__init__(message)
        self.errors = errors or []

    def as_errors(self) -> list[dict]:
        return self.errors or [{"code": self.code, "message": str(self)}]


class NotApplicationOwner(ProposalError):
    """Seul le titulaire du dossier propose un garant pour ce dossier."""

    code = "NOT_APPLICATION_OWNER"
    http_status = 403


class ApplicationNotOpenForGuarantee(ProposalError):
    """Le dossier n'est plus à une étape où une garantie peut être ajoutée."""

    code = "APPLICATION_NOT_OPEN_FOR_GUARANTEE"
    http_status = 409


class TooManyOpenProposals(ProposalError):
    """Trop de propositions attendent déjà une décision sur ce dossier.

    Garde-fou anti-sondage : sans plafond, un demandeur pourrait proposer en
    série pour découvrir, par les refus, qui de son groupe a de l'épargne. Le
    plafond est explicite, il est dit à l'utilisateur, et son déclenchement est
    journalisé — jamais un blocage silencieux.
    """

    code = "TOO_MANY_OPEN_PROPOSALS"


class ProposalQuotaExceeded(ProposalError):
    """Plafond de propositions atteint sur la durée de vie du dossier."""

    code = "PROPOSAL_QUOTA_EXCEEDED"


class DuplicateProposal(ProposalError):
    """Cette personne est déjà proposée sur ce dossier et attend une décision."""

    code = "DUPLICATE_PROPOSAL"
    http_status = 409


class ProposalAlreadyDecided(ProposalError):
    """Une proposition déjà tranchée ne se rejoue pas (principe 3)."""

    code = "PROPOSAL_ALREADY_DECIDED"
    http_status = 409


class MoralGuaranteeAlreadyLive(ProposalError):
    """Une caution solidaire vivante existe déjà sur ce dossier.

    `register_moral_guarantee` éteint la caution précédente quand une nouvelle
    est posée. C'est le bon comportement pour une correction d'agence, mais pas
    pour une validation de proposition : on libérerait en silence un garant qui a
    déjà consenti, et personne ne le lui dirait. La règle exige donc un acte
    explicite (libération de la caution en cours) avant d'en valider une autre.
    """

    code = "MORAL_GUARANTEE_ALREADY_LIVE"
    http_status = 409


class DecisionReasonRequired(ProposalError):
    """Toute décision d'agent est motivée (principe 2)."""

    code = "DECISION_REASON_REQUIRED"


class RefusalReasonInvalid(ProposalError):
    """Le motif de refus n'appartient pas au vocabulaire fixé."""

    code = "REFUSAL_REASON_INVALID"


class GuarantorIdentityRequired(ProposalError):
    """La pièce d'identité du garant n'a pas été relevée.

    C'est l'apport propre de l'agent : le demandeur propose une personne, l'agent
    atteste avoir vu la pièce. Sans elle, la caution retomberait au déclaratif —
    exactement ce que le principe 9 refuse.
    """

    code = "GUARANTOR_IDENTITY_REQUIRED"


class InvalidProposalAmount(ProposalError):
    """Montant proposé absent, nul, négatif ou illisible."""

    code = "INVALID_PROPOSAL_AMOUNT"


# ── Paramètres (principe 8 : les seuils vivent en base, pas dans le code) ──────

#: Valeurs de secours. `InstitutionConfig` (app `referentiel`) est le bon foyer
#: pour ces deux plafonds — leur ajout y appartient au comité, pas à ce lot :
#: modifier un modèle d'une autre app depuis le module crédit créerait une
#: migration hors périmètre. En attendant, `settings` les surcharge sans
#: redéploiement de code, et tout repli est loggé.
FALLBACK_MAX_OPEN_PROPOSALS = 3
FALLBACK_MAX_PROPOSALS_PER_APPLICATION = 10


def _setting(name: str, fallback):
    value = getattr(settings, name, None)
    if value is None:
        logger.warning(
            "%s non configuré : repli sur la valeur de secours %s. Ce plafond "
            "devrait être décidé par le comité, pas subi par défaut.",
            name, fallback,
        )
        return fallback
    return value


def max_open_proposals() -> int:
    """Propositions simultanément en attente de décision, par dossier."""
    return int(_setting("CREDIT_MAX_OPEN_GUARANTEE_PROPOSALS",
                        FALLBACK_MAX_OPEN_PROPOSALS))


def max_proposals_per_application() -> int:
    """Propositions au total sur la vie d'un dossier, décidées comprises."""
    return int(_setting("CREDIT_MAX_GUARANTEE_PROPOSALS_PER_APPLICATION",
                        FALLBACK_MAX_PROPOSALS_PER_APPLICATION))


#: Étapes du dossier où une garantie peut encore être constituée. Après
#: l'approbation, la couverture a servi à décider : y ajouter une caution
#: changerait rétroactivement la base d'une décision déjà prise.
OPEN_FOR_GUARANTEE_STATUSES = ("draft", "submitted", "in_analysis", "adjourned")


# ── Lectures ──────────────────────────────────────────────────────────────────

def open_proposals(application):
    from credits.models import GuaranteeProposal
    return GuaranteeProposal.objects.filter(
        application=application, status=GuaranteeProposal.Status.PROPOSED,
    )


def live_moral_guarantee(application):
    """Caution solidaire encore vivante sur ce dossier, ou `None`."""
    from credits.models import CreditGuarantee
    return (
        application.guarantees.filter(
            guarantee_type=CreditGuarantee.GuaranteeType.MORALE,
            status__in=[
                CreditGuarantee.Status.PENDING,
                CreditGuarantee.Status.PENDING_CONSENT,
                CreditGuarantee.Status.CONSENTED,
                CreditGuarantee.Status.ACTIVE,
                CreditGuarantee.Status.CALLED,
            ],
        )
        .order_by("-created_at")
        .first()
    )


def proposals_of(user, status: str = ""):
    """Les propositions déposées par `user` — et rien d'autre."""
    from credits.models import GuaranteeProposal
    qs = (
        GuaranteeProposal.objects
        .filter(proposed_by=user)
        .select_related("application", "guarantor", "guarantee")
    )
    if status:
        qs = qs.filter(status=status)
    return qs.order_by(_pending_first(), "-created_at")


def pending_queue(status: str = "", application_code: str = ""):
    """File de validation du personnel — les propositions en attente d'abord."""
    from credits.models import GuaranteeProposal
    qs = (
        GuaranteeProposal.objects
        .select_related("application__client", "application__value_chain",
                        "proposed_by", "guarantor", "guarantee")
    )
    qs = qs.filter(status=status) if status else qs.filter(
        status=GuaranteeProposal.Status.PROPOSED,
    )
    if application_code:
        qs = qs.filter(application__code=application_code)
    # Les plus anciennes d'abord : une proposition qui attend est un dossier qui
    # n'avance pas, et le demandeur ne peut rien faire de son côté.
    return qs.order_by(_pending_first(), "created_at")


def _pending_first():
    from django.db.models import Case, IntegerField, Value, When
    from credits.models import GuaranteeProposal
    return Case(
        When(status=GuaranteeProposal.Status.PROPOSED, then=Value(0)),
        default=Value(1), output_field=IntegerField(),
    ).asc()


def candidates_for(application) -> list[dict]:
    """Personnes que le titulaire peut proposer : les membres de ses groupes.

    Volontairement **sans aucune donnée de capacité** : ni épargne, ni nombre de
    cautions, ni disponibilité. Une liste qui dirait « éligible / non éligible »
    serait exactement l'oracle que le principe 7 interdit — il suffirait de la
    lire pour savoir qui du groupe a de l'épargne.

    Le tri est alphabétique et stable : un ordre « par capacité » divulguerait
    la même information sans la nommer.
    """
    from credits.guarantor import shared_groups

    client = application.client
    deja_proposes = set(
        open_proposals(application).values_list("guarantor_id", flat=True)
    )

    try:
        from savings.models import SavingsGroupMember
    except Exception:                                   # module épargne absent
        return []

    membres: dict[Any, dict] = {}
    # `shared_groups(client, client)` = les groupes du client — la fonction
    # existante fait exactement ce travail, on ne réécrit pas une seconde requête
    # d'appartenance (principe 6).
    for group in shared_groups(client, client):
        for membership in (
            SavingsGroupMember.objects
            .filter(group=group).select_related("user")
        ):
            user = membership.user
            if str(user.pk) == str(client.pk):
                continue
            entry = membres.setdefault(user.pk, {
                "sub": str(user.pk),
                "displayName": user.full_name or str(user.pk),
                "sharedGroups": [],
                "alreadyProposed": user.pk in deja_proposes,
            })
            entry["sharedGroups"].append(
                {"id": group.pk, "name": group.name, "type": group.type},
            )
    return sorted(membres.values(), key=lambda m: m["displayName"].lower())


# ── Proposition (acte du CLIENT) ──────────────────────────────────────────────

def propose(
    application,
    proposer,
    guarantor_sub: str,
    covered_amount: Decimal | None = None,
    message: str = "",
    ip: str | None = None,
):
    """Le titulaire du dossier propose une personne comme garant.

    Rien d'opposable ne naît ici : ni caution, ni notification au garant, ni
    engagement. Le garant pressenti n'apprend même pas qu'on a pensé à lui —
    c'est la validation de l'agent qui le sollicitera, et alors seulement.

    Les contrôles exécutés sont **structurels** : titularité, étape du dossier,
    quotas, existence du garant, lien de groupe, montant. Les quatre règles
    financières de `credits.guarantor` (épargne, cautions vivantes, défaut,
    caution croisée) ne sont **pas** évaluées : elles portent sur la situation
    d'un tiers, et ce qui n'est pas calculé ici ne peut pas fuir vers le
    demandeur. Elles s'appliquent intégralement à la validation, où l'agent est
    le destinataire légitime du diagnostic.

    Volontairement **non atomique dans son ensemble** — même raison que
    `guarantees.record_guarantor_consent` : le contrôle de quota doit pouvoir
    ÉCRIRE (la trace du blocage) puis lever. Sous un `@atomic` global, ce `raise`
    annulait l'écriture du journal, et le garde-fou anti-sondage devenait
    exactement ce qu'il prétend interdire : un blocage silencieux. L'atomicité
    est posée là où elle a un sens — autour de la création et de sa
    journalisation, indissociables.
    """
    from credits.guarantor import (
        GuarantorIsApplicant, GuarantorNotInGroup, GuarantorUnknown, shared_groups,
    )
    from credits.models import GuaranteeProposal

    _assert_owner(application, proposer)
    _assert_application_open(application)

    montant = _clean_amount(covered_amount, application)

    guarantor = _resolve_user(guarantor_sub)
    if guarantor is None:
        raise GuarantorUnknown(
            "Cette personne n'a pas de compte AGRICAP. Un garant doit pouvoir "
            "accepter lui-même : proposez un membre inscrit de votre groupe."
        )
    if str(guarantor.pk) == str(application.client_id):
        raise GuarantorIsApplicant(
            "Vous ne pouvez pas vous porter caution à vous-même."
        )
    if not shared_groups(application.client, guarantor):
        raise GuarantorNotInGroup(
            "Vous ne partagez aucun groupe ni coopérative avec cette personne. "
            "Une caution solidaire s'appuie sur un lien de groupe réel : "
            "choisissez un membre de l'un de vos groupes."
        )

    _assert_quotas(application, proposer, guarantor, ip=ip)

    with transaction.atomic():
        proposal = GuaranteeProposal.objects.create(
            application=application,
            proposed_by=proposer,
            guarantor=guarantor,
            covered_amount=montant,
            currency=application.currency,
            message=(message or "").strip(),
        )
        _audit(
            actor=str(proposer.pk), action="credit.guarantee.proposal_submitted",
            proposal=proposal, ip=ip,
            details={"guarantorSub": str(guarantor.pk),
                     "coveredAmount": str(montant)},
        )
    _notify_agency(proposal)
    return proposal


def _assert_owner(application, proposer) -> None:
    if str(application.client_id) != str(getattr(proposer, "pk", "")):
        raise NotApplicationOwner(
            "Vous ne pouvez proposer un garant que pour votre propre dossier."
        )


def _assert_application_open(application) -> None:
    if application.status not in OPEN_FOR_GUARANTEE_STATUSES:
        from credits.models import CreditApplication
        try:
            libelle = CreditApplication.Status(application.status).label
        except ValueError:
            libelle = application.status
        raise ApplicationNotOpenForGuarantee(
            f"Ce dossier est « {libelle} » : il n'accepte plus de nouvelle "
            "garantie. Adressez-vous à votre agence."
        )


def _assert_quotas(application, proposer, guarantor, ip: str | None = None) -> None:
    """Plafonds anti-sondage. Chaque blocage est journalisé, jamais silencieux."""
    from credits.models import GuaranteeProposal

    deja = open_proposals(application)
    if deja.filter(guarantor=guarantor).exists():
        raise DuplicateProposal(
            "Vous avez déjà proposé cette personne sur ce dossier et la demande "
            "attend encore la validation de votre agence."
        )

    plafond_ouvert = max_open_proposals()
    en_cours = deja.count()
    if en_cours >= plafond_ouvert:
        _audit_blocked(application, proposer, guarantor,
                       TooManyOpenProposals.code, ip=ip,
                       details={"openProposals": en_cours, "ceiling": plafond_ouvert})
        raise TooManyOpenProposals(
            f"Vous avez déjà {en_cours} proposition(s) de caution en attente sur "
            f"ce dossier (maximum {plafond_ouvert}). Attendez la réponse de "
            "votre agence avant d'en proposer une autre."
        )

    plafond_total = max_proposals_per_application()
    total = GuaranteeProposal.objects.filter(application=application).count()
    if total >= plafond_total:
        _audit_blocked(application, proposer, guarantor,
                       ProposalQuotaExceeded.code, ip=ip,
                       details={"totalProposals": total, "ceiling": plafond_total})
        raise ProposalQuotaExceeded(
            f"Ce dossier a déjà fait l'objet de {total} propositions de caution "
            f"(maximum {plafond_total}). Contactez votre agence pour la suite."
        )


def _clean_amount(covered_amount, application) -> Decimal:
    brut = (
        covered_amount
        if covered_amount is not None
        else (application.amount_approved or application.amount_requested)
    )
    try:
        montant = Decimal(str(brut)).quantize(CENT, rounding=ROUND_HALF_UP)
    except Exception:
        raise InvalidProposalAmount(
            "Le montant de la caution demandée n'est pas un nombre valide."
        )
    if montant <= 0:
        raise InvalidProposalAmount(
            "Indiquez le montant que vous demandez à votre garant de couvrir : "
            "c'est ce montant qui définira son engagement."
        )
    return montant


def _resolve_user(sub: str):
    if not (sub or "").strip():
        return None
    from accounts.models import FintechUser
    return FintechUser.objects.filter(pk=str(sub).strip()).first()


# ── Décision (acte de l'AGENT — humaine et motivée, principe 2) ───────────────

def validate(
    proposal,
    agent_sub: str,
    comment: str,
    guarantor_id_number: str,
    guarantor_name: str = "",
    guarantor_phone: str = "",
    covered_amount: Decimal | None = None,
    ip: str | None = None,
):
    """Transforme la proposition en désignation opposable.

    Le mécanisme de désignation n'est PAS réimplémenté : cette fonction appelle
    `credits.guarantees.register_moral_guarantee`, seule porte d'entrée vers une
    `CreditGuarantee` de type caution. Les sept contrôles de capacité, la
    fenêtre de consentement, la notification du garant et la journalisation de
    la désignation restent donc exactement ceux qui existaient — et un refus de
    règle remonte avec SON code (`GUARANTOR_OVEREXTENDED`,
    `GUARANTOR_TOO_MANY_PLEDGES`…), destiné à l'agent et à lui seul.

    Ce que l'agent apporte en propre : la pièce d'identité relevée, un motif, et
    la faculté d'ajuster le montant couvert. C'est cet ajustement qui fait de la
    validation une décision et non un enregistrement.

    Non atomique dans son ensemble, volontairement : `register_moral_guarantee`
    porte sa propre transaction, et les contrôles de capacité qu'elle exécute
    doivent pouvoir échouer sans annuler autre chose. L'atomicité est posée là
    où elle est indispensable — autour de la mise à jour de la proposition et de
    sa journalisation, indissociables (une décision sans trace ne vaut rien).
    """
    from credits.guarantees import register_moral_guarantee
    from credits.models import GuaranteeProposal

    _assert_pending(proposal)
    _assert_application_open(proposal.application)

    motif = (comment or "").strip()
    if not motif:
        raise DecisionReasonRequired(
            "Indiquez le motif de votre validation : une caution engage une "
            "personne, la décision qui la constitue doit être expliquée."
        )

    piece = (guarantor_id_number or "").strip()
    if not piece:
        raise GuarantorIdentityRequired(
            "Renseignez le numéro de la pièce d'identité du garant (carte "
            "d'électeur, passeport…), relevée en agence."
        )

    vivante = live_moral_guarantee(proposal.application)
    if vivante is not None:
        raise MoralGuaranteeAlreadyLive(
            "Une caution solidaire est déjà en cours sur ce dossier "
            f"({vivante.get_status_display().lower()}). Libérez-la ou attendez "
            "son issue avant de valider une autre proposition."
        )

    montant = (
        _clean_amount(covered_amount, proposal.application)
        if covered_amount is not None
        else proposal.covered_amount
    )

    garantie = register_moral_guarantee(
        application=proposal.application,
        guarantor_name=(guarantor_name or "").strip() or (
            proposal.guarantor.full_name or str(proposal.guarantor.pk)
        ),
        guarantor_phone=(guarantor_phone or "").strip() or (
            getattr(proposal.guarantor, "phone", "") or ""
        ),
        guarantor_id_number=piece,
        registered_by_sub=agent_sub,
        guarantor_sub=str(proposal.guarantor.pk),
        montant_couvert=montant,
        notes=motif,
    )

    with transaction.atomic():
        proposal.status = GuaranteeProposal.Status.VALIDATED
        proposal.decided_by_sub = agent_sub
        proposal.decided_at = timezone.now()
        proposal.decision_comment = motif
        proposal.guarantee = garantie
        proposal.save(update_fields=[
            "status", "decided_by_sub", "decided_at", "decision_comment",
            "guarantee", "updated_at",
        ])
        _audit(
            actor=agent_sub, action="credit.guarantee.proposal_validated",
            proposal=proposal, ip=ip,
            details={
                "guaranteeId": garantie.pk,
                "coveredAmount": str(montant),
                # L'écart entre ce qui a été demandé et ce qui a été retenu est
                # une donnée de gouvernance : il dit ce que l'agence corrige.
                "proposedAmount": str(proposal.covered_amount),
                "amountAdjusted": montant != proposal.covered_amount,
            },
        )
    _notify_applicant(proposal, validated=True)
    return proposal


@transaction.atomic
def refuse(proposal, agent_sub: str, reason_code: str, comment: str,
           ip: str | None = None):
    """L'agent écarte la proposition — motivée, journalisée, jamais effacée.

    Deux motifs coexistent et ne s'adressent pas aux mêmes personnes :
    `reason_code` appartient à un vocabulaire fixe et non-divulguant, et c'est
    lui que le demandeur lira ; `comment` est le motif libre de l'agent, qui
    reste interne. Rien n'empêche un agent d'écrire « il a déjà trois cautions »
    dans le champ libre : c'est précisément pourquoi ce champ ne sort jamais
    vers le demandeur.
    """
    from credits.models import GuaranteeProposal

    _assert_pending(proposal)

    motif = (comment or "").strip()
    if not motif:
        raise DecisionReasonRequired(
            "Indiquez le motif de votre refus : il reste au dossier et devra "
            "pouvoir être relu."
        )

    codes = {c for c, _ in GuaranteeProposal.RefusalReason.choices}
    if reason_code not in codes:
        raise RefusalReasonInvalid(
            "Motif de refus inconnu. Motifs admis : " + ", ".join(sorted(codes)) + "."
        )

    proposal.status = GuaranteeProposal.Status.REFUSED
    proposal.decided_by_sub = agent_sub
    proposal.decided_at = timezone.now()
    proposal.decision_comment = motif
    proposal.refusal_reason_code = reason_code
    proposal.save(update_fields=[
        "status", "decided_by_sub", "decided_at", "decision_comment",
        "refusal_reason_code", "updated_at",
    ])

    _audit(
        actor=agent_sub, action="credit.guarantee.proposal_refused",
        proposal=proposal, ip=ip,
        details={"reasonCode": reason_code},
    )
    _notify_applicant(proposal, validated=False)
    return proposal


def _assert_pending(proposal) -> None:
    from credits.models import GuaranteeProposal
    if proposal.status != GuaranteeProposal.Status.PROPOSED:
        raise ProposalAlreadyDecided(
            f"Cette proposition a déjà été traitée "
            f"({proposal.get_status_display().lower()}). Une décision ne se "
            "rejoue pas : le demandeur doit déposer une nouvelle proposition."
        )


# ── Journalisation ────────────────────────────────────────────────────────────

def _audit(*, actor: str, action: str, proposal, details: dict | None = None,
           ip: str | None = None) -> None:
    """Trace append-only d'un acte sur une proposition (principe 3).

    Non best-effort : c'est la décision d'un humain sur l'engagement d'un tiers.
    L'appel vit dans la transaction de l'appelant — si l'audit échoue, la
    décision est annulée avec lui.
    """
    from audit.services import record

    payload = {
        "applicationCode": proposal.application.code,
        "proposalId": proposal.pk,
        "status": proposal.status,
        "guarantorSub": str(proposal.guarantor_id),
    }
    payload.update(details or {})
    record(actor=actor or "", action=action, entity_type="GuaranteeProposal",
           entity_id=proposal.pk, details=payload, ip=ip)


def _audit_blocked(application, proposer, guarantor, code: str,
                   details: dict | None = None, ip: str | None = None) -> None:
    """Journalise un plafond atteint. Un blocage silencieux n'existe pas ici.

    C'est la moitié utile du garde-fou anti-sondage : le plafond arrête la
    série, le journal la rend visible. Un demandeur qui bute dix fois sur le
    plafond n'a pas un problème d'interface, il a un comportement à regarder.
    """
    from audit.services import record

    payload = {
        "applicationCode": application.code,
        "reason": code,
        "proposerSub": str(getattr(proposer, "pk", "")),
        "guarantorSub": str(getattr(guarantor, "pk", "")),
    }
    payload.update(details or {})
    record(actor=str(getattr(proposer, "pk", "")),
           action="credit.guarantee.proposal_blocked",
           entity_type="CreditApplication", entity_id=application.code,
           details=payload, ip=ip)


# ── Notifications (best-effort : elles n'annulent jamais une décision) ────────

#: Écrans front. Portés dans le corps faute de champ d'URL sur
#: `notifications.Notification` — même limite que `guarantees.GUARANTEE_REQUESTS_PATH`.
PROPOSAL_QUEUE_PATH = "/credit/cautions-a-valider"
PROPOSAL_TRACKING_PATH = "/mes-cautions"


def _notify_applicant(proposal, validated: bool) -> None:
    """Prévient le demandeur de la décision — sans jamais expliquer un refus.

    Le message de refus reprend le LIBELLÉ du motif codifié, pas le commentaire
    de l'agent. C'est la même frontière que dans `serialize_for_applicant`, et
    elle doit tenir sur les deux canaux : une notification bavarde annulerait un
    sérialiseur prudent.
    """
    try:
        from notifications.models import Notification

        app = proposal.application
        if validated:
            titre = "Votre proposition de caution est validée"
            corps = (
                f"Votre agence a validé la caution proposée pour le dossier "
                f"{app.code}. {proposal.guarantor.full_name or 'La personne'} "
                f"a été sollicitée et doit maintenant accepter ou refuser. "
                f"Vous serez prévenu de sa réponse.\n\n"
                f"Suivre : {PROPOSAL_TRACKING_PATH}"
            )
        else:
            libelle = _refusal_label(proposal)
            titre = "Votre proposition de caution n'a pas été retenue"
            corps = (
                f"Votre agence n'a pas retenu la caution proposée pour le "
                f"dossier {app.code}. {libelle} Vous pouvez proposer une autre "
                f"personne de votre groupe.\n\n"
                f"Suivre : {PROPOSAL_TRACKING_PATH}"
            )
        Notification.objects.create(
            user=proposal.proposed_by, title=titre, body=corps,
        )
    except Exception:
        logger.warning(
            "[NOTIF] décision de proposition %s non notifiée au demandeur",
            getattr(proposal, "pk", "?"), exc_info=True,
        )


def _notify_agency(proposal) -> None:
    """Aucune notification poussée : la file de validation est le canal.

    Notifier « l'agence » supposerait de savoir QUI notifier — le rattachement
    d'un dossier à une agence existe (`CreditApplication.agency`), mais la liste
    des agents d'une agence appartient à `rbac`, hors du périmètre de ce lot.
    Une notification adressée à tout le personnel serait pire que pas de
    notification. La file `GET /guarantee-proposals/queue/` est donc le canal, et
    ce point est signalé au fondateur plutôt que bricolé.
    """
    return None


# ── Sérialiseurs — un par rôle, jamais un `if` d'affichage ────────────────────

#: États dérivés servis au demandeur. Ils ne sont pas stockés : ils composent
#: `GuaranteeProposal.status` (décision de l'agence) et
#: `CreditGuarantee.status` (réponse du garant), qui restent les deux seules
#: nomenclatures canoniques (principe 6). Le front affiche, il ne compose pas.
_GUARANTEE_STATE = {
    "pending": "awaiting_consent",
    "pending_consent": "awaiting_consent",
    "consented": "accepted",
    "active": "accepted",
    "called": "called",
    "declined": "declined_by_guarantor",
    "expired": "expired",
    "released": "released",
}


def applicant_state(proposal) -> str:
    """État unique et lisible du parcours, calculé côté serveur."""
    from credits.models import GuaranteeProposal

    if proposal.status == GuaranteeProposal.Status.PROPOSED:
        return "proposed"
    if proposal.status == GuaranteeProposal.Status.REFUSED:
        return "refused_by_agent"

    garantie = proposal.guarantee
    if garantie is None:
        return "validated"
    if garantie.is_consent_expired:
        return "expired"
    return _GUARANTEE_STATE.get(garantie.status, garantie.status)


def _refusal_label(proposal) -> str:
    from credits.models import GuaranteeProposal
    if not proposal.refusal_reason_code:
        return ""
    try:
        return GuaranteeProposal.RefusalReason(proposal.refusal_reason_code).label
    except ValueError:
        return GuaranteeProposal.RefusalReason.AUTRE.label


def serialize_for_applicant(proposal) -> dict[str, Any]:
    """Forme servie au DEMANDEUR — ce qu'il a demandé, à qui, et où ça en est.

    Ce sérialiseur est le garde-fou principal de ce lot. Les règles de capacité
    du garant portent sur la situation financière d'une AUTRE personne : son
    épargne, ses cautions en cours, ses incidents de paiement. Le demandeur
    n'apprend rien de tout cela — il apprend, au plus, que la personne ne peut
    pas se porter caution en ce moment, et c'est le libellé figé du motif qui le
    lui dit.

    N'apparaissent donc jamais ici : l'épargne du garant, son plafond
    d'engagement, ses cautions vivantes, ses défauts, le code technique de la
    règle qui a bloqué, ni le commentaire libre de l'agent. La fonction est
    séparée de `serialize_for_staff` justement pour qu'aucun `if` ne puisse un
    jour laisser passer l'un de ces champs.
    """
    garantie = proposal.guarantee
    return {
        "id": proposal.pk,
        "applicationCode": proposal.application.code,
        "state": applicant_state(proposal),
        "status": proposal.status,
        "guarantor": {
            "displayName": proposal.guarantor.full_name or str(proposal.guarantor.pk),
        },
        "coveredAmount": float(proposal.covered_amount),
        "coveredCurrency": proposal.currency,
        "message": proposal.message,
        "createdAt": proposal.created_at.isoformat(),
        "decidedAt": proposal.decided_at.isoformat() if proposal.decided_at else None,
        # Libellé fixe et non-divulguant, jamais `decision_comment`.
        "refusalReason": _refusal_label(proposal),
        "consentExpiresAt": (
            garantie.consent_expires_at.isoformat()
            if garantie is not None and garantie.consent_expires_at else None
        ),
    }


def serialize_for_staff(proposal, with_capacity: bool = False) -> dict[str, Any]:
    """Forme servie au PERSONNEL — la même proposition, plus de quoi décider.

    L'agent voit ce que le demandeur ne voit pas, et c'est légitime : c'est lui
    qui porte la décision. `with_capacity` ajoute le diagnostic chiffré du
    garant (épargne, plafond, cautions vivantes) et la première règle qui
    bloquerait la désignation — la file de validation en a besoin, une liste de
    consultation non.

    Ce diagnostic coûte plusieurs requêtes par ligne : il est calculé pour la
    file (courte par construction, et bornée par `limit`), pas pour un export.
    """
    garantie = proposal.guarantee
    app = proposal.application
    return {
        "id": proposal.pk,
        "applicationCode": app.code,
        "applicationStatus": app.status,
        "state": applicant_state(proposal),
        "status": proposal.status,
        "applicant": {
            "sub": str(proposal.proposed_by_id),
            "displayName": (
                proposal.proposed_by.full_name or str(proposal.proposed_by_id)
            ),
        },
        "guarantor": {
            "sub": str(proposal.guarantor_id),
            "displayName": proposal.guarantor.full_name or str(proposal.guarantor_id),
            "phone": getattr(proposal.guarantor, "phone", "") or "",
        },
        "coveredAmount": float(proposal.covered_amount),
        "coveredCurrency": proposal.currency,
        "loanAmount": (
            float(app.amount_approved or app.amount_requested)
            if (app.amount_approved or app.amount_requested) is not None else None
        ),
        "loanCurrency": app.currency,
        "message": proposal.message,
        "createdAt": proposal.created_at.isoformat(),
        "decidedBySub": proposal.decided_by_sub or None,
        "decidedAt": proposal.decided_at.isoformat() if proposal.decided_at else None,
        "decisionComment": proposal.decision_comment,
        "refusalReasonCode": proposal.refusal_reason_code or None,
        "guaranteeId": garantie.pk if garantie is not None else None,
        "guaranteeStatus": garantie.status if garantie is not None else None,
        "consentExpiresAt": (
            garantie.consent_expires_at.isoformat()
            if garantie is not None and garantie.consent_expires_at else None
        ),
        **(_capacity_block(proposal) if with_capacity else {}),
    }


def _capacity_block(proposal) -> dict[str, Any]:
    """Diagnostic de capacité du garant — STAFF UNIQUEMENT.

    Rejoue les sept contrôles sans rien écrire, et renvoie la première règle qui
    refuserait la désignation, avec son code. L'agent sait donc avant de cliquer
    si sa validation passera, et pourquoi elle ne passerait pas — ce que le
    demandeur, lui, n'apprendra jamais.
    """
    from credits.guarantor import GuarantorError, assert_can_guarantee, capacity_snapshot

    blocage = None
    try:
        assert_can_guarantee(
            proposal.application, proposal.guarantor, proposal.covered_amount,
        )
    except GuarantorError as exc:
        blocage = {"code": exc.code, "message": str(exc)}

    snapshot = capacity_snapshot(proposal.guarantor)
    return {
        "capacity": {
            "savings": str(snapshot["savings"]),
            "ceiling": str(snapshot["ceiling"]),
            "committed": str(snapshot["committed"]),
            "livePledges": snapshot["livePledges"],
            "maxPledges": snapshot["maxPledges"],
            "computedAt": snapshot["computedAt"],
        },
        "blockingRule": blocage,
    }
