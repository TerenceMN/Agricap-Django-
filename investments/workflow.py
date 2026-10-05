"""
Machine à états du cycle d'investissement P01→P13 (Annexe C du cahier HAZINA).

Trois règles gouvernent ce fichier :

1. **Aucune transition sautée.** Le graphe ci-dessous est exhaustif : ce qui n'y est
   pas est refusé (409). Un projet ne passe pas de la due diligence au décaissement
   parce qu'un écran l'a demandé.
2. **Chaque transition porte un acteur, un horodatage et un motif**, journalisés en
   append-only — dans `audit.services.record` (journal transverse de l'auditeur) ET
   dans `ProjectTransition` (historique interrogeable du dossier). La journalisation
   vit DANS la transaction : si elle échoue, la transition est annulée avec elle
   (même parti pris que `credits.workflow._audit_transition`).
3. **Chaque transition a sa garde métier**, celle de la colonne « transition
   autorisée si… » de l'Annexe C. Une garde qui échoue est un refus argumenté
   (`code` + message), jamais un silence.

Le retour arrière n'existe que sur P06→P05 (suspension motivée d'une levée). Tout le
reste avance ou sort par P12 (défaut) / P13 (annulation, avant P08 uniquement).

Les transitions à effet monétaire (P07 clôture, P08 décaissement, P11 clôture réussie)
ne s'appellent pas directement : elles sont pilotées par `investments/funding.py`, qui
compose l'effet financier ET la transition dans une seule transaction atomique.
"""
from __future__ import annotations

from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from common.exceptions import BusinessError

from .models import Project, ProjectTransition, Subscription

S = Project.Status


# ── Exceptions ────────────────────────────────────────────────────────────────

class WorkflowError(BusinessError):
    """Refus d'une transition. `code` consommé par le front, jamais le message."""

    code = "investment_workflow_error"
    http_status = 422


class InvalidTransition(WorkflowError):
    """Le statut courant n'autorise pas cette transition (transition sautée ou retour
    arrière interdit)."""

    code = "INVALID_TRANSITION"
    http_status = 409


class TransitionGuardFailed(WorkflowError):
    """La transition existe dans le graphe mais sa condition métier n'est pas remplie."""

    code = "TRANSITION_GUARD_FAILED"
    http_status = 422


class ReasonRequired(WorkflowError):
    """Motif absent — une transition non motivée ne se reconstitue pas."""

    code = "TRANSITION_REASON_REQUIRED"
    http_status = 422


# ── Graphe ────────────────────────────────────────────────────────────────────

#: Transitions autorisées. P12 (défaut) n'est atteignable qu'une fois l'argent sorti ;
#: P13 (annulation) qu'avant le décaissement — Annexe C.
#:
#: TROIS ÉTAPES RETIRÉES (décision du fondateur, août 2026) : « Analyse initiale »
#: (P02), « Due diligence » (P03) et « Comité d'investissement » (P04). Le module
#: d'investissement ne score plus et ne fait plus approuver par un comité : le
#: projet publie une PIÈCE JOINTE, et l'investisseur décide en souscrivant — ou
#: non. Souscrire EST son approbation ; il n'y a plus d'instance intermédiaire.
#:
#: Les codes des étapes conservées ne bougent PAS. Renuméroter P05→P02 aurait
#: obligé à réécrire le statut de tous les projets vivants, alors que le journal
#: d'audit et les événements comptables déjà émis continueraient de référencer les
#: anciens codes : deux vocabulaires pour un même état, dont l'un illisible.
ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    S.P01: {S.P05, S.P13},
    S.P05: {S.P06, S.P13},
    S.P06: {S.P07, S.P05, S.P13},   # P06→P05 : unique retour arrière du cycle
    S.P07: {S.P08, S.P13},
    S.P08: {S.P09, S.P12},
    S.P09: {S.P10, S.P12},
    S.P10: {S.P11, S.P12},
    S.P11: set(),
    S.P12: set(),
    S.P13: set(),
}

TERMINAL_STATUSES = frozenset({S.P11, S.P12, S.P13})


def allowed_targets(status: str) -> set[str]:
    return set(ALLOWED_TRANSITIONS.get(status, set()))


# ── Gardes (Annexe C, colonne « transition autorisée si… ») ───────────────────

def _fail(message: str) -> None:
    raise TransitionGuardFailed(message)


def _guard_p05(project: Project, **_) -> None:
    """UNE PIÈCE JOINTE — ce que l'investisseur lira pour décider. Rien d'autre.

    Cette étape exigeait le procès-verbal signé d'un comité d'investissement. Le
    comité a été retiré : il n'y a plus d'instance qui approuve à la place de celui
    qui met l'argent. Souscrire EST la décision de l'investisseur.

    **Une seule condition, et c'est délibéré.** La complétude du dossier promoteur
    (promoteur, description, filière) venait de la garde P02, supprimée avec
    l'étape ; elle a d'abord été repliée ici « pour ne pas la perdre ». C'était une
    erreur : le formulaire de création ne collecte ni description ni objectifs, si
    bien qu'un projet créé par l'application était bloqué à la première transition
    et ne pouvait avancer qu'en passant par l'écran de modification. Une garde qui
    barre le chemin normal n'est pas un contrôle de qualité, c'est une impasse.

    Ces champs restent modifiables et servent l'investisseur ; ils ne commandent
    plus la transition. Publier un projet sans document, en revanche, revient à
    demander de financer une ligne de titre.
    """
    from .models import AnalysisAttachment

    if not AnalysisAttachment.objects.filter(project=project).exists():
        _fail(
            "Aucune pièce n'est jointe à ce projet : l'investisseur n'aurait rien à "
            "lire pour décider. Déposez le dossier du projet avant de l'approuver."
        )


#: P06 n'a plus de garde (décision du fondateur, août 2026).
#:
#: Elle exigeait une offre OUVERTE : « la levée de fonds ne peut pas démarrer sans
#: offre publiée ». L'ordre imposé — créer l'offre, puis passer en levée — ne
#: correspondait pas à la façon dont le back-office travaille, et le refus arrivait
#: après coup, sur un projet déjà approuvé.
#:
#: CE QUE CELA CHANGE, et il faut le savoir : un projet peut désormais afficher
#: « Levée de fonds » sans aucune offre. L'investisseur voit une levée en cours et
#: n'a rien à souscrire. Ce n'est pas rattrapé ailleurs — `funding.reserve` refuse
#: une souscription sans offre, mais c'est un refus au moment de souscrire, pas un
#: écran qui prévient. L'ordre « offre d'abord » reste donc le bon usage ; il n'est
#: simplement plus imposé par la machine à états.


def _guard_p06_back(project: Project, **_) -> None:
    """P06 → P05 : suspension motivée d'une levée.

    Refusée dès qu'un franc a été encaissé : suspendre une levée déjà encaissée
    laisserait de l'argent d'investisseurs cantonné sur un projet suspendu sans
    décision de remboursement. Le chemin correct est alors P07 puis P13.
    """
    if project.funded_amount > Decimal("0"):
        _fail(
            "Des souscriptions ont déjà été encaissées sur ce projet : la levée ne "
            "peut plus être suspendue vers P05. Clôturez la souscription (P07) puis "
            "annulez avec remboursement (P13) si nécessaire."
        )


def min_funding_floor(project: Project) -> Decimal:
    """Plancher de min-funding du projet = Σ des planchers de ses offres."""
    total = sum((Decimal(o.min_funding_amount) for o in project.offers.all()), Decimal("0"))
    return total.quantize(Decimal("0.01"))


def committed_amount(project: Project) -> Decimal:
    """Σ des montants ALLOUÉS aux souscriptions vivantes — les engagements fermes.

    Distinct de `funded_amount` (l'argent reçu) : à la clôture on juge l'adhésion des
    investisseurs (engagements), au décaissement on juge la trésorerie (encaissements).
    """
    total = sum(
        (Decimal(s.allocated_amount) for s in Subscription.objects.filter(
            offer__project=project, status__in=Subscription.LIVE_STATUSES)),
        Decimal("0"),
    )
    return total.quantize(Decimal("0.01"))


def _guard_p07(project: Project, **_) -> None:
    """Souscription clôturée : toutes les offres fermées et min-funding atteint.

    Le min-funding se juge sur les ENGAGEMENTS à l'échéance : c'est le test « la levée
    a-t-elle rencontré son marché ». Le test « l'argent est-il arrivé » vient au
    décaissement (P08).
    """
    from .models import Offer

    ouvertes = project.offers.exclude(status=Offer.Status.CLOTURE).count()
    if ouvertes:
        _fail(f"{ouvertes} offre(s) encore ouverte(s) : clôturez la souscription d'abord.")
    plancher = min_funding_floor(project)
    engage = committed_amount(project)
    if engage < plancher:
        _fail(
            f"Min-funding non atteint : {engage} engagé pour un plancher de {plancher}. "
            "Les souscripteurs doivent être remboursés et le projet annulé (P13)."
        )


def _guard_p08(project: Project, **_) -> None:
    """Décaissement : la levée est clôturée ET l'argent est réellement encaissé."""
    if project.funded_amount <= Decimal("0"):
        _fail(
            "Aucune souscription encaissée : une réservation ne finance rien, "
            "le décaissement est impossible."
        )
    plancher = min_funding_floor(project)
    if project.funded_amount < plancher:
        _fail(
            f"Seuls {project.funded_amount} ont été encaissés pour un plancher de "
            f"{plancher} : les engagements n'ont pas été honorés, le décaissement "
            "est impossible."
        )


def _guard_p09(project: Project, **_) -> None:
    """Fonds reçus par le promoteur."""
    if project.disbursed_amount <= Decimal("0"):
        _fail("Le décaissement vers le promoteur n'a pas été enregistré.")


def _guard_p10(project: Project, **_) -> None:
    """Échéancier de retour en cours."""
    from .models import RepaymentSchedule

    if not RepaymentSchedule.objects.filter(offer__project=project).exists():
        _fail("Aucun échéancier de retour n'est enregistré sur les offres du projet.")


def _guard_p11(project: Project, **_) -> None:
    """Capital + rendement distribués : le cantonnement 419-OFF doit être soldé."""
    if project.distributed_amount <= Decimal("0"):
        _fail("Aucune distribution n'a été versée aux investisseurs.")
    solde = segregated_balance(project)
    if solde != Decimal("0.00"):
        _fail(
            f"Le cantonnement de ce projet n'est pas soldé (solde {solde}). "
            "Un projet ne se clôture pas avec de l'argent d'investisseurs encore cantonné."
        )


def _guard_p12(project: Project, **_) -> None:
    """Défaut — constaté, jamais deviné : le motif suffit, mais l'argent doit être sorti."""
    if project.disbursed_amount <= Decimal("0"):
        _fail(
            "Aucun fonds décaissé sur ce projet : il n'y a rien qui puisse être en "
            "défaut. Utilisez l'annulation (P13)."
        )


def _guard_p13(project: Project, **_) -> None:
    """Annulation : avant P08 uniquement, et toute souscription encaissée remboursée."""
    reste = (
        Subscription.objects.filter(offer__project=project,
                                     status__in=Subscription.FUNDED_STATUSES)
        .exclude(settled_amount=Decimal("0"))
        .count()
    )
    if reste:
        _fail(
            f"{reste} souscription(s) encaissée(s) ne sont pas remboursées : "
            "l'annulation d'un projet suppose la contrepassation des encaissements."
        )


GUARDS = {
    S.P05: _guard_p05,
    S.P07: _guard_p07,
    S.P08: _guard_p08,
    S.P09: _guard_p09,
    S.P10: _guard_p10,
    S.P11: _guard_p11,
    S.P12: _guard_p12,
    S.P13: _guard_p13,
}


def segregated_balance(project: Project) -> Decimal:
    """Solde du cantonnement 419-OFF du projet, vu depuis les événements métier.

    `encaissé (B10) − décaissé (B11) + retours (B12) − distribué (B13)`. C'est une
    LECTURE de l'état applicatif, à réconcilier avec le grand livre par la
    comptabilité — pas un substitut à celui-ci (principe 11 du cahier HAZINA).
    """
    return (
        Decimal(project.funded_amount)
        - Decimal(project.disbursed_amount)
        + Decimal(project.returned_amount)
        - Decimal(project.distributed_amount)
    ).quantize(Decimal("0.01"))


# ── Journalisation (miroir de `credits.workflow._audit_transition`) ───────────

def _audit_transition(project: Project, *, actor: str, action: str, etape: str, **details) -> None:
    """Trace append-only d'une transition dans le journal transverse de l'auditeur.

    Volontairement NON best-effort et à l'intérieur de la transaction : une
    transition d'investissement non journalisée ne se reconstitue pas.
    `entity_id = project.code` — la référence humaine, filtrable directement dans
    `GET /api/audit/entries?entity_type=Project&entity_id=PRJ-…`.
    """
    from audit.services import record

    payload = {"projectCode": project.code, "etape": etape, "statut": project.status}
    payload.update({k: v for k, v in details.items() if v not in (None, "")})
    record(
        actor=actor or "",
        action=action,
        entity_type="Project",
        entity_id=project.code,
        details=payload,
    )


# ── Transition ────────────────────────────────────────────────────────────────

@transaction.atomic
def transition(project: Project, *, to_status: str, actor_sub: str, reason: str,
               actor_role: str = "", details: dict | None = None,
               skip_guard: bool = False) -> Project:
    """Fait passer `project` à `to_status`. Point d'entrée UNIQUE du changement de statut.

    Aucune vue, aucun autre service n'écrit `project.status` directement — c'est la
    règle §5 de `CONVENTIONS.md`, et l'anomalie `portfolio /action` est ici le contre-exemple
    à ne pas reproduire.

    `skip_guard` n'est ouvert qu'aux services de `funding.py` qui viennent d'accomplir
    eux-mêmes l'effet que la garde vérifierait (ex. le décaissement B11, passé dans la
    même transaction juste avant la transition P07→P08). Il n'est jamais exposé par
    l'API.
    """
    reason = (reason or "").strip()
    if not reason:
        raise ReasonRequired(
            "Chaque transition du cycle d'investissement exige un motif : "
            "un statut qui change sans raison écrite n'est pas auditable."
        )

    from_status = project.status
    if to_status not in ALLOWED_TRANSITIONS:
        raise InvalidTransition(f"Statut cible inconnu : « {to_status} ».")
    if to_status == from_status:
        raise InvalidTransition(f"Le projet est déjà en {from_status}.")
    if to_status not in ALLOWED_TRANSITIONS.get(from_status, set()):
        libelles = ", ".join(sorted(ALLOWED_TRANSITIONS.get(from_status, set()))) or "aucune"
        raise InvalidTransition(
            f"Transition {from_status} → {to_status} non autorisée : depuis "
            f"{from_status}, les seules suites possibles sont {libelles}."
        )

    if not skip_guard:
        guard = GUARDS.get(to_status)
        if guard is not None:
            # P06→P05 a sa propre garde : la garde d'entrée en P05 (dossier complet
            # et pièce jointe) a déjà été satisfaite une fois, ce n'est pas elle
            # qu'on re-teste — c'est l'absence d'encaissement.
            if from_status == S.P06 and to_status == S.P05:
                _guard_p06_back(project)
            else:
                guard(project)

    now = timezone.now()
    project.status = to_status
    touched = ["status", "updated_at"]
    if to_status == S.P12:
        project.defaulted_at = now
        touched.append("defaulted_at")
    if to_status == S.P13:
        project.cancelled_at = now
        touched.append("cancelled_at")
    project.save(update_fields=touched)

    ProjectTransition.objects.create(
        project=project, from_status=from_status, to_status=to_status,
        actor_sub=actor_sub or "", actor_role=actor_role or "", reason=reason,
        details=details or {},
    )
    _audit_transition(
        project, actor=actor_sub, action="investments.project.transition",
        etape=f"{from_status}->{to_status}", fromStatus=from_status, toStatus=to_status,
        motif=reason, role=actor_role or None, **(details or {}),
    )

    if to_status == S.P12:
        _on_default(project, actor_sub=actor_sub, reason=reason)

    return project


def _on_default(project: Project, *, actor_sub: str, reason: str) -> None:
    """P12 — le risque se montre le jour où il naît.

    Les souscriptions encaissées passent en DEFAULTED (le dashboard de leurs
    investisseurs change le jour même) et un événement est produit pour que la
    comptabilité constitue sa provision. Aucune écriture n'est passée ici.
    """
    from .models import InvestmentEvent

    Subscription.objects.filter(
        offer__project=project, status__in=Subscription.FUNDED_STATUSES,
    ).update(status=Subscription.Status.DEFAULTED, updated_at=timezone.now())

    InvestmentEvent.objects.create(
        event_type=InvestmentEvent.Type.PROJECT_DEFAULTED,
        project=project,
        amount=Decimal(project.disbursed_amount) - Decimal(project.returned_amount),
        occurred_at=timezone.now(),
        actor_sub=actor_sub or "",
        payload={"reason": reason, "projectCode": project.code},
    )
