"""
Miroir local de l'identité IdP (provisioning JIT). L'IdP AGRICAP reste la source
de vérité ; on ne stocke ni mot de passe ni secret. Clé = `sub` du jeton.
"""
from __future__ import annotations

from django.db import models


class FintechUser(models.Model):
    sub = models.CharField(max_length=255, primary_key=True)     # sub IdP
    email = models.EmailField(blank=True)
    full_name = models.CharField(max_length=200, blank=True)
    role = models.CharField(max_length=40, default="client")     # client|investor|admin|manager…
    phone = models.CharField(max_length=40, blank=True)

    # Claims métier utiles (mobile money / KYC), lus s'ils sont présents.
    farmer_id = models.CharField(max_length=64, blank=True)
    national_id = models.CharField(max_length=100, blank=True)
    company_name = models.CharField(max_length=150, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    # --- Contrat DRF (request.user) --------------------------------------------
    @property
    def is_authenticated(self) -> bool:
        return True

    @property
    def is_staff_role(self) -> bool:
        # Généralise l'ancien binaire admin/manager aux 16 rôles métier (même bascule que
        # `support.views` pour la visibilité des tickets) : tout rôle non-Client est "staff".
        from rbac.role_registry import get_role
        return get_role(self.role).type != "Client"

    def __str__(self) -> str:
        return f"{self.email or self.sub} ({self.role})"
