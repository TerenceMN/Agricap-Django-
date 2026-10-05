from rest_framework.permissions import BasePermission


class IsStaff(BasePermission):
    """Rôles internes (admin/manager) — requis pour l'ingestion du référentiel."""
    message = "Réservé au personnel AGRICAP (admin/manager)."

    def has_permission(self, request, view) -> bool:
        user = getattr(request, "user", None)
        return bool(user and getattr(user, "is_staff_role", False))
