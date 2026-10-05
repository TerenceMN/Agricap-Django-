"""Logging structuré des requêtes API : une ligne par requête (méthode, chemin, statut,
utilisateur, durée) — vient compléter l'access log basique de `runserver` (qui n'expose
pas l'identité de l'appelant) sans dupliquer le journal d'audit métier (`audit.services.
record`, réservé aux mutations)."""
from __future__ import annotations

import logging
import time

logger = logging.getLogger("agricap.requests")


class RequestLoggingMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        start = time.monotonic()
        response = self.get_response(request)
        duration_ms = round((time.monotonic() - start) * 1000)
        # `request.user` n'est peuplé qu'après le passage dans la vue DRF (IdpBearerAuthentication
        # le resynchronise sur le HttpRequest sous-jacent une fois résolu) — donc lu ici, après
        # `get_response()`, pas avant.
        user = getattr(request, "user", None)
        sub = user.sub if user is not None and getattr(user, "is_authenticated", False) else "-"
        level = logging.WARNING if response.status_code >= 500 else logging.INFO
        logger.log(level, "%s %s -> %s (%sms) user=%s",
                   request.method, request.path, response.status_code, duration_ms, sub)
        return response
