"""Services investissements — façade du module.

Le mouvement d'argent réel est TOUJOURS délégué à `transactions`/`caisses` (référencé en
FK sur `Movement.transaction`) ; cette app ne détient jamais elle-même le solde. Elle
orchestre le cycle et produit les événements que la comptabilité consomme.

Répartition des responsabilités depuis le lot « cycle de vie » :

- `workflow.py` : machine à états P01→P13, gardes, journal des transitions ;
- `funding.py` : réservation, encaissement, clôture, décaissement, retours, distributions ;
- `metrics.py` : XIRR, défaut, concentration, score de santé ;
- ce fichier : création des entités et opérations sans effet monétaire.
"""
from __future__ import annotations

import logging
from decimal import ROUND_HALF_UP, Decimal

from django.db import IntegrityError, transaction
from django.db.models import Sum
from django.utils import timezone

from audit.services import record as audit_record
from common.exceptions import ConflictError, NotFoundError, ValidationFailed
from common.money import arrondir
from common.parsing import to_date, to_decimal, to_int

from . import funding, project_code, workflow
from .models import (
    AnalysisAttachment, AnalystObservation, InvestmentConfig, Investor, Offer,
    PerformanceReport, Project, SubPortfolio, Subscription, TargetAllocation,
)

logger = logging.getLogger(__name__)

#: Ré-export : le graphe canonique vit dans `workflow.py`, un seul endroit.
ALLOWED_TRANSITIONS = workflow.ALLOWED_TRANSITIONS

#: Ré-exports des services monétaires — les appelants historiques n'ont pas à savoir
#: dans quel module la mécanique a été rangée.
reserve = funding.reserve
settle = funding.settle
cancel_reservation = funding.cancel_reservation
close_offer = funding.close_offer
close_fundraising = funding.close_fundraising
disburse = funding.disburse
record_return = funding.record_return
distribute = funding.distribute
refund_project = funding.refund_project
cancel_project = funding.cancel_project


#: Nombre de tentatives d'insertion quand le code est GÉNÉRÉ. Deux postes qui créent
#: un projet de la même province à la même seconde calculent le même numéro ; le
#: perdant recalcule et repart. Cinq essais couvrent très largement la concurrence
#: réelle d'un back-office, et l'échec au-delà est une erreur explicite plutôt qu'une
#: boucle sans fin.
TENTATIVES_CODE = 5


@transaction.atomic
def create_project(*, code: str = "", title: str, sector: str = "", location: str = "",
                    funding_target: Decimal | str = "0", promoter: str = "", manager_sub: str = "",
                    credit_application_code: str = "", pieces=None, by: str = "") -> Project:
    """Crée un projet d'investissement.

    `credit_application_code` est le chemin NORMAL depuis la décision « 1 projet =
    1 demande de crédit » : le dossier apporte le promoteur, la filière, la zone, le
    montant demandé et le score, et le projet n'a plus qu'à porter la levée. La
    création sans dossier reste possible pour la reprise de l'existant.

    **`code` est facultatif et le reste** : sans lui, il est GÉNÉRÉ au format
    `AG-CD-KIN-01` depuis la localisation (`investments/project_code.py`). Un code
    explicite demeure accepté — c'est ce qui permet de reprendre un historique déjà
    codé sans réécrire des références que des tiers ont déjà notées (principe 3).

    `pieces` est une liste de `(nature, fichier, libellé)` — les documents du dossier,
    déposés dans le même formulaire que le projet. Une pièce refusée (extension,
    taille) fait échouer la création entière — la transaction est atomique, et créer
    un projet en perdant en silence les pièces que l'utilisateur vient de joindre
    serait pire qu'un refus.

    Elles ne pèsent PLUS sur la note. Elles la portaient au temps du moteur
    d'investissement, dont le critère « complétude du dossier » comptait leur
    présence ; ce moteur a été retiré et la note vient du module crédit, qui lit la
    feuille de besoins ingérée. Ces pièces sont ce qu'elles disent être : des
    justificatifs qu'un analyste ouvre, pas des points.
    """
    if not title:
        raise ValidationFailed("Titre du projet requis.")
    code = (code or "").strip()
    if code and Project.objects.filter(code=code).exists():
        raise ValidationFailed(f"Le code projet « {code} » existe déjà.")

    champs = dict(
        title=title, sector=sector, location=location,
        funding_target=to_decimal(funding_target), promoter=promoter,
        manager_sub=manager_sub, created_by=by,
    )
    project = None
    for _ in range(1 if code else TENTATIVES_CODE):
        candidat = code or project_code.generer(localisation=location)
        try:
            # Sous-transaction : une violation d'unicité casse la transaction
            # courante en PostgreSQL, et sans ce point de reprise la tentative
            # suivante échouerait sur une transaction déjà avortée.
            with transaction.atomic():
                project = Project.objects.create(code=candidat, **champs)
            break
        except IntegrityError:
            if code:
                raise ValidationFailed(f"Le code projet « {code} » existe déjà.")
    if project is None:
        raise ConflictError(
            "Impossible d'attribuer un code à ce projet : plusieurs créations "
            "simultanées sur la même province. Réessayez.",
            code="PROJECT_CODE_CONTENTION",
        )
    audit_record(actor=by, action="investments.project.create", entity_type="Project", entity_id=project.code)
    if credit_application_code:
        project = link_credit_application(project=project,
                                           application_code=credit_application_code, by=by)
    # Les pièces du dossier sont jointes AVANT l'analyse : le moteur compte leur
    # présence dans la complétude, et les joindre après donnerait une première note
    # qui ignore des documents pourtant déposés dans le même formulaire.
    for nature, fichier, libelle in (pieces or []):
        joindre_piece_analyse(project=project, kind=nature, fichier=fichier,
                              label=libelle, by=by)
    return project


#: Statuts pendant lesquels un dossier de crédit peut encore être rattaché : tant que
#: la levée n'est pas ouverte.
LINKABLE_STATUSES = (Project.Status.P01, Project.Status.P05)


@transaction.atomic
def link_credit_application(*, project: Project, application_code: str = "",
                             application=None, by: str = "") -> Project:
    """Rattache un dossier de crédit à un projet — un dossier, un projet.

    L'unicité est portée par la base (`OneToOneField`) ; elle est aussi vérifiée ici
    pour rendre un refus lisible (« déjà financé par PRJ-… ») plutôt qu'une violation
    d'intégrité. Le montant demandé du dossier devient l'objectif de levée : c'est le
    seul champ recopié, parce que c'est celui sur lequel portent les gardes de
    min-funding et de décaissement (les autres sont lus à la volée, cf. `Project`).
    """
    from credits.models import CreditApplication

    if application is None:
        application = CreditApplication.objects.filter(code=application_code).first()
        if application is None:
            raise NotFoundError(f"Dossier de crédit introuvable : « {application_code} ».")

    if project.credit_application_id == application.pk:
        return project
    if project.credit_application_id:
        raise ConflictError(
            f"Le projet {project.code} finance déjà le dossier "
            f"{project.credit_application.code} : un projet ne change pas de dossier "
            "(créez un autre projet).",
            code="PROJECT_ALREADY_LINKED",
        )
    deja = Project.objects.filter(credit_application=application).exclude(pk=project.pk).first()
    if deja is not None:
        raise ConflictError(
            f"Le dossier {application.code} est déjà financé par le projet {deja.code} : "
            "deux projets ne peuvent pas financer la même demande de crédit.",
            code="CREDIT_APPLICATION_ALREADY_FINANCED",
        )
    if project.status not in LINKABLE_STATUSES:
        raise ConflictError(
            f"Le projet {project.code} est en {project.status} : le dossier de crédit "
            "se rattache avant l'ouverture de la levée (P06). Après, les investisseurs "
            "ont souscrit sur la foi du dossier publié.",
            code="LINK_AFTER_FUNDRAISING",
        )
    if application.amount_requested is None:
        raise ValidationFailed(
            f"Le dossier {application.code} ne porte pas de montant demandé : il n'y a "
            "pas d'objectif de levée à en dériver.",
            code="CREDIT_APPLICATION_WITHOUT_AMOUNT",
        )

    ancien_objectif = project.funding_target
    project.credit_application = application
    project.funding_target = to_decimal(application.amount_requested)
    project.save(update_fields=["credit_application", "funding_target", "updated_at"])
    audit_record(actor=by, action="investments.project.link_credit_application",
                 entity_type="Project", entity_id=project.code,
                 details={"application": application.code,
                          "previousFundingTarget": str(ancien_objectif),
                          "fundingTarget": str(project.funding_target)})
    return project


#: Champs du projet qui, une fois le dossier de crédit rattaché, ne se saisissent plus :
#: ils sont LUS dans le dossier (principe 6 — une seule source par concept).
DERIVED_FIELDS = {
    "title": None,  # aucun équivalent au dossier — voir `project_detail` (divergence assumée)
    "sector": "value_chain",
    "location": "agency",
    "promoter": "client",
    "promoterContact": "client",
    "fundingTarget": "amount_requested",
}


def identity_disclosed_to(project: Project, investor: Investor | None) -> bool:
    """L'identité de l'emprunteur est-elle lisible par CET investisseur ?

    Deux conditions cumulatives (décision du fondateur) :

    1. le projet est décaissé (P08 et au-delà) — avant, la levée est anonyme pour tous ;
    2. l'investisseur a une souscription **encaissée** sur ce projet — porter le rôle
       `invest` ne suffit jamais, et une réservation non encaissée non plus : une
       intention n'achète pas le droit de savoir qui l'on finance.
    """
    from .serializers import IDENTITY_DISCLOSED_STATUSES

    if investor is None or project.status not in IDENTITY_DISCLOSED_STATUSES:
        return False
    return Subscription.objects.filter(
        offer__project=project, investor=investor,
        status__in=Subscription.FUNDED_STATUSES, settled_amount__gt=Decimal("0"),
    ).exists()


def transition_status(*, project: Project, to_status: str, by: str = "", reason: str = "",
                       actor_role: str = "", details: dict | None = None) -> Project:
    """Façade de `workflow.transition` — point d'entrée unique du changement de statut.

    Aucun motif de complaisance n'est fabriqué ici : « Passage en P08 » n'explique rien.
    Un appelant qui ne fournit pas de motif reçoit `TRANSITION_REASON_REQUIRED`.
    """
    return workflow.transition(project, to_status=to_status, actor_sub=by, reason=reason,
                                actor_role=actor_role, details=details)


def set_expert_valuation(*, project: Project, amount, valuation_date, source: str = "",
                          by: str = "") -> Project:
    """Enregistre la valorisation d'expert d'un projet — Annexe D, titres de capital.

    Les trois éléments sont indissociables : une valeur sans date ni source n'est pas
    une expertise, c'est une opinion, et `metrics._expert_valuation()` la refusera. On
    refuse donc de l'enregistrer plutôt que de l'enregistrer pour la voir écartée en
    silence à l'affichage. Une expertise antidatée du futur n'existe pas non plus.

    Aucun écrasement muet : l'ancienne valeur part au journal d'audit, qui est
    append-only — la trajectoire des valorisations successives est elle-même une
    donnée (une action réévaluée trois fois en six mois se remarque).
    """
    montant = to_decimal(amount)
    if montant < Decimal("0"):
        raise ValidationFailed("Une valorisation d'expert ne peut pas être négative.")
    jour = to_date(valuation_date)
    if jour is None:
        raise ValidationFailed(
            "La date de la valorisation d'expert est obligatoire : une valeur sans date "
            "n'est pas une expertise."
        )
    # `localdate()` et NON `now().date()` : `timezone.now()` est en UTC, et sa
    # date est celle d'UTC. L'institution vit à Africa/Kinshasa (UTC+1), réglé
    # dans `settings.TIME_ZONE` précisément pour cela.
    #
    # DÉFAUT MESURÉ : entre 00:00 et 01:00 heure de Kinshasa, `now().date()`
    # rendait encore la veille. Un agent saisissant une expertise datée du JOUR
    # se voyait répondre « la date ne peut pas être future » — une heure par
    # nuit pendant laquelle le contrôle refusait le présent. Le reste du backend
    # emploie déjà `localdate()` (17 usages) : ces deux occurrences étaient les
    # exceptions, pas la règle.
    if jour > timezone.localdate():
        raise ValidationFailed("La date de la valorisation d'expert ne peut pas être future.")
    if not (source or "").strip():
        raise ValidationFailed(
            "La source de la valorisation d'expert est obligatoire (cabinet, rapport, "
            "référence) : une valorisation sans auteur n'est pas opposable."
        )
    ancien = {
        "amount": str(project.expert_valuation) if project.expert_valuation is not None else None,
        "date": project.expert_valuation_date.isoformat() if project.expert_valuation_date else None,
        "source": project.expert_valuation_source,
    }
    project.expert_valuation = montant
    project.expert_valuation_date = jour
    project.expert_valuation_source = source.strip()
    project.save(update_fields=["expert_valuation", "expert_valuation_date",
                                 "expert_valuation_source", "updated_at"])
    audit_record(actor=by, action="investments.project.expert_valuation", entity_type="Project",
                 entity_id=project.code,
                 details={"previous": ancien, "amount": str(montant), "date": jour.isoformat(),
                          "source": project.expert_valuation_source})
    return project


@transaction.atomic
def create_offer(*, project: Project, code: str, coupon_rate: Decimal | str, maturity_months: int,
                  min_ticket: Decimal | str, available_bonds: int, funding_goal: Decimal | str = "0",
                  min_funding_amount: Decimal | str | None = None, oversubscription_policy: str = "",
                  subscription_deadline=None, type_of_title: str = "", by: str = "") -> Offer:
    """Crée une offre. Le plancher de min-funding, s'il n'est pas donné, est dérivé du
    ratio paramétré en base — jamais d'un nombre écrit dans le code.

    `type_of_title` gouverne la MÉTHODE DE VALORISATION de toutes les positions issues
    de cette offre (`metrics._valuation`) : une obligation se valorise au pair avec ses
    intérêts courus, une action ou une part sociale par expertise datée. Il n'était pas
    exposé ici : aucune offre en titres de capital ne pouvait donc naître par l'API, et
    la branche « expertise » de la valorisation était inatteignable en production. Une
    offre créée sans précision reste une obligation, comme avant.
    """
    if Offer.objects.filter(code=code).exists():
        raise ValidationFailed(f"Le code offre « {code} » existe déjà.")
    titre = type_of_title or Offer.TypeOfTitle.OBLIGATION
    if titre not in Offer.TypeOfTitle.values:
        raise ValidationFailed(
            f"Type de titre inconnu : « {titre} ». Valeurs admises : "
            f"{', '.join(Offer.TypeOfTitle.values)}."
        )
    goal = to_decimal(funding_goal)
    cfg = InvestmentConfig.active()
    if min_funding_amount in (None, ""):
        plancher = (goal * Decimal(cfg.default_min_funding_ratio)).quantize(Decimal("0.01"))
    else:
        plancher = to_decimal(min_funding_amount)
    if plancher > goal:
        raise ValidationFailed(
            f"Le plancher de min-funding ({plancher}) ne peut pas dépasser l'objectif ({goal})."
        )
    politique = (oversubscription_policy or cfg.default_oversubscription_policy or
                 Offer.Oversubscription.QUEUE)
    if politique not in Offer.Oversubscription.values:
        raise ValidationFailed(f"Politique de sursouscription inconnue : « {politique} ».")
    offer = Offer.objects.create(
        project=project, code=code, type_of_title=titre,
        coupon_rate=to_decimal(coupon_rate), maturity_months=maturity_months,
        min_ticket=to_decimal(min_ticket), available_bonds=available_bonds, max_bonds=available_bonds,
        funding_goal=goal, min_funding_amount=plancher, oversubscription_policy=politique,
        subscription_deadline=to_date(subscription_deadline),
    )
    audit_record(actor=by, action="investments.offer.create", entity_type="Offer", entity_id=offer.code,
                 details={"fundingGoal": str(goal), "minFunding": str(plancher), "policy": politique,
                          "typeOfTitle": titre})
    return offer


def subscribe(*, investor: Investor, offer_id: int, bonds: int, idempotency_key: str,
              by: str = "") -> Subscription:
    """Alias historique de `funding.reserve` — une souscription RÉSERVE, elle n'encaisse pas."""
    return funding.reserve(investor=investor, offer_id=offer_id, bonds=bonds,
                            idempotency_key=idempotency_key, by=by)


def _montant(value) -> Decimal:
    """Montant du reporting promoteur, quantizé à 0,01 / `ROUND_HALF_UP` — jamais un `float`."""
    return to_decimal(value or 0).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _quantite(value) -> Decimal:
    """Quantité produite, quantizée à 0,001 (tonnes, litres… — pas un montant)."""
    return to_decimal(value or 0).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)


def _deviation_percent(actual, forecast) -> Decimal:
    """`(réalisé − prévu) / prévu × 100`, en `Decimal`, quantifié à 0,01.

    Sans prévision, il n'y a pas d'écart — pas un écart de 0 % : `0` signifierait
    « conforme à la prévision », ce qui est faux quand aucune prévision n'a été posée.
    On retourne 0 faute de champ nullable dans un modèle déjà consommé, mais
    `has_forecast` ci-dessous permet à l'appelant de distinguer les deux cas.
    """
    prevu = to_decimal(forecast or 0)
    if prevu == 0:
        return Decimal("0.00")
    return ((to_decimal(actual or 0) - prevu) / prevu * Decimal("100")).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP)


def submit_performance_report(*, project: Project, data: dict, by: str = "") -> PerformanceReport:
    """Enregistre le reporting du promoteur ET calcule SES écarts — les trois.

    L'écart de revenu était seul calculé côté serveur : les écrans recalculaient donc
    l'écart de coûts eux-mêmes, avec leur propre formule, pour une grandeur qui décide
    d'une observation de risque. Deux formules pour une grandeur, c'est l'incident de
    données du principe 11 en germe. Les trois écarts sont désormais calculés ici, une
    fois, et figés avec le rapport.

    Le seuil d'alerte vient de `InvestmentConfig` (principe 8), et le sens de chaque
    écart est respecté : des coûts SUPÉRIEURS à la prévision sont défavorables, des
    revenus supérieurs ne le sont pas.
    """
    cfg = InvestmentConfig.active()
    seuil = Decimal(cfg.performance_deviation_alert_percent)

    revenu = _deviation_percent(data.get("actualRevenue"), data.get("forecastRevenue"))
    couts = _deviation_percent(data.get("actualCosts"), data.get("forecastCosts"))
    production = _deviation_percent(data.get("actualProduction"), data.get("forecastProduction"))

    # Les montants sont écrits en `Decimal` : le `float(…)` qui les enveloppait
    # reprenait d'une main ce que `to_decimal` donnait de l'autre, et rendait
    # 1 000,10 relisible en 1 000,0999999999999 (principe 4).
    report = PerformanceReport.objects.create(
        project=project, reporting_period=data.get("reportingPeriod", ""),
        actual_revenue=_montant(data.get("actualRevenue")),
        forecast_revenue=_montant(data.get("forecastRevenue")),
        actual_costs=_montant(data.get("actualCosts")),
        forecast_costs=_montant(data.get("forecastCosts")),
        actual_production=_quantite(data.get("actualProduction")),
        forecast_production=_quantite(data.get("forecastProduction")),
        deviation_percent=revenu, cost_deviation_percent=couts,
        production_deviation_percent=production,
        deviation_comments=data.get("deviationComments", ""),
    )

    # Un écart de revenu ou de production s'apprécie en valeur absolue (une prévision
    # dépassée de 40 % est autant un signal sur la QUALITÉ de la prévision qu'un bon
    # résultat — CONVENTIONS.md §4.3) ; un écart de coûts ne se signale que s'il DÉPASSE.
    alertes = []
    if abs(revenu) > seuil:
        alertes.append(f"revenu {revenu:+} %")
    if couts > seuil:
        alertes.append(f"coûts {couts:+} % au-dessus de la prévision")
    if abs(production) > seuil:
        alertes.append(f"production {production:+} %")
    if alertes:
        AnalystObservation.objects.create(
            project=project, category=AnalystObservation.Category.RISK,
            risk_flag=AnalystObservation.RiskFlag.HIGH,
            observation=(
                f"Écart supérieur au seuil de {seuil} % sur le rapport de performance "
                f"{report.reporting_period or report.pk} : {' ; '.join(alertes)}."
            ),
        )
    audit_record(actor=by, action="investments.performance_report.submit", entity_type="PerformanceReport",
                 entity_id=str(report.pk),
                 details={"deviation_percent": str(revenu), "cost_deviation_percent": str(couts),
                          "production_deviation_percent": str(production), "threshold": str(seuil)})
    return report


@transaction.atomic
def investor_action(*, investor: Investor, action: str, by: str = "") -> Investor:
    """Suspend/réactive un investisseur (pas de suppression — même logique que
    `agencies.services.suspend`/`unlock_temporary`)."""
    if action == "suspend":
        investor.status = Investor.Status.SUSPENDED
    elif action == "activate":
        investor.status = Investor.Status.ACTIVE
    else:
        raise ValidationFailed(f"Action inconnue : {action}")
    investor.save(update_fields=["status", "updated_at"])
    audit_record(actor=by, action=f"investments.investor.{action}", entity_type="Investor",
                 entity_id=str(investor.pk))
    return investor


#: Types de titre logés dans la poche « stocks » — les titres de CAPITAL.
#: Une action et une obligation ne portent ni le même risque ni le même rang de
#: remboursement : les fondre dans une seule poche rendrait toute cible d'allocation
#: dépourvue de sens.
TITRES_DE_CAPITAL = (Offer.TypeOfTitle.ACTION, Offer.TypeOfTitle.PART_SOCIALE)


def _encaisse_par_poche(souscriptions) -> dict[str, Decimal]:
    """Ventile des souscriptions ENCAISSÉES entre les poches `bonds` et `stocks`.

    Le critère est le TYPE DE TITRE de l'offre, seule source qui sache ce qui a été
    souscrit — jamais le libellé du projet ni une convention d'écran.
    """
    totaux = {"bonds": Decimal("0"), "stocks": Decimal("0")}
    for sub in souscriptions:
        poche = "stocks" if sub.offer.type_of_title in TITRES_DE_CAPITAL else "bonds"
        totaux[poche] += Decimal(sub.settled_amount)
    return totaux


def portfolio_allocation(*, investor: Investor) -> dict:
    """Allocation du portefeuille — sur l'argent RÉELLEMENT placé.

    Une souscription seulement réservée ne pèse pas dans une allocation d'actifs :
    elle n'est pas un actif, elle est une intention. Seuls les montants encaissés
    comptent (`settled_amount` sur les souscriptions vivantes).
    """
    from caisses.models import ClientWallet

    from .models import ObligationPosition
    souscriptions = list(
        Subscription.objects.filter(investor=investor, status__in=Subscription.FUNDED_STATUSES)
        .select_related("offer")
    )
    ventile = _encaisse_par_poche(souscriptions)
    bonds_total = ventile["bonds"]
    obligations_total = ObligationPosition.objects.filter(investor=investor).aggregate(
        total=Sum("invested_amount"))["total"] or Decimal("0")
    cash_total = ClientWallet.objects.filter(user=investor.user).aggregate(total=Sum("balance"))["total"] or Decimal("0")
    positions_obligataires = ObligationPosition.objects.filter(investor=investor).count()
    return {
        "bonds": float(bonds_total + obligations_total),
        # `bonds` additionne DEUX grandeurs qui ne se valent pas, et le total seul le
        # cachait. À gauche, de l'argent reçu et comptabilisé (B10), rapprochable d'une
        # pièce. À droite, des positions obligataires dont le montant est saisi libre,
        # dont le coupon, le taux et la maturité viennent des défauts du modèle
        # (250 / 9 % / 24 mois) et qui ne sont rattachées à aucune offre — elles
        # n'ont jamais transité par un encaissement.
        #
        # Conséquence mesurable : `GET /metrics/mine` ne compte QUE les souscriptions
        # encaissées, donc le même investisseur lit deux « investi » différents sur
        # deux écrans — l'incident de données du principe 11. Tant que le produit
        # obligataire n'est pas raccroché au cycle des offres, la ventilation est le
        # minimum honnête : le total reste servi tel quel pour ne rien casser, mais
        # sa composition est désormais lisible.
        "bondsFromSubscriptions": float(bonds_total),
        "bondsFromObligationPositions": float(obligations_total),
        "obligationPositionsCount": positions_obligataires,
        "reconciliationWarning": (
            "Les positions obligataires sont incluses dans « bonds » mais absentes de "
            "`metrics/mine.totalInvested`, qui ne compte que les souscriptions "
            "encaissées : les deux écrans afficheront des montants différents pour cet "
            "investisseur tant que ce produit n'est pas rattaché au cycle des offres."
            if obligations_total else None
        ),
        "cash": float(cash_total),
        #: Titres de CAPITAL encaissés (action, part sociale). Cette poche valait 0 en
        #: dur — « aucun produit actions n'existe » — alors que `Offer.TypeOfTitle` en
        #: porte deux : une souscription d'action était donc comptée en obligations, et
        #: une cible d'allocation posée sur ces poches comparait des choses fausses.
        "stocks": float(ventile["stocks"]),
    }


# ── Allocation cible d'un sous-portefeuille ──────────────────────────────────────────


def _poids_valides(brut) -> dict:
    """Valide et normalise les poids d'une cible. Lève `ValidationFailed` sur écart.

    La somme est contrôlée à 100 CÔTÉ SERVEUR : une cible qui ne somme pas à 100 %
    n'est pas une cible, et un écran qui la contrôlerait seul laisserait passer tout
    appel direct à l'API.
    """
    from common.money import CENTIME
    from decimal import ROUND_HALF_UP, InvalidOperation

    if not isinstance(brut, dict) or not brut:
        raise ValidationFailed("Les poids de la cible sont requis.", code="TARGET_WEIGHTS_REQUIRED")

    inconnues = sorted(set(brut) - set(TargetAllocation.POCHES))
    if inconnues:
        raise ValidationFailed(
            f"Poche(s) inconnue(s) : {', '.join(inconnues)}. "
            f"Poches admises : {', '.join(TargetAllocation.POCHES)}.",
            code="TARGET_BUCKET_UNKNOWN",
        )

    poids = {}
    for poche in TargetAllocation.POCHES:
        try:
            valeur = Decimal(str(brut.get(poche, 0))).quantize(CENTIME, rounding=ROUND_HALF_UP)
        except (InvalidOperation, TypeError):
            raise ValidationFailed(f"Poids illisible pour « {poche} ».",
                                    code="TARGET_WEIGHT_INVALID") from None
        if valeur < 0:
            raise ValidationFailed(f"Le poids de « {poche} » ne peut pas être négatif.",
                                    code="TARGET_WEIGHT_NEGATIVE")
        poids[poche] = valeur

    total = sum(poids.values())
    if total != Decimal("100.00"):
        raise ValidationFailed(
            f"La somme des poches doit faire exactement 100 % — elle fait {total} %.",
            code="TARGET_SUM_INVALID",
        )
    return {k: str(v) for k, v in poids.items()}


def target_allocation_courante(*, sub_portfolio: SubPortfolio) -> dict | None:
    """Dernière version de la cible, ou `None` si aucune n'a jamais été posée."""
    cible = sub_portfolio.target_allocations.first()   # ordering = ["-version"]
    if cible is None:
        return None
    return {
        "subPortfolioId": sub_portfolio.pk,
        "version": cible.version,
        "weights": cible.weights,
        "note": cible.note,
        "createdBy": cible.created_by,
        "createdAt": cible.created_at.isoformat(),
    }


@transaction.atomic
def set_target_allocation(*, sub_portfolio: SubPortfolio, weights, note: str = "",
                          by: str = "") -> dict:
    """Pose une NOUVELLE version de la cible. L'ancienne reste lisible."""
    valides = _poids_valides(weights)
    derniere = sub_portfolio.target_allocations.select_for_update().first()
    version = (derniere.version if derniere else 0) + 1

    cible = TargetAllocation.objects.create(
        sub_portfolio=sub_portfolio, version=version, weights=valides,
        note=(note or "").strip(), created_by=by or "",
    )
    audit_record(actor=by, action="investments.target_allocation.set",
                 entity_type="SubPortfolio", entity_id=str(sub_portfolio.pk),
                 details={"version": version, "weights": valides, "note": cible.note})
    return target_allocation_courante(sub_portfolio=sub_portfolio)


#: Libellés des poches, servis avec la répartition. Le front n'invente aucun nom :
#: la nomenclature est celle du serveur (principe 6).
LIBELLES_POCHES = {
    "bonds": "Obligations",
    "cash": "Liquidités",
    "stocks": "Titres de capital",
}


def sub_portfolio_allocation(*, sub_portfolio: SubPortfolio) -> dict:
    """Répartition RÉELLE d'un sous-portefeuille, son écart à la cible et les écarts
    en montant qui fondent un ordre de rééquilibrage.

    Le périmètre est celui des souscriptions RATTACHÉES à cette poche
    (`Subscription.sub_portfolio`) et ENCAISSÉES : une réservation n'est pas un actif.

    **Les liquidités ne sont pas partitionnables.** Un portefeuille client est un
    solde unique ; l'attribuer à une poche plutôt qu'à une autre serait un choix
    inventé, et le répartir au prorata ferait apparaître le même argent dans
    plusieurs poches. La poche « cash » d'un sous-portefeuille vaut donc zéro, elle
    le DIT (`attributable`), et le solde réel est servi à part
    (`availableCash`) comme ce qu'il est : la capacité de financement d'un ordre,
    pas un actif de la poche.
    """
    souscriptions = list(
        Subscription.objects.filter(sub_portfolio=sub_portfolio,
                                     status__in=Subscription.FUNDED_STATUSES)
        .select_related("offer")
    )
    ventile = _encaisse_par_poche(souscriptions)
    cible = target_allocation_courante(sub_portfolio=sub_portfolio)

    montants = {"bonds": ventile["bonds"], "cash": Decimal("0"), "stocks": ventile["stocks"]}
    base = sum(montants.values(), Decimal("0"))

    parts = {}
    for poche in TargetAllocation.POCHES:
        montant = montants[poche]
        part = ((montant / base * Decimal("100")).quantize(Decimal("0.1"))
                if base else None)
        objectif = Decimal(cible["weights"][poche]) if cible else None
        #: **UN SEUL SENS POUR LES DEUX ÉCARTS** : `cible − actuel`, donc un écart
        #: POSITIF désigne une poche à renforcer. Servir les points dans un sens et
        #: les montants dans l'autre — ce qui était le cas — donne un écran où la
        #: colonne « écart » affiche « −50 % · +4 000 USD » sur la même ligne, et un
        #: ordre passé à l'envers ne se voit qu'après exécution.
        ecart_points = (objectif - part) if (part is not None and objectif is not None) else None
        # L'écart en MONTANT est ce qui rend l'ordre exécutable : « 12,3 points » ne
        # se souscrit pas, « 1 230 USD à placer en obligations » se souscrit.
        ecart_montant = (arrondir(base * objectif / Decimal("100") - montant)[0]
                          if objectif is not None else None)
        #: **La cible en MONTANT, et sa base déclarée avec elle.**
        #:
        #: Un pourcentage cible ne se lit pas : « 30 % » ne dit pas combien placer.
        #: Sa contrepartie en argent est un calcul métier, donc SERVEUR — jamais
        #: `total × cible / 100` dans un navigateur, qui produirait un second
        #: chiffre pour la même grandeur.
        #:
        #: La base retenue est l'ENCAISSÉ RATTACHÉ À CETTE POCHE (`basisAmount`),
        #: la même que celle des parts actuelles : une cible se lit contre ce
        #: qu'elle répartit. Le solde disponible du portefeuille n'y entre PAS —
        #: il n'appartient à aucune poche (`availableCash` le sert à part), et
        #: l'y inclure ferait apparaître la même somme dans chaque poche de
        #: chaque sous-portefeuille.
        #:
        #: Base nulle → `None` et son motif. « Pas de pourcentage sans base » vaut
        #: dans les deux sens : 30 % d'une poche vide font 0 USD, et afficher ce 0
        #: laisserait croire que la cible est atteinte alors qu'il n'y a rien à
        #: répartir.
        montant_cible = (arrondir(base * objectif / Decimal("100"))[0]
                          if (objectif is not None and base > 0) else None)
        parts[poche] = {
            "bucket": poche,
            "label": LIBELLES_POCHES[poche],
            "amount": float(montant),
            # Pas de pourcentage sans base (§4.6) : un portefeuille vide n'a pas
            # une répartition de 0 %, il n'en a pas.
            "actualPercent": float(part) if part is not None else None,
            "targetPercent": float(objectif) if objectif is not None else None,
            "targetAmount": float(montant_cible) if montant_cible is not None else None,
            "targetAmountUnavailableReason": (
                None if montant_cible is not None else
                "Cette poche ne porte aucune position encaissée : une cible en "
                "pourcentage n'a pas encore de contrepartie en argent."
                if objectif is not None else
                "Aucune allocation cible n'est fixée."
            ),
            "gapPercent": float(ecart_points) if ecart_points is not None else None,
            "gapAmount": float(ecart_montant) if ecart_montant is not None else None,
            "attributable": poche != "cash",
        }

    disponible = _solde_disponible(sub_portfolio.investor)
    positions = [
        {
            "subscriptionId": s.pk, "offerId": s.offer_id, "offerCode": s.offer.code,
            "typeOfTitle": s.offer.type_of_title,
            "bucket": "stocks" if s.offer.type_of_title in TITRES_DE_CAPITAL else "bonds",
            "settledAmount": float(s.settled_amount), "status": s.status,
        }
        for s in souscriptions
    ]
    return {
        "subPortfolioId": sub_portfolio.pk,
        "subPortfolioName": sub_portfolio.name,
        "scope": "subPortfolio",
        "basisAmount": float(base),
        #: Base des parts ET des montants cibles — annoncée, jamais devinée : un
        #: montant cible de 300 USD sur une poche à 0 USD ne veut rien dire pour
        #: qui ignore ce qu'on a divisé.
        "targetBasis": "Encaissé rattaché à cette poche.",
        "targetBasisAmount": float(base),
        "buckets": parts,
        "bucketOrder": list(TargetAllocation.POCHES),
        "positionsCount": len(souscriptions),
        "positions": positions,
        "availableCash": float(disponible),
        "target": cible,
        #: Un ordre ne s'instruit que contre une cible : sans cible, il n'y a pas
        #: d'écart, donc rien à corriger.
        "rebalancingAvailable": bool(cible),
        "rebalancingReason": (
            "Un ordre de rééquilibrage se réserve puis s'encaisse comme une "
            "souscription : contrôle de solde, réservation, encaissement, journal."
            if cible else
            "Fixez d'abord une allocation cible : sans elle, aucun écart n'est mesurable."
        ),
    }


def _solde_disponible(investor: Investor) -> Decimal:
    """Solde des portefeuilles de l'investisseur — la capacité de financement réelle."""
    from caisses.models import ClientWallet

    total = ClientWallet.objects.filter(user=investor.user).aggregate(
        total=Sum("balance"))["total"]
    return Decimal(total or 0)


# ── Saisie des analyses technique et financière ──────────────────────────────────────
#
# Ces analyses conditionnent l'entrée en comité (`_guard_p04`) et n'avaient AUCUN
# chemin de création dans l'application : les modèles existaient, les endpoints de
# lecture aussi, mais rien ne permettait de les écrire. Aucun projet ne pouvait donc
# atteindre P04 autrement qu'en insérant les lignes à la main.

#: Extensions acceptées pour une pièce d'analyse. Liste blanche, jamais une liste
#: noire : ce qui n'est pas explicitement autorisé est refusé.
EXTENSIONS_PIECES = (".pdf", ".xlsx", ".csv", ".png", ".jpg", ".jpeg")
TAILLE_MAX_PIECE = 5 * 1024 * 1024   # 5 Mo, même plafond que la feuille de besoins


def _nombre(valeur, defaut="0"):
    return to_decimal(valeur, default=defaut)


def joindre_piece_analyse(*, project: Project, kind: str, fichier, label: str = "",
                          by: str = "") -> AnalysisAttachment:
    """Attache une pièce à une analyse. Extension en liste blanche, taille plafonnée."""
    if kind not in AnalysisAttachment.Kind.values:
        raise ValidationFailed(f"Nature d'analyse inconnue : « {kind} ».",
                                code="ANALYSIS_KIND_UNKNOWN")
    if fichier is None:
        raise ValidationFailed("Aucun fichier reçu.", code="FILE_REQUIRED")
    nom = (getattr(fichier, "name", "") or "").lower()
    if not nom.endswith(EXTENSIONS_PIECES):
        raise ValidationFailed(
            f"Extension refusée. Formats acceptés : {', '.join(EXTENSIONS_PIECES)}.",
            code="FILE_EXTENSION_REFUSED")
    if getattr(fichier, "size", 0) > TAILLE_MAX_PIECE:
        raise ValidationFailed("Fichier trop volumineux (5 Mo maximum).",
                                code="FILE_TOO_LARGE")

    piece = AnalysisAttachment.objects.create(
        project=project, kind=kind, file=fichier,
        original_name=getattr(fichier, "name", "")[:255],
        label=(label or "").strip()[:200], uploaded_by=by or "",
    )
    audit_record(actor=by, action="investments.analysis.attachment.add", entity_type="Project",
                 entity_id=project.code,
                 details={"kind": kind, "fichier": piece.original_name})
    return piece
