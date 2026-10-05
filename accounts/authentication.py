"""
Authentification DRF par jeton IdP : le SPA obtient un access token (OIDC/PKCE)
auprès de l'IdP AGRICAP et l'envoie en Bearer. On valide en appelant `/userinfo`
(validation déléguée → révocation respectée), puis on provisionne l'utilisateur
local (JIT). Résolutions mises en cache (TTL) + déduplication des appels concurrents.
"""
from __future__ import annotations

import threading
import time

import requests
from django.conf import settings
from rest_framework import authentication, exceptions

from .models import FintechUser

_cache: dict[str, tuple[float, str]] = {}   # token -> (expiry_ts, sub)
_lock = threading.Lock()

# Verrou par jeton : une rafale de requêtes concurrentes portant le MÊME token frais (page
# non encore en cache) ne doit déclencher qu'UN seul appel /userinfo + provisioning JIT — pas
# un par requête (sans ça, N requêtes simultanées font N écritures concurrentes sur la même
# ligne FintechUser/StaffProfile → "database is locked" sur SQLite).
_token_locks: dict[str, threading.Lock] = {}
_token_locks_guard = threading.Lock()


def _lock_for(token: str) -> threading.Lock:
    with _token_locks_guard:
        lock = _token_locks.get(token)
        if lock is None:
            lock = threading.Lock()
            _token_locks[token] = lock
        return lock


def _pick(v):
    return v[0] if isinstance(v, list) and v else (v if not isinstance(v, list) else None)


def _provision(claims: dict) -> FintechUser:
    sub = claims["sub"]
    role = _pick(claims.get("role")) or _pick(claims.get("agricap_role")) or "client"
    data = {
        "email": claims.get("email") or f"{sub}@idp.local",
        "full_name": claims.get("name") or "",
        "phone": claims.get("phone_number") or "",
        "farmer_id": claims.get("farmer_id") or "",
        "national_id": claims.get("national_id") or "",
        "company_name": claims.get("company_name") or "",
    }
    # Écrire uniquement sur création ou changement réel. `update_or_create`
    # écrivait la ligne à CHAQUE requête authentifiée — c'est ce write inutile,
    # multiplié par les appels concurrents d'un même chargement de page, qui
    # produisait la contention d'écriture sur SQLite. Les claims d'un jeton ne
    # changent qu'à la connexion suivante ; relire et comparer suffit.
    user = FintechUser.objects.filter(sub=sub).first()
    if user is None:
        # `role` du claim IdP : seule fois où sa valeur compte. Une fois la
        # ligne créée, `rbac.views.user_detail` (PATCH /rbac/users/<sub>,
        # capacité `config`, audité) devient la SEULE source qui l'écrit.
        user = FintechUser.objects.create(sub=sub, role=role, **data)
    else:
        # `role` DÉLIBÉRÉMENT hors de `data` : le resynchroniser depuis le
        # claim IdP à chaque jeton écrasait silencieusement toute affectation
        # faite depuis /users — un admin change le rôle, l'écran confirme,
        # puis le prochain jeton de la personne (TTL du cache /userinfo) le
        # remettait à ce que l'IdP avait au moment de sa connexion. Deux
        # sources pour un même champ, la mauvaise qui gagnait (principe 6).
        modifie = [champ for champ, valeur in data.items()
                   if getattr(user, champ) != valeur]
        if modifie:
            for champ in modifie:
                setattr(user, champ, data[champ])
            user.save(update_fields=modifie)
    from rbac.services import sync_staff_profile
    sync_staff_profile(user)
    return user


class IdpBearerAuthentication(authentication.BaseAuthentication):
    keyword = "Bearer"

    def authenticate_header(self, request):
        # Sans ceci, DRF renvoie 403 sur échec d'auth. Avec, il renvoie 401 →
        # le SPA déclenche son rafraîchissement de jeton (retry sur 401).
        return "Bearer"

    def authenticate(self, request):
        header = authentication.get_authorization_header(request).decode("utf-8")
        if not header or not header.lower().startswith("bearer "):
            return None  # laisse passer (endpoints publics) ; IsAuthenticated tranchera
        token = header.split(" ", 1)[1].strip()

        ttl = settings.IDP["USERINFO_CACHE_TTL"]

        def _cached_user():
            with _lock:
                cached = _cache.get(token)
            if cached and cached[0] > time.time():
                return FintechUser.objects.filter(sub=cached[1]).first()
            return None

        user = _cached_user()
        if user:
            return (user, token)

        # Cache manquant : une seule requête par jeton fait le travail (appel /userinfo +
        # provisioning JIT) ; les autres, arrivées en rafale avec le même jeton frais,
        # attendent ici puis relisent le cache (désormais rempli) au lieu de dupliquer
        # l'appel réseau et l'écriture DB.
        with _lock_for(token):
            user = _cached_user()
            if user:
                return (user, token)

            try:
                resp = requests.get(
                    settings.IDP["USERINFO_URL"],
                    headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                    timeout=8,
                )
            except requests.RequestException as exc:
                raise exceptions.AuthenticationFailed(f"IdP injoignable : {exc}")

            if resp.status_code != 200:
                with _lock:
                    _cache.pop(token, None)
                raise exceptions.AuthenticationFailed("Jeton invalide ou expiré.")

            claims = resp.json()
            if not claims.get("sub"):
                raise exceptions.AuthenticationFailed("Réponse /userinfo sans sub.")

            user = _provision(claims)
            with _lock:
                _cache[token] = (time.time() + ttl, user.sub)
        return (user, token)
