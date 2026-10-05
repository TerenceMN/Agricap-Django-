"""Injecte le couplage endpoint / écran dans le schéma OpenAPI.

Hook de post-traitement et non 310 annotations `@extend_schema` : une seule
source, recalculée à chaque génération, plutôt que 310 à maintenir à jour.
"""
from __future__ import annotations

from drf_spectacular.extensions import OpenApiAuthenticationExtension

from common.frontend_usage import (
    FRONTENDS, attendu_pour, couplage, sans_ecran_pour,
)


class IdpBearerScheme(OpenApiAuthenticationExtension):
    """Déclare `accounts.IdpBearerAuthentication` au schéma.

    Sans elle, spectacular émet un avertissement par vue (« could not resolve
    authenticator ») et surtout n'affiche AUCUN bouton « Authorize » : la
    documentation serait consultable et inutilisable, puisque toute route du
    backend exige un jeton porteur émis par l'IdP AGRICAP.

    Ce module est importé par drf-spectacular au moment de la génération (il
    porte le hook de post-traitement), ce qui suffit à enregistrer l'extension —
    pas d'`AppConfig.ready()` à ajouter pour cela seul.
    """

    target_class = "accounts.authentication.IdpBearerAuthentication"
    name = "IdpBearer"

    def get_security_definition(self, auto_schema):  # noqa: ARG002
        return {
            "type": "http",
            "scheme": "bearer",
            "bearerFormat": "JWT",
            "description": (
                "Jeton porteur émis par l'IdP AGRICAP "
                "(`Authorization: Bearer <access_token>`). Les rôles et le "
                "plafond de délégation sont résolus depuis le profil associé au "
                "`sub` du jeton, jamais depuis la requête."
            ),
        }

#: Au-delà, la description deviendrait illisible dans Swagger UI. Le nombre total
#: reste annoncé — une liste tronquée sans son total se lit comme une liste
#: complète (CONVENTIONS.md §4.6, « total_rows sur toute liste tronquée »).
MAX_APPELS_AFFICHES = 8

_SEPARATEUR = "\n\n---\n\n"


def _bloc_connecte(verdict) -> str:
    lignes = [f"**Couplage frontend — connecté** ({len(verdict.appels)} appel(s) "
              f"dans {', '.join(verdict.frontends)})", ""]
    for appel in verdict.appels[:MAX_APPELS_AFFICHES]:
        lignes.append(f"- `{appel.fichier}:{appel.ligne}` — {appel.frontend}")
    reste = len(verdict.appels) - MAX_APPELS_AFFICHES
    if reste > 0:
        lignes.append(f"- … et {reste} autre(s) appel(s) non listé(s) ici.")

    # « Appelé » ne veut pas dire « atteignable » : un composant que personne
    # n'importe appelle sans que personne ne puisse cliquer. Le dire ICI, sur un
    # endpoint marqué connecté, est le seul endroit où l'information corrige
    # l'impression que le verdict donne.
    sans_ecran = sans_ecran_pour(verdict.chemin)
    if sans_ecran:
        lignes += ["", sans_ecran]
    return "\n".join(lignes)


def _bloc_non_connecte(verdict) -> str:
    scannes = ", ".join(FRONTENDS)
    lignes = [
        "**Couplage frontend — non connecté à ce jour**", "",
        f"Aucun appel littéral de cette route n'a été trouvé dans : {scannes}.",
    ]
    attendu = verdict.attendu or attendu_pour(verdict.chemin)
    if attendu:
        lignes += ["", f"**Devrait être branché à** : {attendu}"]
    else:
        lignes += [
            "",
            "**Branchement cible non arbitré.** Deux lectures possibles, et elles "
            "n'appellent pas la même action : soit l'écran reste à construire, soit "
            "l'endpoint n'a plus d'usage et doit être retiré plutôt que maintenu. "
            "Un endpoint sans écran ni décision est une surface d'attaque que "
            "personne ne surveille.",
        ]
    lignes += [
        "",
        "> Un chemin construit dynamiquement côté front (concaténation de variables) "
        "échappe à cette lecture statique : vérifier avant de conclure à un mort.",
    ]
    return "\n".join(lignes)


def couplage_frontend(result, generator, request, public):  # noqa: ARG001
    """Hook `POSTPROCESSING_HOOKS` — annote chaque opération du schéma.

    Signature imposée par drf-spectacular ; `generator`, `request` et `public` ne
    sont pas utilisés, le couplage ne dépendant ni de l'appelant ni de la
    visibilité du schéma.
    """
    total = connectes = 0

    for chemin, operations in (result.get("paths") or {}).items():
        verdict = couplage(chemin)
        attendu = verdict.attendu or attendu_pour(verdict.chemin)
        bloc = _bloc_connecte(verdict) if verdict.connecte else _bloc_non_connecte(verdict)

        extension = {
            "connecte": verdict.connecte,
            "frontends": verdict.frontends,
            "appels": [{"frontend": a.frontend, "fichier": a.fichier, "ligne": a.ligne}
                       for a in verdict.appels],
            "brancherA": attendu,
            # Appelé, mais depuis un fichier qu'aucun écran monté n'atteint.
            "appeleMaisSansEcran": bool(sans_ecran_pour(verdict.chemin)),
        }

        for methode, operation in operations.items():
            if methode.lower() not in ("get", "post", "put", "patch", "delete",
                                       "head", "options", "trace"):
                continue
            total += 1
            if verdict.connecte:
                connectes += 1
            description = operation.get("description") or ""
            operation["description"] = (description + _SEPARATEUR + bloc).strip()
            operation["x-agricap-frontend"] = extension

    # Récapitulatif en tête de la documentation : le lecteur voit la proportion
    # avant d'ouvrir la première route, au lieu de la reconstituer endpoint par
    # endpoint. Pas de pourcentage sans base (CONVENTIONS.md §4.6).
    orphelins = total - connectes
    part = f"{(connectes / total * 100):.0f} %" if total else "n/a"
    result.setdefault("info", {})
    description = result["info"].get("description") or ""
    result["info"]["description"] = description + (
        f"\n\n---\n\n### Couplage frontend\n\n"
        f"**{connectes} opération(s) sur {total} sont appelées par un frontend "
        f"({part}) ; {orphelins} ne le sont pas.**\n\n"
        f"Frontends scannés : {', '.join(FRONTENDS)}. Les fichiers de test sont "
        f"exclus : un endpoint appelé uniquement depuis un test n'est pas branché "
        f"à un écran.\n\n"
        f"Chaque opération porte son verdict en fin de description, et la même "
        f"information en clair sous l'extension `x-agricap-frontend`. Le verdict "
        f"est MESURÉ (appel littéral trouvé, avec fichier et ligne) ; la mention "
        f"« devrait être branché à » est un jugement, tenu à part dans "
        f"`common/frontend_usage.py`."
    )
    return result
