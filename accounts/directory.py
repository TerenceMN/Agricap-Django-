"""Annuaire : résolution d'identifiants IdP (`sub`) en noms lisibles.

Un `sub` n'est pas un nom. Plusieurs écrans d'administration affichent des acteurs
stockés en `CharField` (`AuditEntry.actor`, `Investor.assigned_manager_sub`,
`Project.manager_sub`…) — pas des FK, parce que l'acteur d'une action système peut
être vide ou avoir disparu et qu'une FK stricte casserait l'écriture de la trace
elle-même. La résolution est donc un service, pas une jointure.

Ce module est LE point unique de cette résolution (principe 6) : il vient de
`audit.views._names_by_sub`, remonté ici parce que `accounts` possède le miroir IdP.
Tout écran qui affiche un `sub` passe par ici — jamais par une requête maison.
"""
from __future__ import annotations

from django.db.models import Q


def search(query: str, *, staff_only: bool = False, limit: int = 20) -> tuple[list[dict], int]:
    """`(résultats, effectif total)` — recherche d'un utilisateur PAR NOM.

    Le pendant de `names_by_sub` : celle-ci résout un `sub` connu en nom, celle-là part
    du nom pour retrouver le `sub`. Les écrans d'administration demandaient jusqu'ici de
    SAISIR un `sub` (« Identifiant utilisateur IdP », « sub du gestionnaire ») — un
    identifiant opaque que personne ne connaît par cœur et qui se recopie de travers.

    `staff_only` restreint aux rôles non-Client, pour les champs qui désignent un membre
    du personnel (gestionnaire assigné) et non un titulaire de compte.

    L'effectif total est rendu à part : une liste plafonnée qui ne dit pas combien de
    résultats existent laisse croire qu'on a tout vu (§4.6).
    """
    from rbac.role_registry import get_role

    from .models import FintechUser

    query = (query or "").strip()
    if len(query) < 2:
        return [], 0

    qs = FintechUser.objects.filter(
        Q(full_name__icontains=query) | Q(email__icontains=query) | Q(sub__icontains=query)
    ).order_by("full_name", "email")

    if staff_only:
        # Le filtre de catégorie n'est pas exprimable en SQL (il vit dans le registre
        # RBAC, `RoleOverride` compris) : on filtre en Python sur un jeu déjà réduit
        # par la recherche, pas sur la table entière.
        lignes = [u for u in qs[:200] if get_role(u.role).type != "Client"]
    else:
        lignes = list(qs[:200])

    total = len(lignes)
    return [
        {
            "sub": u.sub,
            "name": u.full_name or u.email or u.sub,
            "email": u.email,
            "role": u.role,
            "roleLabel": get_role(u.role).label,
        }
        for u in lignes[:limit]
    ], total


def names_by_sub(subs) -> dict[str, str]:
    """Résout un ensemble de `sub` en noms lisibles, en UN seul aller-retour.

    Les `sub` inconnus sont absents du dictionnaire — ils ne sont pas mappés sur une
    chaîne vide : l'appelant décide de son repli (le `sub` lui-même, « Système »,
    « Non assigné »), et ce choix dépend de l'écran. Le repli interne, lui, est
    fixé : nom complet, à défaut email — jamais un nom fabriqué à partir du `sub`.
    """
    subs = {s for s in subs if s}
    if not subs:
        return {}
    from .models import FintechUser
    return {
        u.sub: (u.full_name or u.email)
        for u in FintechUser.objects.filter(sub__in=subs)
    }
