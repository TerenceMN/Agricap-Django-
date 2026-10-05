"""`GET /api/rbac/me` : capacités effectives de l'utilisateur courant (additif, ne modifie
pas `/api/me` existant). `GET /api/rbac/roles` : matrice complète des 16 rôles (remplace
les constantes `ROLES`/`PERMISSIONS_MATRIX` codées en dur dans `Users.jsx`/`Roles.jsx`).
`GET/PATCH /api/rbac/users` : annuaire du personnel (remplace `localStorage.
getItem('admin_users')`) — réservé à la capacité `config` (Admin IT / DG)."""
from __future__ import annotations

from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from accounts.permissions import IsStaff
from audit.services import record as audit_record
from common.parsing import to_int

from .models import DASHBOARD_VIEWS, RoleOverride, StaffProfile
from .permissions import HasCapability
from .role_registry import CAPABILITIES, ROLE_REGISTRY, canonical_role_id, get_role


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def me(request):
    user = request.user
    role = get_role(getattr(user, "role", ""))
    profile = getattr(user, "staff_profile", None)
    return Response({
        "role": role.id,
        "level": profile.level if profile else role.level,
        "zone": profile.zone if profile else "",
        "capabilities": {cap: getattr(role, cap) for cap in CAPABILITIES},
        "isSupervisor": role.is_supervisor,
        "viewOverride": profile.view_override if profile else "",
    })


def _role_row(role) -> dict:
    return {
        "id": role.id, "label": role.label, "level": role.level, "type": role.type,
        "permissions": {cap: getattr(role, cap) for cap in CAPABILITIES},
        "isSupervisor": role.is_supervisor,
        "isCustom": isinstance(role, RoleOverride) and role.is_custom,
        "isOverridden": isinstance(role, RoleOverride) and not role.is_custom,
    }


def _effective_roles() -> list:
    """Fusionne `ROLE_REGISTRY` (rôles d'usine) avec les `RoleOverride` en base — une
    ligne `RoleOverride` remplace entièrement le rôle d'usine du même `id` ; les
    `RoleOverride` sans équivalent d'usine sont des rôles personnalisés en plus."""
    merged: dict[str, object] = dict(ROLE_REGISTRY)
    for o in RoleOverride.objects.all():
        merged[o.id] = o
    return [_role_row(r) for r in merged.values()]


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def roles(request):
    if request.method == "GET":
        return Response(_effective_roles())

    # POST : création d'un rôle personnalisé — réservé à la capacité `config`.
    if not bool(getattr(get_role(getattr(request.user, "role", "")), "config", False)):
        return Response({"detail": "Capacité requise : config."}, status=403)
    data = request.data or {}
    role_id = (data.get("id") or "").strip()
    if not role_id:
        return Response({"detail": "Identifiant de rôle requis."}, status=400)
    if role_id in ROLE_REGISTRY or RoleOverride.objects.filter(id=role_id).exists():
        return Response({"detail": f"Le rôle '{role_id}' existe déjà."}, status=409)
    override = RoleOverride.objects.create(
        id=role_id,
        label=data.get("label") or role_id,
        level=int(data.get("level") or 0),
        type=data.get("type") or "Gestion",
        is_custom=True,
        created_by=getattr(request.user, "sub", ""),
        **{cap: bool((data.get("permissions") or {}).get(cap)) for cap in CAPABILITIES},
    )
    audit_record(actor=getattr(request.user, "sub", ""), action="rbac.role.create", entity_type="RoleOverride",
                 entity_id=role_id, details=data)
    return Response(_role_row(override), status=201)


@api_view(["PATCH"])
@permission_classes([HasCapability("config")])
def role_detail(request, role_id):
    base = ROLE_REGISTRY.get(role_id)
    existing_override = RoleOverride.objects.filter(id=role_id).first()
    if base is None and existing_override is None:
        return Response({"detail": "Rôle introuvable."}, status=404)

    current = existing_override or base
    data = request.data or {}
    permissions = data.get("permissions") or {}

    # On ne modifie pas les pouvoirs de SON PROPRE rôle.
    #
    # La capacité `config` suffisait à s'attribuer `disburse` — le pouvoir de
    # sortir de l'argent — et le changement prenait effet immédiatement. Vérifié :
    # `admin_it.disburse` False → True en un PATCH, par son propre titulaire.
    # C'est l'escalade de privilège la plus directe qui soit : celui qui garde
    # les clés se sert lui-même.
    #
    # Le §7.2 exige maker ≠ checker « partout où il y a de l'argent ». Modifier
    # une matrice de capacités, c'est décider qui touchera l'argent : la règle
    # s'y applique. On refuse donc l'auto-modification, sans interdire la
    # modification — un collègue porteur de `config` le fera, et le journal
    # gardera qui a demandé quoi à qui.
    #
    # Les autres champs (libellé, niveau, type) restent modifiables : ils ne
    # confèrent aucun pouvoir.
    role_appelant = getattr(request.user, "role", "") or ""
    if permissions and role_appelant == role_id:
        return Response(
            {"detail": (
                "Vous ne pouvez pas modifier les permissions de votre propre rôle "
                f"« {current.label} ». Un autre administrateur doit le faire — c'est "
                "ce qui empêche quelqu'un de s'attribuer seul des pouvoirs, "
                "notamment celui de décaisser."
            ), "code": "AUTO_ESCALADE_INTERDITE"},
            status=403,
        )
    override, _ = RoleOverride.objects.update_or_create(id=role_id, defaults={
        "label": data.get("label", current.label),
        "level": int(data.get("level", current.level)),
        "type": data.get("type", current.type),
        "is_supervisor": bool(data.get("isSupervisor", current.is_supervisor)),
        "mfa_step_up_required": bool(data.get("mfaStepUpRequired", getattr(current, "mfa_step_up_required", False))),
        "is_custom": existing_override.is_custom if existing_override else False,
        "created_by": existing_override.created_by if existing_override else getattr(request.user, "sub", ""),
        **{cap: bool(permissions.get(cap, getattr(current, cap))) for cap in CAPABILITIES},
    })
    audit_record(actor=getattr(request.user, "sub", ""), action="rbac.role.update", entity_type="RoleOverride",
                 entity_id=role_id, details=data)
    return Response(_role_row(override))


@api_view(["GET"])
@permission_classes([HasCapability("audit")])
def supervisors(request):
    """Répertoire minimal des approbateurs superviseurs (dg/dir_ops/aud_tech/aud_fin) —
    réservé aux rôles `audit` (ex. escalade d'un cas spécial), plus étroit que
    `GET /rbac/users` (réservé `config`) pour ne pas exposer tout l'annuaire du personnel."""
    from accounts.models import FintechUser
    people = FintechUser.objects.all()
    return Response([
        {"sub": u.sub, "name": u.full_name or u.email, "email": u.email, "role": u.role}
        for u in people if get_role(u.role).is_supervisor
    ])


@api_view(["GET"])
@permission_classes([IsStaff, HasCapability("create")])
def directory_search(request):
    """Recherche d'un utilisateur PAR NOM — pour les champs qui exigeaient un `sub`.

    Plus étroit que `GET /rbac/users` (réservé `config`, annuaire complet du personnel
    avec zone, plafonds et statut) : ici, une recherche d'au moins deux caractères, un
    nombre de résultats plafonné, et seulement de quoi identifier une personne. La
    capacité `create` est le bon garde : on ne cherche un titulaire ou un gestionnaire
    que pour l'ASSIGNER quelque part.

    `staff=1` restreint aux rôles non-Client — le champ « gestionnaire assigné » ne doit
    pas proposer un client.
    """
    from accounts import directory

    resultats, total = directory.search(
        request.GET.get("q", ""),
        staff_only=request.GET.get("staff") in ("1", "true", "True"),
        limit=min(to_int(request.GET.get("limit"), 20) or 20, 50),
    )
    return Response({"results": resultats, "totalRows": total,
                     "truncated": total > len(resultats)})


def _user_row(user) -> dict:
    role = get_role(user.role)
    profile = getattr(user, "staff_profile", None)
    return {
        "sub": user.sub, "name": user.full_name or user.email, "email": user.email,
        "fullName": user.full_name, "phone": user.phone, "role": user.role,
        "roleLabel": role.label, "level": profile.level if profile else role.level,
        "zone": profile.zone if profile else "", "assignmentId": profile.assignment_id if profile else None,
        "perOperationCeiling": float(profile.per_operation_ceiling)
        if profile and profile.per_operation_ceiling is not None else None,
        "status": profile.status if profile else StaffProfile.Status.ACTIF,
        "lastLogin": profile.last_login_at.isoformat() if profile and profile.last_login_at else None,
        "viewOverride": profile.view_override if profile else "",
        "security": {
            "locked": profile.locked if profile else False,
            "pinResetRequired": profile.pin_reset_required if profile else False,
            # Politique du rôle (source IdP Role.mfaRequired, miroir dans ROLE_REGISTRY) —
            # PAS le statut réel d'enrôlement MFA de l'utilisateur (TOTP/WebAuthn), qui vit
            # côté IdP et n'est pas consultable depuis ce backend.
            "mfaPolicyRequired": role.mfa_step_up_required,
        },
    }


@api_view(["GET"])
@permission_classes([HasCapability("config")])
def users(request):
    from accounts.models import FintechUser
    people = FintechUser.objects.select_related("staff_profile").all()
    return Response([_user_row(u) for u in people])


@api_view(["PATCH"])
@permission_classes([HasCapability("config")])
def user_detail(request, sub):
    from accounts.models import FintechUser
    user = FintechUser.objects.filter(sub=sub).first()
    if not user:
        return Response(
            {"detail": "Utilisateur introuvable (doit s'être connecté au moins une fois via l'IdP)."}, status=404,
        )
    data = request.data or {}
    if "role" in data:
        # Le champ `accounts.FintechUser.role` est un CharField libre : rien n'empêchait
        # d'y écrire « auditeur », « caissier » ou une faute de frappe. `get_role()` les
        # faisait alors retomber sur `client` — l'utilisateur perdait tout accès interne
        # sans qu'aucun écran ne le dise. On refuse l'écriture à la source (principe 6 :
        # tout nouveau code rejoint le référentiel existant ou n'existe pas), et on
        # canonicalise les alias de nomenclature connus plutôt que de les stocker tels
        # quels. Un rôle personnalisé (`RoleOverride`) reste évidemment accepté.
        demande = (data["role"] or "").strip()
        canonique = canonical_role_id(demande)
        if canonique is None and not RoleOverride.objects.filter(id=demande).exists():
            return Response(
                {"detail": (
                    f"Rôle inconnu : « {demande} ». Utilisez un identifiant du registre "
                    f"(GET /api/rbac/roles) — par exemple « aud_tech »/« aud_fin » pour "
                    f"un auditeur, « gest_caisse »/« agent_cash » pour une fonction de "
                    f"caisse. Un rôle non reconnu déclasserait l'utilisateur en client "
                    f"lecture seule."
                ), "code": "ROLE_INCONNU"},
                status=400,
            )
        user.role = canonique or demande
        user.save(update_fields=["role"])
    profile, _ = StaffProfile.objects.get_or_create(user=user)
    if "zone" in data:
        profile.zone = data["zone"]
    if "assignmentId" in data:
        profile.assignment_id = data["assignmentId"]
    if "perOperationCeiling" in data:
        profile.per_operation_ceiling = data["perOperationCeiling"] or None
    if "viewOverride" in data:
        view_override = data["viewOverride"] or ""
        if view_override and view_override not in DASHBOARD_VIEWS:
            return Response({"detail": f"Vue inconnue : {view_override}."}, status=400)
        profile.view_override = view_override
    profile.level = get_role(user.role).level
    profile.save()
    audit_record(actor=getattr(request.user, "sub", ""), action="rbac.user.update", entity_type="FintechUser",
                 entity_id=sub, details=data)
    return Response(_user_row(user))


@api_view(["POST"])
@permission_classes([HasCapability("config")])
def user_action(request, sub):
    from accounts.models import FintechUser
    user = FintechUser.objects.filter(sub=sub).first()
    if not user:
        return Response({"detail": "Utilisateur introuvable."}, status=404)
    profile, _ = StaffProfile.objects.get_or_create(user=user)
    action = (request.data or {}).get("action")
    if action == "suspend":
        profile.status, profile.locked = StaffProfile.Status.SUSPENDU, True
    elif action == "activate":
        profile.status, profile.locked = StaffProfile.Status.ACTIF, False
    elif action == "unlock":
        profile.locked = False
    elif action == "lock":
        # Verrouillage immédiat (ex. suspicion de compromission de compte) SANS changer le
        # statut métier "Suspendu" — distinct de `suspend`, qui couple les deux. Un compte
        # verrouillé perd toute capacité (voir `HasCapability`), même s'il reste "Actif".
        profile.locked = True
    elif action == "reset_pin":
        profile.pin_reset_required = True
    else:
        return Response({"detail": f"Action inconnue : {action}"}, status=400)
    profile.save()
    audit_record(actor=getattr(request.user, "sub", ""), action=f"rbac.user.{action}", entity_type="FintechUser",
                 entity_id=sub)
    return Response(_user_row(user))
