"""
ViewContextService — Étape 7.

Centralise l'isolation des données et la personnalisation des vues
selon le rôle et le sub OIDC de l'utilisateur courant.

Les rôles manipulés ici sont les identifiants canoniques de `rbac.role_registry`,
regroupés par fonction dans `credits.roles` — voir ce module pour le mapping et
l'historique de l'incident de nomenclature.
"""
from __future__ import annotations

from typing import Any

from credits.roles import (
    CAN_CONFIRM_DISBURSEMENT,
    CAN_DECIDE,
    CAN_INSTRUCT,
    CAN_REQUEST_DISBURSEMENT,
    CLIENT_ROLES,
    STAFF_ROLES,
)

# Rôles considérés comme « personnel interne »
_STAFF_ROLES = STAFF_ROLES

# Actions disponibles par statut × rôle.
# Valeur : ensemble des rôles canoniques qui peuvent déclencher cette action.
# Ces ensembles pilotent `availableActions`, donc l'affichage des boutons du
# front — ils doivent rester le miroir exact des gardes serveur de `views.py`.
_ACTION_ROLES: dict[str, dict[str, set[str]]] = {
    "draft": {
        "submit": set(CLIENT_ROLES | CAN_INSTRUCT),
    },
    "submitted": {
        "start_analysis": set(CAN_INSTRUCT),
        # Seule issue d'un dossier dont la confirmation client a expire.
        "renew_consent": set(CAN_INSTRUCT),
    },
    "in_analysis": {
        "approve": set(CAN_DECIDE),
        "reject": set(CAN_DECIDE),
        "adjourn": set(CAN_INSTRUCT),
        "score": set(CAN_INSTRUCT),
    },
    "adjourned": {
        "reopen_analysis": set(CAN_INSTRUCT),
    },
    "approved": {
        "request_disbursement": set(CAN_REQUEST_DISBURSEMENT),
    },
    "pending_disbursement": {
        "confirm_disbursement": set(CAN_CONFIRM_DISBURSEMENT),
        "cancel_disbursement": set(CAN_REQUEST_DISBURSEMENT),
    },
    # `active` n'avait AUCUNE entrée : la transition `close` existait, son
    # endpoint existait, ses tests passaient — et aucun écran ne pouvait la
    # proposer, puisque `availableActions` ne la contenait jamais. Un dossier
    # décaissé restait donc `active` indéfiniment.
    #
    # Ce n'est pas un bouton manquant, c'est la boucle d'apprentissage sans
    # déclencheur (principe 10) : `n_cas_reels` restait à zéro pour les quatorze
    # filières, et chaque analyse continuait d'annoncer « référentiel indicatif,
    # fiabilité limitée » quel que soit le nombre de dossiers réellement bouclés.
    #
    # CAN_DECIDE et non CAN_INSTRUCT : clore, c'est aussi passer un « abandon de
    # créance ». Un agent de terrain monte des dossiers, il ne solde pas une
    # dette — même frontière qu'à l'approbation, et elle vaut aussi à la sortie
    # du cycle.
    "active": {
        "close": set(CAN_DECIDE),
    },
}

# Champs sensibles masqués pour les non-staff
_CLIENT_HIDDEN_FIELDS = {
    "submittedBySub",
    "reviewedBySub",
    "disbursedBySub",
    "initiatedBySub",
    "prefillSnapshot",
    "overriddenFields",
    "scoreResult",        # le client ne voit pas le détail du scoring
    "approvalComment",    # commentaire interne
}

# `agency` n'est délibérément PAS dans cette liste — décision, pas oubli.
#
# Ce qui est masqué au client relève de deux familles : les identifiants des
# PERSONNES qui ont manipulé son dossier (`*BySub`, `approvalComment`) et les
# paramètres du MOTEUR (`scoreResult`, principe 7 : ni barèmes, ni seuils, ni
# plages). L'agence d'instruction n'est ni l'un ni l'autre : c'est le guichet où
# le client s'est présenté, une information qu'il détient déjà et dont il a
# besoin pour savoir à qui s'adresser. La masquer n'aurait protégé personne — et
# aurait été un masquage de façade, puisque les vues de workflow
# (`submit`, `client_consent`…) répondent par `serialize_application` sans
# passer par ce service.
#
# Le bloc servi (`{code, name}`) ne porte AUCUN paramètre exploitable : pas de
# plafond de délégation, pas de statut, pas de gérant. Si un jour l'agence
# portait un seuil décisionnel, ce seuil resterait côté staff — ce serait alors
# la donnée à masquer, pas l'agence.
_CLIENT_VISIBLE_BY_DESIGN = {"agency"}


class ViewContextService:
    """
    Service d'isolation des données et de personnalisation des réponses.

    Usage typique dans une vue :
        vcs = ViewContextService(sub=request.sub, roles=request.roles)
        qs  = vcs.filter_qs(CreditApplication.objects.all())
        data = vcs.serialize_for_role(app)
        actions = vcs.available_actions(app)
    """

    def __init__(self, sub: str, roles: list[str]):
        from credits.roles import SUPERADMIN_ROLES

        self.sub = sub or ""
        self.roles: set[str] = set(roles or [])
        self.is_staff: bool = bool(self.roles & _STAFF_ROLES)
        self.is_admin: bool = bool(self.roles & SUPERADMIN_ROLES)

    # ── Filtrage du queryset ──────────────────────────────────────────────────

    def filter_qs(self, qs):
        """
        Restreint le queryset CreditApplication selon le rôle :
          - admin / staff → tout
          - client        → uniquement ses propres dossiers
        """
        if self.is_staff:
            return qs

        # Client : uniquement ses dossiers (initiés par lui ou pour lui)
        return qs.filter(client__sub=self.sub)

    def can_read_app(self, app) -> bool:
        """Retourne True si l'utilisateur peut consulter ce dossier."""
        if self.is_staff:
            return True
        return str(app.client.sub) == self.sub

    # ── Actions disponibles ───────────────────────────────────────────────────

    def available_actions(self, app) -> list[str]:
        """
        Liste des actions que cet utilisateur peut déclencher sur ce dossier.

        Règles :
          - basées sur status × rôle
          - maker ≠ checker : l'initiateur d'une soumission ne peut pas approuver
          - délégation : le bouton Approuver disparaît au-dessus du plafond du rôle
          - Le client peut toujours enregistrer son consentement si pending_client_consent
          - Le propriétaire du dossier (client) peut soumettre même s'il n'a que "read"
        """
        status = app.status
        actions_for_status = _ACTION_ROLES.get(status, {})
        is_owner = str(app.client.sub) == self.sub
        actions: list[str] = []

        for action, allowed_roles in actions_for_status.items():
            # Le propriétaire du dossier est traité comme ayant le rôle "client"
            effective_roles = self.roles | ({"client"} if is_owner else set())
            if not (effective_roles & allowed_roles):
                continue
            # Maker ≠ checker pour approve/confirm_disbursement
            if action == "approve" and app.submitted_by_sub == self.sub:
                continue
            # Délégation : ne pas proposer une approbation que le serveur refusera
            if action == "approve" and not self._within_delegation(app):
                continue
            if action == "confirm_disbursement":
                try:
                    dr = app.disbursement_request
                    if dr.requested_by_sub == self.sub:
                        continue
                except Exception:
                    pass
            actions.append(action)

        # Consentement client : disponible si le dossier l'exige et n'est pas expiré
        if is_owner and getattr(app, "pending_client_consent", False):
            actions.append("client_consent")

        return actions

    def _within_delegation(self, app) -> bool:
        """True si l'utilisateur peut approuver ce montant sans escalade.

        Applique côté affichage la règle que le serveur re-vérifie de toute
        façon dans `workflow.approve` : un bouton proposé puis refusé en 403 est
        une promesse non tenue faite à l'analyste.
        """
        from credits.roles import NoDelegationAuthority, delegation_limit
        from credits.workflow import _to_usd

        try:
            limit = delegation_limit(self.roles)
        except NoDelegationAuthority:
            return False
        if limit is None:
            return True

        amount = app.amount_approved or app.amount_requested
        if amount is None:
            # Montant inconnu : on laisse le serveur trancher plutôt que de
            # masquer l'action à tort.
            return True
        return _to_usd(amount, app.currency) <= limit

    # ── Sérialisation personnalisée ───────────────────────────────────────────

    def serialize_for_role(self, app) -> dict[str, Any]:
        """
        Retourne la représentation JSON du dossier adaptée au rôle.

        Staff → données complètes
        Client → données filtrées (champs internes masqués)
        """
        from credits.workflow import serialize_application
        data = serialize_application(app)
        data["availableActions"] = self.available_actions(app)

        if not self.is_staff:
            _strip_internal_fields(data)

        return data

    # ── Visibilité d'un champ ─────────────────────────────────────────────────

    def can_see_field(self, field_name: str) -> bool:
        if self.is_staff:
            return True
        return field_name not in _CLIENT_HIDDEN_FIELDS


# ── Helpers ───────────────────────────────────────────────────────────────────

def _strip_internal_fields(data: dict) -> None:
    """Supprime les champs internes d'une représentation sérialisée."""
    for field in list(data.keys()):
        if field in _CLIENT_HIDDEN_FIELDS:
            del data[field]

    # Dans le score_result, masquer le détail du barème pour le client
    # mais conserver les indicateurs synthétiques
    if "scoreResult" not in _CLIENT_HIDDEN_FIELDS:
        # Si score_result a survécu au filtre, on garde uniquement le résumé
        sr = data.get("scoreResult")
        if sr:
            data["scoreResult"] = {
                "score": sr.get("score"),
                "eligible": sr.get("eligible"),
                "valuationNote": sr.get("valuationNote"),
            }
