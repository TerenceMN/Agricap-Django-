"""Fixtures de test partagées : mock de l'appel réseau IdP `/userinfo` (aucun helper
équivalent n'existait avant ce module — tous les `tests.py` actuels des autres apps sont
des stubs vides). Point d'interception : `accounts.authentication.requests.get` (seul
appel réseau fait par `IdpBearerAuthentication.authenticate()`)."""
from __future__ import annotations

import shutil
import tempfile
from contextlib import contextmanager
from unittest.mock import patch

from django.test import override_settings
from rest_framework.test import APITestCase


class _FakeUserinfoResponse:
    status_code = 200

    def __init__(self, claims: dict) -> None:
        self._claims = claims

    def json(self) -> dict:
        return self._claims


@contextmanager
def mock_userinfo(claims: dict):
    """Patch `/userinfo` pour renvoyer `claims` (doit inclure `sub`) comme si l'IdP les
    avait validées."""
    with patch("accounts.authentication.requests.get", return_value=_FakeUserinfoResponse(claims)):
        yield


class AuthedAPITestCase(APITestCase):
    """Base pour les tests des nouvelles apps : `self.login(role=..., sub=...)` authentifie
    `self.client` comme le ferait un vrai Bearer IdP, sans IdP réel.

    `MEDIA_ROOT` est redirigé vers un dossier temporaire pour toute la durée des tests
    d'une classe : Django n'utilise PAS de stockage en mémoire pour les `FileField` par
    défaut, donc un test qui uploade un fichier (ex. `AgencyReactivation.document`) l'écrit
    RÉELLEMENT sur disque. Sans cet override, ces fichiers de test atterrissent dans le
    `media/` du serveur de dev réel, au même endroit que les vrais uploads — déjà arrivé
    une fois : un nettoyage `rm -rf` censé ne viser que des artefacts de test a supprimé un
    document réellement uploadé par un utilisateur via le serveur en cours d'exécution."""

    @classmethod
    def setUpClass(cls):
        cls._media_root_override = tempfile.mkdtemp(prefix="agricap_test_media_")
        cls._media_root_ctx = override_settings(MEDIA_ROOT=cls._media_root_override)
        cls._media_root_ctx.enable()
        super().setUpClass()

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        cls._media_root_ctx.disable()
        shutil.rmtree(cls._media_root_override, ignore_errors=True)

    def login(self, *, role: str = "client", sub: str = "test-sub", **claims) -> dict:
        payload = {"sub": sub, "role": role, "email": claims.pop("email", f"{sub}@test.local"), **claims}
        patcher = patch("accounts.authentication.requests.get", return_value=_FakeUserinfoResponse(payload))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer faketoken-{sub}")
        return payload
