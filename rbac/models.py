"""Extension fintech-spécifique de l'identité IdP (zone/agence/niveau) — additive, n'ajoute
aucune colonne à `accounts.FintechUser` (aucun risque de migration sur une table déjà en
prod)."""
from __future__ import annotations

from django.db import models

# Les 6 vues/dashboards existants (voir `menuKeyFor`/`allMenuItems` dans
# `src/components/Layout.jsx`) — utilisé par `StaffProfile.view_override` pour affecter un
# utilisateur donné à une vue précise indépendamment du mapping par défaut de son rôle.
DASHBOARD_VIEWS = ("client", "investor", "admin", "comptable", "caissier", "auditeur")


class StaffProfile(models.Model):
    class Status(models.TextChoices):
        ACTIF = "Actif", "Actif"
        SUSPENDU = "Suspendu", "Suspendu"

    user = models.OneToOneField(
        "accounts.FintechUser", primary_key=True, on_delete=models.CASCADE, related_name="staff_profile",
    )
    zone = models.CharField(max_length=120, blank=True)
    assignment = models.ForeignKey(
        "agencies.Agency", null=True, blank=True, on_delete=models.SET_NULL, related_name="staff",
    )
    level = models.IntegerField(default=0)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.ACTIF)
    # Plafond individuel par opération (transfert/ajustement de caisse exécuté en un seul
    # acteur, cf. `caisses.services.transfer_funds`/`adjust_account`) — null = pas de
    # plafond configuré (comportement historique, aucune restriction). Distinct des seuils
    # auto/manager/quorum déjà en place (`transactions.ValidationThreshold`,
    # `caisses.WithdrawalThreshold`/`RegularizationThreshold`) : ceux-ci gouvernent un
    # workflow multi-parties ; celui-ci borne ce qu'UN staff peut exécuter seul.
    per_operation_ceiling = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    locked = models.BooleanField(default=False)
    pin_reset_required = models.BooleanField(default=False)
    view_override = models.CharField(
        max_length=20, blank=True, choices=[(v, v) for v in DASHBOARD_VIEWS],
    )
    last_login_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return f"StaffProfile({self.user_id})"


class RoleOverride(models.Model):
    """Permet à un Admin IT/DG (capacité `config`) de modifier la matrice de capacités
    d'un rôle existant, ou de créer un rôle entièrement personnalisé — sans toucher au
    code (`rbac/role_registry.py` reste le jeu de rôles "d'usine"). Une ligne présente ici
    REMPLACE entièrement la définition de base pour ce `id` (pas de fusion champ par
    champ) : `rbac.role_registry.get_role()` la retourne en priorité. Champs et noms
    dupliqués intentionnellement sur `RoleDef` pour un accès par attribut identique
    (duck-typing) partout où `get_role(...)` est déjà consommé (permissions, ABAC,
    serializers)."""
    id = models.CharField(max_length=40, primary_key=True)
    label = models.CharField(max_length=120)
    level = models.IntegerField(default=0)
    type = models.CharField(max_length=40, default="Gestion")
    read = models.BooleanField(default=True)
    create = models.BooleanField(default=False)
    validate = models.BooleanField(default=False)
    disburse = models.BooleanField(default=False)
    audit = models.BooleanField(default=False)
    config = models.BooleanField(default=False)
    #: Gestion des coopératives, groupes et équipes — voir `RoleDef.cooperatives`.
    cooperatives = models.BooleanField(default=False)
    is_supervisor = models.BooleanField(default=False)
    mfa_step_up_required = models.BooleanField(default=False)
    is_custom = models.BooleanField(default=False)
    created_by = models.CharField(max_length=120, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return f"RoleOverride({self.id})"
