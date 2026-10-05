"""Source de vérité unique des 16 rôles métier AGRICAP (+ rôles historiques admin/manager/
client déjà utilisés par `accounts.FintechUser.role`) et de leur matrice de capacités.

Le mock frontend (`src/pages/Users.jsx` `ROLES`/`PERMISSIONS_MATRIX`) ne définissait que 5
lignes sur 16 (commentaire "... others mapped similarly in real app") — ce module devient
la référence que le frontend consomme désormais via `GET /api/rbac/roles`, plutôt que de
la coder en dur.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RoleDef:
    id: str
    label: str
    level: int
    type: str
    read: bool = False
    create: bool = False
    validate: bool = False
    disburse: bool = False
    audit: bool = False
    config: bool = False
    #: Gestion des coopératives, groupes et équipes (créer, affecter des membres,
    #: désigner un responsable). Distincte de `config` : administrer le
    #: RÉFÉRENTIEL et animer le RÉSEAU MUTUALISTE ne sont pas le même métier —
    #: un gestionnaire de zone anime des coopératives sans toucher aux barèmes.
    cooperatives: bool = False
    is_supervisor: bool = False
    mfa_step_up_required: bool = False


_ROLES = [
    RoleDef("dg", "Directeur Général", 1, "Direction",
            read=True, create=True, validate=True, disburse=True, audit=True, config=True,
            is_supervisor=True, mfa_step_up_required=True, cooperatives=True),
    RoleDef("dir_ops", "Directeur Opérations", 1, "Direction",
            read=True, create=True, validate=True, disburse=True, audit=True,
            is_supervisor=True, mfa_step_up_required=True, cooperatives=True),
    RoleDef("aud_tech", "Auditeur Technique", 2, "Audit",
            read=True, audit=True, is_supervisor=True, mfa_step_up_required=True),
    RoleDef("aud_fin", "Auditeur Financier", 2, "Audit",
            read=True, audit=True, is_supervisor=True, mfa_step_up_required=True),
    RoleDef("gest_credit", "Gestionnaire Crédits", 3, "Gestion",
            read=True, create=True, validate=True),
    RoleDef("gest_port", "Gestionnaire Portefeuille", 3, "Gestion",
            read=True, create=True, validate=True),
    RoleDef("gest_agents", "Gestionnaire Agents", 4, "Opérations",
            read=True, create=True, cooperatives=True),
    RoleDef("gest_zone", "Gestionnaire Zones", 4, "Opérations",
            read=True, create=True, validate=True, cooperatives=True),
    RoleDef("gest_caisse", "Gestionnaire Caisses", 4, "Opérations",
            read=True, create=True, validate=True, disburse=True),
    RoleDef("agent_terrain", "Agent Terrain", 5, "Terrain",
            read=True, create=True),
    RoleDef("agent_cash", "Agent Cash Express", 5, "Terrain",
            read=True, create=True, disburse=True),
    RoleDef("agri_op", "Opérateur Agricole", 0, "Client",
            read=True),
    RoleDef("invest", "Investisseur", 0, "Client",
            read=True, create=True),
    RoleDef("risk_analyst", "Analyste Risque", 0, "Support",
            read=True, audit=True),
    RoleDef("compliance", "Responsable Compliance", 0, "Support",
            read=True, validate=True, audit=True),
    RoleDef("support", "Support Client", 0, "Support",
            read=True, create=True),
    RoleDef("admin_it", "Admin IT", 0, "IT",
            read=True, validate=True, config=True),
    # Rôles historiques (déjà en usage sur accounts.FintechUser.role ET déjà seedés côté
    # IdP avant l'introduction de la hiérarchie à 16 rôles) — conservés pour compatibilité,
    # hors hiérarchie 16-rôles. Sans ces lignes, un utilisateur IdP déjà taggé
    # role="investor"/"partner" tomberait silencieusement sur le fallback "client"
    # (read-only) dans get_role().
    RoleDef("admin", "Administrateur (legacy)", 1, "Direction",
            read=True, create=True, validate=True, disburse=True, audit=True, config=True,
            is_supervisor=True),
    RoleDef("manager", "Manager (legacy)", 3, "Gestion",
            read=True, create=True, validate=True),
    RoleDef("client", "Client", 0, "Client", read=True),
    RoleDef("investor", "Investisseur (legacy)", 0, "Client", read=True, create=True),
    RoleDef("partner", "Partenaire / Fournisseur (legacy)", 0, "Client", read=True),
]

ROLE_REGISTRY: dict[str, RoleDef] = {r.id: r for r in _ROLES}

CAPABILITIES = ("read", "create", "validate", "disburse", "audit", "config", "cooperatives")

#: Identifiant servi quand aucune définition ne correspond. Type "Client", read-only :
#: c'est un repli SÛR (il ne donne rien), mais il ne doit jamais être SILENCIEUX —
#: un membre du personnel dégradé en client ne perd pas l'accès avec un message, il
#: le perd avec un 403 inexplicable.
FALLBACK_ROLE_ID = "client"

#: Alias de nomenclature → identifiant canonique du registre (principe 6 : une seule
#: nomenclature par concept).
#:
#: « auditeur » et « caissier » NE SONT PAS des rôles : ce sont deux des six **vues**
#: de tableau de bord (`rbac.models.DASHBOARD_VIEWS`, valeurs de `ROLE_MENU_MAP` dans
#: `src/components/Layout.jsx`). Le front mappe correctement ses rôles canoniques vers
#: ces vues ; le risque est ailleurs — un claim IdP, une donnée héritée ou une saisie
#: manuelle qui pose « auditeur » dans `accounts.FintechUser.role`. Sans alias, cette
#: chaîne retombait sur `client` : le back traitait en externe read-only quelqu'un à
#: qui la barre latérale sert un menu interne.
#:
#: On ne CRÉE pas ces deux rôles au registre : ce serait un troisième vocabulaire à
#: côté de `aud_tech`/`aud_fin` et `gest_caisse`/`agent_cash`. On les renvoie vers le
#: canonique, en choisissant le MOINS privilégié quand la correspondance est ambiguë :
#:   - « auditeur » → `aud_tech` : `aud_tech` et `aud_fin` ont des capacités
#:     RIGOUREUSEMENT identiques (read + audit, superviseur, MFA step-up) ; l'alias
#:     n'accorde donc rien de plus que l'autre branche. La distinction technique /
#:     financier est organisationnelle et doit être tranchée à la source.
#:   - « caissier » → `agent_cash` : `gest_caisse` porte en plus `validate`. Aliaser
#:     vers `gest_caisse` accorderait silencieusement un pouvoir de validation à une
#:     chaîne dont personne n'a décidé le niveau. `agent_cash` (read + create +
#:     disburse) est le plancher commun aux deux métiers de caisse.
#: Dans les deux cas l'alias est LOGGÉ : il répare l'accès sans masquer la dette.
ROLE_ALIASES: dict[str, str] = {
    "auditeur": "aud_tech",
    "caissier": "agent_cash",
}


def canonical_role_id(role_id: str) -> str | None:
    """Identifiant canonique correspondant à `role_id`, ou ``None`` s'il est inconnu.

    Ne touche pas la base : ne connaît que le registre d'usine et les alias. Les
    appelants qui doivent aussi accepter les rôles personnalisés (`RoleOverride`)
    complètent eux-mêmes — c'est le cas de `rbac.views.user_detail`.
    """
    if role_id in ROLE_REGISTRY:
        return role_id
    return ROLE_ALIASES.get((role_id or "").strip().lower())


def get_role(role_id: str) -> RoleDef:
    """Un `rbac.models.RoleOverride` (édité depuis Roles.jsx) remplace entièrement la
    définition d'usine pour ce `role_id` s'il en existe un — même interface par attribut
    que `RoleDef`, donc transparent pour tous les appelants existants (permissions, ABAC,
    serializers). Import différé : `role_registry` doit rester chargeable sans app Django
    prête (import à froid), seul l'appel (toujours en cours de requête) touche la DB.

    Un identifiant inconnu retombe sur `client` — mais BRUYAMMENT (log d'avertissement).
    Le repli muet précédent transformait une faute de frappe ou un alias de vue en
    déclassement invisible : `is_staff_role` passait à False, tous les gardes `IsStaff`
    traitaient l'utilisateur en externe, et rien nulle part ne le disait.
    """
    from .models import RoleOverride
    override = RoleOverride.objects.filter(id=role_id).first()
    if override is not None:
        return override

    known = ROLE_REGISTRY.get(role_id)
    if known is not None:
        return known

    normalise = (role_id or "").strip().lower()
    alias = ROLE_ALIASES.get(normalise)
    if alias is not None:
        logger.warning(
            "Rôle « %s » absent du registre : alias de nomenclature vers « %s ». "
            "« %s » est une VUE de tableau de bord (rbac.models.DASHBOARD_VIEWS), pas "
            "un rôle — corriger la source (claim IdP / accounts.FintechUser.role) pour "
            "y poser un identifiant canonique.",
            role_id, alias, role_id,
        )
        return ROLE_REGISTRY[alias]

    if not normalise:
        # Utilisateur sans rôle du tout : cas distinct d'un rôle mal orthographié.
        logger.info("Aucun rôle porté par l'utilisateur — repli sur « %s ».", FALLBACK_ROLE_ID)
    else:
        logger.warning(
            "Rôle inconnu « %s » — repli sur « %s » (type Client, lecture seule). "
            "Tout garde `IsStaff` traitera cet utilisateur en externe. Rôles "
            "canoniques disponibles : %s.",
            role_id, FALLBACK_ROLE_ID, ", ".join(sorted(ROLE_REGISTRY)),
        )
    return ROLE_REGISTRY[FALLBACK_ROLE_ID]
