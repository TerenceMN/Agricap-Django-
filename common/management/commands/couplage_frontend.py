"""
Inventaire du couplage API / frontends, en console.

    python manage.py couplage_frontend                 # récapitulatif par app
    python manage.py couplage_frontend --orphelins     # seulement les non branchés
    python manage.py couplage_frontend --app credits   # une app
    python manage.py couplage_frontend --markdown      # rapport collable

La même information est servie dans Swagger (`/api/docs/`) sur chaque opération.
Cette commande existe pour la lire sans lancer de serveur — et pour qu'elle
puisse entrer dans une revue ou un rapport de dette sans capture d'écran.
"""
from __future__ import annotations

from collections import defaultdict

from django.core.management.base import BaseCommand
from django.urls import get_resolver

from common.frontend_usage import (
    FRONTENDS, attendu_pour, couplage, sans_ecran_pour,
)


#: Routes de DOCUMENTATION : elles décrivent l'API, elles n'en font pas partie.
#: Les compter donnerait trois « orphelins » permanents et incorrigibles — aucun
#: écran métier n'a vocation à appeler le schéma OpenAPI.
ROUTES_DOCUMENTATION = ("api/schema", "api/docs", "api/redoc")


def _routes() -> list[str]:
    """Toutes les routes `api/…` déclarées, à plat, hors documentation."""
    def parcourir(resolver, prefixe=""):
        for motif in resolver.url_patterns:
            chemin = prefixe + str(motif.pattern)
            if hasattr(motif, "url_patterns"):
                yield from parcourir(motif, chemin)
            else:
                yield chemin

    vues = {c for c in parcourir(get_resolver())
            if c.startswith("api/")
            and not c.rstrip("/").startswith(ROUTES_DOCUMENTATION)}
    return sorted(vues)


#: Glyphes usuels des notes qui n'existent pas en cp1252, et leur repli ASCII.
#: La console Windows de l'institution est en cp1252 : un glyphe hors cp1252 dans
#: une note faisait planter la commande sur sa propre sortie. Le repli est explicite
#: plutôt que muet — « -> » se lit, un caractère avalé ne se lit pas.
REPLIS_CONSOLE = {
    "→": "->", "✓": "v", "✗": "x", "–": "-", "—": "-",
}


class Command(BaseCommand):
    help = ("Dit, pour chaque route de l'API, si un frontend l'appelle — et sinon "
            "où elle devrait être branchée.")

    def write(self, texte: str, style=None) -> None:
        """Écrit une ligne en la rendant encodable par la console courante.

        Les notes de branchement sont écrites pour Swagger, en markdown, et
        peuvent porter des glyphes que la console ne sait pas encoder. Les
        remplacer ici — au dernier moment, à la frontière d'affichage — évite
        d'appauvrir le texte partout ailleurs pour la seule contrainte du
        terminal.
        """
        for glyphe, repli in REPLIS_CONSOLE.items():
            texte = texte.replace(glyphe, repli)
        encodage = getattr(self.stdout, "encoding", None) or "utf-8"
        try:
            texte.encode(encodage)
        except (UnicodeEncodeError, LookupError):
            texte = texte.encode(encodage, "replace").decode(encodage, "replace")
        self.stdout.write(style(texte) if style else texte)

    def write_warn(self, texte: str) -> None:
        self.write(texte, self.style.WARNING)

    def write_ok(self, texte: str) -> None:
        self.write(texte, self.style.SUCCESS)

    def add_arguments(self, parser):
        parser.add_argument("--orphelins", action="store_true",
                            help="N'affiche que les routes qu'aucun front n'appelle.")
        parser.add_argument("--app", default=None,
                            help="Restreint à un préfixe d'app (credits, investments…).")
        parser.add_argument("--markdown", action="store_true",
                            help="Sortie markdown, collable dans un rapport.")

    def handle(self, *args, **opts):
        routes = _routes()
        if opts["app"]:
            prefixe = f"api/{opts['app'].strip('/')}/"
            routes = [r for r in routes if r.startswith(prefixe)]

        verdicts = [(route, couplage(route)) for route in routes]
        connectes = [v for _, v in verdicts if v.connecte]
        orphelins = [(r, v) for r, v in verdicts if not v.connecte]

        if opts["markdown"]:
            self._markdown(verdicts, orphelins)
            return

        # ── Récapitulatif par app ────────────────────────────────────────────
        par_app: dict[str, list] = defaultdict(list)
        for route, verdict in verdicts:
            par_app[route.split("/")[1]].append(verdict)

        if not opts["orphelins"]:
            self.write("")
            self.write(f"{'App':<18}{'routes':>8}{'branchees':>11}{'orphelines':>12}")
            self.write("-" * 49)
            for app in sorted(par_app):
                total = len(par_app[app])
                relies = sum(1 for v in par_app[app] if v.connecte)
                ligne = f"{app:<18}{total:>8}{relies:>11}{total - relies:>12}"
                style = self.style.SUCCESS if relies == total else self.style.WARNING
                self.write(style(ligne))
            self.write("-" * 49)
            self.write(
                f"{'TOTAL':<18}{len(verdicts):>8}{len(connectes):>11}"
                f"{len(orphelins):>12}")

        # ── Appelés, mais depuis un fichier qu'aucun écran n'atteint ─────────
        # Ils ne sont PAS dans la liste des orphelins — le scan les voit appelés,
        # et il a raison. Les taire ici les rendrait invisibles des deux côtés.
        sans_ecran = [(r, v) for r, v in verdicts
                      if v.connecte and sans_ecran_pour(v.chemin)]
        if sans_ecran and not opts["orphelins"]:
            self.write("")
            self.write_warn((
                f"{len(sans_ecran)} route(s) appelee(s) par du code que personne "
                f"n'atteint :"))
            self.write("")
            for route, verdict in sans_ecran:
                self.write(f"  {route}")
                for morceau in self._envelopper(sans_ecran_pour(verdict.chemin), 88):
                    self.write(f"      {morceau}")
                self.write("")

        # ── Orphelins, avec leur branchement attendu ─────────────────────────
        self.write("")
        self.write_warn((
            f"{len(orphelins)} route(s) qu'aucun frontend n'appelle :"))
        self.write("")
        for route, verdict in orphelins:
            self.write(f"  {route}")
            attendu = verdict.attendu or attendu_pour(verdict.chemin)
            if attendu:
                for morceau in self._envelopper(attendu, 88):
                    self.write(f"      {morceau}")
            else:
                self.write_warn((
                    "      (branchement cible non arbitre : ecran a construire, "
                    "ou endpoint a retirer)"))
            self.write("")

        self.write(
            f"Frontends scannes : {', '.join(FRONTENDS)} (fichiers de test exclus).")
        self.write(
            "Un chemin construit dynamiquement echappe a cette lecture statique : "
            "verifier avant de conclure a un mort.")

    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _envelopper(texte: str, largeur: int) -> list[str]:
        mots, lignes, courante = texte.split(), [], ""
        for mot in mots:
            if len(courante) + len(mot) + 1 > largeur:
                lignes.append(courante)
                courante = mot
            else:
                courante = f"{courante} {mot}".strip()
        if courante:
            lignes.append(courante)
        return lignes

    def _markdown(self, verdicts, orphelins) -> None:
        connectes = sum(1 for _, v in verdicts if v.connecte)
        total = len(verdicts)
        part = f"{connectes / total * 100:.0f} %" if total else "n/a"

        # ASCII pour les glyphes hors cp1252 : cette sortie est écrite sur la
        # console Windows de l'institution, qui n'encode pas « / » et planterait
        # sur son propre rapport.
        self.write("## Couplage API / frontends\n")
        self.write(
            f"**{connectes} route(s) sur {total} sont appelées par un frontend "
            f"({part}) ; {len(orphelins)} ne le sont pas.**\n")
        self.write(
            f"Frontends scannés : {', '.join(FRONTENDS)}. Fichiers de test exclus.\n")

        self.write("\n### Routes branchées\n")
        self.write("| Route | Frontend(s) | Premier appel |")
        self.write("|---|---|---|")
        for route, verdict in verdicts:
            if not verdict.connecte:
                continue
            premier = verdict.appels[0]
            self.write(
                f"| `/{route}` | {', '.join(verdict.frontends)} | "
                f"`{premier.fichier}:{premier.ligne}` |")

        self.write("\n### Routes sans écran\n")
        self.write("| Route | Devrait être branché à |")
        self.write("|---|---|")
        for route, verdict in orphelins:
            attendu = (verdict.attendu or attendu_pour(verdict.chemin)
                       or "_non arbitré — écran à construire, ou endpoint à retirer_")
            self.write(f"| `/{route}` | {attendu} |")
