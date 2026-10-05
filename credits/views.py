"""
Vues Crédits Agricoles (Étapes 2-4).

Endpoints :
  GET  /api/credits/application/prefill/                        → préremplissage
  POST /api/credits/needs-sheet/parse/                          → parse Feuille de Besoins
  GET  /api/credits/needs-sheet/saisie/                         → rubriques du template actif
  POST /api/credits/needs-sheet/saisie/enregistrer/             → saisie manuelle (sans Excel)
  GET  /api/credits/needs-sheet-template/                       → modèle Excel
  POST /api/credits/simulate/                                   → simulation scoring
  POST /api/credits/applications/<code>/score/                  → re-scorer un dossier
  GET  /api/credits/applications/<code>/guarantees/             → liste garanties
  POST /api/credits/applications/<code>/guarantees/savings/     → bloc épargne
  POST /api/credits/applications/<code>/guarantees/moral/       → caution morale
  POST /api/credits/applications/<code>/guarantees/<id>/confirm/ → confirmer caution morale
  POST /api/credits/applications/<code>/guarantees/<id>/release/ → libérer garantie
"""
from __future__ import annotations

import logging
import os
import tempfile
from typing import Any

from django.http import HttpResponse
from rest_framework import status
from rest_framework.decorators import api_view, parser_classes, permission_classes
from rest_framework.parsers import MultiPartParser
from rest_framework.permissions import BasePermission, IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response

from common.exceptions import PermissionDeniedError
from credits.permissions import CanInstructCredit, IsDesignatedGuarantor

from credits.needs_parser import NeedsSheetParseError, parse_needs_sheet
from credits.prefill import get_prefill_data
from credits.dataio_simulator import dataio_simulate
from credits.roles import (
    CAN_AUDIT,
    CAN_CONFIRM_DISBURSEMENT,
    CAN_DECIDE,
    CAN_INSTRUCT,
    CAN_REQUEST_DISBURSEMENT,
    COMMITTEE_ROLES,
    STAFF_ROLES,
    in_group,
    roles_of,
)
from credits.view_context import ViewContextService

logger = logging.getLogger(__name__)


# ── Socle des permissions déclaratives ────────────────────────────────────────
#
# §5 du cahier des charges : « `permission_classes` déclaratives sur CHAQUE vue
# (jamais de contrôle uniquement dans le corps) ; toute vue sans permission
# explicite est un bug ». Le module crédit y dérogeait par `_require_read()` et
# `_require_group()` appelés en première ligne de corps. La règle FONCTIONNAIT —
# mais elle était invisible depuis la signature de la vue, elle ne s'auditait pas
# mécaniquement (aucun outil ne lit un `if` de corps comme une politique d'accès)
# et une seule ligne oubliée à la création d'une vue suffisait à ouvrir un
# endpoint sans que rien ne le signale.
#
# Ce lot rend la règle déclarative SANS la changer : chaque vue conserve
# exactement le même public et le même statut de refus. Ce qui reste dans le
# corps est ce qui ne peut structurellement pas monter sur le décorateur —
# les contrôles qui dépendent de l'OBJET (« ce dossier est-il le sien ? »,
# « est-il le garant de CETTE caution ? ») ou du PAYLOAD (« crée-t-il un dossier
# pour un tiers ? ») : une permission de vue ne connaît ni l'objet ni le corps de
# la requête. Ces contrôles-là sont désormais les SEULS restants dans les corps,
# et chacun est signalé comme tel.


def MembreDuGroupe(groupe, *, message: str = "Permission refusée.",
                   code: str | None = None):
    """Fabrique la permission « appartenir à ce groupe fonctionnel de `credits.roles` ».

    Équivalent déclaratif strict de `_require_group(request, groupe)` : même
    prédicat (authentifié ET `in_group`), même statut de refus (403), même corps
    de réponse. `message` reprend mot pour mot celui que la vue renvoyait.

    `code` n'est passé que pour les vues dont le refus en portait un
    (`STAFF_REQUIS`, `INSTRUCTION_REQUISE`, `PERMISSION_REFUSEE`) : le `message`
    de DRF ne produit qu'un `{"detail": …}`, alors que le front branche sur
    `code`. On lève alors `PermissionDeniedError`, que
    `config.exceptions.agricap_exception_handler` rend en `{"detail", "code"}` à
    403 — la forme exacte que la vue construisait à la main.

    Pourquoi pas `rbac.permissions.HasCapability` : `client`, `agri_op`,
    `investor` et `partner` portent tous `read=True` au registre RBAC — une
    capacité ne dit JAMAIS « interne ». Pourquoi pas
    `accounts.permissions.IsStaff` là où le garde était `STAFF_ROLES` : `IsStaff`
    lit le TYPE du rôle au registre, donc couvre aussi `gest_agents` et
    `support`, absents de `credits.roles.STAFF_ROLES` — le substituer élargirait
    l'accès. Le garde reste l'appartenance au groupe, à l'identique.
    """
    groupe = frozenset(groupe)

    class _MembreDuGroupe(BasePermission):
        def has_permission(self, request, view) -> bool:
            user = getattr(request, "user", None)
            if not user or not getattr(user, "is_authenticated", False):
                # `IsAuthenticated`, toujours déclaré en premier, produit le 401
                # attendu par le SPA (qui rafraîchit son jeton dessus).
                return False
            if in_group(request, groupe):
                return True
            if code is not None:
                raise PermissionDeniedError(message, code=code)
            return False

    _MembreDuGroupe.message = message
    return _MembreDuGroupe


def GroupeSelonMethode(*, messages: dict[str, str] | None = None, **par_methode):
    """Groupe fonctionnel exigé, DIFFÉRENT selon la méthode HTTP.

    Jumelle assumée de `rbac.permissions.CapaciteSelonMethode`, dont elle reprend
    le contrat au mot près : table explicite sur le décorateur, et **méthode
    absente de la table = REFUSÉE** (le défaut est fermé, jamais ouvert). Elle
    existe parce que le module crédit sépare ses publics par GROUPE FONCTIONNEL
    (`credits.roles`) et non par capacité : sur `analysis-report/`, la lecture est
    ouverte au titulaire du dossier et l'écriture réservée à `CAN_INSTRUCT` —
    aucune paire de capacités ne décrit ce couple, `create` n'étant porté ni par
    `client` ni par `agri_op` ni par `partner`, qui déposent pourtant leurs
    dossiers.

    Valeur `None` = aucun groupe requis, l'authentification suffit : c'est le
    `_require_read` d'origine. Le cloisonnement par dossier reste alors dans le
    corps, car il porte sur l'objet et non sur la vue.
    """
    table = {methode.upper(): groupe for methode, groupe in par_methode.items()}
    libelles = {methode.upper(): texte for methode, texte in (messages or {}).items()}

    class _GroupeSelonMethode(BasePermission):
        message = "Permission refusée."

        def has_permission(self, request, view) -> bool:
            user = getattr(request, "user", None)
            if not user or not getattr(user, "is_authenticated", False):
                return False
            methode = (request.method or "").upper()
            if methode not in table:
                return False
            self.message = libelles.get(methode, "Permission refusée.")
            groupe = table[methode]
            return True if groupe is None else in_group(request, groupe)

    return _GroupeSelonMethode


#: Groupes utilisés par plus d'une vue — nommés ici pour qu'on lise « qui peut
#: quoi » sans dérouler le fichier. Les gardes à message ou à `code` propres sont
#: déclarés sur leur vue, au plus près de l'endpoint qu'ils protègent.
PeutDecider = MembreDuGroupe(CAN_DECIDE)
PeutDemanderUnDecaissement = MembreDuGroupe(CAN_REQUEST_DISBURSEMENT)
PeutConfirmerUnDecaissement = MembreDuGroupe(CAN_CONFIRM_DISBURSEMENT)


def _roles(request: Request) -> list[str]:
    """Rôles canoniques de la requête — remplace `request.roles`, jamais posé.

    Voir `credits.roles.roles_of` : un middleware ne peut pas remplir cet
    attribut ici, l'authentification DRF s'exécutant après les middlewares.
    """
    return roles_of(request)


def _vcs(request: Request) -> ViewContextService:
    """Construit un ViewContextService depuis la requête courante."""
    return ViewContextService(
        sub=getattr(request.user, "sub", "") or "",
        roles=_roles(request),
    )


def _require_read(request: Request) -> bool:
    """Tout utilisateur authentifié — les données sont filtrées par ViewContextService.

    N'est PLUS un garde de vue : c'est désormais `permission_classes` qui porte
    l'entrée dans la vue (`IsAuthenticated`, dont ce prédicat est l'exact
    équivalent — `sub` est la clé primaire de `FintechUser`, donc présente sur tout
    utilisateur authentifié). Il ne subsiste que comme brique de `_require_group`,
    lui-même réduit aux contrôles dépendant de l'objet ou du payload.

    Il n'a jamais rien dit du dossier demandé : dès qu'une vue résout un dossier
    par son `code`, elle DOIT enchaîner sur `_assert_can_read_app()`. Le code est
    prévisible (`CRED-AAAAMMJJ-NNNN`) — sans ce second contrôle,
    « authentifié » + `_get_application(code)` est un IDOR complet.
    """
    return bool(request.user and hasattr(request.user, "sub"))


def _assert_can_read_app(request: Request, app) -> Response | None:
    """`None` si l'appelant a le droit de lire CE dossier, sinon la réponse de refus.

    Réponse 404 et non 403, alignée sur `analysis_report` : les codes de dossier suivent
    `CRED-AAAAMMJJ-NNNN` et s'énumèrent. Un 403 confirmerait l'existence du dossier
    sondé — l'oracle suffit à cartographier la production de l'institution jour par jour.
    Le message est donc rigoureusement celui d'un dossier absent.
    """
    if _vcs(request).can_read_app(app):
        return None
    return Response({"detail": "Dossier introuvable."}, status=404)


def _workflow_error(exc) -> Response:
    """Réponse unique pour tout refus du workflow.

    `code` ET statut HTTP viennent de l'exception (`credits.workflow`), jamais
    d'une valeur réécrite dans la vue : c'est la duplication qui avait produit
    deux vocabulaires parallèles pour les mêmes concepts (`delegation_exceeded`
    vs `DELEGATION_EXCEEDED`) — le défaut que le principe 6 interdit. Une règle
    nouvelle définit son code et son statut à un seul endroit, et toutes les vues
    la relaient correctement sans être modifiées.

    `errors` détaille cause par cause quand il y en a plusieurs, et préserve les
    codes et messages d'origine — notamment `GUARANTEE_TYPE_NOT_ELIGIBLE`, dont
    le message énumère les types admis pour la filière : le front ne peut pas
    reconstituer cette liste et ne doit pas la connaître autrement (principe 7).
    """
    return Response(
        {"detail": str(exc), "code": exc.code, "errors": exc.as_errors()},
        status=exc.http_status,
    )


def _require_group(request: Request, group) -> bool:
    """Appartenance à un groupe fonctionnel de `credits.roles`, en cours de vue.

    N'est plus un garde d'ENTRÉE : tout garde de vue est monté sur
    `permission_classes` (`MembreDuGroupe`). Les appels restants sont les seuls
    qui ne peuvent pas y monter — ils dépendent de l'objet ou du payload et se
    lisent tous « X, OU un agent instructeur » :

      - créer / simuler / téléverser POUR UN TIERS (`client_sub` du payload) ;
      - agir sur un dossier dont on n'est pas le titulaire ;
      - confirmer un gage d'actif plutôt qu'une caution morale (type de l'objet).

    Remplace l'ancien `_require_permission(request, "agent"|"analyst")`, qui
    comparait `user.role` à des libellés (« agent », « analyst ») n'existant
    dans aucun registre : seul `admin` franchissait ces gardes, et le workflow
    crédit était de fait inaccessible à tous les rôles métier.
    """
    if not _require_read(request):
        return False
    return in_group(request, group)


def _to_decimal(value) -> "decimal.Decimal":
    """Convertit une valeur de payload en `Decimal` — jamais en `float`.

    Principe 4 : aucun `float` ne doit entrer dans un champ financier. Django ne
    convertit pas à la création (`objects.create(...)`), donc l'instance en
    mémoire conserve le type reçu jusqu'au prochain rechargement. Un `float`
    passé ici produisait un `TypeError` dès qu'un calcul le croisait — c'est ce
    qui faisait échouer `POST /api/credits/applications/` en 500 sur le calcul
    du ratio de couverture.

    `Decimal(str(...))` et non `Decimal(float)` : le second reporte l'erreur de
    représentation binaire dans le décimal (0.1 → 0.1000000000000000055…).
    """
    import decimal
    return decimal.Decimal(str(value).replace(",", "."))


# ── 0. Chaînes de valeur (niveau au-dessus de la filière) ────────────────────

@api_view(["GET"])
@permission_classes([IsAuthenticated])
def list_value_chain_families(request: Request) -> Response:
    """
    GET /api/credits/value-chain-families/

    Les 14 chaînes de valeur AGRICAP (`referentiel.chains`, source de vérité
    unique — principe 6). Le formulaire de demande les propose comme premier
    niveau de sélection ; le second niveau (la filière, `ValueChain`) se filtre
    ensuite côté client sur le `chainCode` que `prefill_application` attache à
    chaque filière (`common.filieres.numero_pour`), sans appel supplémentaire.

    Entrée : tout membre authentifié — c'est un référentiel de code en lecture,
    pas une donnée de dossier.
    """
    from referentiel.chains import CHAINS

    return Response([
        {"code": c.code, "label": c.libelle} for c in CHAINS
    ])


# ── 1. Préremplissage ────────────────────────────────────────────────────────

@api_view(["GET"])
@permission_classes([IsAuthenticated])
def prefill_application(request: Request) -> Response:
    """
    GET /api/credits/application/prefill/?client_sub=<sub>

    Retourne les données préremplies pour un nouveau dossier de crédit.
    Si client_sub est omis, utilise le sub du demandeur lui-même.

    Entrée : tout membre authentifié — il prépare SON dossier. Le garde de rôle
    ne se déclenche que sur le `client_sub` du payload (créer au nom d'un tiers),
    donnée qu'une permission de vue ne peut pas connaître : il reste dans le corps.
    """
    requester_sub: str = getattr(request.user, "sub", "") or ""
    client_sub: str = request.query_params.get("client_sub", requester_sub)

    if not client_sub:
        return Response({"detail": "client_sub est requis."}, status=400)

    # Pour créer un dossier pour un tiers, il faut la permission agent
    if client_sub != requester_sub and not _require_group(request, CAN_INSTRUCT):
        return Response(
            {"detail": "Permission 'agent' requise pour créer un dossier au nom d'un client."},
            status=403,
        )

    data = get_prefill_data(client_sub=client_sub, requester_sub=requester_sub)

    if "error" in data:
        return Response({"detail": "Client introuvable.", "code": data["error"]}, status=404)

    return Response(data)


# ── 2. Parse Feuille de Besoins ──────────────────────────────────────────────

@api_view(["POST"])
@permission_classes([IsAuthenticated])
@parser_classes([MultiPartParser])
def parse_needs_sheet_view(request: Request) -> Response:
    """
    POST /api/credits/needs-sheet/parse/
    Content-Type: multipart/form-data

    Champs requis  : file (xlsx)
    Champs optionnels : value_chain_code, area_ha, currency (USD|CDF), application_code

    Avec `application_code` (mode SPEC chantier 1) : le classeur est validé (6
    contrôles, 422 structuré) puis **ingéré en tables** (`dataio`, kind
    FEUILLE_BESOINS) et rattaché au dossier. Simulation et scoring liront ensuite
    ces tables — plus jamais le fichier.

    Sans `application_code` (parcours client avant création du dossier) : parsing
    en mémoire hérité, conservé pour ne pas casser l'étape 2 du simulateur.

    Entrée : tout membre authentifié — le client téléverse SA feuille. Le
    rattachement à un dossier passe par `_ingest_needs_sheet`, qui vérifie le
    titulaire (contrôle d'objet, donc dans le corps).
    """
    file = request.FILES.get("file")
    if not file:
        return Response({"detail": "Fichier manquant (champ 'file' requis)."}, status=400)

    if not file.name.lower().endswith(".xlsx"):
        return Response(
            {"errors": [{
                "code": "FORMAT_INVALIDE",
                "message": "Seuls les classeurs .xlsx sont acceptés (pas de .xls ni .xlsm).",
            }]},
            status=422,
        )

    application_code: str = (request.data.get("application_code") or "").strip()
    if application_code:
        return _ingest_needs_sheet(request, file, application_code)

    return _parser_hors_dossier(
        request, file,
        value_chain_code=request.data.get("value_chain_code", ""),
        area_ha_raw=request.data.get("area_ha", ""),
        currency=request.data.get("currency", "USD").upper(),
    )


def _parser_hors_dossier(request: Request, file, *, value_chain_code: str,
                         area_ha_raw: str, currency: str) -> Response:
    """Lecture d'une feuille de besoins AVANT création du dossier.

    Extrait de `parse_needs_sheet_view` pour que la SAISIE MANUELLE emprunte
    exactement le même traitement : mêmes contrôles, même persistance, même
    analyse documentaire. Deux portes d'entrée, un seul traitement.
    """
    if currency not in ("USD", "CDF"):
        return Response({"detail": "currency doit être USD ou CDF."}, status=400)

    # Résoudre la filière
    value_chain = None
    if value_chain_code:
        try:
            from reference_data.models import ValueChain
            value_chain = ValueChain.objects.get(code=value_chain_code, active=True)
        except Exception:
            return Response(
                {"detail": f"Filière '{value_chain_code}' introuvable ou inactive."},
                status=400,
            )

    # Convertir area_ha
    import decimal
    area_ha = None
    if area_ha_raw:
        try:
            area_ha = decimal.Decimal(area_ha_raw.replace(",", "."))
        except Exception:
            return Response({"detail": "area_ha invalide."}, status=400)

    # Sauvegarder temporairement
    suffix = os.path.splitext(file.name)[1]
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    try:
        for chunk in file.chunks():
            tmp.write(chunk)
        tmp.flush()
        tmp.close()

        try:
            result = parse_needs_sheet(
                file_path=tmp.name,
                value_chain=value_chain,
                area_ha=area_ha,
                currency=currency,
            )
        except NeedsSheetParseError as exc:
            return Response({"detail": str(exc), "code": "PARSE_ERROR"}, status=422)
        except Exception as exc:
            return Response({"detail": f"Erreur de traitement : {exc}"}, status=500)
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass

    # Persister en base
    sub = getattr(request.user, "sub", "") or ""
    needs_sheet_id = _persist_needs_sheet(
        file=file,
        result=result,
        value_chain=value_chain,
        area_ha=area_ha,
        currency=currency,
        uploaded_by=sub,
    )

    # Analyse documentaire (Partie H) — silencieuse, ne bloque pas la réponse
    analysis_summary = {}
    if needs_sheet_id:
        try:
            from credits.analysis import run_analysis
            analysis_summary = run_analysis(
                needs_sheet_id=needs_sheet_id,
                value_chain=value_chain,
                area_ha=area_ha,
                currency=currency,
            )
        except Exception:
            pass

    return Response(
        {
            "ok": result["ok"],
            "needsSheetId": needs_sheet_id,
            "grandTotal": result["grandTotal"],
            "totalByModule": result["totalByModule"],
            "warnings": result["warnings"],
            "anomalies": result["anomalies"],
            "items": result["items"],
            "analysis": analysis_summary or None,
            # Servis parce que l'écran les affiche. Ils manquaient : `CreditAnalysis`
            # lisait `currency` et `area_ha` sur cette réponse, n'obtenait
            # `undefined`, et montrait « Total : 1 330 » sans devise ni superficie.
            # Un montant sans sa devise n'est pas un montant (CONVENTIONS.md §4.6).
            "parsedOk": result["ok"],
            "currency": currency,
            "area_ha": float(area_ha) if area_ha is not None else None,
        },
        status=201,
    )


def _persist_needs_sheet(
    file, result: dict, value_chain, area_ha, currency: str, uploaded_by: str
) -> int | None:
    """Crée un NeedsSheet + ses NeedItem en base."""
    try:
        from credits.models import NeedsSheet, NeedItem
        import decimal

        ns = NeedsSheet.objects.create(
            uploaded_by=uploaded_by,
            raw_file=file,
            value_chain=value_chain,
            area_ha=area_ha,
            currency=currency,
            parsed_ok=result["ok"],
            warnings=result["warnings"],
            anomalies=result["anomalies"],
            total_by_module=result["totalByModule"],
            grand_total=decimal.Decimal(str(result["grandTotal"])),
        )

        NeedItem.objects.bulk_create([
            NeedItem(
                sheet=ns,
                module=item["module"],
                label=item["label"],
                quantity=decimal.Decimal(item["quantity"]) if item.get("quantity") else None,
                unit=item.get("unit", ""),
                unit_price=decimal.Decimal(item["unit_price"]) if item.get("unit_price") else None,
                declared_total=decimal.Decimal(item["declared_total"]) if item.get("declared_total") else None,
                computed_total=decimal.Decimal(item["computed_total"]) if item.get("computed_total") else None,
                suggested_supplier=item.get("suggested_supplier", ""),
                supplier_warning=item.get("supplier_warning", ""),
            )
            for item in result.get("items", [])
        ])

        return ns.pk
    except Exception:
        return None


def _ingest_needs_sheet(request: Request, file, application_code: str) -> Response:
    """Validation + ingestion dataio de la feuille de besoins d'un dossier.

    Le dossier doit être en `draft` : après soumission, la base de calcul est figée
    (une révision de plus changerait rétroactivement ce qui a été instruit).
    """
    from credits.models import CreditApplication
    from credits.needs_sheet import NeedsSheetValidationError, parse_and_ingest

    app = CreditApplication.objects.filter(code=application_code).first()
    if app is None:
        return Response({"detail": "Dossier introuvable."}, status=404)

    requester_sub: str = getattr(request.user, "sub", "") or ""
    if app.client.sub != requester_sub and not _require_group(request, CAN_INSTRUCT):
        return Response({"detail": "Accès interdit à ce dossier."}, status=403)

    if app.status != CreditApplication.Status.DRAFT:
        return Response(
            {"detail": (f"Le dossier {app.code} n'est plus modifiable "
                        f"(statut « {app.get_status_display()} »)."),
             "errors": [{"code": "APPLICATION_NOT_DRAFT",
                         "message": "La feuille de besoins ne peut être remplacée "
                                    "qu'en brouillon."}]},
            status=409,
        )

    try:
        result = parse_and_ingest(file, app, uploaded_by=requester_sub)
    except NeedsSheetValidationError as exc:
        return Response({"errors": exc.errors}, status=422)

    return Response(
        {
            "ok": True,
            "applicationCode": app.code,
            "needsSourceId": result["needs_source_id"],
            "revision": result["revision"],
            "sha256": result["sha256"],
            "totalByModule": result["totals"],
            "grandTotal": result["grand_total"],
        },
        status=201,
    )


# ── 2 bis. Saisie manuelle de la feuille de besoins ──────────────────────────

@api_view(["GET"])
@permission_classes([IsAuthenticated])
def needs_sheet_saisie_options(request: Request) -> Response:
    """
    GET /api/credits/needs-sheet/saisie/

    Rubriques proposées au formulaire de saisie — celles du template ACTIF
    (principe 11), servies par le serveur. Une liste recopiée au front
    divergerait au premier template activé par l'admin, et les lignes tomberaient
    dans le vide sans que rien ne le dise.
    """
    from credits.needs_manual import rubriques_de_saisie

    return Response(rubriques_de_saisie())


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def needs_sheet_saisie(request: Request) -> Response:
    """
    POST /api/credits/needs-sheet/saisie/

    Corps JSON :
      lines             (liste, requis) — {rubrique, designation, unite, quantite,
                                           prix_unitaire, frequence, observations}
      application_code  (str, optionnel) — rattache et INGÈRE (dataio, révision, sha256)
      value_chain_code / area_ha / currency (optionnels, parcours pré-dossier)

    La saisie est transformée en classeur conforme au template actif, puis passe
    par le MÊME pipeline qu'un dépôt Excel : mêmes contrôles, même ingestion,
    même lignée. Le moteur ne saura pas — et n'a pas à savoir — si la feuille est
    née d'un fichier ou d'un écran (principe 1).
    """
    from django.core.files.uploadedfile import SimpleUploadedFile

    from credits.needs_manual import SaisieInvalide, classeur_depuis_saisie

    try:
        octets, lignes = classeur_depuis_saisie(request.data.get("lines"))
    except SaisieInvalide as exc:
        return Response({"errors": exc.errors}, status=422)

    fichier = SimpleUploadedFile(
        "saisie-manuelle.xlsx", octets,
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )

    application_code: str = (request.data.get("application_code") or "").strip()
    if application_code:
        reponse = _ingest_needs_sheet(request, fichier, application_code)
    else:
        reponse = _parser_hors_dossier(
            request, fichier,
            value_chain_code=request.data.get("value_chain_code", "") or "",
            area_ha_raw=str(request.data.get("area_ha", "") or ""),
            currency=str(request.data.get("currency", "USD") or "USD").upper(),
        )

    # L'écran doit pouvoir réafficher ce qui a été enregistré, et l'agent voir que
    # ses lignes sont bien celles qui ont été comptées.
    if isinstance(reponse.data, dict) and reponse.status_code < 400:
        reponse.data["source"] = "saisie_manuelle"
        reponse.data["lignesSaisies"] = len(lignes)
    return reponse


# ── 3. Modèle Excel (template) ───────────────────────────────────────────────

@api_view(["GET"])
@permission_classes([])
def download_needs_sheet_template(request: Request) -> HttpResponse:
    """
    GET /api/credits/needs-sheet-template/?value_chain_code=<code>

    Principe 11 — sert EXACTEMENT le template actif (`dataio`, maker-checker) :
    le fichier téléchargé par le client est celui contre lequel sa feuille sera
    validée (une seule source, jamais un `.xlsx` statique ni une génération
    dynamique de repli). Sans template actif POUR LA CHAÎNE demandée → 503 +
    `TEMPLATE_NOT_CONFIGURED`, message explicite, jamais un fichier « best effort »
    ni le template d'une autre chaîne.

    `value_chain_code` (la filière choisie, `MAIS`/`RIZ`/…) est résolu vers sa
    chaîne `01`-`14` (`common.filieres.numero_pour`) : c'est cette chaîne qui
    détermine le template, pas la filière elle-même — plusieurs filières d'une
    même chaîne partagent le même modèle Excel. Omis, le template GÉNÉRIQUE du
    kind est servi (compatibilité avec un déploiement sans template par chaîne).
    """
    from common.filieres import numero_pour
    from dataio.models import KIND_FEUILLE_BESOINS
    from dataio.services_templates import TemplateNotConfigured, serve_active

    value_chain_code = request.GET.get("value_chain_code", "") or ""
    chain_code = numero_pour(value_chain_code) if value_chain_code else ""

    try:
        data, original_name, ref = serve_active(KIND_FEUILLE_BESOINS, chain_code)
    except TemplateNotConfigured as exc:
        return Response(
            {"detail": exc.message, "code": exc.code},
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    filename = original_name or "AGRICAP_Feuille_Besoins.xlsx"
    response = HttpResponse(
        data,
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    response["Access-Control-Allow-Origin"] = "*"
    # Le client (et l'auditeur) sait quelle version il a téléchargée = celle
    # contre laquelle il sera validé.
    response["X-Template-Id"] = str(ref.get("templateId", ""))
    response["X-Template-Version"] = str(ref.get("version", ""))
    return response


# ── 4. Simulation scoring ────────────────────────────────────────────────────

@api_view(["POST"])
@permission_classes([IsAuthenticated])
def simulate_scoring(request: Request) -> Response:
    """
    POST /api/credits/simulate/

    Simule le scoring et le plan de remboursement sans créer de dossier en base.

    Corps JSON :
      application_code   (str, optionnel) — mode SPEC : les montants par module sont
                         LUS dans les DataRecord de `application.needs_source`. Tout
                         `ns_totals` du payload est alors ignoré (principe 1).
      client_sub         (str, requis si pas d'application_code)
      value_chain_code   (str, optionnel)
      needs_sheet_id     (int, optionnel) — legacy, NeedsSheet déjà parsée
      area_ha            (float, optionnel)
      amount_requested   (float, optionnel)
      currency           (str, "USD"|"CDF", défaut "USD")

    Entrée : tout membre authentifié — un client simule pour lui-même. Simuler
    pour un tiers (`client_sub` ≠ le sien) exige `CAN_INSTRUCT` ; ce garde-là
    dépend du payload et reste donc dans le corps.
    """
    data = request.data
    if (application_code := (data.get("application_code") or "").strip()):
        return _simulate_from_source(request, application_code, data)

    client_sub: str = data.get("client_sub", "")
    requester_sub: str = getattr(request.user, "sub", "") or ""

    if not client_sub:
        client_sub = requester_sub
    if not client_sub:
        return Response({"detail": "client_sub requis."}, status=400)

    # Agent simulant pour un tiers → permission agent
    if client_sub != requester_sub and not _require_group(request, CAN_INSTRUCT):
        return Response({"detail": "Permission 'agent' requise."}, status=403)

    value_chain_code: str = data.get("value_chain_code", "")
    needs_sheet_id = data.get("needs_sheet_id")
    currency: str = data.get("currency", "USD").upper()
    # ns_totals envoyé directement par le frontend (nsResult.totalByModule)
    ns_totals_raw = data.get("ns_totals") or {}
    ns_totals = {k: float(v) for k, v in ns_totals_raw.items() if v} if isinstance(ns_totals_raw, dict) else {}
    # Financement par module (contrat §1) — % demandé par module (coûts non touchés).
    from credits.dataio_simulator import normalize_module_financing
    module_financing = normalize_module_financing(
        data.get("module_financing") or data.get("moduleFinancing"))

    try:
        area_ha = _to_decimal(data["area_ha"]) if data.get("area_ha") else None
    except (ValueError, TypeError):
        return Response({"detail": "area_ha invalide."}, status=400)

    try:
        amount_requested = (
            _to_decimal(data["amount_requested"]) if data.get("amount_requested") else None
        )
    except (ValueError, TypeError):
        return Response({"detail": "amount_requested invalide."}, status=400)

    # Charger le client et la feuille de besoins depuis la base
    from accounts.models import FintechUser
    from credits.models import NeedsSheet

    try:
        client = FintechUser.objects.select_related("kyc_profile").get(sub=client_sub)
    except FintechUser.DoesNotExist:
        return Response({"detail": "Client introuvable.", "code": "CLIENT_NOT_FOUND"}, status=404)

    needs_sheet = None
    if needs_sheet_id:
        try:
            needs_sheet = NeedsSheet.objects.get(pk=int(needs_sheet_id), uploaded_by=client_sub)
            # Si pas de ns_totals du frontend, utiliser ceux de la feuille
            if not ns_totals and needs_sheet.total_by_module:
                ns_totals = {k: float(v) for k, v in needs_sheet.total_by_module.items()}
        except NeedsSheet.DoesNotExist:
            pass

    result = dataio_simulate(
        client=client,
        value_chain_code=value_chain_code or None,
        needs_sheet=needs_sheet,
        ns_totals=ns_totals or None,
        area_ha=area_ha,
        amount_requested=amount_requested,
        currency=currency,
        guarantees_data=None,
        module_financing=module_financing,
    )

    if "error" in result:
        return Response({"detail": result["error"]}, status=400)

    return Response(_filtrer_simulation(result, request))


#: Clés de `refData` qui décrivent le MOTEUR, pas le dossier du client.
#: `refTotals` porte les coûts de référence par module (les plages du
#: référentiel), `rateAnnual`/`durationMonths`/`deferredMonths` les paramètres
#: de calcul, `dscr`/`dscrStress` des indicateurs d'instruction.
_REF_DATA_STAFF = (
    "refTotals", "dscr", "dscrStress", "rateAnnual",
    "durationMonths", "deferredMonths", "grandTotalNS",
)

#: Champs de `breakdown[]` qui exposent la PONDÉRATION du barème.
_BREAKDOWN_STAFF = ("maxPoints", "weight", "weightedScore", "score")


def _filtrer_simulation(result: dict, request: Request) -> dict:
    """Retire du résultat de simulation ce qu'un client ne doit jamais voir.

    PRINCIPE 7 — anti-gaming par asymétrie d'information. Le client voit son
    score, sa lettre et des pistes ; il ne voit JAMAIS les barèmes, les seuils,
    les tolérances par module, les plages du référentiel ni les règles du moteur.

    Ce filtre existe parce qu'il manquait. `dataio_simulator` documentait
    pourtant l'intention à la lettre — « la grille ne descend jamais au client
    (principe 7) ; c'est la vue qui filtre » — mais aucune vue ne filtrait :
    `simulate/` renvoyait `result` brut, et `_require_read` laisse passer un
    client. La grille de taux (bande, ajustement, plancher), les coûts de
    référence par module et le score minimum requis descendaient donc jusqu'au
    navigateur du demandeur. Un commentaire qui décrit une garantie non
    implémentée est pire qu'un silence : il fait croire la porte fermée.

    Le front avait cessé de les AFFICHER ; les données continuaient de partir.
    Un onglet réseau ouvert suffisait à lire le barème et à caler son dossier
    juste au-dessus de la barre — exactement le comportement que §4.3 apprend
    à détecter.
    """
    if getattr(getattr(request, "user", None), "is_staff_role", False):
        return result

    filtre = dict(result)
    # La grille qui EXPLIQUE le taux est un barème ; le taux lui-même reste
    # servi (`proposedRate`) — le client a le droit de savoir ce qu'on lui
    # propose, pas comment la bande a été franchie.
    filtre.pop("tarification", None)
    # Un seuil d'éligibilité est une règle du moteur : `eligible` suffit à
    # informer, `minScoreRequired` dirait où viser.
    filtre.pop("minScoreRequired", None)

    if isinstance(filtre.get("refData"), dict):
        filtre["refData"] = {k: v for k, v in filtre["refData"].items()
                             if k not in _REF_DATA_STAFF}

    if isinstance(filtre.get("breakdown"), list):
        filtre["breakdown"] = [
            {k: v for k, v in ligne.items() if k not in _BREAKDOWN_STAFF}
            if isinstance(ligne, dict) else ligne
            for ligne in filtre["breakdown"]
        ]

    # `poidsCalculable`/`poidsTotal` sont la somme des poids du barème : on garde
    # l'honnêteté de la couverture (combien de critères n'ont pas pu être évalués)
    # sans livrer la pondération elle-même.
    couverture = filtre.get("scoreCouverture")
    if isinstance(couverture, dict):
        filtre["scoreCouverture"] = {
            k: v for k, v in couverture.items()
            if k in ("nbCriteresExclus", "renormalise")
        }
    return filtre


def _load_needs_totals(app):
    """(totaux par module en float, lignage) depuis la révision courante en base.

    Retourne `(None, {})` si le dossier n'a pas encore de feuille ingérée : c'est
    au appelant de refuser — on ne calcule jamais un score sur des montants absents
    ou fournis par le client.
    """
    from credits.needs_sheet import extract_module_totals, needs_source_lineage

    source = app.needs_source
    if source is None:
        return None, {}
    totals = extract_module_totals(source)
    return {k: float(v) for k, v in totals.items()}, needs_source_lineage(source)


def _simulate_from_source(request: Request, application_code: str, data) -> Response:
    """Simulation adossée aux tables du dossier — aucun montant n'est accepté du client."""
    from credits.models import CreditApplication

    app = (CreditApplication.objects
           .select_related("client__kyc_profile", "value_chain", "needs_source")
           .filter(code=application_code).first())
    if app is None:
        return Response({"detail": "Dossier introuvable."}, status=404)

    vcs = _vcs(request)
    if not vcs.can_read_app(app):
        return Response({"detail": "Accès interdit."}, status=403)

    ns_totals, lineage = _load_needs_totals(app)
    if ns_totals is None:
        return Response(
            {"errors": [{
                "code": "NEEDS_SOURCE_MISSING",
                "message": (f"Le dossier {app.code} n'a pas de feuille de besoins ingérée. "
                            f"Téléversez-la via POST /api/credits/needs-sheet/parse/ "
                            f"avec application_code={app.code}."),
            }]},
            status=422,
        )

    currency: str = (data.get("currency") or app.currency or "USD").upper()
    # Financement par module (contrat §1) — % demandé par module. Les COÛTS
    # restent lus des DataRecord (`ns_totals`), jamais du payload (principe 1).
    from credits.dataio_simulator import normalize_module_financing
    module_financing = normalize_module_financing(
        data.get("module_financing") or data.get("moduleFinancing"))
    result = dataio_simulate(
        client=app.client,
        value_chain_code=app.value_chain.code if app.value_chain else None,
        needs_sheet=None,
        ns_totals=ns_totals,
        area_ha=float(app.area_ha) if app.area_ha else None,
        amount_requested=float(app.amount_requested) if app.amount_requested else None,
        currency=currency,
        guarantees_data=None,
        module_financing=module_financing,
    )
    if "error" in result:
        return Response({"detail": result["error"]}, status=400)

    result["needsSource"] = lineage
    return Response(result)


# ── 5. Re-scoring d'un dossier existant ───────────────────────────────────────

@api_view(["POST"])
@permission_classes([IsAuthenticated, CanInstructCredit])
def score_application(request: Request, code: str) -> Response:
    """
    POST /api/credits/applications/<code>/score/

    Calcule (ou recalcule) le score d'un dossier existant et le persiste dans score_result.
    Réservé au staff (permission 'analyst' ou 'agent').

    Les montants par module sont relus dans les `DataRecord` de la révision courante
    de la feuille de besoins ; le rapport conserve `needs_source_id + revision +
    sha256` — une analyse est rejouable à l'identique des mois plus tard.
    """
    from credits.models import CreditApplication
    from credits.scoring import CreditScoringEngine

    try:
        app = CreditApplication.objects.select_related(
            "client__kyc_profile", "value_chain", "needs_sheet", "needs_source"
        ).get(code=code)
    except CreditApplication.DoesNotExist:
        return Response({"detail": "Dossier introuvable."}, status=404)

    needs_totals, lineage = _load_needs_totals(app)

    engine = CreditScoringEngine(app, needs_totals=needs_totals)
    result = engine.compute()
    result["needsSource"] = lineage
    result["needsTotals"] = needs_totals or {}

    app.score_result = result
    app.save(update_fields=["score_result", "updated_at"])

    return Response(result)


# ── Dossiers : liste et détail ────────────────────────────────────────────────

@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def list_applications(request: Request) -> Response:
    """
    GET  /api/credits/applications/           → liste (filtrée par rôle)
    POST /api/credits/applications/           → crée un dossier DRAFT

    Filtres (GET) : `status`, `client_sub` (staff), `value_chain_code`,
    `agency` (staff — code d'agence, ou `none` pour les dossiers sans agence).
    AUCUN filtre ne s'applique par défaut : un dossier sans agence reste servi
    tant que `?agency=` n'est pas demandé explicitement.

    Les DEUX méthodes s'arrêtent à l'authentification, et c'est délibéré : la
    lecture est cloisonnée par `ViewContextService.filter_qs` (un client ne voit
    que ses dossiers), et la création est l'acte normal d'un demandeur.
    `CapaciteSelonMethode(GET="read", POST="create")` aurait paru plus fin mais
    aurait FERMÉ le dépôt de dossier à `client`, `agri_op` et `partner`, qui ne
    portent pas `create` au registre RBAC — un durcissement déguisé en migration.
    """
    from credits.models import CreditApplication
    from credits.workflow import serialize_application

    vcs = _vcs(request)
    qs = CreditApplication.objects.select_related(
        "client__kyc_profile", "value_chain", "needs_sheet", "agency"
    ).prefetch_related("guarantees")

    qs = vcs.filter_qs(qs)

    # Filtres query params
    if status_filter := request.query_params.get("status"):
        qs = qs.filter(status=status_filter)
    if client_sub := request.query_params.get("client_sub"):
        if vcs.is_staff:
            qs = qs.filter(client__sub=client_sub)
    if vc_code := request.query_params.get("value_chain_code"):
        qs = qs.filter(value_chain__code=vc_code)

    if request.method == "POST":
        return _create_application(request)

    # Filtre agence — après le POST : c'est un filtre de LECTURE, il n'a rien à
    # dire d'une création, et il peut refuser la requête (403/400).
    if raw_agency := request.query_params.get("agency"):
        qs, refus = _filtre_agence(qs, raw_agency, vcs)
        if refus is not None:
            return refus

    apps = qs.order_by("-created_at")[:100]
    return Response([vcs.serialize_for_role(a) for a in apps])


#: Valeurs de `?agency=` qui désignent la population SANS agence.
#: Cette population doit rester ATTEIGNABLE : c'est elle que le tableau de bord
#: d'agence rattache par approximation, et donc elle qu'un responsable doit
#: pouvoir lister pour la faire corriger. Sans sentinelle, on saurait la compter
#: (`scope.dossiers.approche`) sans jamais pouvoir l'ouvrir.
_AGENCE_ABSENTE = {"none", "null", "aucune", "sans", "-"}


def _filtre_agence(qs, brut: str, vcs) -> tuple[Any, Response | None]:
    """Applique `?agency=` — retourne `(queryset, None)` ou `(qs, Response)`.

    Réservé au personnel : un client ne voit déjà que ses propres dossiers, et
    lui offrir un filtre par agence l'inviterait à sonder la répartition interne
    de l'institution.

    Un code inconnu est REFUSÉ, pas filtré en silence : « 0 dossier » et
    « cette agence n'existe pas » ne portent pas la même information, et la
    première se lit comme une agence sans activité — exactement le genre de zéro
    qu'un responsable croit sur parole.
    """
    if not vcs.is_staff:
        return qs, Response(
            {"detail": "Le filtre par agence est réservé au personnel.",
             "code": "AGENCY_FILTER_STAFF_ONLY"},
            status=403,
        )

    valeur = (brut or "").strip()
    if valeur.lower() in _AGENCE_ABSENTE:
        return qs.filter(agency__isnull=True), None

    from agencies.models import Agency
    agence = Agency.objects.filter(code__iexact=valeur).first()
    if agence is None:
        return qs, Response(
            {"detail": (
                f"Aucune agence ne porte le code « {valeur} ». Utilisez un code "
                f"d'agence existant, ou `agency=none` pour les dossiers sans "
                f"rattachement."
            ), "code": "AGENCY_NOT_FOUND"},
            status=400,
        )
    return qs.filter(agency=agence), None


def _create_application(request: Request) -> Response:
    """
    POST /api/credits/applications/
    Crée un dossier de crédit en DRAFT.

    Corps JSON :
      client_sub          (str, optionnel — sinon = requester_sub)
      value_chain_code    (str, optionnel)
      area_ha             (float, optionnel)
      currency            (str, défaut USD)
      amount_requested    (float, requis)
      needs_sheet_id      (int, optionnel)
      guarantee_type      (str, optionnel: epargne|morale)
    """
    from credits.models import CreditApplication, NeedsSheet, resolve_agency_for_sub
    from accounts.models import FintechUser
    from reference_data.models import ValueChain

    data = request.data
    requester_sub: str = getattr(request.user, "sub", "") or ""
    client_sub: str = data.get("client_sub") or requester_sub

    if not client_sub:
        return Response({"detail": "client_sub est requis."}, status=400)

    # Seul un agent peut créer pour un tiers
    if client_sub != requester_sub and not _require_group(request, CAN_INSTRUCT):
        return Response({"detail": "Permission 'agent' requise."}, status=403)

    try:
        client = FintechUser.objects.get(sub=client_sub)
    except FintechUser.DoesNotExist:
        return Response({"detail": "Client introuvable.", "code": "CLIENT_NOT_FOUND"}, status=404)

    # Un membre interne ne peut pas être le BÉNÉFICIAIRE d'un crédit : il
    # instruirait, approuverait ou décaisserait sa propre demande. C'est le même
    # principe que maker ≠ checker, appliqué à la racine — un contrôle
    # d'indépendance ne tient pas si l'intéressé est des deux côtés du dossier.
    # Le message dit quoi faire, pas seulement ce qui est refusé : le crédit doit
    # être assigné à un client, via `client_sub`.
    if client.is_staff_role:
        return Response(
            {"detail": (
                f"« {client.full_name or client.sub} » est un membre interne "
                f"(rôle « {client.role} ») : un membre du personnel ne peut pas être "
                "bénéficiaire d'un crédit AGRICAP — il serait juge et partie sur son "
                "propre dossier. Assignez ce crédit à un client en précisant son "
                "`client_sub`."
            ), "code": "BENEFICIAIRE_INTERNE"},
            status=422,
        )

    try:
        amount_requested = _to_decimal(data.get("amount_requested", 0) or 0)
    except (ValueError, TypeError):
        return Response({"detail": "amount_requested invalide."}, status=400)

    if amount_requested <= 0:
        return Response({"detail": "Le montant demandé doit être positif."}, status=400)

    # Filière
    vc = None
    if vc_code := data.get("value_chain_code"):
        try:
            vc = ValueChain.objects.get(code=vc_code, active=True)
        except ValueChain.DoesNotExist:
            return Response({"detail": f"Filière '{vc_code}' introuvable."}, status=404)

    # ── Feuille de besoins ────────────────────────────────────────────────────
    # `uploaded_by` est FILTRÉ, et ce n'est pas une précaution de style.
    #
    # `NeedsSheet.pk` est un entier séquentiel, et cette résolution était nue
    # (`objects.get(pk=...)`). N'importe quel membre pouvait donc rattacher à SON
    # dossier la feuille de besoins d'un autre, puis la lire par la porte de
    # devant : `GET /applications/<son propre code>/analysis-report/` sert
    # `serialize_analysis_report(app.needs_sheet)` — les lignes du classeur
    # d'autrui (libellés, quantités, prix, écarts) et jusqu'aux commentaires
    # internes de l'analyste. Le contrôle d'étanchéité de cette vue ne pouvait
    # rien voir : le DOSSIER interrogé appartient bien à l'appelant. C'est la
    # référence entrante qui n'était pas vérifiée, pas la lecture.
    #
    # Deux origines légitimes, et deux seulement : le client a téléversé sa
    # feuille lui-même, ou l'agent qui monte le dossier l'a fait pour lui
    # (`parse_needs_sheet_view` pose `uploaded_by` = sub du DÉPOSANT, d'où les
    # deux valeurs admises et non la seule `client_sub`).
    #
    # Un identifiant qui existe mais n'appartient à aucun des deux répond comme
    # un identifiant inexistant : rien ne doit renseigner sur le nombre de
    # feuilles en base ni sur celles d'autrui.
    ns = None
    if ns_id := data.get("needs_sheet_id"):
        try:
            ns = NeedsSheet.objects.get(pk=int(ns_id),
                                        uploaded_by__in={client_sub, requester_sub})
        except (NeedsSheet.DoesNotExist, ValueError):
            return Response({"detail": "Feuille de besoins introuvable."}, status=404)

    area_ha = None
    if raw_area := data.get("area_ha"):
        try:
            area_ha = _to_decimal(raw_area)
        except (ValueError, TypeError):
            pass

    guarantee_type = data.get("guarantee_type") or ""

    # ── Agence d'instruction ──────────────────────────────────────────────────
    # Déduite du COMPTE QUI CRÉE le dossier, jamais lue dans le payload : un
    # `agency` accepté depuis le corps de la requête laisserait un agent
    # rattacher son dossier à n'importe quelle agence, et le périmètre de chaque
    # responsable deviendrait déclaratif au lieu d'être constaté.
    #
    # Indéterminable → le champ reste VIDE. Deux cas légitimes : le client qui
    # dépose lui-même sa demande (aucune affectation, et il n'en a pas à avoir),
    # et l'agent dont l'affectation n'est pas renseignée. Dans les deux cas, une
    # agence par défaut serait une invention : elle gonflerait le portefeuille
    # d'une agence qui n'a jamais vu le dossier, sans qu'aucun écran ne puisse le
    # détecter. Le dossier reste visible partout (`agency` ne filtre nulle part
    # sans demande explicite) et le tableau de bord agence le rattache par
    # approximation, en le disant (`scope.rattachement`).
    agency = resolve_agency_for_sub(requester_sub)
    if agency is None and requester_sub != client_sub:
        # Un agent qui monte un dossier POUR un tiers devrait avoir une agence :
        # son absence est une donnée manquante côté RBAC, pas un cas normal.
        logger.warning(
            "Dossier créé par « %s » pour « %s » sans agence de rattachement "
            "(ni `StaffProfile.assignment`, ni `Agency.manager_sub`) : le dossier "
            "restera hors du périmètre exact de toute agence.",
            requester_sub, client_sub,
        )

    # Génération du code automatique
    from datetime import date
    import random, string
    today = date.today()
    suffix = ''.join(random.choices(string.digits, k=4))
    code = f"CRED-{today.strftime('%Y%m%d')}-{suffix}"
    while CreditApplication.objects.filter(code=code).exists():
        suffix = ''.join(random.choices(string.digits, k=4))
        code = f"CRED-{today.strftime('%Y%m%d')}-{suffix}"

    app = CreditApplication.objects.create(
        code=code,
        client=client,
        initiated_by_sub=requester_sub,
        agency=agency,
        value_chain=vc,
        area_ha=area_ha,
        currency=(data.get("currency") or "USD").upper(),
        amount_requested=amount_requested,
        needs_sheet=ns,
        guarantee_type=guarantee_type if guarantee_type in ("epargne", "morale") else "",
        status=CreditApplication.Status.DRAFT,
        prefill_snapshot=data.get("prefill_snapshot") or {},
    )

    from credits.workflow import serialize_application
    vcs = _vcs(request)
    return Response(vcs.serialize_for_role(app), status=201)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def application_detail(request: Request, code: str) -> Response:
    """GET /api/credits/applications/<code>/

    Entrée authentifiée ; le cloisonnement par dossier (`can_read_app`) porte sur
    l'objet et reste dans le corps.
    """
    from credits.models import CreditApplication
    from credits.workflow import serialize_application

    try:
        app = CreditApplication.objects.select_related(
            "client__kyc_profile", "value_chain", "needs_sheet", "agency"
        ).prefetch_related("guarantees").get(code=code)
    except CreditApplication.DoesNotExist:
        return Response({"detail": "Dossier introuvable."}, status=404)

    vcs = _vcs(request)
    if not vcs.can_read_app(app):
        return Response({"detail": "Accès interdit."}, status=403)

    return Response(vcs.serialize_for_role(app))


# ── Workflow ──────────────────────────────────────────────────────────────────

def _load_app(code: str, select_related: list[str] | None = None):
    from credits.models import CreditApplication
    qs = CreditApplication.objects.select_related(
        # `agency` est sérialisé par `serialize_application` sur TOUTES les
        # réponses du workflow : sans jointure, chaque transition ajoutait une
        # requête pour lire le code de l'agence.
        "client__kyc_profile", "value_chain", "needs_sheet", "agency",
        *(select_related or [])
    )
    try:
        return qs.get(code=code)
    except CreditApplication.DoesNotExist:
        return None


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def submit_application(request: Request, code: str) -> Response:
    """POST /api/credits/applications/<code>/submit/

    Deux acteurs légitimes — le TITULAIRE et l'agent instructeur : la condition
    porte sur le dossier, donc elle reste dans le corps. Le décorateur porte ce
    qui est vrai de la vue elle-même : il faut être authentifié.
    """
    app = _load_app(code)
    if not app:
        return Response({"detail": "Dossier introuvable."}, status=404)

    sub = getattr(request.user, "sub", "") or ""
    is_owner = str(app.client.sub) == sub
    is_agent = in_group(request, CAN_INSTRUCT)

    if not (is_owner or is_agent):
        return Response({"detail": "Accès interdit."}, status=403)

    from credits.workflow import submit, WorkflowError
    try:
        submit(app, submitter_sub=sub)
    except WorkflowError as exc:
        # Statut porté par la règle : 409 pour un dossier déjà soumis (conflit
        # d'état), 422 pour un dossier incomplet (principe 5).
        return _workflow_error(exc)

    from credits.workflow import serialize_application
    # Audience MIXTE (titulaire ou agent) : `pour_staff` se CALCULE, il ne
    # s'affirme pas. Un `True` en dur ici enverrait au demandeur la pièce
    # d'identité de son garant — exactement ce que le masquage par défaut
    # protège.
    return Response(serialize_application(app, pour_staff=_vcs(request).is_staff))


@api_view(["POST"])
@permission_classes([IsAuthenticated, CanInstructCredit])
def start_analysis(request: Request, code: str) -> Response:
    """POST /api/credits/applications/<code>/start-analysis/"""
    app = _load_app(code)
    if not app:
        return Response({"detail": "Dossier introuvable."}, status=404)

    from credits.workflow import start_analysis as _start, WorkflowError
    try:
        _start(app, analyst_sub=getattr(request.user, "sub", "") or "")
    except WorkflowError as exc:
        return _workflow_error(exc)

    from credits.workflow import serialize_application
    return Response(serialize_application(app, pour_staff=True))


@api_view(["POST"])
@permission_classes([IsAuthenticated, PeutDecider])
def approve_application(request: Request, code: str) -> Response:
    """
    POST /api/credits/applications/<code>/approve/
    Corps JSON : { amount_approved, comment? }

    `PeutDecider` (= `CAN_DECIDE`) n'est que le droit d'ENTRÉE. Le plafond de
    délégation et le maker ≠ checker restent tranchés par `workflow.approve` :
    ils dépendent du montant et de qui a soumis, pas du rôle seul.
    """
    app = _load_app(code)
    if not app:
        return Response({"detail": "Dossier introuvable."}, status=404)

    data = request.data
    if not data.get("amount_approved"):
        return Response({"detail": "amount_approved requis."}, status=400)

    import decimal
    try:
        amount = decimal.Decimal(str(data["amount_approved"]))
    except Exception:
        return Response({"detail": "amount_approved invalide."}, status=400)

    roles = _roles(request)
    from credits.workflow import approve, WorkflowError
    try:
        approve(
            app,
            approver_sub=getattr(request.user, "sub", "") or "",
            amount_approved=amount,
            comment=data.get("comment", ""),
            approver_roles=roles,
        )
    except WorkflowError as exc:
        return _workflow_error(exc)

    from credits.workflow import serialize_application
    return Response(serialize_application(app, pour_staff=True))


@api_view(["POST"])
@permission_classes([IsAuthenticated, PeutDecider])
def reject_application(request: Request, code: str) -> Response:
    """
    POST /api/credits/applications/<code>/reject/
    Corps JSON : { reason_code, comment? }
    """
    app = _load_app(code)
    if not app:
        return Response({"detail": "Dossier introuvable."}, status=404)

    data = request.data
    if not data.get("reason_code"):
        return Response({"detail": "reason_code requis."}, status=400)

    from credits.workflow import reject, WorkflowError
    try:
        result = reject(
            app,
            rejector_sub=getattr(request.user, "sub", "") or "",
            reason_code=data["reason_code"],
            comment=data.get("comment", ""),
        )
    except WorkflowError as exc:
        return _workflow_error(exc)

    return Response(result)


@api_view(["POST"])
@permission_classes([IsAuthenticated, CanInstructCredit])
def adjourn_application(request: Request, code: str) -> Response:
    """
    POST /api/credits/applications/<code>/adjourn/
    Corps JSON : { comment }
    """
    app = _load_app(code)
    if not app:
        return Response({"detail": "Dossier introuvable."}, status=404)

    comment = request.data.get("comment", "")
    from credits.workflow import adjourn, WorkflowError
    try:
        adjourn(app, approver_sub=getattr(request.user, "sub", "") or "", comment=comment)
    except WorkflowError as exc:
        return _workflow_error(exc)

    from credits.workflow import serialize_application
    return Response(serialize_application(app, pour_staff=True))


@api_view(["POST"])
@permission_classes([IsAuthenticated, CanInstructCredit])
def reopen_analysis(request: Request, code: str) -> Response:
    """POST /api/credits/applications/<code>/reopen-analysis/"""
    app = _load_app(code)
    if not app:
        return Response({"detail": "Dossier introuvable."}, status=404)

    from credits.workflow import reopen_analysis as _reopen, WorkflowError
    try:
        _reopen(app, analyst_sub=getattr(request.user, "sub", "") or "")
    except WorkflowError as exc:
        return _workflow_error(exc)

    from credits.workflow import serialize_application
    return Response(serialize_application(app, pour_staff=True))


@api_view(["POST"])
@permission_classes([IsAuthenticated, PeutDecider])
def close_application(request: Request, code: str) -> Response:
    """POST /api/credits/applications/<code>/close/ — ACTIVE → CLOSED.

    Corps JSON : `{ comment }` — motif OBLIGATOIRE (`workflow.close` refuse un
    motif vide) : « soldé à l'échéance », « remboursement anticipé », « abandon
    de créance ». Deux ans plus tard, c'est cette phrase qui explique la fin du
    dossier.

    C'est l'UNIQUE déclencheur de la boucle d'apprentissage (principe 10). La
    transition existait, testée, et rien ne l'atteignait : un dossier décaissé
    restait `active` indéfiniment, `n_cas_reels` restait à zéro pour toutes les
    filières, et chaque analyse continuait d'annoncer « référentiel indicatif,
    fiabilité limitée » quel que soit le nombre de dossiers réellement bouclés.
    Un mécanisme sans porte n'est pas un mécanisme.

    `CAN_DECIDE` et non `CAN_INSTRUCT` : clore, c'est aussi passer un « abandon
    de créance ». Un agent de terrain monte des dossiers, il ne solde pas une
    dette — c'est la même frontière qu'à l'approbation, et elle vaut aussi à la
    sortie du cycle. La clôture reste un acte HUMAIN (principe 2) : le moteur
    n'en déduit aucune d'un solde à zéro, ce qui confondrait « remboursé » et
    « pas encore appelé ».
    """
    app = _load_app(code)
    if not app:
        return Response({"detail": "Dossier introuvable."}, status=404)

    from credits.workflow import close as _close, WorkflowError
    try:
        observation = _close(
            app,
            closer_sub=getattr(request.user, "sub", "") or "",
            comment=request.data.get("comment", ""),
        )
    except WorkflowError as exc:
        return _workflow_error(exc)

    from credits.workflow import serialize_application
    data = serialize_application(app, pour_staff=True)
    # Ce que la clôture a CAPITALISÉ, dit explicitement. Une clôture qui n'a rien
    # appris (filière sans référentiel actif, ventilation absente) est légitime —
    # `enregistrer_cloture` retourne `None` sans faire échouer l'acte — mais elle
    # doit se voir : sinon `n_cas_reels` stagne et personne ne sait pourquoi.
    data["closure"] = {
        "closedAt": app.closed_at.isoformat() if app.closed_at else None,
        "comment": app.closure_comment,
        "capitalisee": observation is not None,
        "observationId": observation.pk if observation else None,
        "referentiel": observation.referentiel.code if observation else None,
        # `contributive` : l'observation compte-t-elle dans `n_cas_reels` ?
        # Une observation figée sans ventilation de coûts exploitable est
        # conservée (principe 3) mais n'apprend rien à la filière.
        "contributive": bool(observation.contributive) if observation else False,
    }
    return Response(data)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def client_consent(request: Request, code: str) -> Response:
    """
    POST /api/credits/applications/<code>/client-consent/
    Corps JSON : { method? } — "app" | "sms" | "ussd"

    Le consentement est l'acte du titulaire : aucun rôle ne l'ouvre ni ne le
    ferme, seul le lien au dossier compte. Le garde est donc entièrement
    d'objet (`_assert_can_read_app`, en 404), et le décorateur ne porte que
    l'authentification.
    """
    app = _load_app(code)
    if not app:
        return Response({"detail": "Dossier introuvable."}, status=404)
    # `record_client_consent` refuse déjà un consentement donné par un tiers, mais son
    # refus (« Seul le client bénéficiaire… ») CONFIRME l'existence du dossier sondé.
    # Le cloisonnement doit répondre avant la règle métier, et répondre comme un dossier
    # absent.
    if (refus := _assert_can_read_app(request, app)) is not None:
        return refus

    sub = getattr(request.user, "sub", "") or ""
    method = request.data.get("method", "app")

    from credits.workflow import record_client_consent, WorkflowError
    try:
        record_client_consent(app, client_sub=sub, method=method)
    except WorkflowError as exc:
        return _workflow_error(exc)

    from credits.workflow import serialize_application
    # Audience mixte — et ici c'est le TITULAIRE qui est l'appelant normal :
    # `pour_staff` calculé, jamais affirmé.
    return Response(serialize_application(app, pour_staff=_vcs(request).is_staff))


@api_view(["POST"])
@permission_classes([IsAuthenticated, MembreDuGroupe(
    CAN_INSTRUCT,
    message=("Seule l'équipe d'instruction peut relancer une demande de "
             "confirmation client."),
    code="PERMISSION_REFUSEE")])
def renew_client_consent(request: Request, code: str) -> Response:
    """POST /api/credits/applications/<code>/renew-consent/

    Relance la demande de confirmation auprès du client quand le délai a expiré.
    Réservé à l'équipe d'instruction : c'est elle qui constate l'absence de
    réponse et recontacte le client. Le client, lui, confirme via
    `client-consent/`.
    """
    app = _load_app(code)
    if not app:
        return Response({"detail": "Dossier introuvable."}, status=404)

    from credits.workflow import renew_client_consent as _renew, WorkflowError
    try:
        _renew(app, agent_sub=getattr(request.user, "sub", "") or "")
    except WorkflowError as exc:
        return _workflow_error(exc)

    from credits.workflow import serialize_application
    return Response(serialize_application(app, pour_staff=True))


# ── Garanties ─────────────────────────────────────────────────────────────────

def _get_application(code: str):
    from credits.models import CreditApplication
    try:
        return CreditApplication.objects.select_related("client__kyc_profile").get(code=code)
    except CreditApplication.DoesNotExist:
        return None


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def list_guarantees(request: Request, code: str) -> Response:
    """GET /api/credits/applications/<code>/guarantees/

    Le titulaire du dossier et le personnel — personne d'autre. `get_guarantee_summary`
    porte le **nom, le téléphone et le numéro de pièce d'identité nationale** du garant,
    ainsi que les soldes d'épargne gagés : c'est la réponse la plus nominative de tout le
    module crédit. Elle n'était protégée que par `_require_read`, c'est-à-dire par rien —
    tout membre authentifié pouvait la lire sur n'importe quel `code`.
    """
    app = _get_application(code)
    if not app:
        return Response({"detail": "Dossier introuvable."}, status=404)
    if (refus := _assert_can_read_app(request, app)) is not None:
        return refus
    from credits.guarantees import get_guarantee_summary
    # Le titulaire ET le personnel atteignent cette vue : c'est LA vue à
    # audience mixte du module, celle pour laquelle `pour_staff` a été
    # introduit. L'agent a besoin de la pièce d'identité du garant (elle est la
    # preuve qu'il l'a vue) ; le demandeur n'en a aucun usage.
    return Response(get_guarantee_summary(app, pour_staff=_vcs(request).is_staff))


@api_view(["POST"])
@permission_classes([IsAuthenticated, CanInstructCredit])
def place_savings_guarantee(request: Request, code: str) -> Response:
    """
    POST /api/credits/applications/<code>/guarantees/savings/

    Corps JSON : { savings_plan_id, amount, notes? }
    """
    app = _get_application(code)
    if not app:
        return Response({"detail": "Dossier introuvable."}, status=404)

    data = request.data
    savings_plan_id = data.get("savings_plan_id")
    amount_raw = data.get("amount")
    if not savings_plan_id or not amount_raw:
        return Response({"detail": "savings_plan_id et amount sont requis."}, status=400)

    import decimal
    try:
        amount = decimal.Decimal(str(amount_raw))
        if amount <= 0:
            raise ValueError
    except Exception:
        return Response({"detail": "amount invalide (doit être > 0)."}, status=400)

    from credits.guarantees import (
        place_savings_hold, InsufficientSavingsError, GuaranteeError
    )
    try:
        place_savings_hold(
            application=app,
            savings_plan_id=int(savings_plan_id),
            amount=amount,
            registered_by_sub=getattr(request.user, "sub", "") or "",
            notes=data.get("notes", ""),
        )
    except InsufficientSavingsError as exc:
        return Response({"detail": str(exc), "code": "INSUFFICIENT_BALANCE",
                         "errors": [{"code": "INSUFFICIENT_BALANCE", "message": str(exc)}]}, status=422)
    except GuaranteeError as exc:
        return Response({"detail": str(exc), "code": exc.code,
                         "errors": [{"code": exc.code, "message": str(exc)}]}, status=400)

    from credits.guarantees import get_guarantee_summary
    return Response(get_guarantee_summary(app, pour_staff=True), status=201)


@api_view(["POST"])
@permission_classes([IsAuthenticated, CanInstructCredit])
def register_moral_guarantee(request: Request, code: str) -> Response:
    """
    POST /api/credits/applications/<code>/guarantees/moral/

    Corps JSON :
      guarantor_sub                (sub du garant — REQUIS, il doit pouvoir consentir)
      guarantor_name, guarantor_phone, guarantor_id_number
      montant_couvert?             (défaut : le montant du dossier)
      notes?

    Les permissions de cet endpoint sont INCHANGÉES (`CAN_INSTRUCT`) : qui, du
    client ou de l'agent, désigne le garant est une décision de gouvernance qui
    n'appartient pas au backend — cf. le rapport de lot. Le mécanisme fonctionne
    des deux côtés ; seul ce garde-fou tranche aujourd'hui.
    """
    app = _get_application(code)
    if not app:
        return Response({"detail": "Dossier introuvable."}, status=404)

    data = request.data
    required = ["guarantor_name", "guarantor_phone", "guarantor_id_number"]
    missing = [f for f in required if not (data.get(f) or "").strip()]
    if missing:
        return Response({"detail": f"Champs requis : {', '.join(missing)}."}, status=400)

    montant_raw = data.get("montant_couvert")
    montant = None
    if montant_raw is not None:
        import decimal
        try:
            montant = decimal.Decimal(str(montant_raw))
        except Exception:
            return Response({"detail": "montant_couvert invalide."}, status=400)

    from credits.guarantees import register_moral_guarantee as _register, GuaranteeError
    from credits.guarantor import GuarantorError

    try:
        _register(
            application=app,
            guarantor_name=data["guarantor_name"],
            guarantor_phone=data["guarantor_phone"],
            guarantor_id_number=data["guarantor_id_number"],
            registered_by_sub=getattr(request.user, "sub", "") or "",
            guarantor_sub=data.get("guarantor_sub", ""),
            montant_couvert=montant,
            notes=data.get("notes", ""),
        )
    except GuarantorError as exc:
        # Chaque règle de capacité porte son code et son statut (cf.
        # `credits.guarantor`) : le front guide le client au lieu d'afficher
        # « erreur », et une reformulation de message ne casse rien.
        return Response(
            {"detail": str(exc), "code": exc.code, "errors": exc.as_errors()},
            status=exc.http_status,
        )
    except GuaranteeError as exc:
        return Response(
            {"detail": str(exc), "code": exc.code,
             "errors": [{"code": exc.code, "message": str(exc)}]},
            status=422,
        )

    from credits.guarantees import get_guarantee_summary
    return Response(get_guarantee_summary(app, pour_staff=True), status=201)


# ── Demandes de caution du garant (SPEC §2.5) ─────────────────────────────────

@api_view(["GET"])
@permission_classes([IsAuthenticated])
def list_guarantee_requests(request: Request) -> Response:
    """GET /api/credits/guarantee-requests/

    Les demandes de caution dont l'utilisateur connecté est le garant désigné.
    Aucun rôle n'élargit ce périmètre : la liste est celle de SES engagements.

    Les demandes expirées, consenties et refusées sont servies avec les autres —
    le front doit pouvoir afficher « expirée » sans l'inférer d'une date passée
    (aucun chiffre ni statut métier calculé côté client).
    """
    from credits.guarantees import guarantee_requests_for, serialize_guarantee_request
    from credits.guarantor import consent_window_hours

    status_filter = request.query_params.get("status", "")
    requests_qs = guarantee_requests_for(request.user, status=status_filter)
    items = [serialize_guarantee_request(g) for g in requests_qs]

    return Response({
        "total_rows": len(items),
        # Fenêtre CONFIGURÉE, jamais une constante : le front décompte dessus et
        # n'écrit « 72 h » nulle part (principe 8 jusque dans l'affichage).
        "consent_window_hours": consent_window_hours(),
        "items": items,
    })


@api_view(["POST"])
@permission_classes([IsAuthenticated, IsDesignatedGuarantor])
def consent_guarantee_request(request: Request, guarantee_id: int) -> Response:
    """POST /api/credits/guarantee-requests/<id>/consent/  {"accept": bool}

    L'acte par lequel une caution devient opposable. La permission
    `IsDesignatedGuarantor` est déclarative et s'exécute avant ce corps : le
    contrôle d'identité ne dépend pas de l'ordre des `return` ci-dessous.
    """
    from credits.guarantees import record_guarantor_consent, serialize_guarantee_request
    from credits.guarantor import GuarantorError
    from credits.models import CreditGuarantee

    accept = (request.data or {}).get("accept")
    if not isinstance(accept, bool):
        return Response(
            {"detail": "Le champ `accept` est requis et doit valoir true ou false.",
             "code": "ACCEPT_REQUIRED",
             "errors": [{"code": "ACCEPT_REQUIRED",
                         "message": "Réponse attendue : accepter ou refuser."}]},
            status=400,
        )

    guarantee = (
        CreditGuarantee.objects
        .select_related("application__client", "application__value_chain", "guarantor")
        .filter(pk=guarantee_id,
                guarantee_type=CreditGuarantee.GuaranteeType.MORALE)
        .first()
    )
    if guarantee is None:
        return Response({"detail": "Demande de caution introuvable."}, status=404)

    try:
        record_guarantor_consent(
            guarantee,
            responder_sub=str(getattr(request.user, "pk", "")),
            accept=accept,
            channel="app",
            ip=_client_ip(request),
        )
    except GuarantorError as exc:
        # `code` et statut HTTP viennent de la règle, jamais de la vue —
        # même discipline que `_workflow_error`.
        return Response(
            {"detail": str(exc), "code": exc.code, "errors": exc.as_errors()},
            status=exc.http_status,
        )

    guarantee.refresh_from_db()
    return Response({
        "detail": "Consentement enregistré." if accept else "Refus enregistré.",
        "item": serialize_guarantee_request(guarantee),
    })


def _client_ip(request: Request) -> str | None:
    """IP d'origine, journalisée dans `consent_meta` comme preuve d'origine.

    `X-Forwarded-For` est renseigné par le reverse proxy du VPS ; on prend la
    première entrée (le client), pas la dernière (le proxy).
    """
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if forwarded:
        return forwarded.split(",")[0].strip() or None
    return request.META.get("REMOTE_ADDR") or None


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def confirm_guarantee(request: Request, code: str, guarantee_id: int) -> Response:
    """
    POST /api/credits/applications/<code>/guarantees/<id>/confirm/

    Confirmation par le garant (ou un agent avec preuve physique).

    Deux acteurs légitimes, et deux seulement : l'équipe d'instruction, et le GARANT
    désigné de cette caution précise. Le garde d'origine (`_require_read`) n'en vérifiait
    aucun : le dossier était résolu par son code, la caution par son identifiant, et tout
    membre authentifié pouvait rendre opposable — au nom d'autrui — la caution d'un
    dossier qui ne le concernait pas. C'est l'inverse exact de ce que le consentement
    établit (cf. `IsDesignatedGuarantor`).

    Le décorateur s'arrête à l'authentification : les deux acteurs admis se
    définissent PAR RAPPORT À LA CAUTION VISÉE (« le garant désigné de
    celle-ci ») — ce que seule la vue, qui l'a chargée, peut trancher.
    """
    app = _get_application(code)
    if not app:
        return Response({"detail": "Dossier introuvable."}, status=404)

    from credits.models import CreditGuarantee
    try:
        guarantee = CreditGuarantee.objects.get(pk=guarantee_id, application=app)
    except CreditGuarantee.DoesNotExist:
        return Response({"detail": "Garantie introuvable."}, status=404)

    # Deux refus DISTINCTS, et la distinction est délibérée :
    #
    #  - un inconnu (ni garant, ni agent, ni titulaire) reçoit « Dossier introuvable » en
    #    404, exactement comme sur un code inexistant : lui répondre 403 lui apprendrait
    #    que le dossier existe, et le code s'énumère ;
    #  - le TITULAIRE reçoit un 403 explicite. Il sait déjà que son dossier existe : rien
    #    ne fuite, et il a droit à la raison — confirmer sa propre caution reviendrait à
    #    se porter garant de soi-même.
    #
    # `guarantor_id` est le lien opposable ; les cautions déclaratives historiques n'en
    # portent pas, et restent donc du seul ressort de l'agent.
    est_garant = (
        guarantee.guarantor_id is not None
        and str(guarantee.guarantor_id) == str(getattr(request.user, "pk", ""))
    )
    if not (est_garant or _require_group(request, CAN_INSTRUCT)):
        if (refus := _assert_can_read_app(request, app)) is not None:
            return refus
        return Response(
            {"detail": "Seul le garant désigné ou un agent instructeur peut confirmer "
                       "cette garantie.",
             "code": "CONFIRMATION_NON_AUTORISEE"},
            status=403,
        )

    from credits.guarantees import (
        GuaranteeError, confirm_asset_guarantee, confirm_moral_guarantee,
    )
    from credits.guarantor import GuarantorError

    confirmer = getattr(request.user, "sub", "") or ""
    try:
        if guarantee.guarantee_type in CreditGuarantee.ASSET_BACKED_TYPES:
            # Le gage effectif de l'actif est posé ici, sous verrou atomique —
            # réservé au staff : un client ne nantit pas son propre actif.
            if not _require_group(request, CAN_INSTRUCT):
                return Response(
                    {"detail": "La confirmation d'un gage est réservée aux agents."},
                    status=403,
                )
            confirm_asset_guarantee(guarantee, confirmer_sub=confirmer)
        else:
            confirm_moral_guarantee(guarantee, confirmer_sub=confirmer)
    except GuarantorError as exc:
        return Response(
            {"detail": str(exc), "code": exc.code, "errors": exc.as_errors()},
            status=exc.http_status,
        )
    except GuaranteeError as exc:
        return Response({"detail": str(exc)}, status=400)

    from credits.guarantees import get_guarantee_summary
    # Deux acteurs, dont un NON-staff : le garant désigné confirme sa propre
    # caution. Il n'a pas à recevoir pour autant la pièce d'identité des AUTRES
    # garants du dossier — d'où le calcul plutôt que le `True`.
    return Response(get_guarantee_summary(app, pour_staff=_vcs(request).is_staff))


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def place_asset_guarantee_view(request: Request, code: str) -> Response:
    """
    POST /api/credits/applications/<code>/guarantees/asset/
    Corps JSON : { asset_id }

    Propose un actif vérifié du client en garantie. L'actif n'est effectivement
    nanti qu'à la confirmation par un agent.

    Entrée authentifiée : proposer est l'acte du propriétaire du dossier, ou de
    l'agent qui l'instruit — condition d'objet, donc dans le corps.
    """
    app = _get_application(code)
    if not app:
        return Response({"detail": "Dossier introuvable."}, status=404)

    # Le propriétaire du dossier ou un agent qui l'instruit
    sub = getattr(request.user, "sub", "") or ""
    if str(app.client.sub) != sub and not _require_group(request, CAN_INSTRUCT):
        return Response({"detail": "Permission refusée."}, status=403)

    asset_id = (request.data or {}).get("asset_id")
    if not asset_id:
        return Response({"detail": "asset_id requis."}, status=400)

    from credits.guarantees import GuaranteeError, place_asset_guarantee

    try:
        place_asset_guarantee(app, asset_id=int(asset_id), registered_by_sub=sub)
    except GuaranteeError as exc:
        # Chaque règle porte son propre code (cf. `credits.guarantees`) : le front
        # branche sur `code`, jamais sur la formulation de `detail`.
        return Response(
            {"detail": str(exc), "code": exc.code,
             "errors": [{"code": exc.code, "message": str(exc)}]},
            status=422,
        )
    except (TypeError, ValueError):
        return Response({"detail": "asset_id invalide."}, status=400)

    from credits.guarantees import get_guarantee_summary
    # Le propriétaire du dossier propose son propre actif : audience mixte.
    return Response(get_guarantee_summary(app, pour_staff=_vcs(request).is_staff),
                    status=201)


@api_view(["POST"])
@permission_classes([IsAuthenticated, CanInstructCredit])
def release_guarantee(request: Request, code: str, guarantee_id: int) -> Response:
    """
    POST /api/credits/applications/<code>/guarantees/<id>/release/

    Libère une garantie épargne (rejet ou annulation du dossier).
    Réservé aux agents.
    """
    app = _get_application(code)
    if not app:
        return Response({"detail": "Dossier introuvable."}, status=404)

    from credits.models import CreditGuarantee
    try:
        guarantee = CreditGuarantee.objects.get(pk=guarantee_id, application=app)
    except CreditGuarantee.DoesNotExist:
        return Response({"detail": "Garantie introuvable."}, status=404)

    from credits.guarantees import _do_release, release_savings_hold, GuaranteeError
    if guarantee.guarantee_type == CreditGuarantee.GuaranteeType.EPARGNE:
        try:
            release_savings_hold(guarantee)
        except GuaranteeError as exc:
            return Response({"detail": str(exc)}, status=400)
    else:
        # Passe par `_do_release` : une écriture directe de `status` laissait
        # l'actif sous-jacent nanti indéfiniment, donc ingageable ailleurs.
        _do_release(guarantee)

    from credits.guarantees import get_guarantee_summary
    return Response(get_guarantee_summary(app, pour_staff=True))


# ── Décaissement (Étape 6) ────────────────────────────────────────────────────

#: Champs du décaissement qui n'appartiennent pas au client — mêmes deux familles que
#: `view_context._CLIENT_HIDDEN_FIELDS` : les identifiants des PERSONNES qui ont manipulé
#: le dossier (le maker et le checker du décaissement — les nommer, c'est désigner qui
#: solliciter hors procédure) et les références de RATTACHEMENT COMPTABLE, internes par
#: nature. Restent servis : statut, montant, devise, dates — ce que le bénéficiaire a le
#: droit de savoir de son propre argent.
_DISBURSEMENT_STAFF_FIELDS = ("requestedBySub", "confirmedBySub", "journalEntryId", "notes")


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def disbursement_detail(request: Request, code: str) -> Response:
    """GET /api/credits/applications/<code>/disbursement/

    Le titulaire suit SON décaissement ; le personnel voit la chaîne complète. Avant, ni
    l'un ni l'autre n'était vérifié : `_require_read` puis `_load_app(code)` livraient à
    tout membre authentifié, sur n'importe quel code de dossier, l'identité de l'agent
    demandeur et celle du confirmateur — donc la paire maker/checker d'un décaissement
    réel — plus sa référence d'écriture comptable.
    """
    app = _load_app(code)
    if not app:
        return Response({"detail": "Dossier introuvable."}, status=404)
    if (refus := _assert_can_read_app(request, app)) is not None:
        return refus
    from credits.disbursement import serialize_disbursement
    data = serialize_disbursement(app)
    if data is None:
        return Response({"detail": "Aucune demande de décaissement pour ce dossier."}, status=404)
    if not _vcs(request).is_staff:
        data = {k: v for k, v in data.items() if k not in _DISBURSEMENT_STAFF_FIELDS}
    return Response(data)


@api_view(["POST"])
@permission_classes([IsAuthenticated, PeutDemanderUnDecaissement])
def request_disbursement_view(request: Request, code: str) -> Response:
    """
    POST /api/credits/applications/<code>/disbursement/request/
    Transition : APPROVED → PENDING_DISBURSEMENT (maker)
    """
    app = _load_app(code)
    if not app:
        return Response({"detail": "Dossier introuvable."}, status=404)

    from credits.disbursement import request_disbursement as _request, DisbursementError
    from credits.workflow import WorkflowError
    try:
        _request(
            app,
            requester_sub=getattr(request.user, "sub", "") or "",
            notes=request.data.get("notes", ""),
        )
    except (DisbursementError, WorkflowError) as exc:
        return _workflow_error(exc)

    from credits.workflow import serialize_application
    return Response(serialize_application(app, pour_staff=True), status=201)


@api_view(["POST"])
@permission_classes([IsAuthenticated, PeutConfirmerUnDecaissement])
def confirm_disbursement_view(request: Request, code: str) -> Response:
    """
    POST /api/credits/applications/<code>/disbursement/confirm/
    Transition : PENDING_DISBURSEMENT → ACTIVE (checker, maker≠checker)

    Le groupe donne l'entrée ; `maker ≠ checker` (le demandeur ne confirme pas)
    dépend de la demande elle-même et reste dans `disbursement.confirm_disbursement`.
    """
    app = _load_app(code)
    if not app:
        return Response({"detail": "Dossier introuvable."}, status=404)

    from credits.disbursement import confirm_disbursement as _confirm, DisbursementError
    from credits.workflow import WorkflowError
    try:
        result = _confirm(app, confirmer_sub=getattr(request.user, "sub", "") or "")
    except DisbursementError as exc:
        return _workflow_error(exc)
    except WorkflowError as exc:
        return _workflow_error(exc)

    return Response(result)


@api_view(["POST"])
@permission_classes([IsAuthenticated, PeutDemanderUnDecaissement])
def cancel_disbursement_view(request: Request, code: str) -> Response:
    """
    POST /api/credits/applications/<code>/disbursement/cancel/
    Annule la demande PENDING (retour à APPROVED).
    """
    app = _load_app(code)
    if not app:
        return Response({"detail": "Dossier introuvable."}, status=404)

    from credits.disbursement import cancel_disbursement_request, DisbursementError
    from credits.workflow import WorkflowError
    try:
        cancel_disbursement_request(app, cancelled_by_sub=getattr(request.user, "sub", "") or "")
    except (DisbursementError, WorkflowError) as exc:
        return _workflow_error(exc)

    from credits.workflow import serialize_application
    return Response(serialize_application(app, pour_staff=True))


# ── Tableau de bord — Étape 7 ─────────────────────────────────────────────────

@api_view(["GET"])
@permission_classes([IsAuthenticated])
def credits_dashboard(request: Request) -> Response:
    """
    GET /api/credits/dashboard/

    Retourne des KPIs adaptés au rôle du demandeur.
    Chaque rôle reçoit une vue agrégée différente.

    Entrée authentifiée : le tri par rôle est le SUJET de la vue, pas son garde —
    `get_dashboard` choisit la lentille et lève `PermissionError` (403) sur une
    `?view=` que le rôle ne peut pas demander.
    """
    from credits.dashboard import get_dashboard

    sub: str = getattr(request.user, "sub", "") or ""
    roles: set[str] = set(_roles(request))
    view: str = request.query_params.get("view", "")

    try:
        return Response(get_dashboard(sub=sub, roles=roles, view=view))
    except PermissionError as exc:
        return Response({"detail": str(exc)}, status=403)


# ── Partie H : Rapport d'analyse documentaire ────────────────────────────────

@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated, GroupeSelonMethode(
    GET=None, POST=CAN_INSTRUCT,
    messages={"POST": "Seul un analyste peut mettre à jour un finding."})])
def analysis_report(request: Request, code: str) -> Response:
    """
    GET  /api/credits/applications/<code>/analysis-report/
         → Rapport d'analyse complet (findings, module summaries, chaîne de preuve).

    POST /api/credits/applications/<code>/analysis-report/
         → Décision analyste sur un finding : {finding_id, status, comment}
           status : justifie | corrige | confirme_anomalie

    Deux publics sur une route : le titulaire LIT son rapport, seul l'instructeur
    ÉCRIT une décision d'analyste. La matrice est sur le décorateur plutôt que
    dans un `if request.method` de corps — c'est précisément la forme de garde
    qu'un lecteur de la vue ne voyait pas. L'étanchéité par dossier, elle, reste
    dans le corps : elle porte sur l'objet.

    Effet de bord assumé : l'écriture refusée répond désormais 403 AVANT le 404
    du dossier absent (la permission s'exécute dans `initial()`). Le refus est
    ainsi identique que le dossier existe ou non — l'ordre inverse renseignait
    sur l'existence d'un code.
    """
    try:
        from credits.models import CreditApplication
        app = CreditApplication.objects.get(code=code)
    except CreditApplication.DoesNotExist:
        return Response({"detail": "Dossier introuvable."}, status=404)

    # Étanchéité : un client ne voit que son propre dossier
    sub = getattr(request.user, "sub", "") or ""
    if not in_group(request, STAFF_ROLES) and app.client.sub != sub:
        return Response({"detail": "Dossier introuvable."}, status=404)

    ns = app.needs_sheet
    if ns is None:
        return Response({"detail": "Aucune feuille de besoins attachée à ce dossier."}, status=404)

    if request.method == "GET":
        from credits.analysis import serialize_analysis_report
        return Response(serialize_analysis_report(ns))

    # POST : décision analyste sur un finding.
    # Le garde `CAN_INSTRUCT` est monté sur le décorateur (`GroupeSelonMethode`).
    # Historique : la garde d'origine testait `role in ("analyste", "admin",
    # "superviseur")` — trois libellés français absents de tout registre, donc
    # seul "admin" passait. C'est cette classe de bug que la nomenclature unique
    # de `credits.roles` a supprimée.
    data = request.data
    finding_id = data.get("finding_id")
    new_status = data.get("status", "").strip()
    comment = data.get("comment", "").strip()

    VALID_STATUSES = {"justifie", "corrige", "confirme_anomalie"}
    if new_status not in VALID_STATUSES:
        return Response(
            {"detail": f"status invalide. Valeurs acceptées : {', '.join(sorted(VALID_STATUSES))}."},
            status=400,
        )
    if new_status == "justifie" and len(comment) < 10:
        return Response(
            {"detail": "Un commentaire d'au moins 10 caractères est requis pour justifier un finding."},
            status=400,
        )

    try:
        import datetime
        from credits.models import LineFinding
        finding = LineFinding.objects.get(pk=finding_id, needs_sheet=ns)
        finding.analyst_status = new_status
        finding.analyst_comment = comment
        finding.analyst_updated_at = datetime.datetime.now(datetime.timezone.utc)
        finding.save(update_fields=["analyst_status", "analyst_comment", "analyst_updated_at"])
    except LineFinding.DoesNotExist:
        return Response({"detail": "Finding introuvable."}, status=404)

    # Recompute document_confidence after analyst override (justifié retire la pénalité)
    _recompute_confidence_after_override(ns)

    from credits.analysis import serialize_analysis_report
    return Response(serialize_analysis_report(ns))


def _recompute_confidence_after_override(ns) -> None:
    """Recalcule document_confidence en excluant les findings justifiés/corrigés."""
    from credits.models import LineFinding
    _SEV_WEIGHT = {"bloquant": 20, "anomalie": 8, "a_justifier": 3, "info": 0, "point_fort": 0}
    active_findings = LineFinding.objects.filter(
        needs_sheet=ns,
    ).exclude(analyst_status__in=("justifie", "corrige"))
    penalty = sum(_SEV_WEIGHT.get(f.severity, 0) for f in active_findings)
    ns.document_confidence = max(0.0, min(100.0, 100.0 - penalty))
    ns.save(update_fields=["document_confidence"])


# ── Moteur d'analyse technico-économique (SPEC Moteur) ────────────────────────
#
# Routes alignées sur la convention du module — `applications/<code>/…` — et NON
# sur celles de la SPEC (`admin/demandes/<ref>/…`), qui adressent des modèles
# inexistants ici (`DemandeCredit`, `PlanFinancierUpload`). Le contrat publié
# dans `src/services/api.ts` est celui-ci.
#
# Convention d'absence, arrêtée avec l'agent front : **404 + code
# `ANALYSE_ABSENTE`** quand aucune analyse n'a encore été exécutée. Jamais un 200
# à corps vide — un écran ne doit pas avoir à distinguer « pas encore analysé »
# d'« analysé sans résultat » en inspectant la forme de la réponse.


def _analyse_error(exc) -> Response:
    """Réponse unique pour tout refus du moteur — même contrat que `_workflow_error`."""
    return Response(
        {"detail": str(exc), "code": exc.code, "errors": exc.as_errors()},
        status=exc.http_status,
    )


def _derniere_analyse(app):
    from credits.models import AnalyseCredit
    return (AnalyseCredit.objects
            .filter(application=app)
            .select_related("application", "referentiel", "needs_source")
            .order_by("-execute_le", "-id")
            .first())


def _parametres_analyse(app, data: dict):
    """Durée / différé / taux / mode, depuis le corps de la requête ou le dossier.

    Ce sont les leviers du simulateur analyste (`RateMaturityModal`) : l'analyste
    fait varier la maturité et relance le moteur. Aucune valeur n'est inventée —
    à défaut de saisie, on prend le cycle et le taux de base de la filière.
    """
    import decimal

    chain = app.value_chain
    duree = data.get("duree_mois", data.get("dureeMois"))
    if duree in (None, ""):
        duree = getattr(chain, "cycle_months", None) or 12
    differe = data.get("differe_mois", data.get("differeMois")) or 0
    taux = data.get("taux_annuel", data.get("tauxAnnuel"))
    mode = (data.get("mode_differe") or data.get("modeDiffere") or "interets_seuls")

    try:
        duree = int(duree)
        differe = int(differe)
        taux = decimal.Decimal(str(taux)) if taux not in (None, "") else None
    except (TypeError, ValueError, decimal.InvalidOperation):
        return None, Response(
            {"detail": "Paramètres de crédit invalides : duree_mois et differe_mois "
                       "doivent être entiers, taux_annuel un nombre.",
             "code": "PARAMETRES_INVALIDES"},
            status=400,
        )
    return {"duree_mois": duree, "differe_mois": differe,
            "taux_annuel": taux, "mode_differe": mode}, None


@api_view(["GET"])
@permission_classes([IsAuthenticated, MembreDuGroupe(
    STAFF_ROLES,
    message="L'analyse détaillée est réservée au personnel d'instruction.",
    code="STAFF_REQUIS")])
def analyse_detail(request: Request, code: str) -> Response:
    """GET /api/credits/applications/<code>/analyse/ — dernière analyse, vue STAFF.

    Réservée au staff : cette réponse expose les barèmes appliqués, les plages du
    référentiel et les tolérances par module (principe 7). Un client authentifié
    et propriétaire du dossier n'y a PAS accès — il a `analyse-resume`.

    `STAFF_ROLES` de `credits.roles`, et non `accounts.permissions.IsStaff` :
    ce dernier lit le TYPE du rôle au registre et ouvrirait la vue à
    `gest_agents` et `support`, absents du groupe. Le garde est l'appartenance.
    """
    app = _get_application(code)
    if not app:
        return Response({"detail": "Dossier introuvable."}, status=404)

    analyse = _derniere_analyse(app)
    if analyse is None:
        return Response(
            {"detail": "Aucune analyse n'a encore été exécutée sur ce dossier.",
             "code": "ANALYSE_ABSENTE"},
            status=404,
        )

    from credits.analyse import serialiser_analyse_staff
    return Response(serialiser_analyse_staff(analyse))


@api_view(["POST"])
@permission_classes([IsAuthenticated, MembreDuGroupe(
    CAN_INSTRUCT,
    message="Seul un agent instructeur peut justifier un indicateur.",
    code="INSTRUCTION_REQUISE")])
def analyse_justifier(request: Request, code: str) -> Response:
    """POST /api/credits/applications/<code>/analyse/justifier/

    Corps : `{"indicateur": "cout_module:semences", "justification": "..."}`.
    Ajoute une justification à la DERNIÈRE analyse — append only, journalisée.
    Retourne l'analyse complète mise à jour (contrat `api.ts`).
    """
    app = _get_application(code)
    if not app:
        return Response({"detail": "Dossier introuvable."}, status=404)

    analyse = _derniere_analyse(app)
    if analyse is None:
        return Response(
            {"detail": "Aucune analyse à justifier sur ce dossier.",
             "code": "ANALYSE_ABSENTE"},
            status=404,
        )

    from credits.analyse import (
        AnalyseError, justifier_indicateur, serialiser_analyse_staff,
    )

    data = request.data or {}
    try:
        justifier_indicateur(
            analyse,
            indicateur=data.get("indicateur") or data.get("indicator") or "",
            justification=data.get("justification") or "",
            agent=getattr(request.user, "sub", "") or "",
        )
    except AnalyseError as exc:
        return _analyse_error(exc)

    analyse.refresh_from_db()
    return Response(serialiser_analyse_staff(analyse))


@api_view(["POST"])
@permission_classes([IsAuthenticated, MembreDuGroupe(
    CAN_INSTRUCT,
    message="Seul un agent instructeur peut lancer une analyse.",
    code="INSTRUCTION_REQUISE")])
def reanalyser(request: Request, code: str) -> Response:
    """POST /api/credits/applications/<code>/reanalyser/

    Corps optionnel : `{duree_mois, differe_mois, taux_annuel, mode_differe}`.

    Crée une NOUVELLE `AnalyseCredit` — l'ancienne reste intacte (principe 3).
    C'est le simulateur de l'analyste : faire varier la maturité et relancer.
    Cette route ne déplace JAMAIS le dossier dans la machine à états : le moteur
    recommande, l'humain décide (principe 2).

    Interdit à l'audit, qui lit sans exécuter : `AUDIT_ROLES` est dans
    `STAFF_ROLES` mais hors de `CAN_INSTRUCT` — la nuance est portée par le
    groupe, et c'est pour elle que le garde n'est pas « staff ».
    """
    app = _get_application(code)
    if not app:
        return Response({"detail": "Dossier introuvable."}, status=404)

    params, erreur = _parametres_analyse(app, request.data or {})
    if erreur is not None:
        return erreur

    from credits.analyse import (
        AnalyseError, executer_analyse, serialiser_analyse_staff,
    )

    try:
        analyse = executer_analyse(
            app,
            execute_par=getattr(request.user, "sub", "") or "",
            **params,
        )
    except AnalyseError as exc:
        return _analyse_error(exc)

    return Response(serialiser_analyse_staff(analyse), status=201)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def analyse_resume(request: Request, code: str) -> Response:
    """GET /api/credits/applications/<code>/analyse-resume/ — vue CLIENT.

    Anti-gaming (principe 7) : ni barème, ni seuil, ni tolérance, ni plage du
    référentiel, ni score chiffré, ni DSCR, ni recommandation. Le client voit sa
    lettre et des pistes d'amélioration formulées en actions.

    Accessible au titulaire du dossier ; le staff peut la consulter pour voir ce
    que le client voit — c'est le seul moyen de vérifier l'écran client depuis le
    backoffice sans se connecter au compte d'un membre.
    """
    app = _get_application(code)
    if not app:
        return Response({"detail": "Dossier introuvable."}, status=404)

    if not _vcs(request).can_read_app(app):
        return Response({"detail": "Accès interdit."}, status=403)

    analyse = _derniere_analyse(app)
    if analyse is None:
        return Response(
            {"detail": "Votre dossier n'a pas encore été analysé.",
             "code": "ANALYSE_ABSENTE"},
            status=404,
        )

    from credits.analyse import serialiser_analyse_resume
    return Response(serialiser_analyse_resume(analyse))


# ── Comité de crédit — décision collégiale à quorum (CONTRAT §2) ──────────────

def _committee_error(exc) -> Response:
    """Réponse unique pour tout refus du comité — même contrat que `_workflow_error`."""
    return Response(
        {"detail": str(exc), "code": exc.code, "errors": exc.as_errors()},
        status=exc.http_status,
    )


@api_view(["GET"])
@permission_classes([IsAuthenticated, MembreDuGroupe(
    COMMITTEE_ROLES | CAN_AUDIT,
    message="Vue comité réservée à la direction et à l'audit.")])
def committee_votes(request: Request, code: str) -> Response:
    """GET /api/credits/applications/<code>/committee-votes/

    Procès-verbal du comité : quorum, votes nominatifs, décompte, résolution.
    Réservé au comité et aux auditeurs (lecture) — jamais au client (§7).
    """
    app = _load_app(code)
    if not app:
        return Response({"detail": "Dossier introuvable."}, status=404)

    from credits.committee import votes_summary
    return Response(votes_summary(app))


@api_view(["POST"])
@permission_classes([IsAuthenticated, MembreDuGroupe(
    COMMITTEE_ROLES,
    message="Seul un membre du comité de crédit peut voter.")])
def committee_vote(request: Request, code: str) -> Response:
    """POST /api/credits/applications/<code>/committee-vote/

    Corps : `{ decision: "approve"|"reject", comment, conditions? }`.
    Un vote par membre (append-only). Quorum atteint → transition via `workflow`.

    L'audit LIT le procès-verbal (`committee-votes/`) mais ne vote pas : deux
    groupes distincts sur deux routes, et c'est le décorateur qui le dit.
    """
    app = _load_app(code)
    if not app:
        return Response({"detail": "Dossier introuvable."}, status=404)

    data = request.data or {}
    from credits.committee import cast_vote, CommitteeError
    try:
        result = cast_vote(
            app,
            voter_sub=getattr(request.user, "sub", "") or "",
            decision=data.get("decision", ""),
            comment=data.get("comment", ""),
            conditions=data.get("conditions", ""),
            voter_roles=_roles(request),
        )
    except CommitteeError as exc:
        return _committee_error(exc)

    return Response(result, status=201)


# ── Barèmes de score éditables par le comité (CONTRAT §5) ─────────────────────

def _bareme_error(exc) -> Response:
    return Response(
        {"detail": str(exc), "code": exc.code, "errors": exc.as_errors()},
        status=exc.http_status,
    )


@api_view(["GET"])
@permission_classes([IsAuthenticated, MembreDuGroupe(
    STAFF_ROLES, message="Barèmes réservés au personnel.")])
def list_baremes(request: Request) -> Response:
    """GET /api/credits/baremes/ — courbes par critère + historique (staff seul).

    Anti-gaming (principe 7) : les barèmes, seuils et tolérances ne transitent
    JAMAIS vers un client. Réservé au staff.
    """
    from credits.baremes import serialize_bareme
    from credits.models import BaremeScore

    baremes = BaremeScore.objects.all().prefetch_related("revisions")
    data = [serialize_bareme(b, include_history=True) for b in baremes]
    return Response({"baremes": data, "totalRows": len(data)})


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated, GroupeSelonMethode(
    GET=STAFF_ROLES, POST=COMMITTEE_ROLES,
    messages={"GET": "Barèmes réservés au personnel.",
              "POST": "Seul le comité de crédit édite les barèmes."})])
def bareme_detail(request: Request, code: str) -> Response:
    """GET  /api/credits/baremes/<code>/  — un barème + son historique (staff).
    POST /api/credits/baremes/<code>/  — proposition d'édition (comité, maker).

    La proposition calcule et FIGE l'impact sur le golden set AVANT toute
    activation (principe 8) ; elle n'active rien (maker ≠ checker, l'activation
    est un second acte).

    Lire un barème et le RÉÉCRIRE n'ouvrent pas au même public : c'est
    exactement le cas que `GroupeSelonMethode` existe pour rendre lisible sur le
    décorateur, au lieu de le laisser dans un `if request.method` de corps.
    """
    from credits.baremes import (
        BaremeError, serialize_bareme, proposer_revision, serialize_revision,
    )
    from credits.models import BaremeScore

    if request.method == "GET":
        try:
            bareme = BaremeScore.objects.prefetch_related("revisions").get(code=code)
        except BaremeScore.DoesNotExist:
            return Response({"detail": "Barème introuvable.",
                             "code": "BAREME_INTROUVABLE"}, status=404)
        return Response(serialize_bareme(bareme, include_history=True))

    # POST — proposition (comité) ; garde porté par `GroupeSelonMethode`.
    data = request.data or {}
    try:
        revision = proposer_revision(
            code=code,
            points=data.get("points"),
            parametres=data.get("parametres"),
            comment=data.get("comment", ""),
            proposed_by_sub=getattr(request.user, "sub", "") or "",
        )
    except BaremeError as exc:
        return _bareme_error(exc)

    return Response(serialize_revision(revision, with_preview=True), status=201)


@api_view(["POST"])
@permission_classes([IsAuthenticated, MembreDuGroupe(
    COMMITTEE_ROLES,
    message="Seul le comité de crédit prévisualise les barèmes.")])
def bareme_preview(request: Request, code: str) -> Response:
    """POST /api/credits/baremes/<code>/preview/ — impact sur le golden set,
    SANS créer de révision (aide à la décision avant proposition). Comité seul.
    """
    from credits.baremes import BaremeError, previsualiser_impact, valider_contenu
    from credits.models import BaremeScore

    try:
        bareme = BaremeScore.objects.get(code=code)
    except BaremeScore.DoesNotExist:
        return Response({"detail": "Barème introuvable.",
                         "code": "BAREME_INTROUVABLE"}, status=404)

    data = request.data or {}
    points = data.get("points") if data.get("points") is not None else bareme.points
    parametres = (data.get("parametres")
                  if data.get("parametres") is not None else bareme.parametres)
    try:
        valider_contenu(code, points, parametres)
    except BaremeError as exc:
        return _bareme_error(exc)

    return Response(previsualiser_impact(bareme, points=points, parametres=parametres))


@api_view(["POST"])
@permission_classes([IsAuthenticated, MembreDuGroupe(
    COMMITTEE_ROLES, message="Seul le comité de crédit active un barème.")])
def bareme_activate(request: Request, revision_id: int) -> Response:
    """POST /api/credits/baremes/revisions/<revision_id>/activate/

    Active une révision brouillon (checker ≠ maker) : bascule le barème actif et
    archive le précédent. Journalisé.

    Le groupe donne l'entrée ; `checker ≠ maker` porte sur la révision visée et
    reste dans `baremes.activer_revision`.
    """
    from credits.baremes import BaremeError, activer_revision, serialize_revision
    try:
        revision = activer_revision(
            revision_id=revision_id,
            activated_by_sub=getattr(request.user, "sub", "") or "",
        )
    except BaremeError as exc:
        return _bareme_error(exc)

    return Response(serialize_revision(revision, with_preview=True))


# ── Propositions de caution : le client propose, l'agent valide ───────────────
#
# Trois surfaces, trois publics, trois sérialiseurs :
#   • le DEMANDEUR propose et suit ses propositions   → serialize_for_applicant
#   • le PERSONNEL instruit sa file et décide          → serialize_for_staff
#   • le GARANT répond, sur la surface qui existe déjà → guarantee-requests/
#
# Rien ici ne double la surface du garant : une proposition validée devient une
# `CreditGuarantee` par le chemin unique `guarantees.register_moral_guarantee`,
# et c'est la boîte de réception existante qui la sert au garant.


def _proposal_error(exc) -> Response:
    """Réponse unique pour tout refus de règle sur une proposition.

    `code` ET statut HTTP viennent de l'exception, jamais d'une valeur réécrite
    dans la vue — même discipline que `_workflow_error`. `GuarantorError` et
    `ProposalError` partagent cette convention, un seul relais suffit donc pour
    les deux familles.
    """
    return Response(
        {"detail": str(exc), "code": exc.code, "errors": exc.as_errors()},
        status=getattr(exc, "http_status", 422),
    )


def _get_proposal(proposal_id: int):
    from credits.models import GuaranteeProposal
    return (
        GuaranteeProposal.objects
        .select_related("application__client", "proposed_by", "guarantor", "guarantee")
        .filter(pk=proposal_id)
        .first()
    )


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def application_guarantee_proposals(request: Request, code: str) -> Response:
    """GET/POST /api/credits/applications/<code>/guarantee-proposals/

    POST — le TITULAIRE du dossier propose un garant.
      Corps : {guarantor_sub, covered_amount?, message?}
      201 avec la proposition. Aucune caution n'est créée, aucun tiers notifié :
      une proposition n'est pas une désignation.

    GET — les propositions du dossier. Le titulaire reçoit sa vue, le personnel
    la sienne : deux sérialiseurs distincts, choisis ici et jamais mélangés dans
    un seul par des `if` d'affichage.
    """
    from credits.guarantee_proposals import (
        ProposalError, propose, serialize_for_applicant, serialize_for_staff,
    )
    from credits.guarantor import GuarantorError

    app = _get_application(code)
    if not app:
        return Response({"detail": "Dossier introuvable."}, status=404)

    est_titulaire = str(app.client_id) == str(getattr(request.user, "pk", ""))
    est_personnel = in_group(request, CAN_INSTRUCT)

    if request.method == "GET":
        if not (est_titulaire or est_personnel):
            return Response({"detail": "Permission refusée."}, status=403)
        proposals = app.guarantee_proposals.select_related(
            "proposed_by", "guarantor", "guarantee",
        ).order_by("-created_at")
        serialize = serialize_for_staff if est_personnel else serialize_for_applicant
        items = [serialize(p) for p in proposals]
        return Response({"total_rows": len(items), "items": items})

    # POST — le personnel ne passe PAS par ici : il dispose de la désignation
    # directe (`guarantees/moral/`), qui n'a jamais changé de permissions. Une
    # proposition est l'acte du demandeur, et il faut que cela reste lisible dans
    # le journal : `proposed_by` doit vouloir dire quelque chose.
    if not est_titulaire:
        return Response(
            {"detail": "Vous ne pouvez proposer un garant que pour votre propre "
                       "dossier.",
             "code": "NOT_APPLICATION_OWNER",
             "errors": [{"code": "NOT_APPLICATION_OWNER",
                         "message": "Ce dossier n'est pas le vôtre."}]},
            status=403,
        )

    data = request.data or {}
    try:
        proposal = propose(
            application=app,
            proposer=request.user,
            guarantor_sub=str(data.get("guarantor_sub") or ""),
            covered_amount=data.get("covered_amount"),
            message=data.get("message", ""),
            ip=_client_ip(request),
        )
    except (ProposalError, GuarantorError) as exc:
        return _proposal_error(exc)

    return Response(serialize_for_applicant(proposal), status=201)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def guarantee_proposal_candidates(request: Request, code: str) -> Response:
    """GET /api/credits/applications/<code>/guarantee-proposals/candidates/

    Les membres des groupes du titulaire — le vivier dans lequel il choisit.

    Aucune donnée de capacité n'y figure : une liste qui distinguerait les
    personnes « éligibles » des autres serait l'oracle exact que le principe 7
    interdit. Le demandeur voit des noms et des groupes, rien de financier.
    """
    from credits.guarantee_proposals import candidates_for

    app = _get_application(code)
    if not app:
        return Response({"detail": "Dossier introuvable."}, status=404)

    est_titulaire = str(app.client_id) == str(getattr(request.user, "pk", ""))
    if not (est_titulaire or in_group(request, CAN_INSTRUCT)):
        return Response({"detail": "Permission refusée."}, status=403)

    items = candidates_for(app)
    return Response({"total_rows": len(items), "items": items})


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def list_my_guarantee_proposals(request: Request) -> Response:
    """GET /api/credits/guarantee-proposals/  [?status=]

    Le suivi du DEMANDEUR : ce qu'il a proposé, à qui, et où ça en est. Aucun
    rôle n'élargit ce périmètre — c'est la liste de SES demandes, symétrique de
    `guarantee-requests/` qui est celle des engagements du garant.
    """
    from credits.guarantee_proposals import proposals_of, serialize_for_applicant
    from credits.guarantor import consent_window_hours

    status_filter = request.query_params.get("status", "")
    items = [serialize_for_applicant(p)
             for p in proposals_of(request.user, status=status_filter)]
    return Response({
        "total_rows": len(items),
        # Fenêtre CONFIGURÉE : le front décompte dessus et n'écrit « 72 h » nulle
        # part (principe 8 jusque dans l'affichage).
        "consent_window_hours": consent_window_hours(),
        "items": items,
    })


@api_view(["GET"])
@permission_classes([IsAuthenticated, CanInstructCredit])
def guarantee_proposal_queue(request: Request) -> Response:
    """GET /api/credits/guarantee-proposals/queue/  [?status=&application=&limit=]

    La file de validation du personnel. Chaque ligne porte le diagnostic de
    capacité du garant et, le cas échéant, la règle qui bloquerait la
    désignation : l'agent sait avant de cliquer si sa validation passera. Ce
    diagnostic ne sort jamais vers le demandeur (principe 7).
    """
    from credits.guarantee_proposals import pending_queue, serialize_for_staff

    try:
        limit = min(int(request.query_params.get("limit", 50)), 200)
    except (TypeError, ValueError):
        limit = 50

    qs = pending_queue(
        status=request.query_params.get("status", ""),
        application_code=request.query_params.get("application", ""),
    )
    total = qs.count()
    items = [serialize_for_staff(p, with_capacity=True) for p in qs[:limit]]
    return Response({
        "total_rows": total,          # avant troncature — jamais len(items)
        "returned_rows": len(items),
        "truncated": total > len(items),
        "items": items,
    })


@api_view(["POST"])
@permission_classes([IsAuthenticated, CanInstructCredit])
def validate_guarantee_proposal(request: Request, proposal_id: int) -> Response:
    """POST /api/credits/guarantee-proposals/<id>/validate/

    Corps : {comment, guarantor_id_number, guarantor_name?, guarantor_phone?,
             covered_amount?}

    L'acte humain motivé qui transforme une proposition en désignation
    opposable. Il réutilise `guarantees.register_moral_guarantee` : les sept
    contrôles de capacité, la fenêtre de consentement et la notification du
    garant sont ceux qui existaient déjà, et un refus de règle remonte avec SON
    code — destiné à l'agent, jamais relayé au demandeur.
    """
    from credits.guarantee_proposals import (
        ProposalError, serialize_for_staff, validate,
    )
    from credits.guarantees import GuaranteeError
    from credits.guarantor import GuarantorError

    proposal = _get_proposal(proposal_id)
    if proposal is None:
        return Response({"detail": "Proposition introuvable."}, status=404)

    data = request.data or {}
    try:
        validate(
            proposal,
            agent_sub=getattr(request.user, "sub", "") or "",
            comment=data.get("comment", ""),
            guarantor_id_number=data.get("guarantor_id_number", ""),
            guarantor_name=data.get("guarantor_name", ""),
            guarantor_phone=data.get("guarantor_phone", ""),
            covered_amount=data.get("covered_amount"),
            ip=_client_ip(request),
        )
    except (ProposalError, GuarantorError) as exc:
        return _proposal_error(exc)
    except GuaranteeError as exc:
        # `GuaranteeError` porte un `code` mais pas de `http_status` : 422, comme
        # dans `register_moral_guarantee`.
        return Response(
            {"detail": str(exc), "code": exc.code,
             "errors": [{"code": exc.code, "message": str(exc)}]},
            status=422,
        )

    proposal.refresh_from_db()
    return Response(serialize_for_staff(proposal))


@api_view(["POST"])
@permission_classes([IsAuthenticated, CanInstructCredit])
def refuse_guarantee_proposal(request: Request, proposal_id: int) -> Response:
    """POST /api/credits/guarantee-proposals/<id>/refuse/

    Corps : {reason_code, comment}

    Le refus est motivé et journalisé, et la proposition n'est pas supprimée
    (principe 3). Deux motifs distincts : `reason_code`, vocabulaire fixe dont
    le libellé est ce que lira le demandeur, et `comment`, motif libre de
    l'agent qui reste interne.
    """
    from credits.guarantee_proposals import ProposalError, refuse, serialize_for_staff

    proposal = _get_proposal(proposal_id)
    if proposal is None:
        return Response({"detail": "Proposition introuvable."}, status=404)

    data = request.data or {}
    try:
        refuse(
            proposal,
            agent_sub=getattr(request.user, "sub", "") or "",
            reason_code=str(data.get("reason_code") or ""),
            comment=data.get("comment", ""),
            ip=_client_ip(request),
        )
    except ProposalError as exc:
        return _proposal_error(exc)

    proposal.refresh_from_db()
    return Response(serialize_for_staff(proposal))
