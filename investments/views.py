"""API investissements — Project/Offer/Investor/Subscription/Movement + produits
obligataires. Alimente `AdminConsole.jsx`, `AdminInvestments.jsx`, `InvestorSpace.jsx`,
`Obligations.jsx`, `Conversions.jsx`, `Holdings.jsx`, `Opportunities.jsx`, `Portfolios.jsx`.

**Asymétrie d'information (§5.2 du cahier HAZINA).** Un investisseur voit SON argent et
les projets OUVERTS. Il ne voit ni les données des autres investisseurs, ni les dossiers
en due diligence (P01→P05) autrement qu'en pipeline agrégé anonymisé. La règle est
appliquée ici, dans le filtrage des vues — pas dans l'affichage : un front ne protège
rien.
"""
from __future__ import annotations

from decimal import Decimal

from django.db.models import Sum
from django.utils import timezone
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from accounts import directory
from compliance import kyc_levels
from compliance.models import KycProfile
from common import idempotency
from rbac.permissions import HasCapability
from rbac.role_registry import get_role

from . import benchmarks, esg, funding, metrics, \
    obligations as obligations_service, rebalancing, serializers, services, workflow
from accounts.permissions import IsStaff
from audit.services import record as audit_record
from common.exceptions import ConflictError, ValidationFailed
from common.money import est_au_centime
from common.parsing import to_date, to_decimal, to_int

from .models import (
    AnalystObservation, BondConversion, BondWithdrawal, Collateral, Distribution,
    InvestmentEvent, Investor, Movement, Offer, ObligationPosition,
    PerformanceReport, Project, ProjectQuestion, ProjectTransition,
    RepaymentSchedule,
    SecondaryMarketListing, Subscription, SubPortfolio, TargetAllocation,
)

#: Statuts d'un projet visibles par un investisseur : la levée ouverte et tout ce qui
#: suit (il a le droit de suivre un projet dans lequel il a mis de l'argent). Les
#: étapes P01→P05 relèvent de l'instruction interne.
PUBLIC_PROJECT_STATUSES = (
    Project.Status.P06, Project.Status.P07, Project.Status.P08, Project.Status.P09,
    Project.Status.P10, Project.Status.P11, Project.Status.P12,
)


def _my_investor(request):
    return Investor.objects.filter(user=request.user).first()


def _require(request, capability: str) -> bool:
    return bool(getattr(get_role(getattr(request.user, "role", "")), capability, False))


def _is_staff(request) -> bool:
    """Personnel de l'institution, par TYPE de rôle et non par capacité.

    Le rôle `invest` porte `create` (il crée ses souscriptions) : tester `create` pour
    distinguer le personnel du client donnerait l'instruction des dossiers aux
    investisseurs. Le type du rôle est le seul discriminant correct.
    """
    return getattr(get_role(getattr(request.user, "role", "")), "type", "Client") != "Client"


def _visible_projects(request):
    """Projets visibles par l'appelant — tout pour le personnel, P06+ pour un client."""
    qs = Project.objects.all()
    if _is_staff(request):
        return qs
    return qs.filter(status__in=PUBLIC_PROJECT_STATUSES)


def _visible_offers(request):
    """Offres visibles par l'appelant.

    Une offre est un satellite de son projet : si le dossier est en due diligence, son
    offre — objectif de levée, taux de coupon, garanties — l'est aussi. Sans ce
    filtre, `GET /offers` révélait à n'importe quel investisseur l'existence, le
    montage et le montant de chaque dossier P01→P05 (le rôle `invest` porte `read`).
    """
    qs = Offer.objects.filter(project__in=_visible_projects(request))
    if _is_staff(request):
        return qs
    #: Un brouillon d'offre n'est pas une offre : il n'est pas encore opposable.
    return qs.exclude(status__in=(Offer.Status.DRAFT, Offer.Status.PREPARATION))


def _structured_error(exc) -> Response:
    """Réponse 422 structurée `{code, message}` — un code par erreur, jamais un message
    générique (CONVENTIONS.md principe 5)."""
    return Response({"detail": str(exc), "code": exc.code, "message": str(exc),
                     "errors": exc.as_errors()}, status=exc.http_status)


def _erreur_metier(exc) -> Response:
    """Même contrat `{code, message}` pour une `BusinessError` NUE.

    `_structured_error` ci-dessus appelle `as_errors()`, que seules quelques
    sous-classes définissent (`funding.WalletDebitError`) : l'utiliser sur un
    `ValidationFailed` ordinaire lève une `AttributeError` et rend un 500 là où le
    service avait produit un refus lisible. Le refus de validation sort en **422**,
    comme partout ailleurs dans le module ; les conflits gardent leur 409.
    """
    statut = 422 if isinstance(exc, ValidationFailed) else getattr(exc, "http_status", 422)
    code = getattr(exc, "code", "business_error")
    message = getattr(exc, "message", str(exc))
    return Response({"detail": message, "code": code, "message": message,
                     "errors": [{"code": code, "message": message}]}, status=statut)


def _project_row_for(request, project: Project) -> dict:
    """Choisit le SÉRIALISEUR selon le rôle et le droit — jamais un `if` d'affichage.

    Trois sérialiseurs distincts, trois publics : le personnel (identité complète et
    provenance du dossier), le client pendant l'anonymat (filière, zone large, score,
    nature du risque), le souscripteur encaissé d'un projet décaissé (identité révélée,
    décision du fondateur P08).
    """
    if _is_staff(request):
        return serializers.project_row(project)
    if services.identity_disclosed_to(project, _my_investor(request)):
        return serializers.project_identified_row(project)
    return serializers.project_public_row(project)


def _project_detail_row_for(request, project: Project) -> dict:
    if _is_staff(request):
        return serializers.project_detail_row(project)
    if services.identity_disclosed_to(project, _my_investor(request)):
        return serializers.project_identified_detail_row(project)
    return serializers.project_public_detail_row(project)


# --- Projects ----------------------------------------------------------------

@api_view(["GET"])
@permission_classes([HasCapability("read")])
def project_next_code(request):
    """
    GET /api/investments/projects/next-code?location=Goma

    Code qui sera attribué au prochain projet de cette zone — `AG-CD-NKV-01` :
    préfixe institutionnel, pays, zone, séquence auto-incrémentée PAR ZONE.

    Servi par le SERVEUR et non composé au navigateur. Le calcul lit la table des
    provinces (`common/provinces.py`) et les codes déjà émis : refaire ce mapping
    en JavaScript poserait un second référentiel de zones, qui divergerait au
    premier chef-lieu ajouté (principes 6 et 8).

    **`indicatif` vaut toujours `true`, et ce n'est pas une précaution de style.**
    `project_code.generer` n'est pas une réservation : entre cet aperçu et
    l'insertion, un autre poste peut prendre le même numéro — c'est l'unicité en
    base qui tranche, et `create_project` réessaie. L'écran doit donc annoncer un
    code *probable*, jamais promettre celui-là.
    """
    if not _is_staff(request):
        return Response({"detail": "Réservé au personnel de l'institution."}, status=403)

    from . import project_code

    localisation = (request.GET.get("location") or "").strip()
    return Response({
        "code": project_code.generer(localisation=localisation),
        "prefixe": project_code.prefixe(localisation),
        "indicatif": True,
    })


@api_view(["GET"])
@permission_classes([HasCapability("read")])
def linkable_credit_applications(request):
    """
    GET /api/investments/credit-applications/linkable

    Dossiers de crédit qu'un projet peut RÉELLEMENT rattacher.

    L'écran ne proposait aucune liste : le code se tapait à la main
    (« CRED-2026-… »), il fallait donc le connaître par cœur. Un opérateur qui ne
    l'a pas sous les yeux ne rattache rien — et sans rattachement, aucun projet
    n'est analysable. C'était le blocage réel du parcours, pas le moteur.

    Le filtre applique les MÊMES règles que `services.link_credit_application` :
    un dossier déjà financé par un autre projet, ou sans montant demandé, n'est
    pas proposé. Offrir un choix que le serveur refusera ensuite est la même faute
    que de ne rien offrir — l'utilisateur découvre le refus après coup.

    Réservé au personnel : la liste porte le nom du client et le montant demandé.
    """
    if not _is_staff(request):
        return Response({"detail": "Réservé au personnel de l'institution."}, status=403)

    from credits.models import CreditApplication

    # `investment_project` est le `related_name` du OneToOne porté par `Project` :
    # son absence dit « pas encore financé », sans requête par ligne.
    qs = (CreditApplication.objects
          .filter(investment_project__isnull=True, amount_requested__isnull=False)
          .select_related("client", "value_chain")
          .order_by("-created_at"))

    recherche = (request.GET.get("q") or "").strip()
    if recherche:
        qs = qs.filter(code__icontains=recherche)

    lignes = [{
        "code": a.code,
        "clientName": getattr(a.client, "full_name", "") or getattr(a.client, "sub", ""),
        "valueChainLabel": getattr(a.value_chain, "label", "") or "",
        "amountRequested": float(a.amount_requested),
        "currency": a.currency,
        "status": a.status,
        "statusLabel": a.get_status_display(),
    } for a in qs[:200]]

    # `totalRows` sur toute liste tronquée (CONVENTIONS.md §4.6) : sans lui, une liste
    # coupée à 200 se lit comme la liste complète.
    return Response({"results": lignes, "totalRows": qs.count(), "returned": len(lignes)})


@api_view(["GET", "POST"])
@permission_classes([HasCapability("read")])
def projects(request):
    if request.method == "GET":
        qs = _visible_projects(request)
        status = request.GET.get("status")
        if status:
            qs = qs.filter(status=status)
        # Forme de liste conservée telle quelle : le front existant la consomme ainsi,
        # et cette liste n'est jamais tronquée (pas de `total_rows` à annoncer).
        return Response([_project_row_for(request, p) for p in qs])
    if not _is_staff(request) or not _require(request, "create"):
        return Response({"detail": "Capacité requise : create."}, status=403)
    data = request.data or {}
    # Pièces du dossier déposées dans le MÊME formulaire (multipart) : UN champ,
    # `pieces`, plusieurs fichiers possibles. Les joindre après coup obligerait à
    # rouvrir le projet.
    #
    # Il y en avait deux — `dossierTechnique` et `dossierFinancier` — parce que
    # l'ancien moteur d'investissement notait la « complétude du dossier » en
    # comptant une pièce de chaque nature. Ce moteur a été retiré (la note vient
    # du module crédit) : plus rien ne lisait ce tri, et il obligeait l'opérateur
    # à ranger un business plan dans une case au moment où aucune des deux
    # analyses n'existe encore. La nature se choisit désormais à l'écran des
    # analyses, où l'on rattache une pièce à ce qu'elle justifie.
    from .models import AnalysisAttachment

    pieces = [(AnalysisAttachment.Kind.DOSSIER, fichier, getattr(fichier, "name", ""))
              for fichier in request.FILES.getlist("pieces")]
    try:
        project = services.create_project(
            code=data.get("code", ""), title=data.get("title", ""), sector=data.get("sector", ""),
            location=data.get("location", ""), funding_target=data.get("fundingTarget", "0"),
            promoter=data.get("promoter", ""), manager_sub=getattr(request.user, "sub", ""),
            credit_application_code=data.get("creditApplicationCode", ""),
            pieces=pieces, by=getattr(request.user, "sub", ""),
        )
    except ValidationFailed as exc:
        return _structured_error(exc)

    # La note est reprise du dossier à la création. Quand elle ne l'est pas, le
    # motif part avec la réponse : sans lui, l'écran annonce « Projet créé » et
    # l'utilisateur découvre un dossier sans note, plus tard, sans cause.
    return Response(
        {**serializers.project_row(project),
         "initialAnalysis": getattr(project, "motif_analyse_initiale", None)},
        status=201,
    )


@api_view(["POST"])
@permission_classes([HasCapability("create")])
def project_link_credit_application(request, code):
    """Rattache un dossier de crédit à un projet — « 1 projet = 1 demande de crédit ».

    Acte de personnel : c'est lui qui décide quel dossier part en levée. Le rattachement
    est journalisé (append-only) parce qu'il change l'origine du promoteur, de la
    filière, de la zone, du montant et du score servis à tous les écrans.
    """
    if not _is_staff(request):
        return Response({"detail": "Réservé au personnel de l'institution."}, status=403)
    project = Project.objects.filter(code=code).first()
    if not project:
        return Response({"detail": "Projet introuvable."}, status=404)
    data = request.data or {}
    project = services.link_credit_application(
        project=project, application_code=data.get("applicationCode", ""),
        by=getattr(request.user, "sub", ""),
    )
    return Response(serializers.project_detail_row(project))


@api_view(["GET", "PATCH"])
@permission_classes([HasCapability("read")])
def project_detail(request, code):
    project = _visible_projects(request).filter(code=code).first()
    if not project:
        # 404 volontaire et non 403 : révéler l'existence d'un dossier en due
        # diligence est déjà une fuite d'information.
        return Response({"detail": "Projet introuvable."}, status=404)
    if request.method == "PATCH":
        if not _is_staff(request) or not _require(request, "create"):
            return Response({"detail": "Capacité requise : create."}, status=403)
        data = request.data or {}
        # Un champ dérivé du dossier de crédit ne se re-saisit pas : accepter la
        # modification créerait une seconde vérité à côté du dossier (principe 6).
        # Le refus nomme le champ ET sa source — l'écran sait où corriger.
        if project.credit_application_id:
            usurpes = [f for f in ("sector", "location", "promoter", "promoterContact",
                                    "fundingTarget") if f in data]
            if usurpes:
                return Response(
                    {"detail": "Ces champs sont dérivés du dossier de crédit "
                               f"{project.credit_application.code} : ils se corrigent "
                               "dans le dossier, pas sur le projet.",
                     "code": "FIELD_DERIVED_FROM_CREDIT_APPLICATION",
                     "errors": [{"code": "FIELD_DERIVED_FROM_CREDIT_APPLICATION",
                                 "message": f"« {champ} » est lu dans le dossier "
                                            f"({services.DERIVED_FIELDS.get(champ)})."}
                                for champ in usurpes]},
                    status=422,
                )
        # `riskScore` et `globalScore` ont disparu de cette liste avec le moteur de
        # scoring : ils n'ont plus de colonne à écrire. `riskCategory` reste — c'est
        # un libellé qui décrit le projet, pas une note.
        for field, model_field in (
            ("title", "title"), ("description", "description"), ("objectives", "objectives"),
            ("riskAnalysis", "risk_analysis"), ("impactEsg", "impact_esg"),
            ("riskCategory", "risk_category"),
        ):
            if field in data:
                setattr(project, model_field, data[field])
        project.save()
    return Response(_project_detail_row_for(request, project))


@api_view(["POST"])
@permission_classes([HasCapability("validate")])
def project_expert_valuation(request, code):
    """Enregistre la valorisation d'expert DATÉE d'un projet (Annexe D).

    C'est le seul intrant de la valorisation des titres de capital : sans elle,
    `metrics` retombe au pair et l'annonce. Acte de personnel — un investisseur ne
    valorise pas lui-même la ligne qu'il détient.

    La capacité exigée est `validate`, PAS `create` : le rôle `invest` porte `create`
    (`rbac/role_registry.py`), si bien qu'un investisseur pourrait sinon réévaluer sa
    propre position à son gré. Le test `test_endpoint_reserve_au_personnel_habilite`
    en est le garde-fou.

    **Endpoint sans bouton, documenté comme l'exige CONVENTIONS.md §7.2.** Aucun écran ne
    l'appelle aujourd'hui : l'onglet back-office qui saisirait une expertise (montant,
    date, cabinet) reste à construire, et il est hors du périmètre investisseur. Tant
    qu'il n'existe pas, toute position en titres de capital reste valorisée
    `PAIR_FAUTE_D_EXPERTISE` — ce qui est honnête, mais ce n'est pas une valorisation.
    Le trou est côté écran, pas côté serveur.
    """
    project = Project.objects.filter(code=code).first()
    if not project:
        return Response({"detail": "Projet introuvable."}, status=404)
    data = request.data or {}
    project = services.set_expert_valuation(
        project=project, amount=data.get("amount", "0"), valuation_date=data.get("valuationDate"),
        source=data.get("source", ""), by=getattr(request.user, "sub", ""),
    )
    return Response(serializers.project_detail_row(project))


@api_view(["POST"])
@permission_classes([HasCapability("validate")])
def project_action(request, code):
    """Transition explicite de la machine à états. Le motif est obligatoire.

    Les transitions à effet monétaire (clôture de souscription, décaissement,
    annulation avec remboursement) ont leurs propres endpoints : elles ne sont pas
    un simple changement de statut et ne passent pas par ici.
    """
    project = Project.objects.filter(code=code).first()
    if not project:
        return Response({"detail": "Projet introuvable."}, status=404)
    data = request.data or {}
    to_status = data.get("toStatus")
    if to_status in (Project.Status.P07, Project.Status.P08):
        return Response(
            {"detail": "Cette étape a son propre endpoint (clôture de souscription, "
                       "décaissement) : elle ne se déclenche pas par un changement de statut.",
             "code": "USE_DEDICATED_ENDPOINT"},
            status=409,
        )
    project = services.transition_status(
        project=project, to_status=to_status, by=getattr(request.user, "sub", ""),
        reason=data.get("reason", ""), actor_role=getattr(request.user, "role", ""),
    )
    return Response(serializers.project_row(project))


@api_view(["GET"])
@permission_classes([HasCapability("read")])
def project_transitions(request, code):
    """Historique append-only des transitions d'un projet — lecture seule absolue."""
    project = _visible_projects(request).filter(code=code).first()
    if not project:
        return Response({"detail": "Projet introuvable."}, status=404)
    rows = [
        {"fromStatus": t.from_status, "toStatus": t.to_status, "actor": t.actor_sub,
         "actorRole": t.actor_role, "reason": t.reason, "details": t.details,
         "createdAt": t.created_at.isoformat()}
        for t in ProjectTransition.objects.filter(project=project)
    ]
    return Response({"projectCode": project.code, "currentStatus": project.status,
                     "allowedTargets": sorted(workflow.allowed_targets(project.status)),
                     "transitions": rows, "totalRows": len(rows)})


# --- Offres ------------------------------------------------------------------

@api_view(["GET", "POST"])
@permission_classes([HasCapability("read")])
def offers(request):
    if request.method == "GET":
        qs = _visible_offers(request)
        project_code = request.GET.get("project")
        if project_code:
            qs = qs.filter(project__code=project_code)
        # `select_related` : `offer_row` lit désormais le statut du projet pour dire
        # si l'offre est visible — sans lui, une requête par offre.
        return Response([serializers.offer_row(o) for o in qs.select_related("project")])
    if not _is_staff(request) or not _require(request, "create"):
        return Response({"detail": "Capacité requise : create."}, status=403)
    data = request.data or {}
    project = Project.objects.filter(code=data.get("projectCode")).first()
    if not project:
        return Response({"detail": "Projet introuvable."}, status=404)
    offer = services.create_offer(
        project=project, code=data.get("code", ""), coupon_rate=data.get("couponRate", "0"),
        maturity_months=data.get("maturityMonths", 24), min_ticket=data.get("minTicket", "0"),
        available_bonds=data.get("availableBonds", 0), funding_goal=data.get("fundingGoal", "0"),
        type_of_title=data.get("typeOfTitle", ""), by=getattr(request.user, "sub", ""),
    )
    return Response(serializers.offer_row(offer), status=201)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def offers_open(request):
    """Offres réellement ouvertes — le seul détail projet auquel un investisseur a droit
    avant d'engager son argent."""
    return Response(metrics.open_offers_summary())


@api_view(["POST"])
@permission_classes([HasCapability("validate")])
def offer_close(request, offer_id):
    offer = Offer.objects.filter(pk=offer_id).first()
    if not offer:
        return Response({"detail": "Offre introuvable."}, status=404)
    offer = funding.close_offer(offer=offer, by=getattr(request.user, "sub", ""))
    return Response(serializers.offer_row(offer))


@api_view(["POST"])
@permission_classes([HasCapability("validate")])
def offer_distribute(request, offer_id):
    """Distribution au prorata des souscriptions ENCAISSÉES (B13)."""
    offer = Offer.objects.select_related("project").filter(pk=offer_id).first()
    if not offer:
        return Response({"detail": "Offre introuvable."}, status=404)
    data = request.data or {}
    key = data.get("idempotencyKey")
    if not key:
        return Response({"detail": "idempotencyKey requis."}, status=400)
    try:
        distribution = funding.distribute(
            offer=offer, amount=data.get("amount", "0"),
            kind=data.get("kind", Distribution.Kind.COUPON), idempotency_key=key,
            by=getattr(request.user, "sub", ""), value_date=data.get("valueDate"),
        )
    except idempotency.IdempotentReplay as exc:
        return idempotency.replay_response(exc)
    return Response(serializers.distribution_row(distribution), status=201)


# --- Levée : clôture, décaissement, retours ----------------------------------

@api_view(["POST"])
@permission_classes([HasCapability("validate")])
def project_close_fundraising(request, code):
    """P06 → P07 (min-funding atteint) ou P06 → P13 avec remboursements (min-funding raté)."""
    project = Project.objects.filter(code=code).first()
    if not project:
        return Response({"detail": "Projet introuvable."}, status=404)
    data = request.data or {}
    project = funding.close_fundraising(project=project, by=getattr(request.user, "sub", ""),
                                         reason=data.get("reason", ""))
    return Response(serializers.project_row(project))


@api_view(["POST"])
@permission_classes([HasCapability("disburse")])
def project_disburse(request, code):
    """P07 → P08 : décaissement vers le promoteur (B11). Jamais avant clôture."""
    project = Project.objects.filter(code=code).first()
    if not project:
        return Response({"detail": "Projet introuvable."}, status=404)
    data = request.data or {}
    key = data.get("idempotencyKey")
    if not key:
        return Response({"detail": "idempotencyKey requis."}, status=400)
    try:
        project = funding.disburse(project=project, amount=data.get("amount", "0"),
                                    idempotency_key=key, by=getattr(request.user, "sub", ""),
                                    reason=data.get("reason", ""))
    except idempotency.IdempotentReplay as exc:
        return idempotency.replay_response(exc)
    return Response(serializers.project_row(project))


@api_view(["POST"])
@permission_classes([HasCapability("validate")])
def project_record_return(request, code):
    """Encaissement d'un retour du projet (B12) — préalable à toute distribution."""
    project = Project.objects.filter(code=code).first()
    if not project:
        return Response({"detail": "Projet introuvable."}, status=404)
    data = request.data or {}
    key = data.get("idempotencyKey")
    if not key:
        return Response({"detail": "idempotencyKey requis."}, status=400)
    try:
        project = funding.record_return(project=project, amount=data.get("amount", "0"),
                                         idempotency_key=key, by=getattr(request.user, "sub", ""),
                                         value_date=data.get("valueDate"),
                                         reason=data.get("reason", ""))
    except idempotency.IdempotentReplay as exc:
        return idempotency.replay_response(exc)
    return Response(serializers.project_row(project))


@api_view(["POST"])
@permission_classes([HasCapability("validate")])
def project_cancel(request, code):
    """P13 : annulation avec remboursement des souscriptions encaissées."""
    project = Project.objects.filter(code=code).first()
    if not project:
        return Response({"detail": "Projet introuvable."}, status=404)
    data = request.data or {}
    project = funding.cancel_project(project=project, by=getattr(request.user, "sub", ""),
                                      reason=data.get("reason", ""))
    return Response(serializers.project_row(project))


@api_view(["GET"])
@permission_classes([HasCapability("audit")])
def accounting_events(request):
    """File des événements métier destinés au moteur d'écritures (B10→B13).

    Lecture seule : `investments` produit, `accounting` consomme. Le filtre
    `?pending=1` sert le consommateur ; l'auditeur, lui, relit tout.
    """
    qs = InvestmentEvent.objects.all()
    if request.GET.get("pending"):
        qs = qs.filter(consumed_at__isnull=True)
    if request.GET.get("project"):
        qs = qs.filter(project__code=request.GET["project"])
    if request.GET.get("type"):
        qs = qs.filter(event_type=request.GET["type"])
    total = qs.count()
    rows = [serializers.investment_event_row(e) for e in qs[:500]]
    return Response({"results": rows, "totalRows": total, "returned": len(rows)})


@api_view(["GET"])
@permission_classes([HasCapability("read")])
def offer_collateral(request, offer_id):
    offer = _visible_offers(request).filter(pk=offer_id).first()
    if not offer:
        return Response({"detail": "Offre introuvable."}, status=404)
    collateral = Collateral.objects.filter(offer=offer).first()
    if not collateral:
        return Response({"detail": "Aucune garantie renseignée pour cette offre."}, status=404)
    return Response(serializers.collateral_row(collateral))


# --- Investors -----------------------------------------------------------

@api_view(["GET", "POST"])
@permission_classes([HasCapability("read")])
def investors(request):
    if request.method == "GET":
        # Un investisseur ne voit jamais les autres investisseurs.
        if not _is_staff(request):
            return Response({"detail": "Réservé au personnel de l'institution."}, status=403)
        # Deux identités par ligne, deux coûts maîtrisés : le titulaire par jointure
        # (`select_related`), le gestionnaire assigné — un `CharField`, pas une FK —
        # par un seul appel d'annuaire pour tout le lot. Sans ces deux-là, la liste
        # fait deux requêtes par investisseur.
        rows = list(Investor.objects.select_related("user"))
        manager_names = directory.names_by_sub(i.assigned_manager_sub for i in rows)
        kyc_statuses = kyc_levels.statuts_par_sub(i.user_id for i in rows)
        return Response([serializers.investor_row(i, manager_names, kyc_statuses) for i in rows])
    if not _is_staff(request) or not _require(request, "create"):
        return Response({"detail": "Capacité requise : create."}, status=403)
    data = request.data or {}
    from accounts.models import FintechUser
    user = FintechUser.objects.filter(sub=data.get("userSub")).first()
    if not user:
        return Response({"detail": "Utilisateur IdP introuvable (doit s'être connecté au moins une fois)."},
                         status=404)
    investor, _ = Investor.objects.update_or_create(
        user=user, defaults={"investor_type": data.get("investorType", "INDIVIDUAL"),
                              "assigned_manager_sub": data.get("assignedManagerSub", "")},
    )
    return Response(serializers.investor_row(investor), status=201)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def my_investor_profile(request):
    investor, _ = Investor.objects.get_or_create(user=request.user)
    return Response(serializers.investor_row(investor))


@api_view(["POST"])
@permission_classes([HasCapability("validate")])
def investor_action(request, investor_id):
    investor = Investor.objects.filter(pk=investor_id).first()
    if not investor:
        return Response({"detail": "Investisseur introuvable."}, status=404)
    action = (request.data or {}).get("action")
    investor = services.investor_action(investor=investor, action=action, by=getattr(request.user, "sub", ""))
    return Response(serializers.investor_row(investor))


# --- Subscriptions ---------------------------------------------------------

@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def subscriptions(request):
    """POST = RÉSERVATION, pas encaissement (voir `subscription_settle`).

    En lecture, un investisseur ne voit que SES souscriptions : le filtre `?investor=`
    n'est honoré que pour le personnel. Sans cette règle, `?investor=42` exposerait le
    portefeuille du voisin.
    """
    if request.method == "GET":
        if _is_staff(request):
            qs = Subscription.objects.all()
            investor_id = request.GET.get("investor")
            if investor_id:
                qs = qs.filter(investor_id=investor_id)
        else:
            investor = _my_investor(request)
            qs = Subscription.objects.filter(investor=investor) if investor else Subscription.objects.none()
        return Response([serializers.subscription_row(s) for s in qs])
    data = request.data or {}
    key = data.get("idempotencyKey")
    if not key:
        return Response({"detail": "idempotencyKey requis."}, status=400)
    investor, _ = Investor.objects.get_or_create(user=request.user)
    try:
        sub = funding.reserve(
            investor=investor, offer_id=data.get("offerId"), bonds=data.get("bonds", 0),
            idempotency_key=key, by=getattr(request.user, "sub", ""),
            # Choix de la poche AU MOMENT de la souscription. Il était annoncé par
            # l'écran des portefeuilles sans exister nulle part : une souscription
            # naissait toujours non classée, et aucune poche ne portait jamais rien.
            sub_portfolio=data.get("subPortfolioId"),
        )
    except idempotency.IdempotentReplay as exc:
        return idempotency.replay_response(exc)
    return Response(serializers.subscription_row(sub), status=201)


@api_view(["POST"])
@permission_classes([HasCapability("validate")])
def subscription_settle(request, subscription_id):
    """Encaissement d'une souscription réservée (B10) par DÉBIT du portefeuille du
    souscripteur — flux interne « une seule porte ». Réservé au personnel qui constate
    l'encaissement ; le montant quitte réellement le wallet du client (`settle_from_wallet`),
    il n'est jamais inscrit sans contrepartie. Solde insuffisant ou portefeuille absent →
    422 structuré, la souscription reste réservée."""
    sub = Subscription.objects.filter(pk=subscription_id).first()
    if not sub:
        return Response({"detail": "Souscription introuvable."}, status=404)
    data = request.data or {}
    key = data.get("idempotencyKey")
    if not key:
        return Response({"detail": "idempotencyKey requis."}, status=400)
    try:
        sub = funding.settle_from_wallet(subscription=sub, idempotency_key=key,
                                          by=getattr(request.user, "sub", ""), amount=data.get("amount"),
                                          value_date=data.get("valueDate"))
    except idempotency.IdempotentReplay as exc:
        return idempotency.replay_response(exc)
    except funding.WalletDebitError as exc:
        return _structured_error(exc)
    return Response(serializers.subscription_row(sub))


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def subscription_cancel(request, subscription_id):
    """Annulation d'une réservation NON encaissée — par son titulaire ou le personnel."""
    sub = Subscription.objects.filter(pk=subscription_id).first()
    if not sub:
        return Response({"detail": "Souscription introuvable."}, status=404)
    if not _is_staff(request) and sub.investor_id != getattr(_my_investor(request), "pk", None):
        return Response({"detail": "Souscription introuvable."}, status=404)
    sub = funding.cancel_reservation(subscription=sub, by=getattr(request.user, "sub", ""),
                                      reason=(request.data or {}).get("reason", ""))
    return Response(serializers.subscription_row(sub))


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def my_subscriptions(request):
    investor = _my_investor(request)
    if not investor:
        return Response([])
    return Response([serializers.subscription_row(s) for s in investor.subscriptions.all()])


# --- Métriques (Annexe D) ----------------------------------------------------

@api_view(["GET"])
@permission_classes([IsAuthenticated])
def my_metrics(request):
    """Le tableau de bord de l'investisseur — SON argent, sur SES flux réels."""
    investor, _ = Investor.objects.get_or_create(user=request.user)
    return Response(metrics.investor_metrics(investor))


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def pipeline(request):
    """Pipeline amont : agrégé et anonymisé pour un client, détaillé pour le personnel.

    Le pipeline amont ne compte plus que deux étapes depuis le retrait du scoring et
    du comité : P01 (Prospection) et P05 (Approbation — pièce jointe). La liste vient
    de `metrics.anonymised_pipeline`, source unique des étapes comptées, pour que les
    deux branches de cette vue ne puissent pas diverger.
    """
    stages = metrics.anonymised_pipeline()
    if not _is_staff(request):
        return Response({"stages": stages, "projects": []})
    etapes = [row["stage"] for row in stages]
    return Response({
        "stages": stages,
        "projects": [serializers.project_row(p)
                     for p in Project.objects.filter(status__in=etapes)],
    })


@api_view(["GET"])
@permission_classes([HasCapability("read")])
def portfolio_metrics(request):
    """Métriques institution — XIRR sur flux réels, défaut valeur ET nombre,
    Herfindahl, score de santé avec sa formule et ses paramètres."""
    if not _is_staff(request):
        return Response({"detail": "Réservé au personnel de l'institution."}, status=403)
    return Response(metrics.portfolio_metrics())


# --- Movements / schedules / sub-portfolios --------------------------------

@api_view(["GET"])
@permission_classes([HasCapability("read")])
def movements(request):
    if _is_staff(request):
        qs = Movement.objects.all()
        investor_id = request.GET.get("investor")
        if investor_id:
            qs = qs.filter(investor_id=investor_id)
        zone = request.GET.get("zone")
        if zone:
            qs = qs.filter(geographic_zone=zone)
    else:
        # Un investisseur ne voit que SES mouvements — jamais ceux d'un autre.
        investor = _my_investor(request)
        qs = Movement.objects.filter(investor=investor) if investor else Movement.objects.none()
    # Forme de liste conservée (le front la consomme telle quelle) ; la troncature à
    # 500 lignes et la pagination associée sont à traiter dans le lot front.
    return Response([serializers.movement_row(m) for m in qs[:500]])


@api_view(["GET"])
@permission_classes([HasCapability("read")])
def schedules(request):
    """Échéanciers de retour (B12) — bornés aux offres visibles par l'appelant : un
    échéancier révèle le montage financier de son offre, donc de son projet."""
    qs = RepaymentSchedule.objects.filter(offer__in=_visible_offers(request))
    offer_id = request.GET.get("offer")
    if offer_id:
        qs = qs.filter(offer_id=offer_id)
    return Response([
        {"id": s.pk, "offerId": s.offer_id, "dueDate": s.due_date.isoformat(), "amountDue": float(s.amount_due),
         "kind": s.kind, "status": s.status}
        for s in qs
    ])


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def sub_portfolios(request):
    investor = _my_investor(request)
    if request.method == "GET":
        if not investor:
            return Response([])
        return Response([
            {"id": sp.pk, "name": sp.name, "description": sp.description} for sp in investor.sub_portfolios.all()
        ])
    if not investor:
        investor, _ = Investor.objects.get_or_create(user=request.user)
    data = request.data or {}
    sp = SubPortfolio.objects.create(investor=investor, name=data.get("name", ""),
                                      description=data.get("description", ""))
    return Response({"id": sp.pk, "name": sp.name, "description": sp.description}, status=201)


# --- Observations / questions / performance reports ------------------------

@api_view(["GET", "POST"])
@permission_classes([HasCapability("read")])
def observations(request):
    """Observations d'analyste. En lecture, elles suivent la visibilité du PROJET :
    les remarques de risque d'un dossier en due diligence ne s'affichent pas à un
    investisseur qui connaîtrait le code. En écriture, elles sont un acte d'analyste —
    donc du personnel : un investisseur ne rédige pas l'analyse de risque du dossier
    dans lequel il place son argent."""
    project_code = request.GET.get("project") if request.method == "GET" else (request.data or {}).get("projectCode")
    project = _visible_projects(request).filter(code=project_code).first()
    if request.method == "GET":
        qs = AnalystObservation.objects.filter(project=project) if project else AnalystObservation.objects.none()
        return Response([
            {"id": o.pk, "projectId": o.project_id, "category": o.category, "riskFlag": o.risk_flag,
             "observation": o.observation, "recommendation": o.recommendation}
            for o in qs
        ])
    if not _is_staff(request) or not _require(request, "create"):
        return Response({"detail": "Capacité requise : create (personnel de l'institution)."}, status=403)
    if not project:
        return Response({"detail": "Projet introuvable."}, status=404)
    data = request.data or {}
    obs = AnalystObservation.objects.create(
        project=project, analyst_name=data.get("analystName", ""), category=data.get("category", "RISK"),
        risk_flag=data.get("riskFlag", "LOW"), observation=data.get("observation", ""),
        recommendation=data.get("recommendation", ""),
    )
    return Response({"id": obs.pk}, status=201)


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def questions(request):
    if request.method == "GET":
        investor = _my_investor(request)
        qs = ProjectQuestion.objects.filter(investor=investor) if investor else ProjectQuestion.objects.none()
        # `?all=1` était gardé par la capacité `read` — que le rôle `invest` PORTE :
        # n'importe quel investisseur récupérait ainsi les questions de tous les autres
        # (leur identifiant, leurs projets, leurs préoccupations). La corbeille des
        # questions est un écran de personnel.
        if request.GET.get("all") and _is_staff(request) and _require(request, "read"):
            qs = ProjectQuestion.objects.all()
        project_code = request.GET.get("project")
        if project_code:
            qs = qs.filter(project__code=project_code)
        return Response([
            {"id": q.pk, "projectId": q.project_id, "question": q.question, "questionDate": q.question_date.isoformat(),
             "answer": q.answer, "answerDate": q.answer_date.isoformat() if q.answer_date else None,
             "answeredBy": q.answered_by, "status": q.status}
            for q in qs
        ])
    data = request.data or {}
    project = Project.objects.filter(code=data.get("projectCode")).first()
    if not project:
        return Response({"detail": "Projet introuvable."}, status=404)
    investor, _ = Investor.objects.get_or_create(user=request.user)
    q = ProjectQuestion.objects.create(project=project, investor=investor, question=data.get("question", ""))
    return Response({"id": q.pk}, status=201)


@api_view(["POST"])
@permission_classes([HasCapability("validate")])
def question_answer(request, question_id):
    q = ProjectQuestion.objects.filter(pk=question_id).first()
    if not q:
        return Response({"detail": "Question introuvable."}, status=404)
    q.answer = (request.data or {}).get("answer", "")
    q.answer_date = timezone.now()
    q.answered_by = getattr(request.user, "sub", "")
    q.status = ProjectQuestion.Status.ANSWERED
    q.save()
    return Response({"id": q.pk, "status": q.status})


@api_view(["GET", "POST"])
@permission_classes([HasCapability("read")])
def performance_reports(request):
    """Reporting du promoteur. Lecture bornée à la visibilité du projet (un
    investisseur suit les projets ouverts et ceux où il a mis de l'argent, pas les
    dossiers en instruction) ; dépôt réservé au personnel, qui saisit le reporting
    reçu du promoteur — celui-ci n'est pas un utilisateur du système."""
    if request.method == "GET":
        qs = PerformanceReport.objects.filter(project__in=_visible_projects(request))
        project_code = request.GET.get("project")
        if project_code:
            qs = qs.filter(project__code=project_code)
        return Response([serializers.performance_report_row(r) for r in qs])
    if not _is_staff(request) or not _require(request, "create"):
        return Response({"detail": "Capacité requise : create (personnel de l'institution)."}, status=403)
    data = request.data or {}
    project = Project.objects.filter(code=data.get("projectCode")).first()
    if not project:
        return Response({"detail": "Projet introuvable."}, status=404)
    report = services.submit_performance_report(project=project, data=data, by=getattr(request.user, "sub", ""))
    return Response({"id": report.pk, "deviationPercent": float(report.deviation_percent)},
                     status=201)


# --- Obligations (produit épargne obligataire client) -----------------------

@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def obligations(request):
    """Positions obligataires de l'investisseur — et leur création par DÉBIT du cash.

    `POST` ne déclare plus un montant : il convertit du cash. Corps attendu :
    `{offerId, bonds, idempotencyKey, name?}`. Le montant est calculé par l'offre
    (`bonds × bondUnitValue`), le portefeuille est débité, la souscription est
    encaissée (B10) et la position hérite des termes de l'offre. Sans offre
    applicable ou sans solde, la création est refusée — jamais partielle.
    """
    investor, _ = Investor.objects.get_or_create(user=request.user)
    if request.method == "GET":
        return Response([
            serializers.obligation_row(p)
            for p in investor.obligation_positions.select_related("offer__project", "subscription")
        ])
    data = request.data or {}
    key = data.get("idempotencyKey")
    if not key:
        return Response({"detail": "idempotencyKey requis.", "code": "IDEMPOTENCY_KEY_REQUIRED"},
                         status=400)
    try:
        position = obligations_service.souscrire(
            investor=investor, offer_id=data.get("offerId"), bonds=data.get("bonds", 0),
            idempotency_key=key, by=getattr(request.user, "sub", ""),
            name=data.get("name", ""),
        )
    except idempotency.IdempotentReplay as exc:
        return idempotency.replay_response(exc)
    except obligations_service.ObligationError as exc:
        return _structured_error(exc)
    return Response(serializers.obligation_row(position), status=201)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def obligation_withdrawals(request, position_id):
    position = ObligationPosition.objects.filter(pk=position_id, investor__user=request.user).first()
    if not position:
        return Response({"detail": "Position introuvable."}, status=404)
    return Response([
        {"id": w.pk, "positionId": w.position_id, "amount": float(w.amount),
         "penaltyRate": float(w.penalty_rate), "reason": w.reason, "status": w.status,
         "date": w.date.isoformat(), "units": serializers.WITHDRAWAL_RATE_UNITS}
        for w in position.withdrawals.all()
    ])


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def obligation_conversions(request, position_id):
    position = ObligationPosition.objects.filter(pk=position_id, investor__user=request.user).first()
    if not position:
        return Response({"detail": "Position introuvable."}, status=404)
    return Response([
        {"id": c.pk, "positionId": c.position_id, "coupons": c.coupons, "value": float(c.value),
         "shares": c.shares, "status": c.status, "date": c.date.isoformat()}
        for c in position.conversions.all()
    ])


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def obligation_withdraw(request, position_id):
    position = ObligationPosition.objects.filter(pk=position_id, investor__user=request.user).first()
    if not position:
        return Response({"detail": "Position introuvable."}, status=404)
    data = request.data or {}
    withdrawal = BondWithdrawal.objects.create(
        position=position, amount=data.get("amount", "0"), reason=data.get("reason", ""),
    )
    return Response({"id": withdrawal.pk, "status": withdrawal.status}, status=201)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def obligation_convert(request, position_id):
    position = ObligationPosition.objects.filter(pk=position_id, investor__user=request.user).first()
    if not position:
        return Response({"detail": "Position introuvable."}, status=404)
    data = request.data or {}
    coupons = int(data.get("coupons", 0) or 0)
    value = position.coupon_amount * coupons
    shares = int(value // Decimal("100"))
    conversion = BondConversion.objects.create(position=position, coupons=coupons, value=value, shares=shares)
    return Response({"id": conversion.pk, "shares": shares}, status=201)


# --- Secondary market -------------------------------------------------------

@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def secondary_market(request):
    if request.method == "GET":
        qs = SecondaryMarketListing.objects.filter(status=SecondaryMarketListing.Status.OPEN)
        return Response([
            {"id": l.pk, "subscriptionId": l.subscription_id, "askPrice": float(l.ask_price), "status": l.status}
            for l in qs
        ])
    investor, _ = Investor.objects.get_or_create(user=request.user)
    data = request.data or {}
    sub = Subscription.objects.filter(pk=data.get("subscriptionId"), investor=investor).first()
    if not sub:
        return Response({"detail": "Souscription introuvable."}, status=404)

    # Une part ne se met en vente que si elle a été ENCAISSÉE : mettre en vente une
    # réservation reviendrait à vendre un engagement dont l'argent n'est jamais entré.
    if sub.status not in Subscription.FUNDED_STATUSES:
        return Response({"detail": "Seule une souscription encaissée peut être mise en vente.",
                         "errors": [{"code": "SUBSCRIPTION_NOT_SETTLED",
                                     "message": f"Cette souscription est au statut « {sub.status} » : "
                                                f"l'argent n'a pas été encaissé."}]}, status=422)

    prix = to_decimal(data.get("askPrice"), default="-1")
    if prix <= 0:
        return Response({"detail": "Le prix demandé doit être strictement positif.",
                         "errors": [{"code": "ASK_PRICE_INVALID",
                                     "message": "Le prix demandé doit être strictement positif."}]},
                        status=422)
    if not est_au_centime(data.get("askPrice")):
        return Response({"detail": "Le prix demandé s'exprime au centime.",
                         "errors": [{"code": "ASK_PRICE_PRECISION",
                                     "message": "Le prix demandé porte plus de deux décimales "
                                                "(principe 4 bis)."}]}, status=422)

    # Une même part ne peut pas être offerte deux fois : sans ce contrôle, deux annonces
    # ouvertes sur la même souscription promettent le même actif à deux acheteurs.
    if SecondaryMarketListing.objects.filter(
            subscription=sub, status=SecondaryMarketListing.Status.OPEN).exists():
        return Response({"detail": "Cette part est déjà en vente.",
                         "errors": [{"code": "ALREADY_LISTED",
                                     "message": "Une annonce ouverte existe déjà pour cette "
                                                "souscription."}]}, status=409)

    listing = SecondaryMarketListing.objects.create(subscription=sub, ask_price=prix)
    audit_record(actor=getattr(request.user, "sub", ""), action="investments.secondary_market.list",
                 entity_type="SecondaryMarketListing", entity_id=str(listing.pk),
                 details={"subscriptionId": sub.pk, "askPrice": str(prix)})
    return Response({"id": listing.pk, "subscriptionId": sub.pk, "askPrice": float(prix),
                     "status": listing.status}, status=201)


# --- Dashboard / portefeuille -----------------------------------------------

@api_view(["GET"])
@permission_classes([HasCapability("read")])
def dashboard_metrics(request):
    """KPI institution. `totalInvested` compte l'argent ENCAISSÉ, pas les réservations :
    additionner des intentions et les appeler « investi » gonflerait le chiffre le plus
    regardé du module."""
    if not _is_staff(request):
        return Response({"detail": "Réservé au personnel de l'institution."}, status=403)
    encaisse = Subscription.objects.filter(
        status__in=Subscription.FUNDED_STATUSES).aggregate(total=Sum("settled_amount"))["total"] or 0
    reserve = Subscription.objects.filter(
        status=Subscription.Status.RESERVED).aggregate(total=Sum("amount"))["total"] or 0
    return Response({
        "totalProjects": Project.objects.count(),
        "totalInvested": float(encaisse),
        "totalReserved": float(reserve),
        # Effectifs des deux agrégats : « 120 000 investis » ne dit rien sans le nombre
        # de souscriptions qui le composent.
        "settledSubscriptionsCount": Subscription.objects.filter(
            status__in=Subscription.FUNDED_STATUSES).count(),
        "reservedSubscriptionsCount": Subscription.objects.filter(
            status=Subscription.Status.RESERVED).count(),
        "activeInvestors": Investor.objects.filter(status=Investor.Status.ACTIVE).count(),
        # Compté depuis `compliance`, seule autorité du KYC : la colonne
        # `Investor.kyc_status` n'étant jamais écrite, ce KPI comptait tous les
        # investisseurs, validés compris.
        "kycPending": sum(
            1 for statut in kyc_levels.statuts_par_sub(
                Investor.objects.values_list("user_id", flat=True)
            ).values() if statut != "VALIDE"
        ) + Investor.objects.exclude(
            user_id__in=KycProfile.objects.values_list("user_id", flat=True)
        ).count(),
        # Devise VÉRIFIÉE sur les flux, pas affirmée : `currency_note` signale toute
        # devise étrangère plutôt que de laisser l'écran additionner des CDF et des USD.
        **metrics.currency_note(distributions=Distribution.objects.all(),
                                  movements=Movement.objects.all()),
        "scope": "Toutes agences, tous projets.",
        # `localdate()` : la date d'arrêté annoncée doit être celle de
        # l'institution (Africa/Kinshasa), pas celle d'UTC. Servir la veille
        # pendant la première heure de la journée ferait dater un agrégat du
        # jour d'hier — sur un KPI qui porte sa période, c'est la période qui
        # devient fausse.
        "asOf": timezone.localdate().isoformat(),
    })


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def my_portfolio_allocation(request):
    investor, _ = Investor.objects.get_or_create(user=request.user)
    return Response(services.portfolio_allocation(investor=investor))


# --- Allocation cible d'un sous-portefeuille ---------------------------------


def _mon_sous_portefeuille(request, sub_id):
    """Sous-portefeuille de l'investisseur COURANT, ou `None`.

    Le filtre porte sur le propriétaire, pas seulement sur l'identifiant : sans lui,
    n'importe quel investisseur lirait — et fixerait — la cible d'un autre.
    """
    investor = _my_investor(request)
    if not investor:
        return None
    return SubPortfolio.objects.filter(pk=sub_id, investor=investor).first()


@api_view(["GET", "PUT"])
@permission_classes([IsAuthenticated])
def sub_portfolio_target_allocation(request, sub_id):
    """Cible d'allocation d'une poche — lecture et pose d'une nouvelle version.

    `PUT` n'écrase rien : il ajoute une version. L'historique reste lisible, avec son
    auteur et sa date, parce qu'une cible engage une décision d'investissement.
    """
    sp = _mon_sous_portefeuille(request, sub_id)
    if not sp:
        return Response({"detail": "Sous-portefeuille introuvable."}, status=404)

    if request.method == "GET":
        courante = services.target_allocation_courante(sub_portfolio=sp)
        return Response({
            "subPortfolioId": sp.pk,
            "current": courante,
            "buckets": list(TargetAllocation.POCHES),
            "history": [
                {"version": t.version, "weights": t.weights, "note": t.note,
                 "createdBy": t.created_by, "createdAt": t.created_at.isoformat()}
                for t in sp.target_allocations.all()
            ],
        })

    data = request.data or {}
    try:
        courante = services.set_target_allocation(
            sub_portfolio=sp, weights=data.get("weights"), note=data.get("note", ""),
            by=getattr(request.user, "sub", ""),
        )
    except ValidationFailed as exc:
        return Response({"detail": exc.message,
                          "errors": [{"code": getattr(exc, "code", "TARGET_INVALID"),
                                      "message": exc.message}]}, status=422)
    return Response(courante, status=200)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def sub_portfolio_allocation(request, sub_id):
    """Répartition réelle d'une poche et son écart à la cible."""
    sp = _mon_sous_portefeuille(request, sub_id)
    if not sp:
        return Response({"detail": "Sous-portefeuille introuvable."}, status=404)
    return Response(services.sub_portfolio_allocation(sub_portfolio=sp))


@api_view(["GET", "POST"])
@permission_classes([HasCapability("read")])
def project_analysis_attachments(request, code):
    """Pièces justificatives du dossier — liste et dépôt.

    C'est le SEUL dossier que l'investisseur lit avant de souscrire depuis le retrait
    du scoring et du comité : sans pièce jointe, le projet ne franchit pas P05
    (`workflow._guard_p05`) et il n'y a rien sur quoi décider.
    """
    project = _visible_projects(request).filter(code=code).first()
    if not project:
        return Response({"detail": "Projet introuvable."}, status=404)

    if request.method == "GET":
        qs = project.analysis_attachments.all()
        nature = request.GET.get("kind")
        if nature:
            qs = qs.filter(kind=nature)
        return Response([serializers.analysis_attachment_row(a) for a in qs])

    if not _is_staff(request) or not _require(request, "create"):
        return Response({"detail": "Capacité requise : create."}, status=403)
    try:
        piece = services.joindre_piece_analyse(
            project=project, kind=request.data.get("kind", ""),
            fichier=request.FILES.get("file"), label=request.data.get("label", ""),
            by=getattr(request.user, "sub", ""),
        )
    except ValidationFailed as exc:
        return Response({"detail": exc.message,
                          "errors": [{"code": getattr(exc, "code", "FILE_REFUSED"),
                                      "message": exc.message}]}, status=422)
    return Response(serializers.analysis_attachment_row(piece), status=201)


@api_view(["DELETE"])
@permission_classes([HasCapability("create")])
def project_analysis_attachment(request, code, piece_id):
    """Retire une pièce justificative — journalisé, et FERMÉ dès l'approbation.

    Une pièce déposée par erreur doit pouvoir partir tant que le dossier est en
    prospection : sans cela, l'écran accumule des fichiers qu'on n'ose pas toucher.

    Le verrou tenait au dépôt du procès-verbal de comité : la pièce sur laquelle le
    comité s'était prononcé ne se retirait plus. Le comité a été retiré, mais la
    raison du verrou n'a pas disparu — elle a changé de lecteur. À partir de P05,
    la pièce jointe EST ce que l'investisseur lit pour décider de souscrire ; l'effacer
    réécrirait ce sur quoi il s'est engagé (garde-fou §9). La borne est la même
    qu'avant (l'entrée en P05), seul le motif est nommé pour ce qu'il est aujourd'hui.

    Le fichier disparaît, l'ACTE reste : nom, auteur et date partent au journal
    d'audit, qui est append-only.
    """
    from .models import AnalysisAttachment

    project = Project.objects.filter(code=code).first()
    if not project:
        return Response({"detail": "Projet introuvable."}, status=404)
    if not _is_staff(request):
        return Response({"detail": "Réservé au personnel de l'institution."}, status=403)

    piece = AnalysisAttachment.objects.filter(project=project, pk=piece_id).first()
    if not piece:
        return Response({"detail": "Pièce introuvable."}, status=404)

    if project.status != Project.Status.P01:
        return Response(
            {"detail": "Ce projet est approuvé : ses pièces sont ce que l'investisseur "
                       "lit pour décider et ne se retirent plus. Déposez une pièce "
                       "corrective plutôt que d'effacer celle qui a été publiée.",
             "code": "ATTACHMENT_LOCKED_BY_APPROVAL",
             "errors": [{"code": "ATTACHMENT_LOCKED_BY_APPROVAL",
                         "message": "Pièce verrouillée par l'approbation du projet."}]},
            status=409)

    nom, nature = piece.original_name, piece.kind
    piece.delete()
    audit_record(actor=getattr(request.user, "sub", ""),
                 action="investments.analysis.attachment.remove",
                 entity_type="Project", entity_id=project.code,
                 details={"fichier": nom, "kind": nature, "pieceId": piece_id})
    return Response(status=204)


# --- Classement d'une souscription dans une poche ----------------------------


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def subscription_sub_portfolio(request, subscription_id):
    """Range (ou déplace) une souscription dans une poche — journalisé.

    Le classement ne déplace aucun franc, mais il change la répartition affichée et
    l'écart à la cible : c'est une lecture sur laquelle un investisseur décide, donc
    une écriture qui se trace avec son avant et son après.
    """
    investor = _my_investor(request)
    sub = Subscription.objects.filter(pk=subscription_id).first()
    if not sub or (not _is_staff(request) and sub.investor_id != getattr(investor, "pk", None)):
        return Response({"detail": "Souscription introuvable."}, status=404)
    try:
        sub = funding.assign_sub_portfolio(
            subscription=sub, sub_portfolio=(request.data or {}).get("subPortfolioId"),
            by=getattr(request.user, "sub", ""),
            reason=(request.data or {}).get("reason", ""),
        )
    except ValidationFailed as exc:
        return _erreur_metier(exc)
    return Response(serializers.subscription_row(sub))


# --- Ordres de rééquilibrage -------------------------------------------------


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def sub_portfolio_rebalancing(request, sub_id):
    """Ordres de rééquilibrage d'une poche — historique et passage d'un ordre.

    `POST` s'arrête à la RÉSERVATION, comme toute souscription : l'encaissement reste
    l'acte distinct constaté par l'institution. Il n'existe aucun chemin qui déplace
    de l'argent entre poches en un clic.
    """
    sp = _mon_sous_portefeuille(request, sub_id)
    if not sp:
        return Response({"detail": "Sous-portefeuille introuvable."}, status=404)

    if request.method == "GET":
        return Response({
            "subPortfolioId": sp.pk,
            "orders": rebalancing.ordres(sub_portfolio=sp),
            "allocation": services.sub_portfolio_allocation(sub_portfolio=sp),
        })

    data = request.data or {}
    key = data.get("idempotencyKey")
    if not key:
        return Response({"detail": "idempotencyKey requis."}, status=400)
    try:
        resultat = rebalancing.passer_ordre(
            sub_portfolio=sp, offer_id=data.get("offerId"), bonds=data.get("bonds", 0),
            idempotency_key=key, by=getattr(request.user, "sub", ""),
            reason=data.get("reason", ""),
        )
    except idempotency.IdempotentReplay as exc:
        return idempotency.replay_response(exc)
    except (ValidationFailed, ConflictError) as exc:
        return _erreur_metier(exc)
    return Response(resultat, status=201)


# --- Séries de référence (benchmarks) ---------------------------------------


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def benchmark_series(request):
    """Séries de référence ACTIVES et leurs points (GET) ; déclaration (POST, `config`).

    Une série en brouillon n'est servie à personne : tant qu'un checker ne l'a pas
    activée, elle n'engage pas l'institution sur ce à quoi ses investisseurs se
    comparent.
    """
    if request.method == "GET":
        return Response({
            "series": benchmarks.series_publiables(
                depuis=to_date(request.GET.get("from")),
                jusqu_a=to_date(request.GET.get("to")),
            ),
            "from": request.GET.get("from") or None,
            "to": request.GET.get("to") or None,
        })

    if not _is_staff(request) or not _require(request, "config"):
        return Response({"detail": "Capacité requise : config."}, status=403)
    data = request.data or {}
    try:
        serie = benchmarks.declarer_serie(
            code=data.get("code", ""), label=data.get("label", ""),
            currency=data.get("currency", "USD"),
            frequency=data.get("frequency", "MONTHLY"), kind=data.get("kind", "RATE"),
            source=data.get("source", ""), licence=data.get("licence", ""),
            notes=data.get("notes", ""), by=getattr(request.user, "sub", ""),
        )
    except (ValidationFailed, ConflictError) as exc:
        return _erreur_metier(exc)
    return Response({"code": serie.code, "status": serie.status}, status=201)


@api_view(["POST"])
@permission_classes([IsStaff, HasCapability("config")])
def benchmark_series_activate(request, code):
    """Active une série — par un acteur DIFFÉRENT du déclarant (maker ≠ checker)."""
    try:
        serie = benchmarks.activer_serie(code=code, by=getattr(request.user, "sub", ""))
    except (ValidationFailed, ConflictError) as exc:
        return _erreur_metier(exc)
    return Response({"code": serie.code, "status": serie.status,
                      "activatedBy": serie.activated_by,
                      "activatedAt": serie.activated_at.isoformat() if serie.activated_at else None})


# --- Référentiel ESG et mesures de projet -----------------------------------


@api_view(["GET", "POST"])
@permission_classes([IsStaff, HasCapability("config")])
def esg_criteria(request):
    """Référentiel ESG — consultation et déclaration d'une version de critère.

    Capacité `config` : décider de ce qui est mesuré et de son poids relève du
    paramétrage de l'institution, pas d'une décision de dossier. Les bornes de
    plausibilité ne sortent jamais de cette vue (principe 7 — un promoteur qui les
    connaît sait exactement quoi déclarer).
    """
    if request.method == "GET":
        qs = esg.EsgCriterion.objects.all()
        if request.GET.get("status"):
            qs = qs.filter(status=request.GET["status"])
        criteres = [esg.critere_row(c) for c in qs]
        actifs = [c for c in criteres if c["status"] == "ACTIVE"]
        return Response({
            "criteria": criteres,
            "activeCount": len(actifs),
            "activeWeightsTotal": sum(c["weight"] for c in actifs),
        })

    data = request.data or {}
    try:
        critere = esg.declarer_critere(
            code=data.get("code", ""), label=data.get("label", ""),
            unit=data.get("unit", ""), direction=data.get("direction", "UP"),
            weight=data.get("weight"), pillar=data.get("pillar", "E"),
            min_value=data.get("minValue"), max_value=data.get("maxValue"),
            by=getattr(request.user, "sub", ""),
        )
    except (ValidationFailed, ConflictError) as exc:
        return _erreur_metier(exc)
    return Response(esg.critere_row(critere), status=201)


@api_view(["POST"])
@permission_classes([IsStaff, HasCapability("config")])
def esg_criteria_activate(request):
    """Active un JEU de critères — checker ≠ maker, somme des poids = 100.

    L'activation porte sur l'ensemble et non sur un critère isolé : une pondération
    se vote entière, et l'activer critère par critère supposerait un état
    intermédiaire où toute moyenne pondérée serait fausse.
    """
    try:
        criteres = esg.activer_criteres(
            criterion_ids=(request.data or {}).get("criterionIds") or [],
            by=getattr(request.user, "sub", ""))
    except (ValidationFailed, ConflictError) as exc:
        return _erreur_metier(exc)
    return Response({"criteria": [esg.critere_row(c) for c in criteres],
                      "activeWeightsTotal": float(sum(c.weight for c in esg.criteres_actifs()))})


def _esg_lisible_par(request, project) -> bool:
    """Qui a le droit de lire la grille ESG d'un projet.

    Le personnel, et l'investisseur qui a de l'argent dessus. PAS le promoteur : la
    grille dit ce qui est mesuré et avec quel poids — la connaître d'avance, c'est
    savoir quoi déclarer (principe 7, asymétrie d'information).
    """
    if _is_staff(request):
        return True
    investor = _my_investor(request)
    if investor is None:
        return False
    return Subscription.objects.filter(
        investor=investor, offer__project=project,
        status__in=Subscription.FUNDED_STATUSES).exists()


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def project_esg(request, code):
    """Mesures ESG d'un projet — lecture, et saisie à l'instruction avec pièce probante."""
    project = Project.objects.filter(code=code).first()
    if not project or not _esg_lisible_par(request, project):
        return Response({"detail": "Projet introuvable."}, status=404)

    if request.method == "GET":
        return Response(esg.projet_esg(project))

    if not _is_staff(request) or not _require(request, "create"):
        return Response({"detail": "Capacité requise : create."}, status=403)
    try:
        mesure = esg.enregistrer_mesure(
            project=project, criterion_code=request.data.get("criterionCode", ""),
            value=request.data.get("value"),
            collected_on=to_date(request.data.get("collectedOn")),
            source=request.data.get("source", ""), fichier=request.FILES.get("file"),
            by=getattr(request.user, "sub", ""),
        )
    except (ValidationFailed, ConflictError) as exc:
        return _erreur_metier(exc)
    return Response(esg.mesure_row(mesure), status=201)


@api_view(["POST"])
@permission_classes([IsStaff, HasCapability("validate")])
def esg_measure_verify(request, measure_id):
    """Vérification terrain d'une mesure — déclaré → vérifié (valeur retenue) ou rejeté."""
    data = request.data or {}
    try:
        mesure = esg.verifier_mesure(
            measure_id=measure_id, decision=data.get("decision", ""),
            valeur_retenue=data.get("retainedValue"), motif=data.get("reason", ""),
            by=getattr(request.user, "sub", ""),
        )
    except (ValidationFailed, ConflictError) as exc:
        return _erreur_metier(exc)
    return Response(esg.mesure_row(mesure))
