"""Vérification de capacité (RBAC) — lit `ROLE_REGISTRY` plutôt qu'un champ booléen figé
(`is_staff_role`) pour retrouver la granularité voulue par `Users.jsx` sans introduire de
`Group`/`Permission` Django.

`HasCapability("read")` NE SIGNIFIE PAS « interne ».
Dans `rbac.role_registry`, **tous** les rôles de type « Client » — `client`, `agri_op`,
`invest`, `investor`, `partner` — portent `read=True` : c'est ce qui leur permet de lire
LEURS propres données. Un endpoint qui sert une donnée de l'institution (grand livre,
trésorerie, KYC de tiers, agrégats, référentiels chiffrés) doit donc CUMULER
`accounts.permissions.IsStaff` avec sa capacité :

    @permission_classes([IsStaff, HasCapability("read")])

Le cumul, et non le remplacement : `IsStaff` ne regarde que le TYPE du rôle et ignore la
suspension d'un profil (`StaffProfile.locked`), que seul `HasCapability` vérifie. Garder
les deux, c'est garder les deux garanties.

Un endpoint qui sert une donnée APPARTENANT à l'appelant reste en `read` seul — mais il
filtre alors par propriétaire dans la requête (`user=request.user`, `investor__user=...`),
jamais par un `get_object_or_404(pk)` nu : c'est la classe de faille (IDOR) que le garde
de capacité ne peut structurellement pas couvrir.
"""
from __future__ import annotations

from rest_framework.permissions import BasePermission

from .role_registry import get_role


def HasCapability(capability: str):
    class _HasCapability(BasePermission):
        message = f"Capacité requise : {capability}."

        def has_permission(self, request, view) -> bool:
            user = getattr(request, "user", None)
            if not user or not getattr(user, "is_authenticated", False):
                return False
            # Un profil suspendu perd immédiatement TOUTE capacité, y compris "read" —
            # sans ça, "Suspendre" un utilisateur (Users.jsx) ne bloquerait rien réellement.
            profile = getattr(user, "staff_profile", None)
            if profile is not None and profile.locked:
                return False
            role = get_role(getattr(user, "role", ""))
            return bool(getattr(role, capability, False))

    return _HasCapability


def CapaciteSelonMethode(**par_methode: str):
    """Fabrique une permission qui exige une capacité DIFFÉRENTE selon la méthode HTTP.

    Usage : `CapaciteSelonMethode(GET="read", POST="create")`. Une méthode absente de la
    table est REFUSÉE — le défaut est fermé, jamais ouvert.

    Elle existe parce que la majorité des vues de ce backend sont multi-méthodes :
    `GET /ledger/entries` est de la lecture, `POST /ledger/entries` poste une écriture
    comptable. Les déclarer sous une capacité unique (`read`) mettait le second acte à
    la portée de tout porteur de `read` — c'est-à-dire de n'importe quel client. Rendre
    la matrice explicite sur le décorateur, plutôt que dans un `if request.method` du
    corps, applique « toute vue sans permission explicite est un bug » (CONVENTIONS.md §5).

    **Définition faisant autorité** (principe 6, dette résorbée). Le mécanisme a existé en
    trois exemplaires : ici, dans `accounting.permissions.CapaciteSelonMethode` (jumeau mot
    pour mot, écrit d'abord pour le seul socle comptable) et dans
    `fx.permissions.CapabilityByMethod` (même chose sous une signature safe/unsafe). `rbac`
    est la bonne maison — aucune app ne doit dépendre de `accounting` pour un garde
    générique. Les deux autres ne sont plus qu'un ré-export et un adaptateur pointant ici ;
    toute évolution de la règle se fait donc dans cette fonction et nulle part ailleurs.
    """
    table = {methode.upper(): capacite for methode, capacite in par_methode.items()}

    class _CapaciteSelonMethode(BasePermission):
        message = "Capacité insuffisante pour cette méthode."

        def has_permission(self, request, view) -> bool:
            capacite = table.get((request.method or "").upper())
            if capacite is None:
                return False
            self.message = f"Capacité requise : {capacite}."
            return HasCapability(capacite)().has_permission(request, view)

    return _CapaciteSelonMethode
