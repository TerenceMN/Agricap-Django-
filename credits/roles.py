"""
Nomenclature unique des rôles pour le module crédit.

Ce module est la **seule** traduction entre le vocabulaire métier du crédit
(« agent », « analyste », « comité ») et les identifiants canoniques du registre
RBAC (`rbac.role_registry`). Aucun autre fichier de `credits/` ne doit comparer
`user.role` à une chaîne littérale.

Contexte historique — le module crédit comparait `user.role` à des libellés
(« agent », « analyst », « branch_manager », « credit_committee ») qui
n'existent dans AUCUN registre : `accounts.FintechUser.role` contient un id du
registre (`agent_terrain`, `gest_credit`, `dg`…). Aucune de ces comparaisons ne
pouvait réussir, sauf `admin`. Le workflow crédit était donc intégralement
bloqué. Ce module supprime cette classe de bug en centralisant le mapping.

Décision d'architecture (juillet 2026) : **aucun nouveau rôle n'est créé.**
Les fonctions crédit sans rôle propre dans le registre — au premier chef le
comité de crédit — sont exercées par les rôles de direction et d'administration
qui disposent déjà d'une délégation illimitée.

Autorité d'approbation — deux conditions cumulatives, jamais une seule :
  1. capacité ``validate`` dans le registre RBAC ;
  2. présence dans ``settings.CREDIT_DELEGATION_USD``.
La capacité seule ne suffit pas : ``admin_it`` et ``compliance`` portent
``validate`` pour la configuration et la conformité, pas pour engager des fonds.
Un rôle absent de la table de délégation n'a aucune autorité d'approbation
crédit, quelle que soit sa capacité.
"""
from __future__ import annotations

from django.conf import settings

from rbac.role_registry import canonical_role_id, get_role


# ── Groupes fonctionnels (identifiants canoniques du registre) ────────────────

#: Le demandeur et les autres profils « Client » du registre.
CLIENT_ROLES = frozenset({"client", "agri_op", "invest", "investor", "partner"})

#: Terrain : monte les dossiers, vérifie les actifs sur place.
FIELD_AGENT_ROLES = frozenset({"agent_terrain", "agent_cash"})

#: Instruction du dossier : analyse et décision dans la limite de délégation.
CREDIT_OFFICER_ROLES = frozenset({"gest_credit", "gest_port", "manager"})

#: Niveau agence (ex-« branch_manager »).
BRANCH_ROLES = frozenset({"gest_zone"})

#: Caisse : exécute le décaissement (checker).
CASHIER_ROLES = frozenset({"gest_caisse"})

#: Direction (ex-« regional_director » et au-dessus).
DIRECTION_ROLES = frozenset({"dg", "dir_ops"})

#: Comité de crédit — pas de rôle propre : exercé par la direction et l'admin.
COMMITTEE_ROLES = frozenset({"dg", "admin"})

#: Lecture des analyses et du journal, sans pouvoir de décision.
AUDIT_ROLES = frozenset({"aud_tech", "aud_fin", "risk_analyst", "compliance"})

#: Administration des référentiels, templates et barèmes (capacité ``config``).
CONFIG_ROLES = frozenset({"admin", "admin_it", "dg"})

#: Accès transverse à tous les dossiers.
SUPERADMIN_ROLES = frozenset({"admin", "dg"})

#: Tout ce qui n'est pas client : conditionne le filtrage des données.
STAFF_ROLES = (
    FIELD_AGENT_ROLES
    | CREDIT_OFFICER_ROLES
    | BRANCH_ROLES
    | CASHIER_ROLES
    | DIRECTION_ROLES
    | AUDIT_ROLES
    | CONFIG_ROLES
    | SUPERADMIN_ROLES
)

#: Peut instruire un dossier (prise en charge, ajournement, réouverture, scoring).
CAN_INSTRUCT = (
    FIELD_AGENT_ROLES | CREDIT_OFFICER_ROLES | BRANCH_ROLES
    | DIRECTION_ROLES | SUPERADMIN_ROLES
)

#: Peut décider (approuver / rejeter) — sous réserve du plafond de délégation.
CAN_DECIDE = CREDIT_OFFICER_ROLES | BRANCH_ROLES | DIRECTION_ROLES | SUPERADMIN_ROLES

#: Peut demander un décaissement (maker).
CAN_REQUEST_DISBURSEMENT = (
    FIELD_AGENT_ROLES | CREDIT_OFFICER_ROLES | BRANCH_ROLES | SUPERADMIN_ROLES
)

#: Peut confirmer un décaissement (checker) — capacité ``disburse`` requise.
CAN_CONFIRM_DISBURSEMENT = (
    CASHIER_ROLES | DIRECTION_ROLES | SUPERADMIN_ROLES | {"agent_cash"}
)

#: Peut consulter le journal d'audit (lecture seule absolue).
CAN_AUDIT = AUDIT_ROLES | DIRECTION_ROLES | SUPERADMIN_ROLES

#: Peut vérifier un actif déclaré et fixer sa valeur retenue.
CAN_VERIFY_ASSET = FIELD_AGENT_ROLES | BRANCH_ROLES | SUPERADMIN_ROLES


# ── Résolution des rôles de la requête ────────────────────────────────────────

def roles_of(request) -> list[str]:
    """Rôles canoniques de l'utilisateur courant, ou ``[]`` si non authentifié.

    Remplace l'attribut ``request.roles`` que le code lisait sans que rien ne le
    pose jamais. Un middleware Django ne peut pas jouer ce rôle ici :
    l'authentification est faite par DRF (``IdpBearerAuthentication``) APRÈS la
    chaîne de middlewares, donc ``request.user`` y serait toujours anonyme.

    Un profil staff verrouillé perd tous ses rôles — même sémantique que
    ``rbac.permissions.HasCapability``, pour qu'une suspension depuis Users.jsx
    bloque réellement le module crédit.
    """
    user = getattr(request, "user", None)
    if not user or not getattr(user, "is_authenticated", False):
        return []

    profile = getattr(user, "staff_profile", None)
    if profile is not None and getattr(profile, "locked", False):
        return []

    role_id = getattr(user, "role", "") or "client"
    # Les groupes fonctionnels ci-dessus comparent des CHAÎNES : un alias de
    # nomenclature (« auditeur », « caissier » — des noms de vues confondus avec
    # des rôles, cf. `rbac.role_registry.ROLE_ALIASES`) resterait hors de tout
    # groupe, donc hors `STAFF_ROLES`, même si `get_role()` le résout
    # correctement en capacités. On canonicalise ici pour que les deux chemins
    # de décision — capacité et appartenance — voient le même rôle.
    return [canonical_role_id(role_id) or role_id]


def has_capability(request, capability: str) -> bool:
    """True si le rôle de l'utilisateur porte la capacité RBAC demandée."""
    for role_id in roles_of(request):
        if getattr(get_role(role_id), capability, False):
            return True
    return False


def in_group(request, group) -> bool:
    """True si l'un des rôles de l'utilisateur appartient au groupe fonctionnel."""
    return bool(set(roles_of(request)) & set(group))


def is_staff(request) -> bool:
    return in_group(request, STAFF_ROLES)


# ── Délégation ────────────────────────────────────────────────────────────────

class NoDelegationAuthority(Exception):
    """Le rôle n'a aucune autorité d'approbation crédit (absent de la table)."""


def delegation_limit(roles) -> float | None:
    """Plafond d'approbation le plus élevé parmi ``roles``.

    ``None`` = illimité. Lève ``NoDelegationAuthority`` si aucun des rôles ne
    figure dans la table de délégation — cas distinct d'un plafond dépassé, et
    qui doit produire un message différent pour l'utilisateur.
    """
    limits: dict = getattr(settings, "CREDIT_DELEGATION_USD", {})
    known = [r for r in roles if r in limits]
    if not known:
        raise NoDelegationAuthority(
            "Votre rôle ne dispose d'aucune délégation d'approbation crédit."
        )

    best: float = 0.0
    for role in known:
        cap = limits[role]
        if cap is None:          # illimité — inutile de chercher plus haut
            return None
        best = max(best, float(cap))
    return best
