"""Synchronisation du profil interne depuis le rôle IdP — appelé à chaque login
(`accounts/authentication.py::_provision()`). Ne touche jamais `zone`/`assignment`/
`status`/`locked` (gérés séparément par un admin via `views.user_action`)."""
from __future__ import annotations

from django.utils import timezone

from .models import StaffProfile
from .role_registry import get_role


def sync_staff_profile(user) -> StaffProfile:
    role = get_role(user.role)
    profile, _ = StaffProfile.objects.update_or_create(
        user=user, defaults={"level": role.level, "last_login_at": timezone.now()},
    )
    return profile
