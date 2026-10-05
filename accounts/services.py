"""UserDisplayService — résolution UUID → nom humain, utilisé partout dans l'application.
Plus jamais d'UUID visible à l'écran pour un utilisateur final."""
from __future__ import annotations

from .models import FintechUser


class UserDisplayService:
    SYSTEM = {"displayName": "Système AGRICAP", "initials": "SYS", "role": "system", "isSystem": True}

    @staticmethod
    def _build(user: FintechUser) -> dict:
        name = user.full_name or user.email or f"Client {str(user.sub)[-4:]}"
        parts = [p for p in name.split() if p]
        initials = (parts[0][0] + parts[-1][0]).upper() if len(parts) >= 2 else (parts[0][:2].upper() if parts else "??")
        return {"displayName": name, "initials": initials, "role": user.role, "isSystem": False}

    @classmethod
    def resolve(cls, sub: str) -> dict:
        if not sub or sub == "system":
            return cls.SYSTEM
        try:
            return cls._build(FintechUser.objects.get(sub=sub))
        except FintechUser.DoesNotExist:
            return {"displayName": f"Agent {sub[:8]}", "initials": sub[:2].upper(), "role": "unknown", "isSystem": False}

    @classmethod
    def resolve_many(cls, subs: set) -> dict:
        """Une seule requête pour N subs — à préférer dans les boucles."""
        result: dict[str, dict] = {"system": cls.SYSTEM, "": cls.SYSTEM}
        real = {s for s in subs if s and s != "system"}
        for u in FintechUser.objects.filter(sub__in=real):
            result[u.sub] = cls._build(u)
        for sub in real:
            if sub not in result:
                result[sub] = {"displayName": f"Agent {sub[:8]}", "initials": sub[:2].upper(), "role": "unknown", "isSystem": False}
        return result
