"""Signature des requêtes vers la plateforme de paiement **Makuta** (Wolf Technologies).

Le partenaire (AGRICAP) génère sa paire de clés RSA, garde la privée secrète, et
transmet la publique à Makuta qui vérifie nos signatures. Makuta ne délivre aucun
certificat : la gestion de la clé nous appartient entièrement.

Contenu signé, selon la documentation fournisseur :
  * GET  → **le chemin seul** (`/api/transactions/XYZ123`) ;
  * POST → **le corps JSON compact** (sans espace, sans retour à la ligne).

Contrairement à `common/sms.py`, rien ne dégrade ici : toute anomalie lève. Un
paiement qui échoue en silence produit un écart de trésorerie invisible jusqu'au
rapprochement.

Trois écarts dans la documentation fournisseur, à soulever avec Wolf Technologies :

1. « Padding PKCS#8 v1.5 » est un format de CLÉ, pas un padding de signature.
   L'exemple PHP dit `RSA::SIGNATURE_PKCS1` — c'est PKCS#1 v1.5 qui est implémenté.
2. Le nom d'en-tête se contredit : `X-Makuta-Signature` (tableau normatif) vs
   `X-Signature` (exemple). Le défaut suit le normatif, `MAKUTA_SIGNATURE_HEADER`
   le rend configurable. **À confirmer avant mise en production.**
3. Ni horodatage ni nonce dans le contenu signé : le rejeu n'est pas détectable
   côté destinataire. On se protège via `common.idempotency` et une référence
   propre par opération — ce n'est pas un substitut à une protection chez Makuta.

Le catalogue des opérations métier (montants, devises, statuts, rappels) manque
encore : la documentation reçue ne couvre que l'authentification.
"""
from __future__ import annotations

import base64
import json
import logging
from typing import Any

import requests
from django.conf import settings

from .exceptions import BusinessError

logger = logging.getLogger("agricap")

#: (connexion, lecture). Un paiement qui pend bloque une caisse : on préfère
#: échouer franchement et laisser l'idempotence permettre la reprise.
_TIMEOUT_SECONDS = (5, 20)

#: Valeur du tableau normatif de la documentation. Voir l'écart n°2 ci-dessus.
DEFAULT_SIGNATURE_HEADER = "X-Makuta-Signature"


class MakutaError(BusinessError):
    """Toute anomalie de l'intégration Makuta. Jamais silencieuse."""

    code = "makuta_error"
    http_status = 502


class MakutaConfigurationError(MakutaError):
    """Clé ou URL absente/illisible — défaut de déploiement, pas d'exécution."""

    code = "makuta_not_configured"
    http_status = 503


class MakutaTransportError(MakutaError):
    """Réseau injoignable ou réponse illisible. Le sort de l'opération est INCONNU."""

    code = "makuta_unreachable"
    http_status = 502


class MakutaRefused(MakutaError):
    """Makuta a répondu et a refusé. Le sort de l'opération est connu : refusée."""

    code = "makuta_refused"
    http_status = 402


def _config() -> dict:
    config = getattr(settings, "MAKUTA", None) or {}
    if not isinstance(config, dict):
        raise MakutaConfigurationError("`settings.MAKUTA` doit être un dictionnaire.")
    return config


def _private_key():
    """Charge la clé privée RSA. Jamais mise en cache module : une clé tournée
    doit prendre effet au redémarrage du process, pas à l'expiration d'un cache
    dont personne ne connaît la durée."""
    try:
        from cryptography.hazmat.primitives import serialization
    except ImportError as exc:  # pragma: no cover - dépendance déclarée
        raise MakutaConfigurationError(
            "Le paquet `cryptography` est requis pour signer les requêtes Makuta."
        ) from exc

    config = _config()
    pem = config.get("PRIVATE_KEY_PEM") or ""
    if not pem and config.get("PRIVATE_KEY_PATH"):
        try:
            with open(config["PRIVATE_KEY_PATH"], "rb") as handle:
                pem = handle.read().decode("utf-8")
        except OSError as exc:
            # `str(exc)` porte le CHEMIN, jamais le contenu : un message d'erreur
            # ne doit pas pouvoir devenir un canal d'exfiltration de la clé.
            raise MakutaConfigurationError(
                f"Clé privée Makuta illisible : {exc.strerror or 'accès refusé'}."
            ) from exc
    if not pem:
        raise MakutaConfigurationError(
            "Clé privée Makuta absente (MAKUTA_PRIVATE_KEY_PEM ou MAKUTA_PRIVATE_KEY_PATH)."
        )

    passphrase = config.get("PRIVATE_KEY_PASSPHRASE") or None
    try:
        return serialization.load_pem_private_key(
            pem.encode("utf-8"),
            password=passphrase.encode("utf-8") if passphrase else None,
        )
    except (ValueError, TypeError) as exc:
        # Volontairement sans `from exc` dans le message : les exceptions de
        # `cryptography` ne portent pas la clé, mais on ne prend pas le pari.
        raise MakutaConfigurationError(
            "Clé privée Makuta invalide : PEM illisible ou phrase de passe incorrecte."
        ) from None


def canonical_body(payload: dict[str, Any]) -> bytes:
    """Corps JSON compact — **les octets exacts qui seront signés ET envoyés**.

    Le piège de cette intégration tient en une phrase : si l'on signe une chose
    et qu'on en envoie une autre, la signature est valide et pourtant rejetée.
    C'est ce qui arrive dès qu'on signe `json.dumps(payload)` puis qu'on passe
    `json=payload` à `requests`, lequel re-sérialise à sa façon. D'où cette
    fonction unique, et l'envoi en `data=<octets>` — jamais en `json=`.

    L'ordre des clés est celui d'insertion, PAS trié : l'exemple de la
    documentation (`{"owner":...,"email":...}`) n'est pas trié alphabétiquement,
    donc le fournisseur vérifie sur les octets reçus et n'impose aucun ordre.
    """
    return json.dumps(
        payload, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")


def sign_bytes(data: bytes) -> str:
    """Signature RSA/SHA-256, padding PKCS#1 v1.5, encodée en base64."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding, rsa

    key = _private_key()
    if not isinstance(key, rsa.RSAPrivateKey):
        raise MakutaConfigurationError(
            "La clé privée Makuta doit être une clé RSA (le fournisseur n'accepte que RSA)."
        )
    signature = key.sign(data, padding.PKCS1v15(), hashes.SHA256())
    return base64.b64encode(signature).decode("ascii")


def sign_payload(payload: dict[str, Any]) -> tuple[bytes, str]:
    """Corps compact + sa signature, dans cet ordre — les deux vont ensemble."""
    body = canonical_body(payload)
    return body, sign_bytes(body)


def sign_path(path: str) -> str:
    """Signature d'un GET : la documentation dit « le chemin » et n'illustre
    aucune requête avec chaîne de requête. On signe donc la chaîne fournie
    telle quelle, et l'appelant est responsable de passer exactement ce qui
    partira sur le réseau. **Point à confirmer auprès du fournisseur** : la
    chaîne de requête entre-t-elle dans le contenu signé ?"""
    if not path.startswith("/"):
        raise MakutaError("Le chemin à signer doit commencer par « / ».")
    return sign_bytes(path.encode("utf-8"))


def verify(data: bytes, signature_b64: str, public_key_pem: str) -> bool:
    """Vérifie une signature avec une clé publique PEM.

    Deux usages : contrôler notre propre signature en test, et — surtout —
    authentifier un **rappel entrant** de Makuta. Attention : la documentation
    fournie ne couvre QUE le sens partenaire → Makuta. Si la plateforme nous
    notifie l'issue d'un paiement, il faut obtenir SA clé publique et le format
    exact de ce qu'elle signe. Sans cela, un rappel non authentifié permettrait à
    n'importe qui de nous déclarer un paiement reçu.
    """
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    try:
        key = serialization.load_pem_public_key(public_key_pem.encode("utf-8"))
    except (ValueError, TypeError) as exc:
        raise MakutaConfigurationError("Clé publique PEM invalide.") from exc
    try:
        key.verify(base64.b64decode(signature_b64), data, padding.PKCS1v15(), hashes.SHA256())
        return True
    except (InvalidSignature, ValueError):
        return False


def _headers(signature: str, *, json_body: bool) -> dict[str, str]:
    header_name = _config().get("SIGNATURE_HEADER") or DEFAULT_SIGNATURE_HEADER
    headers = {header_name: signature, "Accept": "application/json"}
    if json_body:
        headers["Content-Type"] = "application/json"
    return headers


def _base_url() -> str:
    url = (_config().get("BASE_URL") or "").rstrip("/")
    if not url:
        raise MakutaConfigurationError("URL Makuta absente (MAKUTA_BASE_URL).")
    return url


def _read(response: requests.Response) -> Any:
    try:
        return response.json()
    except ValueError as exc:
        raise MakutaTransportError(
            f"Réponse Makuta illisible (HTTP {response.status_code}) : attendu du JSON."
        ) from exc


def get(path: str) -> Any:
    """GET signé. La signature porte le chemin seul (cf. `sign_path`)."""
    url = f"{_base_url()}{path}"  # cf. `post` : résoudre avant de signer/journaliser
    signature = sign_path(path)
    logger.info("[MAKUTA] GET %s", path)
    try:
        response = requests.get(
            url, headers=_headers(signature, json_body=False),
            timeout=_TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        raise MakutaTransportError(f"Makuta injoignable : {exc.__class__.__name__}.") from exc
    body = _read(response)
    if response.status_code >= 400:
        raise MakutaRefused(f"Makuta a refusé la lecture (HTTP {response.status_code}).")
    return body


def post(path: str, payload: dict[str, Any]) -> Any:
    """POST signé.

    Les octets signés sont EXACTEMENT ceux transmis (`data=body`) : voir
    `canonical_body`. Un `json=payload` ici ré-encoderait le corps et invaliderait
    la signature de façon parfaitement silencieuse côté émetteur.

    Cette fonction n'est PAS idempotente à elle seule — le protocole Makuta
    n'offre aucun jeton d'idempotence. L'appelant DOIT l'encadrer avec
    `common.idempotency.begin/complete`, sans quoi un rejeu de la requête HTTP
    (retentative réseau, double clic, relance de tâche) paie deux fois.
    """
    # URL résolue AVANT de signer et de journaliser : sinon le journal annonce un
    # POST qui n'est jamais parti, et sur un module de paiement une trace qui
    # ment sur ce qui a été émis rend la réconciliation impossible.
    url = f"{_base_url()}{path}"
    body, signature = sign_payload(payload)
    logger.info("[MAKUTA] POST %s (%d octets signés)", path, len(body))
    try:
        response = requests.post(
            url, data=body, headers=_headers(signature, json_body=True),
            timeout=_TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        # Le sort de l'opération est INCONNU : la requête a pu aboutir côté
        # Makuta avant la coupure. On ne présume jamais de l'échec d'un paiement
        # sur une erreur de transport — c'est un statut à réconcilier, pas un
        # échec à rejouer à l'aveugle.
        raise MakutaTransportError(
            f"Makuta injoignable ({exc.__class__.__name__}) — issue du paiement INCONNUE, "
            "à réconcilier avant toute nouvelle tentative."
        ) from exc
    body_json = _read(response)
    if response.status_code >= 400:
        raise MakutaRefused(f"Makuta a refusé l'opération (HTTP {response.status_code}).")
    return body_json


def is_configured() -> bool:
    """Vrai si une clé et une URL sont disponibles — permet à une vue d'annoncer
    « paiement indisponible » proprement plutôt que d'échouer en 503 sur un clic."""
    try:
        _base_url()
        _private_key()
        return True
    except MakutaError:
        return False
