"""Correspondance entre les deux nomenclatures de filières (CONVENTIONS.md §6).

Numérique `01`–`14` (`referentiel.chains`) et alphabétique `MAIS`/`RIZ`…
(`reference_data.ValueChain`) ne sont PAS deux noms du même objet : `01` est une
famille, `MAIS` une culture de cette famille. Relation N:1, sens canonique
alphabétique → numérique — d'où une correspondance et non une fusion.

Le bug que `numero_pour()` répare : `credits/referentiel_loader` écrit un code
numérique dans `ReferentielFiliere.value_chain_code`, `credits/analyse` et
`credits/dataio_simulator` le lisent en alphabétique. La jointure ne matche qu'en
test, où les `ValueChain` sont fabriquées avec un code « 01 ».

En code et non en base : le principe 8 vise les seuils métier révisables par le
comité, pas une structure qui ne bouge qu'avec une migration. En base, ce serait
une TROISIÈME nomenclature à tenir synchronisée.

Une filière non résolue renvoie `None` — jamais une chaîne par défaut (principe 10).
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

__all__ = [
    "Correspondance",
    "CORRESPONDANCES_EPINGLEES",
    "est_numero",
    "normaliser_numero",
    "numero_pour",
    "resoudre",
    "codes_par_numero",
]


#: Correspondances figées pour les filières effectivement seedées
#: (`reference_data/fixtures/value_chains_initial.py`). Verrou de non-régression, pas
#: source de vérité : le mécanisme général reste la détection par signaux.
#:
#: `CAFE_ARABICA` → `12` (Agroforesterie & bois) et non `07` (Cultures industrielles) :
#: c'est le classement de `referentiel/chains.py`, où « café » et « cacao » sont des signaux
#: de la chaîne agroforestière alors que « thé » relève des cultures industrielles. Cette
#: asymétrie est celle du référentiel v3, pas un arbitrage de ce module — elle mérite une
#: confirmation métier, elle ne se tranche pas dans du code.
CORRESPONDANCES_EPINGLEES: dict[str, str] = {
    "MAIS": "01",          # Céréales
    "RIZ": "01",           # Céréales
    "HARICOT": "02",       # Légumineuses
    "MANIOC": "03",        # Tubercules & racines
    "CAFE_ARABICA": "12",  # Agroforesterie & bois (cf. avertissement ci-dessus)
}


@dataclass(frozen=True)
class Correspondance:
    """Résultat d'une résolution, avec son lignage.

    `origine` et `indice` ne sont pas décoratifs : un analyste doit pouvoir dire POURQUOI un
    dossier a été rapproché de telle famille de référentiel — « chaque chiffre a une
    provenance ». Une correspondance obtenue par mot-clé n'a pas la même autorité qu'une
    correspondance épinglée, et l'appelant doit pouvoir en tenir compte.
    """

    code_numerique: str   # "01".."14"
    slug: str
    libelle: str
    specialite: str
    origine: str          # "numerique" | "slug" | "epinglee" | "mot_cle"
    indice: str           # ce qui a permis de conclure


def _catalogue():
    """Les 14 chaînes, chargées à l'appel (cf. docstring : pas d'inversion de couches)."""
    from referentiel.chains import BY_CODE, BY_SLUG, CHAINS

    return CHAINS, BY_CODE, BY_SLUG


def _normaliser(texte: str) -> str:
    """Minuscules, sans accents, ponctuation et séparateurs ramenés à des espaces.

    `CAFE_ARABICA` → `cafe arabica` ; `Tubercules & racines` → `tubercules racines`. Les
    `keywords` du catalogue subissent le même traitement, pour que « niébé » et « niebe »
    ou « maïs » et « MAIS » se rencontrent.
    """
    decompose = unicodedata.normalize("NFD", str(texte or ""))
    sans_accents = "".join(c for c in decompose if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", re.sub(r"[^0-9a-zA-Z]+", " ", sans_accents)).strip().lower()


def normaliser_numero(valeur: str) -> str:
    """`"1"`, `"01"`, `" 01 "` → `"01"` si la chaîne existe, sinon `""`.

    Le zéro de tête est significatif partout ailleurs (`chain_code` est un `CharField(2)`,
    les noms de simulateurs sont numérotés `_01_`) : on l'accepte manquant en entrée, jamais
    en sortie.
    """
    brut = str(valeur or "").strip()
    if not brut.isdigit():
        return ""
    numero = brut.zfill(2)
    _, par_code, _ = _catalogue()
    return numero if numero in par_code else ""


def est_numero(valeur: str) -> bool:
    """Vrai si la valeur relève de la nomenclature numérique `01`–`14`."""
    return bool(normaliser_numero(valeur))


def _par_mots_cles(texte_normalise: str):
    """Meilleure chaîne pour ce texte, ou `None`.

    Départage, dans l'ordre : le signal le PLUS LONG l'emporte, puis le nombre de signaux,
    puis le code le plus petit (déterminisme). Le signal le plus long d'abord est ce qui
    fait qu'un « moulin à maïs » est rangé en Transformation (`moulin`, 6 lettres) et non en
    Céréales (`mais`, 4) : le signal le plus spécifique décrit mieux l'activité.
    """
    if not texte_normalise:
        return None
    aiguille = f" {texte_normalise} "
    chaines, _, _ = _catalogue()

    meilleur = None
    for chaine in chaines:
        touches = [
            mot for mot in (_normaliser(k) for k in chaine.keywords)
            if mot and f" {mot} " in aiguille
        ]
        if not touches:
            continue
        plus_long = max(touches, key=len)
        rang = (len(plus_long), len(touches), -int(chaine.code))
        if meilleur is None or rang > meilleur[0]:
            meilleur = (rang, chaine, plus_long)
    if meilleur is None:
        return None
    _, chaine, mot = meilleur
    return chaine, mot


def resoudre(code: str, *, libelle: str = "") -> Correspondance | None:
    """Rattache une filière — quelle que soit sa nomenclature — à sa chaîne `01`–`14`.

    `code` accepte aussi bien `"09"` que `"MAIS"` ou `"cereales"`. `libelle` est le libellé
    humain (`ValueChain.label`), utilisé en dernier recours : il porte souvent le signal que
    le code abrège (`code="AR_TILAPIA"`, `libelle="Tilapia en étang"`).

    Renvoie `None` si rien ne permet de conclure — l'appelant doit alors traiter la filière
    comme non rattachée, pas la ranger d'office quelque part.
    """
    _, par_code, par_slug = _catalogue()

    numero = normaliser_numero(code)
    if numero:
        chaine = par_code[numero]
        return _construire(chaine, "numerique", numero)

    brut = str(code or "").strip()
    chaine = par_slug.get(brut.lower())
    if chaine is not None:
        return _construire(chaine, "slug", brut.lower())

    epingle = CORRESPONDANCES_EPINGLEES.get(brut.upper())
    if epingle is not None:
        return _construire(par_code[epingle], "epinglee", brut.upper())

    for texte in (brut, libelle):
        trouve = _par_mots_cles(_normaliser(texte))
        if trouve is not None:
            chaine, mot = trouve
            return _construire(chaine, "mot_cle", mot)
    return None


def _construire(chaine, origine: str, indice: str) -> Correspondance:
    return Correspondance(
        code_numerique=chaine.code,
        slug=chaine.slug,
        libelle=chaine.libelle,
        specialite=chaine.specialite,
        origine=origine,
        indice=indice,
    )


def numero_pour(code: str, *, libelle: str = "") -> str:
    """Code numérique `01`–`14` d'une filière, ou `""` si non rattachée.

    C'est la fonction que doivent appeler les lecteurs de
    `ReferentielFiliere.value_chain_code` : cette colonne contient du numérique, alors
    qu'ils l'interrogent aujourd'hui avec un code alphabétique (cf. docstring du module).
    """
    correspondance = resoudre(code, libelle=libelle)
    return correspondance.code_numerique if correspondance else ""


def codes_par_numero() -> dict[str, tuple[str, ...]]:
    """Vue inverse des correspondances épinglées : `"01" → ("MAIS", "RIZ")`.

    N:1 assumé — une famille regroupe plusieurs cultures. Ne couvre que les filières
    épinglées : l'ensemble alphabétique n'est pas borné par le code (il vient d'un classeur
    uploadé), donc aucun inverse exhaustif n'est calculable hors de la base.
    """
    inverse: dict[str, list[str]] = {}
    for alphabetique, numerique in sorted(CORRESPONDANCES_EPINGLEES.items()):
        inverse.setdefault(numerique, []).append(alphabetique)
    return {numero: tuple(codes) for numero, codes in sorted(inverse.items())}
