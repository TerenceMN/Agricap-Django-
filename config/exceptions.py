"""Gestionnaire d'exceptions DRF global : mappe les exceptions métier
(`common.exceptions.BusinessError` et ses sous-classes) vers leur code HTTP. N'affecte pas
les vues existantes (elles continuent de construire leurs `Response()` à la main) — ce
handler ne s'active que pour les exceptions non interceptées par la vue, donc uniquement
les nouvelles apps qui lèvent des `BusinessError`."""
from __future__ import annotations

import logging

from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler

from common.exceptions import BusinessError

logger = logging.getLogger("agricap")


def agricap_exception_handler(exc, context):
    if isinstance(exc, BusinessError):
        return Response({"detail": str(exc), "code": exc.code}, status=exc.http_status)

    response = drf_exception_handler(exc, context)
    if response is not None:
        return response

    logger.exception("Erreur non gérée", exc_info=exc)
    return Response({"detail": "Erreur interne."}, status=500)
