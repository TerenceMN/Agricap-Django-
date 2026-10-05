"""Diagnostic du contrat fournisseur Makuta — ce qui manque, nommément, pour que l'argent
puisse réellement bouger.

Ce module ne devine RIEN. Il ne propose aucune valeur par défaut plausible, ne complète
aucun chemin, n'invente aucun nom de champ. Il fait une seule chose : confronter
`settings.MAKUTA` à ce dont `caisses/payments.py` a besoin pour fonctionner, et produire la
liste des manques — chacun accompagné de LA question à poser à Wolf Technologies.

La raison de cette rigueur est dans `payments.py` : la documentation fournisseur décrit
l'authentification et rien d'autre. Tant que le catalogue d'opérations et le vocabulaire de
statuts ne sont pas fournis, le connecteur refuse franchement. Ce diagnostic transforme ce
refus en checklist actionnable au lieu d'une erreur rencontrée un paiement à la fois.

Rendu par `manage.py check_makuta` ; testé directement (le rendu est une présentation, le
diagnostic est la donnée).
"""
from __future__ import annotations

from typing import Any

from django.conf import settings

from common import makuta
from common.makuta import MakutaError

from . import channels

# ── États d'un point de contrôle ──────────────────────────────────────────────
#: Marqueurs volontairement SANS accent : ils doivent rester lisibles même dans une console
#: Windows mal encodée, où ils sont la seule chose qu'on lit en diagonale.
OK = "OK"                    # fourni et exploitable en l'état
MISSING = "MANQUE"           # bloquant : la fonction concernée ne peut pas s'exécuter
TO_CONFIRM = "A CONFIRMER"   # exploitable par défaut, mais non confirmé par le fournisseur
GAP = "ECART"                # fourni, mais incompatible avec ce que le connecteur sait faire

#: Seuls ces états empêchent un paiement d'aboutir.
BLOCKING_STATES = (MISSING, GAP)


def _config() -> dict:
    config = getattr(settings, "MAKUTA", None) or {}
    return config if isinstance(config, dict) else {}


def _item(key: str, label: str, state: str, detail: str, ask: str = "") -> dict[str, str]:
    """Un point de contrôle. `ask` est la question telle qu'elle sera posée au fournisseur —
    formulée pour être copiée dans un courriel, pas pour être relue par un développeur."""
    return {"key": key, "label": label, "state": state, "detail": detail, "ask": ask}


# ═══════════════════════════════════════════════════ SOCLE D'AUTHENTIFICATION

def _transport_section() -> dict:
    config = _config()
    items = []

    base_url = (config.get("BASE_URL") or "").strip()
    items.append(_item(
        "BASE_URL", "URL de la plateforme (MAKUTA_BASE_URL)",
        OK if base_url else MISSING,
        base_url or "Absente : aucune requête ne peut être adressée au fournisseur.",
        ask="URL de base de l'API Makuta, pour la production ET pour un environnement de test.",
    ))

    has_key = bool((config.get("PRIVATE_KEY_PEM") or "").strip()
                   or (config.get("PRIVATE_KEY_PATH") or "").strip())
    if not has_key:
        items.append(_item(
            "PRIVATE_KEY", "Clé privée RSA AGRICAP (MAKUTA_PRIVATE_KEY_PEM / _PATH)", MISSING,
            "Absente : aucune requête ne peut être signée.",
            ask="Confirmation que la clé PUBLIQUE d'AGRICAP a bien été enregistrée chez Makuta "
                "(la paire est générée par nous ; Makuta ne délivre aucun certificat).",
        ))
    else:
        try:
            makuta.sign_path("/diagnostic-check-makuta")
            items.append(_item("PRIVATE_KEY", "Clé privée RSA AGRICAP", OK,
                               "Chargée ; signature RSA/SHA-256 PKCS#1 v1.5 fonctionnelle "
                               "(contrôle local, aucun appel réseau)."))
        except MakutaError as exc:
            items.append(_item("PRIVATE_KEY", "Clé privée RSA AGRICAP", GAP, str(exc),
                               ask="Aucune question fournisseur : défaut de déploiement côté "
                                   "AGRICAP, à corriger avant tout échange."))

    header = (config.get("SIGNATURE_HEADER") or makuta.DEFAULT_SIGNATURE_HEADER)
    items.append(_item(
        "SIGNATURE_HEADER", "En-tete portant NOTRE signature", TO_CONFIRM,
        f"« {header} » est utilise aujourd'hui. La documentation se contredit : le tableau "
        f"normatif impose « X-Makuta-Signature », l'exemple de requete montre « X-Signature ». "
        f"Une signature juste sous un mauvais nom d'en-tete est rejetee comme une fausse.",
        ask="Nom EXACT de l'en-tête de signature attendu par Makuta : « X-Makuta-Signature » "
            "ou « X-Signature » ?",
    ))
    items.append(_item(
        "SIGNED_CONTENT_GET", "Contenu signe d'un GET", TO_CONFIRM,
        "Le connecteur signe le chemin seul (« /api/... »). La documentation n'illustre aucun "
        "GET avec chaîne de requête.",
        ask="La chaîne de requête (« ?a=1 ») entre-t-elle dans le contenu signé d'un GET ?",
    ))
    items.append(_item(
        "REPLAY_PROTECTION", "Protection anti-rejeu cote Makuta", TO_CONFIRM,
        "Le contenu signé ne porte ni horodatage ni nonce : une requête signée capturée reste "
        "valide indéfiniment. AGRICAP se protège de son côté (référence unique + idempotence).",
        ask="Makuta détecte-t-elle le rejeu d'une requête signée identique (horodatage, nonce, "
            "clé d'idempotence) ? Si oui, sous quel en-tête ou champ ?",
    ))
    return {"key": "TRANSPORT", "title": "1. SOCLE D'AUTHENTIFICATION (AGRICAP -> Makuta)",
            "items": items}


# ═══════════════════════════════════════════════ LECTURE DES RÉPONSES (STATUTS)

def _status_section() -> dict:
    config = _config()
    items = []

    field = (config.get("STATUS_FIELD") or "").strip()
    items.append(_item(
        "STATUS_FIELD", "Chemin du champ de statut dans la reponse",
        OK if field else MISSING,
        f"« {field} »" if field else
        "Absent : une réponse HTTP 200 ne peut PAS valoir confirmation. Sans ce champ, tout "
        "encaissement reste en attente et aucun portefeuille n'est crédité.",
        ask="Dans quel champ de la réponse se trouve le statut de l'opération (chemin complet, "
            "ex. « data.status ») ? Un exemple de réponse réelle pour chaque cas suffirait.",
    ))

    for key, label, blocking, question in (
        ("STATUS_CONFIRMED", "Valeurs signifiant « l'argent a bouge »", True,
         "Liste EXHAUSTIVE des valeurs de statut signifiant que l'opération a abouti et que "
         "les fonds ont effectivement bougé."),
        ("STATUS_REFUSED", "Valeurs signifiant « refuse definitivement »", True,
         "Liste EXHAUSTIVE des valeurs de statut signifiant un refus DÉFINITIF (par opposition "
         "à un échec temporaire susceptible d'aboutir plus tard)."),
        ("STATUS_PENDING", "Valeurs signifiant « en cours »", False,
         "Liste des valeurs de statut signifiant « en cours de traitement », et délai au-delà "
         "duquel une opération en cours doit être considérée comme perdue."),
    ):
        values = config.get(key)
        values = [values] if isinstance(values, str) else list(values or [])
        if values:
            state, detail = OK, ", ".join(str(v) for v in values)
        elif blocking:
            state, detail = MISSING, ("Absentes : le connecteur ne saura jamais lire cette "
                                      "issue, l'ordre restera en attente indéfiniment.")
        else:
            state, detail = TO_CONFIRM, ("Absentes : un « en cours » sera traité comme une "
                                         "réponse illisible — prudent, mais bruyant.")
        items.append(_item(key, label, state, detail, ask=question))

    reference_field = (config.get("PROVIDER_REFERENCE_FIELD") or "").strip()
    items.append(_item(
        "PROVIDER_REFERENCE_FIELD", "Chemin de LEUR identifiant de transaction",
        OK if reference_field else TO_CONFIRM,
        f"« {reference_field} »" if reference_field else
        "Absent : nos ordres n'enregistrent aucun identifiant fournisseur, ce qui rend tout "
        "rapprochement avec un relevé Makuta manuel.",
        ask="Dans quel champ la réponse porte-t-elle l'identifiant Makuta de la transaction "
            "(celui qui figurera sur les relevés) ?",
    ))
    items.append(_item(
        "ERROR_CODES", "Catalogue des codes d'erreur", TO_CONFIRM,
        "Non documenté. Le connecteur traite tout HTTP >= 400 comme un refus DÉFINITIF ; si "
        "certains codes sont en réalité temporaires, un retrait sera rendu au portefeuille "
        "alors que le versement partira quand même.",
        ask="Liste des codes d'erreur, avec pour chacun : définitif ou temporaire ?",
    ))
    items.append(_item(
        "CURRENCIES", "Devises et montants acceptes", TO_CONFIRM,
        "Non documentés. AGRICAP manipule USD et CDF.",
        ask="Devises supportées, format attendu des montants (chaîne « 120.00 » ou nombre ? "
            "unité ou centime ?), montants minimum et maximum par opération.",
    ))
    return {"key": "STATUS", "title": "2. LECTURE DES REPONSES (vocabulaire de statut)",
            "items": items}


# ═══════════════════════════════════════════════════════ RAPPELS ENTRANTS

def _callback_section() -> dict:
    config = _config()
    items = []

    public_key = (config.get("CALLBACK_PUBLIC_KEY_PEM") or "").strip()
    items.append(_item(
        "CALLBACK_PUBLIC_KEY_PEM", "Cle PUBLIQUE de Makuta (authentification des rappels)",
        OK if public_key else MISSING,
        "Fournie." if public_key else
        "Absente : TOUT rappel entrant est refusé (HTTP 503). C'est le comportement correct — "
        "un rappel non authentifiable permettrait à n'importe qui sur Internet de se déclarer "
        "payé, donc de créditer un portefeuille AGRICAP par une simple requête HTTP.",
        ask="Clé publique (PEM) avec laquelle Makuta signe ses rappels, et description exacte "
            "des octets signés (corps brut ? corps + horodatage ?).",
    ))

    header = (config.get("CALLBACK_SIGNATURE_HEADER") or config.get("SIGNATURE_HEADER")
              or makuta.DEFAULT_SIGNATURE_HEADER)
    items.append(_item(
        "CALLBACK_SIGNATURE_HEADER", "En-tete portant la signature du rappel",
        OK if config.get("CALLBACK_SIGNATURE_HEADER") else TO_CONFIRM,
        f"« {header} » sera lu." + ("" if config.get("CALLBACK_SIGNATURE_HEADER") else
                                     " Valeur héritée de notre en-tête sortant, non confirmée."),
        ask="Nom de l'en-tête HTTP portant la signature dans les rappels émis par Makuta.",
    ))

    reference_field = (config.get("CALLBACK_REFERENCE_FIELD") or "").strip()
    items.append(_item(
        "CALLBACK_REFERENCE_FIELD", "Champ du rappel portant NOTRE reference",
        OK if reference_field else MISSING,
        f"« {reference_field} »" if reference_field else
        "Absent : un rappel même parfaitement signé serait inexploitable, faute de savoir à "
        "quel ordre il se rapporte.",
        ask="Format complet d'un rappel (exemple réel), en particulier le champ qui reprend "
            "NOTRE référence de transaction.",
    ))
    items.append(_item(
        "CALLBACK_URL", "Enregistrement de notre URL de rappel", TO_CONFIRM,
        "L'endpoint AGRICAP existe : POST /api/caisses/payments/callback. Il doit être déclaré "
        "chez Makuta, avec la liste des adresses IP émettrices pour filtrage.",
        ask="Comment déclarer notre URL de rappel ? Depuis quelles adresses IP les rappels "
            "sont-ils émis ? Y a-t-il des réémissions, et selon quelle cadence ?",
    ))
    return {"key": "CALLBACK", "title": "3. RAPPELS ENTRANTS (Makuta -> AGRICAP)", "items": items}


# ═══════════════════════════════════════════════════ CATALOGUE DES OPÉRATIONS

#: Emplacements que le gabarit de corps DOIT porter pour que l'ordre soit rattachable.
_REFERENCE_PLACEHOLDER = "{reference}"


def _operation_section(operation: str) -> dict:
    operations = _config().get("OPERATIONS")
    operations = operations if isinstance(operations, dict) else {}
    conf = operations.get(operation)
    items = []

    if not isinstance(conf, dict):
        items.append(_item(
            f"{operation}.*", "Operation entierement absente du catalogue", MISSING,
            "Aucun paramétrage : toute tentative est refusée avant écriture (aucun ordre "
            "fantôme n'est créé).",
            ask=f"Contrat complet de l'opération « {_operation_label(operation)} » : chemin, "
                f"méthode HTTP, schéma de requête, schéma de réponse, chemin de relecture de "
                f"statut.",
        ))
        return {"key": f"OPERATION:{operation}", "title": _operation_title(operation),
                "items": items, "operation": operation}

    path = str(conf.get("path") or "").strip()
    items.append(_item(
        f"{operation}.path", "Chemin d'appel", OK if path else MISSING,
        path or "Absent : l'opération ne peut pas être émise.",
        ask=f"Chemin exact de l'opération « {_operation_label(operation)} ».",
    ))

    method = str(conf.get("method") or "").strip().upper()
    if not method:
        items.append(_item(
            f"{operation}.method", "Methode HTTP", TO_CONFIRM,
            "Non précisée. Le connecteur émet un POST signé — c'est la seule forme "
            "implémentée aujourd'hui.",
            ask=f"Méthode HTTP de « {_operation_label(operation)} » (POST attendu ; toute autre "
                f"méthode demande une évolution du connecteur).",
        ))
    elif method == "POST":
        items.append(_item(f"{operation}.method", "Methode HTTP", OK, "POST."))
    else:
        items.append(_item(
            f"{operation}.method", "Methode HTTP", GAP,
            f"« {method} » est configurée, mais le connecteur n'émet que des POST signés. "
            f"L'opération échouerait silencieusement du point de vue du paramétrage.",
            ask=f"Confirmer la méthode de « {_operation_label(operation)} » : si ce n'est pas "
                f"POST, le connecteur AGRICAP doit évoluer (helper à ajouter dans "
                f"common/makuta.py).",
        ))

    body = conf.get("body")
    if not isinstance(body, dict):
        items.append(_item(
            f"{operation}.body", "Gabarit de corps de requete", MISSING,
            "Absent : le schéma de requête n'est pas documenté par le fournisseur. Sans lui, "
            "aucun corps ne peut être construit ni signé.",
            ask=f"Schéma de requête de « {_operation_label(operation)} » : nom de CHAQUE champ, "
                f"type, obligatoire ou non, plus un exemple de requête réelle.",
        ))
    elif _REFERENCE_PLACEHOLDER not in _flatten(body):
        items.append(_item(
            f"{operation}.body", "Gabarit de corps de requete", GAP,
            "Le gabarit ne place NOTRE référence nulle part. Un rappel ou un relevé ne pourrait "
            "pas être rattaché à l'ordre : c'est la réconciliation qui devient impossible.",
            ask=f"Quel champ de « {_operation_label(operation)} » transporte la référence du "
                f"partenaire, restituée telle quelle dans la réponse et les rappels ?",
        ))
    else:
        items.append(_item(f"{operation}.body", "Gabarit de corps de requete", OK,
                           "Fourni, et il place notre référence dans la requête."))

    status_path = str(conf.get("status_path") or "").strip()
    if not status_path:
        items.append(_item(
            f"{operation}.status_path", "Chemin de relecture de statut", MISSING,
            "Absent : un ordre indéterminé (coupure réseau après envoi) ne peut PAS être "
            "réconcilié. C'est le premier contrat à obtenir — sans lui, chaque incident "
            "réseau se règle à la main, sur relevé.",
            ask=f"Comment relire le statut d'une opération « {_operation_label(operation)} » "
                f"déjà émise ? Chemin exact, et clé de recherche (notre référence ou la vôtre).",
        ))
    elif not status_path.startswith("/"):
        items.append(_item(f"{operation}.status_path", "Chemin de relecture de statut", GAP,
                           f"« {status_path} » ne commence pas par « / » : le connecteur le "
                           f"refusera au moment de la réconciliation.",
                           ask="Chemin de relecture de statut, en chemin absolu."))
    elif not any(marker in status_path for marker in ("{reference}", "{provider_reference}")):
        items.append(_item(
            f"{operation}.status_path", "Chemin de relecture de statut", GAP,
            f"« {status_path} » ne porte aucun identifiant d'opération : la relecture "
            f"interrogerait le même chemin pour tous les ordres.",
            ask="Le chemin de relecture doit désigner UNE opération : par notre référence "
                "({reference}) ou par la vôtre ({provider_reference}) ?",
        ))
    else:
        items.append(_item(f"{operation}.status_path", "Chemin de relecture de statut", OK,
                           status_path))

    return {"key": f"OPERATION:{operation}", "title": _operation_title(operation),
            "items": items, "operation": operation}


_OPERATION_LABELS = {
    "MM_COLLECT": "encaissement Mobile Money (depot client)",
    "MM_PAYOUT": "decaissement Mobile Money (retrait client)",
    "BANK_COLLECT": "encaissement bancaire (depot client)",
    "BANK_PAYOUT": "decaissement bancaire (retrait client)",
}


def _operation_label(operation: str) -> str:
    return _OPERATION_LABELS.get(operation, operation)


def _operation_title(operation: str) -> str:
    return f"{operation} — {_operation_label(operation)}"


def _flatten(node: Any) -> str:
    """Aplatit un gabarit en une chaîne, pour y chercher un emplacement sans se soucier de
    l'endroit où le fournisseur l'attend."""
    if isinstance(node, dict):
        return "".join(_flatten(value) for value in node.values())
    if isinstance(node, list):
        return "".join(_flatten(value) for value in node)
    return str(node)


def _extra_operations_section() -> dict | None:
    operations = _config().get("OPERATIONS")
    operations = operations if isinstance(operations, dict) else {}
    extra = [name for name in operations if name not in channels.required_operations()]
    if not extra:
        return None
    return {
        "key": "EXTRA", "title": "OPERATIONS CONFIGUREES HORS CATALOGUE AGRICAP",
        "items": [_item(name, "Operation hors catalogue", TO_CONFIRM,
                        "Configurée mais jamais utilisée par le dépôt/retrait client : le "
                        "routage par canal ne produit que les opérations du catalogue.",
                        ask="") for name in sorted(extra)],
    }


# ═══════════════════════════════════════════════════════════════ DIAGNOSTIC

def diagnose() -> dict:
    """Diagnostic complet. Ne lève jamais : un diagnostic qui échoue n'aide personne."""
    sections = [_transport_section(), _status_section(), _callback_section()]
    for index, operation in enumerate(channels.required_operations(), start=4):
        section = _operation_section(operation)
        section["title"] = f"{index}. {section['title']}"
        sections.append(section)
    extra = _extra_operations_section()
    if extra:
        sections.append(extra)

    items = [item for section in sections for item in section["items"]]
    blocking = [i for i in items if i["state"] in BLOCKING_STATES]
    to_confirm = [i for i in items if i["state"] == TO_CONFIRM]

    seen: set[str] = set()
    questions = []
    for item in blocking + to_confirm:
        ask = item["ask"]
        if ask and ask not in seen:
            seen.add(ask)
            questions.append(ask)

    return {
        "sections": sections,
        "counts": {"total": len(items), "ok": len([i for i in items if i["state"] == OK]),
                   "blocking": len(blocking), "toConfirm": len(to_confirm)},
        "operational": not blocking,
        "questions": questions,
    }
