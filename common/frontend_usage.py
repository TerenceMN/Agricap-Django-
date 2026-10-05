"""Couplage API / frontends : qui appelle quoi, mesuré et non déclaré.

Le couplage est INDEXÉ depuis les littéraux de chemin trouvés dans les sources
front, pas annoté à la main sur 310 vues — une mention écrite reste vraie dans le
code et fausse dans les faits dès le premier écran supprimé.

« Où l'endpoint DEVRAIT être branché » est un jugement, pas une mesure : il vit
séparément dans `BRANCHEMENTS_ATTENDUS`.

Limite assumée : un chemin construit dynamiquement (`base + '/liste/'`) échappe à
la lecture statique. Le scan peut donc dire « non connecté » à tort — jamais
l'inverse, puisqu'il ne rapporte que des littéraux trouvés.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from functools import lru_cache

#: Frontends susceptibles d'appeler ce backend, par nom affiché → dossier source.
#: Chemins relatifs à la racine du dépôt (le parent de « AGRICAP FINTECH »).
FRONTENDS = {
    "AGRICAP FINTECH": os.path.join("AGRICAP FINTECH", "src"),
    "AGRICAP LAND": os.path.join("AGRICAP LAND", "src"),
    "AGRICAP MECANISATION": os.path.join("AGRICAP MECANISATION", "src"),
}

#: Extensions scannées. Les `.test.ts` sont EXCLUS : un endpoint appelé
#: uniquement depuis un test n'est pas branché à un écran, et le compter comme
#: connecté masquerait exactement ce qu'on cherche à voir.
EXTENSIONS = (".ts", ".tsx", ".js", ".jsx", ".vue")
EXCLUS = ("node_modules", "dist", "build", ".next", "coverage", "__snapshots__")
SUFFIXES_TESTS = (".test.ts", ".test.tsx", ".spec.ts", ".spec.tsx",
                  ".test.js", ".test.jsx")

#: Début d'une chaîne (simple, double ou template) contenant un chemin absolu.
#: Le reste est parcouru caractère par caractère par `chemins_dans_ligne` : une
#: expression régulière ne sait pas apparier des accolades imbriquées, et le
#: front en produit (`${vc ? `?code=${vc}` : ''}`).
_DEBUT_CHEMIN = re.compile(r"""['"`](?=/(?:api/)?[a-zA-Z0-9\-_])""")

#: Caractères admis dans un segment littéral de chemin.
_SEGMENT = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_/.")

#: Expressions interpolées qui contiennent une QUERY STRING déjà construite, et
#: non un segment de chemin. Elles se reconnaissent au nom, faute de mieux : le
#: `?` a été assemblé plus haut dans la fonction, il n'apparaît pas sur la ligne.
#:
#: DÉFAUT MESURÉ. `api.ts` écrit :
#:
#:     const qs = params?.status ? `?status=${…}` : '';
#:     return request(`/credits/guarantee-requests/${qs}`);
#:
#: Ici l'interpolation SUIT un slash — la règle « non précédée d'un / » ne la voit
#: donc pas — et ne contient aucun `?` sur sa propre ligne. Lue comme un segment,
#: elle donnait `/credits/guarantee-requests/<*>`, qui ne correspond à aucune
#: route : l'endpoint était rapporté orphelin alors qu'il est appelé depuis des
#: mois. C'est le genre de faux négatif qui envoie construire un écran existant.
#:
#: Heuristique sur un NOM, donc faillible — mais dans le sens sûr : elle produit
#: au pire un chemin plus court, jamais un chemin inventé.
_NOMS_QUERY_STRING = frozenset({
    "qs", "querystring", "querystring", "query", "search", "suffix", "params",
})

#: Segment de paramètre, côté Django (`<int:pk>`, `<code>`) comme côté front
#: (`${code}`). Les deux sont normalisés vers le même marqueur pour être comparés.
_PARAM_DJANGO = re.compile(r"<[^>]+>")
_PARAM_FRONT = re.compile(r"\$\{[^}]*\}")
#: Paramètre au format OpenAPI (`/applications/{code}/score/`) — la forme que
#: drf-spectacular donne aux chemins. Appliqué APRÈS `_PARAM_FRONT`, sans quoi
#: `${code}` deviendrait `$<*>` au lieu de disparaître entièrement.
_PARAM_OPENAPI = re.compile(r"\{[^}]*\}")
_MARQUEUR = "<*>"


@dataclass
class Appel:
    """Un appel d'API trouvé dans le code d'un frontend."""

    frontend: str
    fichier: str          # chemin relatif à la racine du dépôt
    ligne: int

    def __str__(self) -> str:
        return f"{self.frontend} — {self.fichier}:{self.ligne}"


@dataclass
class Couplage:
    """Verdict pour un endpoint : appels mesurés, et attente si aucun."""

    chemin: str
    appels: list[Appel] = field(default_factory=list)
    attendu: str = ""

    @property
    def connecte(self) -> bool:
        return bool(self.appels)

    @property
    def frontends(self) -> list[str]:
        vus: list[str] = []
        for appel in self.appels:
            if appel.frontend not in vus:
                vus.append(appel.frontend)
        return vus


def racine_depot() -> str:
    """Racine du dépôt — le parent du dossier « AGRICAP FINTECH »."""
    ici = os.path.dirname(os.path.abspath(__file__))          # …/backend/common
    backend = os.path.dirname(ici)                            # …/backend
    fintech = os.path.dirname(backend)                        # …/AGRICAP FINTECH
    return os.path.dirname(fintech)                           # …/AGRICAP


def normaliser(chemin: str) -> str:
    """Forme canonique d'un chemin, comparable entre Django et le front.

    Trois différences à neutraliser, et une seule à conserver :

      - le préfixe `/api` : le front l'ajoute tantôt à la main
        (`/api/credits/rapport`), tantôt via la base d'`apiClient` (`/credits/…`) ;
      - les paramètres : `<int:pk>` côté Django, `${id}` côté front ;
      - le slash final, que Django impose et que le front omet parfois.

    Ce qui est CONSERVÉ, c'est la suite des segments littéraux — c'est elle qui
    identifie la route, et deux routes qui ne diffèrent que par un paramètre
    sont bien la même route.
    """
    chemin = chemin.strip()
    chemin = _PARAM_FRONT.sub(_MARQUEUR, chemin)
    chemin = _PARAM_DJANGO.sub(_MARQUEUR, chemin)
    chemin = _PARAM_OPENAPI.sub(_MARQUEUR, chemin)
    chemin = chemin.split("?", 1)[0]
    if not chemin.startswith("/"):
        chemin = "/" + chemin
    if chemin.startswith("/api/"):
        chemin = chemin[4:]
    elif chemin == "/api":
        chemin = "/"
    # Un segment interpolé peut coller au littéral précédent (`/applications/${c}`) :
    # après substitution on obtient `/applications/<*>`, déjà canonique.
    chemin = re.sub(r"/{2,}", "/", chemin)
    return chemin.rstrip("/") or "/"


def chemins_dans_ligne(ligne: str) -> list[str]:
    """Chemins d'API contenus dans une ligne de code frontend.

    Parcours caractère par caractère plutôt qu'expression régulière, pour une
    raison précise : le front écrit des interpolations IMBRIQUÉES, et une regex
    ne sait pas apparier des accolades. Exemple réel :

        request(`/credits/dashboard/${view ? `?view=${view}` : ''}`)

    Un `[^}]*` s'arrête sur le `}` de `${view}` intérieur et produit un chemin
    tronqué au mauvais endroit — `/credits/dashboard/` devenait
    `/credits/dashboard/<*>`, qui ne correspond à aucune route Django. Cinq
    endpoints bien branchés étaient ainsi rapportés « non connectés ».

    Deux règles font la justesse du résultat :

      - une interpolation qui contient un `?` construit une QUERY STRING, pas un
        segment : le chemin s'arrête avant elle. C'est ce qui distingue
        `/credits/applications/${qs ? …}` (liste, avec filtres) de
        `/credits/applications/${code}/` (détail d'un dossier) — deux routes
        différentes que la lecture naïve confondait ;
      - toute autre interpolation est un paramètre, remplacé par le marqueur.
    """
    resultats: list[str] = []

    for depart in _DEBUT_CHEMIN.finditer(ligne):
        guillemet = ligne[depart.start()]
        i = depart.end()
        morceaux: list[str] = []
        exploitable = True

        while i < len(ligne):
            caractere = ligne[i]
            if caractere == guillemet or caractere == "?":
                break

            if caractere == "$" and ligne[i + 1:i + 2] == "{":
                profondeur, j = 0, i + 1
                while j < len(ligne):
                    if ligne[j] == "{":
                        profondeur += 1
                    elif ligne[j] == "}":
                        profondeur -= 1
                        if profondeur == 0:
                            break
                    j += 1
                if profondeur != 0:
                    # Accolade non fermée : le template literal court sur
                    # plusieurs lignes et la suite du chemin nous échappe. On
                    # JETTE ce qu'on a lu au lieu d'en garder le préfixe — un
                    # `/credits/` partiel marquerait la route `/credits/` comme
                    # branchée par un appel qui ne la vise pas. Mieux vaut un
                    # endpoint rapporté orphelin à tort qu'un endpoint mort
                    # rapporté vivant.
                    exploitable = False
                    break
                expression = ligne[i + 2:j].strip()
                if "?" in expression:       # query string écrite en clair
                    break
                if expression.lower() in _NOMS_QUERY_STRING:
                    break                   # query string assemblée plus haut

                # Une interpolation NON PRÉCÉDÉE D'UN SLASH n'ouvre pas un
                # nouveau segment : elle suffixe le dernier. En pratique, c'est
                # une query string produite par un helper —
                # `request(`/accounting/balance${qs(params)}`)` appelle
                # `/accounting/balance`, pas `/accounting/balance<param>`.
                #
                # DÉFAUT MESURÉ : sans cette règle, les 15 restitutions
                # comptables (balance, grand livre, journaux, bilan, résultat,
                # consolidé, provisions, contrôles) étaient rapportées
                # « non connectées » alors que `accountingApi.ts` les appelle
                # toutes. Le service front utilise `qs()` partout où il y a des
                # filtres : la règle « l'interpolation contient un ? » ne voit
                # rien quand le ? est fabriqué à l'intérieur du helper.
                if not morceaux or morceaux[-1] != "/":
                    break

                morceaux.append(_MARQUEUR)
                i = j + 1
                continue

            if caractere not in _SEGMENT:
                break
            morceaux.append(caractere)
            i += 1

        chemin = "".join(morceaux)
        if exploitable and chemin.startswith("/"):
            resultats.append(chemin)

    return resultats


def _fichiers_a_scanner(racine: str):
    for dossier, sous_dossiers, fichiers in os.walk(racine):
        sous_dossiers[:] = [d for d in sous_dossiers if d not in EXCLUS]
        for nom in fichiers:
            if not nom.endswith(EXTENSIONS):
                continue
            if nom.endswith(SUFFIXES_TESTS):
                continue
            yield os.path.join(dossier, nom)


def _empreinte_sources() -> tuple:
    """Signature bon marché de l'état des sources front : (chemin, mtime, taille).

    Elle ne LIT aucun fichier — un `stat()` par fichier, là où le scan complet les
    ouvre tous. C'est ce qui permet de garder le résultat en cache sans le
    laisser mentir.

    POURQUOI CE N'EST PAS UN DÉTAIL. `index_appels()` était en `lru_cache` simple :
    le scan valait pour la vie du PROCESSUS. En ligne de commande, chaque appel
    part d'un process neuf et le problème ne se voit pas. Sur le serveur de
    développement, `/api/docs/` répond depuis un process qui vit des heures : on
    branche un endpoint, on recharge Swagger, et la page continue d'afficher
    « non connecté ». La documentation devient alors exactement ce contre quoi ce
    module a été écrit — une affirmation que rien ne vérifie, qui envoie refaire
    un travail déjà fait.
    """
    racine = racine_depot()
    signature = []
    for relatif in FRONTENDS.values():
        base = os.path.join(racine, relatif)
        if not os.path.isdir(base):
            continue
        for chemin in _fichiers_a_scanner(base):
            try:
                etat = os.stat(chemin)
            except OSError:
                continue
            signature.append((chemin, etat.st_mtime_ns, etat.st_size))
    return tuple(sorted(signature))


def index_appels() -> dict[str, tuple[Appel, ...]]:
    """Index `chemin normalisé → appels trouvés`.

    Le scan lit chaque fichier source une seule fois et indexe TOUT ce qui
    ressemble à un chemin d'API. Rapprocher ensuite 310 routes de cet index est
    une suite de recherches en table — c'est ce qui rend la génération du schéma
    instantanée là où 310 recherches plein texte l'auraient rendue pénible.

    Le résultat est mémorisé, mais invalidé dès qu'un fichier du front change
    (cf. `_empreinte_sources`) : une documentation périmée est pire qu'absente.
    """
    return _index_memorise(_empreinte_sources())


@lru_cache(maxsize=2)
def _index_memorise(_empreinte: tuple) -> dict[str, tuple[Appel, ...]]:
    """Scan réel. `_empreinte` n'est pas lue : elle est la CLÉ du cache."""
    racine = racine_depot()
    index: dict[str, list[Appel]] = {}

    for frontend, relatif in FRONTENDS.items():
        base = os.path.join(racine, relatif)
        if not os.path.isdir(base):
            continue
        for chemin_fichier in _fichiers_a_scanner(base):
            try:
                with open(chemin_fichier, encoding="utf-8", errors="ignore") as fh:
                    lignes = fh.readlines()
            except OSError:
                continue
            relatif_depot = os.path.relpath(chemin_fichier, racine).replace("\\", "/")
            for numero, contenu in enumerate(lignes, 1):
                for brut in chemins_dans_ligne(contenu):
                    cle = normaliser(brut)
                    if cle == "/":
                        continue
                    appel = Appel(frontend, relatif_depot, numero)
                    index.setdefault(cle, []).append(appel)

    return {cle: tuple(appels) for cle, appels in index.items()}


def couplage(chemin_django: str) -> Couplage:
    """Verdict pour une route Django (`api/credits/applications/<code>/score/`).

    Un endpoint est aussi considéré comme atteint quand le front appelle un
    chemin dont il est le PRÉFIXE — cas réel de `/credits/applications/` appelé
    avec une query string, ou d'une route parente montée en sous-chemin.
    """
    cle = normaliser(chemin_django)
    index = index_appels()

    appels = list(index.get(cle, ()))
    if not appels:
        # Repli : un front qui appelle `/credits/applications` (sans slash) ou
        # `/credits/applications/<*>` là où Django déclare la forme sans
        # paramètre. On ne remonte QUE les correspondances exactes de segments.
        for autre, trouves in index.items():
            if autre.rstrip("/") == cle.rstrip("/"):
                appels.extend(trouves)

    return Couplage(chemin=cle, appels=appels,
                    attendu=BRANCHEMENTS_ATTENDUS.get(cle, ""))


# ─────────────────────────────────────────────────────────────────────────────
# Ce qui n'est pas mesurable : où un endpoint DEVRAIT être branché.
#
# Renseigné pour les familles d'endpoints dont l'absence d'écran est une dette
# identifiée dans CONVENTIONS.md §7 et CREDIT_MODULE_STATUS.md §10. La clé est le
# chemin NORMALISÉ (sans `/api`, paramètres en `<*>`, sans slash final) ; un
# préfixe suffit — `attendu_pour()` prend la règle la plus spécifique.
# ─────────────────────────────────────────────────────────────────────────────

BRANCHEMENTS_ATTENDUS: dict[str, str] = {
    "/credits/applications/<*>/analysis-report": (
        "AGRICAP FINTECH — onglet Analyse du dossier, bouton « Rapport d'analyse ». "
        "Le serveur sait produire et relire ce rapport ; aucun écran ne l'appelle. "
        "C'est la pièce que le comité lit avant de voter."),
    "/credits/applications/<*>/close": (
        "AGRICAP FINTECH — `ApplicationDetail.tsx`, action de clôture pilotée par "
        "`app.availableActions` (calculé côté serveur, jamais inféré par le front). "
        "Sans elle, un dossier instruit ne peut être clos que par l'admin Django."),
    "/credits/applications/<*>/renew-consent": (
        "AGRICAP FINTECH — bandeau « consentement expiré » de la file d'instruction. "
        "La fenêtre de consentement client est de 72 h (principe 9) : sans bouton de "
        "relance, un dossier dont le consentement expire est bloqué sans recours à "
        "l'écran."),
    "/caisses/wallets/mine/payment-orders": (
        "Aucun — et c'est un ARBITRAGE, pas un oubli. J'avais d'abord conclu à une "
        "asymétrie (« le front liste des ordres qu'il ne peut pas créer ») ; "
        "vérification faite, c'est faux : `POST /caisses/wallets/mine/deposit` "
        "crée déjà l'ordre dès que le canal est externe, et c'est ce chemin que le "
        "parcours client emprunte. Cette route est le même acte un cran plus bas — "
        "`operation` et `direction` en clair, sans la validation de canal ni "
        "l'exigence de contrepartie qui protègent le dépôt. Lui donner un bouton "
        "créerait un second chemin vers le même acte, moins contrôlé que le "
        "premier (principe 6)."),
    "/savings/plans/<*>/withdraw": (
        "AGRICAP FINTECH — écran Épargne, action de retrait sur un plan. "
        "Le dépôt et la consultation sont branchés, le retrait ne l'est pas : "
        "le client peut alimenter son plan et pas en sortir."),
    "/accounting/comptes/<*>/suppression": (
        "AGRICAP FINTECH — `AccountingPlanComptable.jsx`, à côté du bouton "
        "d'activation. `accountingApi.comptes.suppression` existe dans le service "
        "et n'est appelée par AUCUN composant, pas même celui qui gère les comptes : "
        "une méthode de service sans appelant est un endpoint qu'on croit branché."),
    "/accounting/taux/saisie": (
        "Aucun — et c'est voulu. Cette route lève un 409 qui ÉNONCE LA RÈGLE "
        "(« la saisie d'un taux se fait par POST /api/fx/rates, "
        "`accounting.TauxChange` n'est qu'une projection en lecture seule ») plutôt "
        "que de renvoyer un 404 ambigu à qui la chercherait. Un panneau indicateur "
        "ne se branche pas à un écran ; le brancher reviendrait à afficher un bouton "
        "dont la seule fonction est d'échouer."),
    "/health": (
        "Aucun — sonde de disponibilité destinée au superviseur de production "
        "(systemd, reverse proxy, monitoring). Son absence côté frontend est "
        "normale et ne constitue pas une dette."),
    "/sms/test": (
        "Aucun — outil de diagnostic de l'intégration SMS, réservé à "
        "l'exploitation. À ne pas exposer à un écran client."),
    "/investments/accounting-events": (
        "AGRICAP FINTECH — écran comptable de l'investissement, ou rapprochement "
        "depuis `/accounting`. Événements comptables produits et jamais restitués."),
}

#: Règles par PRÉFIXE de chemin. Plus lisible que 310 entrées, et une famille
#: entière d'endpoints partage presque toujours le même écran manquant.
BRANCHEMENTS_ATTENDUS_PREFIXES: tuple[tuple[str, str], ...] = (
    ("/reference-data", (
        "AGRICAP FINTECH — onglet Référence du backoffice admin. "
        "Le maker-checker des `ValueChain` (upload, diff, activation checker ≠ maker, "
        "historique) n'est accessible que par API : aucun écran ne l'expose "
        "(CONVENTIONS.md §7.1 point 5). À construire dans `src/pages/admin/`, à côté de "
        "`DataAdmin.tsx` qui couvre déjà les simulateurs `dataio`.")),
    ("/credits/baremes", (
        "AGRICAP FINTECH — onglet Référence, sous-écran « Barèmes de score ». "
        "L'édition des courbes par le comité, avec prévisualisation de l'impact sur "
        "le golden set AVANT activation, n'a pas d'interface (CONVENTIONS.md §7.1 point 5). "
        "Sans elle, le principe 8 est tenu côté serveur et inatteignable côté métier.")),
    ("/credits/committee", (
        "AGRICAP FINTECH — vue Comité de crédit, `src/pages/credit/`. "
        "`_committee_dashboard` existe côté serveur ; la corbeille des dossiers "
        "au-dessus du plafond de délégation, le vote collégial avec quorum et le "
        "procès-verbal journalisé n'ont aucun écran (CONVENTIONS.md §7.1 point 4).")),
    ("/assets", (
        "AGRICAP FINTECH — file de vérification des actifs (agent terrain). "
        "Actifs `declare` → fixation de la valeur retenue (décote visible) → "
        "`verifie`/`rejete`, avec photos et documents joints (CONVENTIONS.md §7.1 point 6). "
        "Sans cet écran, aucune garantie matérielle ne devient opposable (principe 9).")),
    ("/audit", (
        "AGRICAP FINTECH — écran Journal & audit, lecture seule absolue. "
        "Consultation transverse du journal append-only avec filtres (dossier, acteur, "
        "étape, période) et export : c'est l'écran de l'auditeur (CONVENTIONS.md §7.1 point 8).")),
    ("/analytics", (
        "AGRICAP FINTECH — tableaux de bord direction. À rapprocher des KPI honnêtes "
        "exigés par CONVENTIONS.md §7.2 : chaque carte porte sa période, son périmètre et "
        "sa devise, aucun agrégat multi-devises sans conversion journalisée.")),
    ("/alerts", (
        "AGRICAP FINTECH — bandeau d'alertes du portefeuille et file de traitement. "
        "Les règles existent en base ; rien ne les affiche à l'agent qui doit agir.")),
    ("/compliance", (
        "AGRICAP FINTECH — écran Conformité (KYC/LCB-FT). À brancher sur le parcours "
        "d'instruction : un dossier ne devrait pas pouvoir être approuvé sans que "
        "l'état de conformité soit visible à l'écran de décision.")),
    ("/contracts", (
        "AGRICAP FINTECH — génération de contrat depuis `CreditDetailsModal`. "
        "Le bouton existe côté écran et est documenté « à brancher » "
        "(CREDIT_MODULE_STATUS.md §10) : action fantôme à résorber.")),
    ("/suppliers", (
        "AGRICAP FINTECH — onglet Référence, sous-écran « Fournisseurs agréés ». "
        "Alimente le décaissement fléché vers fournisseur ; aujourd'hui saisi à la main.")),
    ("/partners", (
        "AGRICAP LAND — espace partenaires. Le backend sert les partenaires, "
        "le site public a un formulaire `BecomePartner.jsx` qui ne le consomme pas.")),
    ("/notifications", (
        "AGRICAP FINTECH — centre de notifications (cloche de l'en-tête). "
        "Les notifications sont produites côté serveur et ne sont lues nulle part.")),
    ("/accounting", (
        "AGRICAP FINTECH — écrans comptables (balance, grand livre, annexes A/B/E). "
        "Socle bi-monnaie natif exposé par l'API, sans interface : la comptabilité "
        "se consulte aujourd'hui par l'admin Django.")),
    ("/ledger", (
        "AGRICAP FINTECH — écrans comptables SYSCOHADA. Même constat que "
        "`/accounting` : servi, non affiché.")),
    ("/fx", (
        "AGRICAP FINTECH — écran Taux de change (admin). `RateMaturityModal` existe "
        "côté front mais garde ses actions en `localStorage` au lieu d'appeler ces "
        "endpoints (CONVENTIONS.md §6, dette listée).")),
    ("/credits/guarantee-proposals", (
        "AGRICAP FINTECH — écran « Suivi des garanties » (CONVENTIONS.md §7.1 point 7) : "
        "cautions en attente de consentement avec compte à rebours 72 h, garanties à "
        "confirmer, libérations, cautions appelées. Tout le mécanisme « le client "
        "propose une caution, l'agent valide » est servi par l'API et n'a aucun "
        "écran — c'est la partie du principe 9 qui n'atteint pas l'utilisateur.")),
    ("/credits/applications/<*>/guarantee-proposals", (
        "AGRICAP FINTECH — panneau Garanties de `ApplicationDetail.tsx`. La liste des "
        "cautions candidates (`/candidates/`) est ce qui permet à l'agent de proposer "
        "un garant éligible plutôt que de saisir un nom au hasard : capacité "
        "d'engagement, cautions actives et absence de caution croisée sont déjà "
        "vérifiées côté serveur.")),
    ("/credits/guarantee-requests", (
        "AGRICAP FINTECH — file des demandes de caution reçues par un garant "
        "(espace client du garant). Sans écran, un membre sollicité comme caution "
        "n'a aucun moyen de voir la demande ni d'y consentir dans les 72 h.")),
    ("/investments", (
        "AGRICAP FINTECH — backoffice Investissement, `src/components/admin-investments/`. "
        "Les écrans de LISTE existent (projets, investisseurs, souscriptions) ; ce sont "
        "les ACTIONS de workflow qui ne sont branchées nulle part : validation "
        "d'analyse, valorisation par expert, vote du comité, clôture de levée, "
        "décaissement, enregistrement de retour, règlement de souscription. "
        "Le cycle de vie d'un projet se pilote donc aujourd'hui par l'admin Django, "
        "hors machine à états et hors journal.")),
    ("/caisses/payments", (
        "AGRICAP FINTECH — écran Paiements & rapprochement de la caisse. "
        "`/callback` est l'entrée du prestataire (Makuta) et n'a pas vocation à être "
        "appelée par un front ; les autres — liste, indéterminés, rapprochement, "
        "annulation, forçage — sont des actions de caissier sans interface.")),
)


# ─────────────────────────────────────────────────────────────────────────────
# Deuxième niveau : APPELÉ n'est pas ATTEIGNABLE.
#
# Le scan mesure si un fichier du front appelle la route. Il ne dit pas si ce
# fichier est lui-même atteint par un écran monté : une route appelée depuis un
# composant que personne n'importe est appelée et morte. Établir cela demanderait
# le graphe d'imports ET le graphe de montage des composants — une analyse
# statique d'un autre ordre, et faillible sur du JSX.
#
# Ces cas sont donc CONSTATÉS À LA MAIN et notés ici, pour que le verdict
# « connecté » ne se lise pas comme « accessible à un utilisateur ». La note est
# affichée EN PLUS du verdict, jamais à sa place.
# ─────────────────────────────────────────────────────────────────────────────

APPELE_MAIS_SANS_ECRAN: dict[str, str] = {
    "/accounting/comptes/<*>/suppression": (
        "Sans bouton, et c'est VOULU — ne pas compter cette route comme une dette. "
        "Elle répond TOUJOURS 409 : « la suppression d'un compte n'existe pas, la "
        "comptabilité est append-only ; désactivez-le par "
        "POST comptes/<code>/activation ». Elle est là pour restituer la RÈGLE à un "
        "client HTTP qui la cherche, plutôt qu'un 404 ambigu. "
        "`accountingApi.comptes.suppression` n'existe que pour typer cette réponse. "
        "Lui donner un bouton reviendrait à proposer une action dont la seule "
        "fonction est d'échouer — et à laisser croire qu'un compte peut disparaître "
        "d'un grand livre."),
    "/accounting/taux/saisie": (
        "Sans écran, et c'est VOULU — ne pas compter cette route comme une dette. "
        "Elle lève un 409 qui énonce la règle (« la saisie d'un taux se fait par "
        "POST /api/fx/rates ; `accounting.TauxChange` n'est qu'une projection en "
        "lecture seule ») au lieu d'un 404 ambigu. `accountingApi.taux.saisieInterdite` "
        "n'existe que pour typer cette réponse. Un panneau indicateur ne se branche "
        "pas à un bouton : l'afficher reviendrait à proposer une action dont la "
        "seule fonction est d'échouer."),
}

APPELE_MAIS_SANS_ECRAN_PREFIXES: tuple[tuple[str, str], ...] = (
    # L'entrée `/accounting/comptes` a été RETIRÉE le jour où
    # `AccountingPlanComptable` a été monté dans l'onglet « Plan — bi-devise » de
    # `src/pages/Accounting.jsx`. Une note de dette qu'on ne retire pas après
    # correction devient une fausse dette : elle envoie refaire un travail déjà
    # fait, exactement comme un endpoint faussement marqué connecté envoie
    # ignorer un travail qui reste à faire.
)


def sans_ecran_pour(chemin_normalise: str) -> str:
    """Note « appelé mais pas atteignable », règle la plus spécifique d'abord."""
    exact = APPELE_MAIS_SANS_ECRAN.get(chemin_normalise)
    if exact:
        return exact
    candidats = [(p, t) for p, t in APPELE_MAIS_SANS_ECRAN_PREFIXES
                 if chemin_normalise == p or chemin_normalise.startswith(p + "/")]
    if not candidats:
        return ""
    return max(candidats, key=lambda c: len(c[0]))[1]


def attendu_pour(chemin_normalise: str) -> str:
    """Recommandation de branchement pour un chemin, règle la plus spécifique d'abord."""
    exact = BRANCHEMENTS_ATTENDUS.get(chemin_normalise)
    if exact:
        return exact
    candidats = [(prefixe, texte) for prefixe, texte in BRANCHEMENTS_ATTENDUS_PREFIXES
                 if chemin_normalise == prefixe or chemin_normalise.startswith(prefixe + "/")]
    if not candidats:
        return ""
    return max(candidats, key=lambda c: len(c[0]))[1]
