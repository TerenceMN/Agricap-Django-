"""
Règles d'engagement du garant — caution solidaire opposable (SPEC §2.5).

Ce module répond à un défaut de fond : jusqu'ici une caution morale s'enregistrait
sur la seule déclaration de l'agent ou du client. Le garant n'était jamais
consulté ; on pouvait donc engager quelqu'un à son insu. Une caution ainsi
constituée est juridiquement vide — elle ne serait opposable devant aucune
juridiction, et le principe 9 (« toute garantie est opposable ou n'est pas ») en
faisait déjà une non-garantie.

Deux mécaniques, séparées volontairement :

  1. **La capacité d'engagement** (ce module) : sept contrôles sur la personne du
     garant, exécutés à la désignation ET re-exécutés au consentement. Un garant
     peut s'être engagé ailleurs, être tombé en défaut ou avoir quitté le groupe
     entre les deux : l'engagement se forme au consentement, c'est donc à ce
     moment que les règles doivent tenir.

  2. **Le consentement lui-même** (`credits.guarantees.record_guarantor_consent`),
     calqué sur le consentement client 72 h de `workflow.record_client_consent` —
     même fenêtre paramétrable, même horodatage, même traçabilité. Le mécanisme
     n'est pas réécrit, il est réutilisé.

Nomenclature — le module crédit a déjà payé cher d'avoir cinq vocabulaires
parallèles. `GuarantorError` copie exactement la convention de
`workflow.WorkflowError` : `code` en MAJUSCULES, `http_status` porté par la règle,
`as_errors()` pour la réponse structurée. Aucune vue ne réécrit un code.

Principe 8 — aucun seuil en dur : le ratio `k`, le plafond de cautions et la
fenêtre de consentement viennent de `referentiel.InstitutionConfig`. Les valeurs
de secours ne s'appliquent qu'avec un warning loggé, jamais en silence.
"""
from __future__ import annotations

import logging
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from django.utils import timezone

logger = logging.getLogger(__name__)

CENT = Decimal("0.01")


# ── Exceptions ────────────────────────────────────────────────────────────────

class GuarantorError(Exception):
    """Refus d'engagement d'un garant, ou refus d'un acte de consentement.

    Convention reprise telle quelle de `credits.workflow.WorkflowError` : le
    front consomme `code`, jamais la formulation du message ; `http_status`
    appartient à la règle et non à la vue ; `as_errors()` produit la
    représentation structurée exigée par le principe 5.
    """

    code = "GUARANTOR_ERROR"
    #: 422 par défaut : « la requête est comprise mais une règle métier la refuse ».
    http_status = 422

    def __init__(self, message: str, errors: list[dict] | None = None) -> None:
        super().__init__(message)
        self.errors = errors or []

    def as_errors(self) -> list[dict]:
        return self.errors or [{"code": self.code, "message": str(self)}]


# — Règles de capacité d'engagement (SPEC §2.5) —

class GuarantorUnknown(GuarantorError):
    """Aucun compte AGRICAP ne correspond au garant désigné.

    Une caution ne peut plus être portée par un simple nom sur un formulaire :
    sans compte, personne ne peut consentir, et l'engagement reste déclaratif.
    """

    code = "GUARANTOR_UNKNOWN"


class GuarantorIsApplicant(GuarantorError):
    """Le garant désigné est le demandeur lui-même — auto-caution."""

    code = "GUARANTOR_IS_APPLICANT"


class GuarantorAccountInactive(GuarantorError):
    """Le compte du garant est suspendu."""

    code = "GUARANTOR_ACCOUNT_INACTIVE"


class GuarantorNotInGroup(GuarantorError):
    """Garant et demandeur ne partagent aucun groupe ni coopérative.

    La caution solidaire tire sa force du lien social réel : c'est la pression du
    groupe qui la rend recouvrable, pas la signature. Un garant sans lien de
    groupe est un tiers quelconque, et la garantie retombe au déclaratif.
    """

    code = "GUARANTOR_NOT_IN_GROUP"


class GuarantorInvalidAmount(GuarantorError):
    """Montant couvert absent, nul ou négatif.

    Signalé par l'agent front : cette règle levait un `GuarantorError` nu, donc
    le code générique `GUARANTOR_ERROR` atteignait le client, intraduisible.
    Un code de base qui sort en production est le symptôme d'une règle sans
    identité — exactement ce que `tests_guarantee_codes` verrouille pour les
    gages.
    """

    code = "GUARANTOR_INVALID_AMOUNT"


class GuarantorOverextended(GuarantorError):
    """Σ(cautions vivantes) + nouvelle caution > k × épargne AGRICAP du garant."""

    code = "GUARANTOR_OVEREXTENDED"


class GuarantorTooManyPledges(GuarantorError):
    """Le garant porte déjà le nombre maximal de cautions vivantes."""

    code = "GUARANTOR_TOO_MANY_PLEDGES"


class GuarantorInDefault(GuarantorError):
    """Prêt en défaut/bloqué, ou caution déjà appelée et non soldée.

    Un garant défaillant ne garantit rien : sa propre solvabilité est la seule
    matière de son engagement.
    """

    code = "GUARANTOR_IN_DEFAULT"


class CrossGuaranteeForbidden(GuarantorError):
    """A cautionne B et B cautionne A sur des dossiers vivants simultanés.

    Les cautions circulaires vident la garantie de sa substance : chacun garantit
    l'autre avec un patrimoine déjà engagé en face. En cas de choc commun — et en
    agriculture les chocs SONT communs (saison, prix, zone) — les deux tombent
    ensemble et aucune des deux cautions n'est mobilisable.
    """

    code = "CROSS_GUARANTEE_FORBIDDEN"


# — Règles du cycle de consentement —

class GuarantorConsentMissing(GuarantorError):
    """Une caution morale du dossier n'a pas été consentie par son garant."""

    code = "GUARANTOR_CONSENT_MISSING"


class GuarantorNotDesignated(GuarantorError):
    """L'utilisateur n'est pas le garant désigné de cette demande."""

    code = "GUARANTOR_NOT_DESIGNATED"
    http_status = 403


class GuarantorAlreadyAnswered(GuarantorError):
    """Le garant a déjà accepté ou déjà refusé — un consentement ne se rejoue pas.

    Principe 3 : le consentement est probant, donc append-only. Se raviser exige
    une nouvelle désignation, pas la réécriture de la précédente.
    """

    code = "GUARANTOR_ALREADY_ANSWERED"
    http_status = 409


class InvalidGuaranteeState(GuarantorError):
    """La caution n'est plus dans un état où un consentement a un sens."""

    code = "INVALID_GUARANTEE_STATE"
    http_status = 409


class GuarantorConsentExpired(GuarantorError):
    """La fenêtre de consentement du garant est dépassée.

    Distincte de `GuarantorConsentMissing` pour la même raison que
    `workflow.ConsentExpired` l'est de `ConsentError` : l'action attendue diffère.
    Un consentement manquant se recueille, un consentement expiré exige une
    nouvelle désignation.
    """

    code = "GUARANTOR_CONSENT_EXPIRED"
    http_status = 410


# ── Paramètres institutionnels (principe 8) ───────────────────────────────────

#: Valeurs de secours — n'ont d'effet que si `InstitutionConfig` est absent ou
#: muet, et JAMAIS en silence (cf. `_param`).
FALLBACK_SAVINGS_MULTIPLE = Decimal("2")     # k
FALLBACK_MAX_LIVE_PLEDGES = 3
FALLBACK_CONSENT_WINDOW_HOURS = 72
FALLBACK_MORAL_HAIRCUT = Decimal("0.70")     # décote de 70 %


def _active_config():
    try:
        from referentiel.models import InstitutionConfig
        return InstitutionConfig.objects.filter(is_active=True).first()
    except Exception:                                   # app absente / non migrée
        return None


#: Les paramètres de caution, leur repli et leur libellé — source unique pour
#: `_param` et pour `config_provenance()`, afin qu'ajouter un paramètre ne puisse
#: pas l'ajouter à l'un sans l'autre.
CAUTION_PARAMS: dict[str, tuple] = {
    "caution_ratio_epargne": (
        FALLBACK_SAVINGS_MULTIPLE, "multiple d'épargne autorisé en caution",
    ),
    "caution_max_actives": (
        FALLBACK_MAX_LIVE_PLEDGES, "nombre maximal de cautions vivantes par garant",
    ),
    "caution_consent_window_hours": (
        FALLBACK_CONSENT_WINDOW_HOURS, "fenêtre de consentement du garant",
    ),
    "decote_caution_morale": (
        FALLBACK_MORAL_HAIRCUT, "décote de la caution morale",
    ),
}


def config_provenance() -> dict[str, dict]:
    """Pour chaque paramètre : sa valeur, et si elle est **décidée** ou subie.

    Un `k = 2` non décidé fonctionne exactement comme un `k = 2` décidé, et le
    seul garde-fou aujourd'hui est un warning loggé — c'est-à-dire quelque chose
    que personne ne lit tant que rien ne va mal. Contrairement à une rupture de
    contrat, une absence de décision ne finit jamais par se voir toute seule.

    Cette fonction rend l'état interrogeable au lieu de seulement traçable. Elle
    n'est **pas** exposée au garant ni au client (elle révèle les seuils du
    moteur, principe 7) : elle existe pour l'onglet Référence du backoffice
    (CONVENTIONS.md §7.1.5), où un administrateur doit pouvoir distinguer un
    référentiel arrêté par le comité d'un défaut de secours.

    Volontairement laissée sans endpoint : construire l'écran d'administration
    des référentiels n'est pas le périmètre de ce lot. Le point de branchement
    existe pour que celui qui le construira n'ait pas à rétro-concevoir `_param`.
    """
    config = _active_config()
    provenance: dict[str, dict] = {}
    for field, (fallback, label) in CAUTION_PARAMS.items():
        value = getattr(config, field, None) if config is not None else None
        configured = value is not None
        provenance[field] = {
            "label": label,
            "value": value if configured else fallback,
            "source": "config" if configured else "fallback",
            "fallback": fallback,
        }
    return provenance


def _param(field: str):
    """Lit un paramètre dans `InstitutionConfig`, ou retombe avec un warning.

    Le principe 8 tolère les valeurs par défaut de secours ; il n'en tolère pas
    l'usage silencieux. Un comité qui croit avoir fixé k = 1,5 doit pouvoir
    constater dans les logs que le code applique encore 2.

    Le repli et le libellé viennent de `CAUTION_PARAMS`, pas des appelants :
    autrement, `config_provenance()` et cette fonction pourraient diverger, et
    l'écran d'administration afficherait un défaut différent de celui réellement
    appliqué — un mensonge pire que l'absence d'écran.
    """
    fallback, label = CAUTION_PARAMS[field]
    config = _active_config()
    value = getattr(config, field, None) if config is not None else None
    if value is None:
        logger.warning(
            "InstitutionConfig.%s absent : repli sur la valeur de secours %s (%s). "
            "Ce seuil devrait vivre en base, pas dans le code.",
            field, fallback, label,
        )
        return fallback
    return value


def savings_multiple() -> Decimal:
    """`k` — multiple d'épargne plafonnant l'engagement total du garant."""
    return Decimal(str(_param("caution_ratio_epargne")))


def max_live_pledges() -> int:
    return int(_param("caution_max_actives"))


def consent_window_hours() -> int:
    return int(_param("caution_consent_window_hours"))


def moral_haircut() -> Decimal:
    """Décote appliquée à une caution morale dans le calcul de couverture.

    70 % par défaut : une caution solidaire sécurise **socialement**, pas
    financièrement. Elle n'apporte aucun actif réalisable ; ce qu'elle apporte
    est une pression de recouvrement. La compter à 100 % reviendrait à
    surestimer la couverture d'un dossier de 3,3 fois.
    """
    return Decimal(str(_param("decote_caution_morale")))


def moral_coverage_weight() -> Decimal:
    """Part de la caution morale retenue en couverture (1 − décote)."""
    return (Decimal("1") - moral_haircut()).quantize(Decimal("0.001"))


# ── Lectures métier ───────────────────────────────────────────────────────────

def live_pledge_statuses() -> tuple[str, ...]:
    """Statuts d'une caution qui engage encore le garant.

    `pending_consent` en fait partie : une demande en attente immobilise déjà la
    capacité du garant, sinon on pourrait le solliciter dix fois en parallèle et
    n'en faire consentir que trois — le plafond serait contournable par la
    simultanéité. `called` compte aussi : une caution appelée est un engagement
    devenu dette, pas un engagement éteint.
    """
    from credits.models import CreditGuarantee
    return (
        CreditGuarantee.Status.PENDING_CONSENT,
        CreditGuarantee.Status.CONSENTED,
        CreditGuarantee.Status.ACTIVE,
        CreditGuarantee.Status.CALLED,
    )


def shared_groups(user_a, user_b) -> list:
    """Groupes/coopératives d'épargne communs aux deux utilisateurs.

    Les groupes vivent dans le module Épargne (`savings.SavingsGroupMember`) —
    aucun registre de groupes n'est créé ici (principe 6).
    """
    try:
        from savings.models import SavingsGroup, SavingsGroupMember
    except Exception:
        return []

    ids_a = set(
        SavingsGroupMember.objects.filter(user=user_a).values_list("group_id", flat=True)
    )
    if not ids_a:
        return []
    ids_b = set(
        SavingsGroupMember.objects.filter(user=user_b).values_list("group_id", flat=True)
    )
    common = ids_a & ids_b
    if not common:
        return []
    return list(SavingsGroup.objects.filter(pk__in=common).order_by("name"))


def guarantor_savings(user) -> Decimal:
    """Épargne AGRICAP mobilisable du garant : solde des plans actifs.

    Dette croisée assumée : les plans sont multi-devises et la somme est faite
    sans conversion. En pratique les plans sont en USD ; le jour où le
    convertisseur du module Accounting sera exposé, cette fonction devra
    journaliser son taux (principe 4). Le cas est loggé plutôt que maquillé.
    """
    try:
        from savings.models import SavingsPlan
    except Exception:
        return Decimal("0")

    plans = SavingsPlan.objects.filter(user=user, status=SavingsPlan.Status.ACTIF)
    devises = {p.currency for p in plans}
    if len(devises) > 1:
        logger.warning(
            "Épargne du garant %s agrégée sur plusieurs devises (%s) sans "
            "conversion : le plafond d'engagement est approximatif.",
            getattr(user, "sub", "?"), ", ".join(sorted(devises)),
        )
    total = sum((p.balance or Decimal("0") for p in plans), Decimal("0"))
    return Decimal(total).quantize(CENT, rounding=ROUND_HALF_UP)


def live_pledges(guarantor, exclude_pk: int | None = None):
    """Cautions morales vivantes portées par ce garant."""
    from credits.models import CreditGuarantee
    qs = CreditGuarantee.objects.filter(
        guarantor=guarantor,
        guarantee_type=CreditGuarantee.GuaranteeType.MORALE,
        status__in=live_pledge_statuses(),
    )
    if exclude_pk is not None:
        qs = qs.exclude(pk=exclude_pk)
    return qs


def committed_amount(guarantor, exclude_pk: int | None = None) -> Decimal:
    total = sum(
        (g.covered_amount or Decimal("0") for g in live_pledges(guarantor, exclude_pk)),
        Decimal("0"),
    )
    return Decimal(total).quantize(CENT, rounding=ROUND_HALF_UP)


# ── Les sept contrôles ────────────────────────────────────────────────────────

def assert_can_guarantee(
    application,
    guarantor,
    montant_couvert: Decimal,
    exclude_pk: int | None = None,
) -> None:
    """Vérifie qu'un garant peut porter cette caution. Lève au premier refus.

    L'ordre n'est pas arbitraire : on écarte d'abord ce qui relève de l'identité
    (gratuit, et rend les contrôles suivants sans objet), puis le lien social,
    puis la solvabilité, puis la capacité chiffrée. Le front reçoit un code
    unique et actionnable plutôt qu'une liste de reproches dont seul le premier
    est corrigeable.

    `exclude_pk` exclut la caution en cours d'examen du cumul — indispensable au
    consentement, sinon la caution que le garant est en train d'accepter serait
    comptée deux fois et se refuserait elle-même.
    """
    _assert_identity(application, guarantor)
    _assert_account_active(guarantor)
    _assert_shared_group(application, guarantor)
    _assert_not_in_default(guarantor)
    _assert_no_cross_guarantee(application, guarantor, exclude_pk)
    _assert_pledge_count(guarantor, exclude_pk)
    _assert_capacity(guarantor, montant_couvert, exclude_pk)


def _assert_identity(application, guarantor) -> None:
    if guarantor is None:
        raise GuarantorUnknown(
            "Aucun compte AGRICAP ne correspond au garant désigné. Une caution "
            "solidaire exige un garant identifié, qui puisse consentir lui-même."
        )
    if str(guarantor.pk) == str(application.client_id):
        raise GuarantorIsApplicant(
            "Le garant désigné est le demandeur lui-même : une auto-caution "
            "n'apporte aucune garantie."
        )


def _assert_account_active(guarantor) -> None:
    """Compte actif.

    Limite connue : `accounts.FintechUser` ne porte aucun statut de compte pour
    les clients — seul un profil staff (`rbac.StaffProfile`) peut être suspendu.
    Le contrôle est donc réel pour un garant staff et vide pour un garant client.
    Signalé plutôt que simulé : le jour où un statut de compte client existera,
    c'est ici et nulle part ailleurs qu'il se branchera.
    """
    profile = getattr(guarantor, "staff_profile", None)
    if profile is None:
        return
    if getattr(profile, "status", None) == "Suspendu" or getattr(profile, "locked", False):
        raise GuarantorAccountInactive(
            "Le compte du garant est suspendu : il ne peut pas s'engager."
        )


def _assert_shared_group(application, guarantor) -> None:
    common = shared_groups(application.client, guarantor)
    if not common:
        raise GuarantorNotInGroup(
            "Le garant ne partage aucun groupe ni coopérative avec le demandeur. "
            "Une caution solidaire s'appuie sur un lien de groupe réel."
        )


def _assert_not_in_default(guarantor) -> None:
    from credits.models import CreditGuarantee

    try:
        from portfolio.models import Loan
        en_defaut = Loan.objects.filter(
            borrower_sub=str(guarantor.pk),
            status__in=[Loan.Status.DEFAUT, Loan.Status.BLOQUE],
        ).exists()
    except Exception:                                   # app portefeuille absente
        logger.warning(
            "Contrôle de défaut du garant impossible (module portefeuille "
            "indisponible) : la règle GUARANTOR_IN_DEFAULT n'a pas été appliquée."
        )
        en_defaut = False

    if en_defaut:
        raise GuarantorInDefault(
            "Le garant porte un prêt en défaut ou bloqué : il ne peut pas "
            "cautionner un tiers."
        )

    caution_appelee = CreditGuarantee.objects.filter(
        guarantor=guarantor,
        guarantee_type=CreditGuarantee.GuaranteeType.MORALE,
        status=CreditGuarantee.Status.CALLED,
    ).exists()
    if caution_appelee:
        raise GuarantorInDefault(
            "Une caution déjà appelée et non soldée pèse sur ce garant : il ne "
            "peut pas en contracter une nouvelle."
        )


def _assert_no_cross_guarantee(application, guarantor, exclude_pk: int | None) -> None:
    from credits.models import CreditGuarantee

    reciproque = CreditGuarantee.objects.filter(
        guarantor_id=application.client_id,
        guarantee_type=CreditGuarantee.GuaranteeType.MORALE,
        status__in=live_pledge_statuses(),
        application__client_id=guarantor.pk,
    )
    if exclude_pk is not None:
        reciproque = reciproque.exclude(pk=exclude_pk)

    if reciproque.exists():
        raise CrossGuaranteeForbidden(
            "Caution croisée interdite : le demandeur cautionne déjà ce garant "
            "sur un dossier en cours. Deux personnes qui se garantissent "
            "mutuellement ne garantissent rien."
        )


def _assert_pledge_count(guarantor, exclude_pk: int | None) -> None:
    plafond = max_live_pledges()
    actuelles = live_pledges(guarantor, exclude_pk).count()
    if actuelles >= plafond:
        raise GuarantorTooManyPledges(
            f"Le garant porte déjà {actuelles} cautions en cours "
            f"(maximum autorisé : {plafond})."
        )


def _assert_capacity(guarantor, montant_couvert: Decimal, exclude_pk: int | None) -> None:
    montant = Decimal(str(montant_couvert or 0)).quantize(CENT, rounding=ROUND_HALF_UP)
    if montant <= 0:
        raise GuarantorInvalidAmount(
            "Le montant couvert par la caution doit être strictement positif. "
            "C'est ce montant qui définit l'engagement du garant : il ne peut "
            "être ni nul ni implicite."
        )

    epargne = guarantor_savings(guarantor)
    k = savings_multiple()
    plafond = (epargne * k).quantize(CENT, rounding=ROUND_HALF_UP)
    engage = committed_amount(guarantor, exclude_pk)
    total = (engage + montant).quantize(CENT, rounding=ROUND_HALF_UP)

    if total > plafond:
        raise GuarantorOverextended(
            f"Capacité d'engagement dépassée : {total} déjà engagés ou demandés "
            f"pour un plafond de {plafond} (dont {engage} sur d'autres dossiers). "
            "Le garant doit renforcer son épargne ou réduire le montant couvert."
        )


# ── Diagnostic (usage staff / tests) ──────────────────────────────────────────

def capacity_snapshot(guarantor, exclude_pk: int | None = None) -> dict[str, Any]:
    """Photo chiffrée de la capacité d'un garant, à un instant donné.

    Destinée à l'analyste et aux tests — **jamais** servie au garant ni au
    client : elle expose les plafonds, donc les règles du moteur (principe 7).
    """
    epargne = guarantor_savings(guarantor)
    k = savings_multiple()
    return {
        "savings": epargne,
        "multiple": k,
        "ceiling": (epargne * k).quantize(CENT, rounding=ROUND_HALF_UP),
        "committed": committed_amount(guarantor, exclude_pk),
        "livePledges": live_pledges(guarantor, exclude_pk).count(),
        "maxPledges": max_live_pledges(),
        "computedAt": timezone.now().isoformat(),
    }
