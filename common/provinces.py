"""Zones d'exploitation — les 26 provinces de la RDC et leurs chefs-lieux.

Ce module ne sert qu'à une chose : donner à une localisation écrite en clair
(« Kinshasa », « Nord-Kivu », « Goma », « Bukavu, Sud-Kivu ») le **segment de trois
lettres** que portent les références métier de l'institution — `AG-CD-KIN-01` pour un
projet, comme `AG-BUK-01` pour une agence. C'est ce segment qui rend une liste de
codes lisible sans ouvrir chaque dossier.

**Pourquoi en code et non en base** (principe 8) : le découpage administratif de la
RDC n'est pas un seuil métier que le comité révise, c'est une donnée d'État qui n'a
bougé qu'une fois depuis 2015 — et quand elle bougera, ce sera par une migration,
pas par un écran. La mettre en table donnerait la fausse impression qu'on peut la
changer à chaud sur un historique de codes déjà émis (or un code émis ne se réécrit
jamais, principe 3).

**Une localisation non reconnue rend `RDC`, jamais une approximation.** Fabriquer un
segment avec les trois premières lettres du texte saisi produirait « AUC » pour
« Au Congo » ou « LES » pour « Les collines de Kimbanseke » : trois lettres qui
ressemblent à un code de province sans en être un, et qu'un lecteur croirait
signifiantes. `RDC` dit exactement ce qui est su — le pays, pas la province.

Les villes sont indexées vers LEUR province : un projet à Goma est un projet du
Nord-Kivu, et deux projets de la même province se rangent sous le même préfixe
qu'ils aient été saisis « Goma » ou « Nord-Kivu ».
"""
from __future__ import annotations

import re
import unicodedata

__all__ = [
    "ZONE_INDETERMINEE",
    "PROVINCES",
    "VILLES",
    "code_zone",
    "libelle_zone",
]

#: Localisation non résolue — le pays est su, la province ne l'est pas.
ZONE_INDETERMINEE = "RDC"

#: Les 26 provinces (découpage de 2015), code → libellé officiel.
PROVINCES: dict[str, str] = {
    "KIN": "Kinshasa",
    "KON": "Kongo-Central",
    "KWA": "Kwango",
    "KWI": "Kwilu",
    "MAI": "Mai-Ndombe",
    "KAS": "Kasaï",
    "KAC": "Kasaï-Central",
    "KAO": "Kasaï-Oriental",
    "LOM": "Lomami",
    "SAN": "Sankuru",
    "MAN": "Maniema",
    "SKV": "Sud-Kivu",
    "NKV": "Nord-Kivu",
    "ITU": "Ituri",
    "HUE": "Haut-Uélé",
    "TSH": "Tshopo",
    "BUE": "Bas-Uélé",
    "NUB": "Nord-Ubangi",
    "MON": "Mongala",
    "SUB": "Sud-Ubangi",
    "EQU": "Équateur",
    "TSU": "Tshuapa",
    "TAN": "Tanganyika",
    "HLO": "Haut-Lomami",
    "LUA": "Lualaba",
    "HKA": "Haut-Katanga",
}

#: Chefs-lieux et grandes villes → province qui les contient. Un agent de terrain
#: écrit le nom de la ville où il travaille, pas celui de la province.
VILLES: dict[str, str] = {
    "Kinshasa": "KIN",
    "Matadi": "KON", "Boma": "KON", "Muanda": "KON", "Kimpese": "KON",
    "Kenge": "KWA",
    "Kikwit": "KWI", "Bandundu": "KWI",
    "Inongo": "MAI",
    "Tshikapa": "KAS",
    "Kananga": "KAC",
    "Mbuji-Mayi": "KAO",
    "Kabinda": "LOM", "Mwene-Ditu": "LOM",
    "Lusambo": "SAN",
    "Kindu": "MAN",
    "Bukavu": "SKV", "Uvira": "SKV",
    "Goma": "NKV", "Beni": "NKV", "Butembo": "NKV",
    "Bunia": "ITU",
    "Isiro": "HUE",
    "Kisangani": "TSH",
    "Buta": "BUE",
    "Gbadolite": "NUB",
    "Lisala": "MON",
    "Gemena": "SUB",
    "Mbandaka": "EQU",
    "Boende": "TSU",
    "Kalemie": "TAN",
    "Kamina": "HLO",
    "Kolwezi": "LUA",
    "Lubumbashi": "HKA", "Likasi": "HKA", "Kipushi": "HKA",
}

#: Orthographes alternatives couramment saisies. Rangées à part des libellés
#: officiels : ce sont des tolérances de saisie, pas des noms de provinces.
ALIAS: dict[str, str] = {
    "Bas-Congo": "KON",       # nom d'avant 2015, encore employé
    "Katanga": "HKA",         # ancienne province, éclatée en quatre depuis 2015
    "Orientale": "TSH",       # idem — la Tshopo en est le cœur
    "Maindombe": "MAI",
    "Equateur": "EQU",
    "Haut Uele": "HUE", "Bas Uele": "BUE",
}


def _normaliser(texte) -> str:
    """Majuscules, sans accents, séparateurs unifiés, encadré d'espaces.

    L'encadrement permet une recherche par MOT ENTIER : sans lui, « Beni » se
    trouverait dans « Benin » et « Kin » dans « Kinkole ».
    """
    brut = unicodedata.normalize("NFD", str(texte or ""))
    sans_accents = "".join(c for c in brut if unicodedata.category(c) != "Mn")
    return " " + re.sub(r"[^A-Za-z0-9]+", " ", sans_accents.upper()).strip() + " "


def _index() -> list[tuple[str, str]]:
    """(aiguille normalisée, code), du libellé le plus long au plus court.

    L'ordre est ce qui fait la justesse : « Kasaï-Central » contient « Kasaï ».
    Chercher le plus long d'abord évite de ranger un projet du Kasaï-Central sous
    le Kasaï — deux provinces distinctes.
    """
    entrees = [(libelle, code) for code, libelle in PROVINCES.items()]
    entrees += list(VILLES.items())
    entrees += list(ALIAS.items())
    normalisees = [(_normaliser(libelle).strip(), code) for libelle, code in entrees]
    return sorted(normalisees, key=lambda e: len(e[0]), reverse=True)


_INDEX = _index()


def code_zone(localisation, *, defaut: str = ZONE_INDETERMINEE) -> str:
    """Segment de trois lettres pour une localisation écrite en clair.

    Accepte aussi le code lui-même (« KIN »), pour qu'une reprise de données déjà
    codées ne soit pas re-devinée.
    """
    normalise = _normaliser(localisation)
    nu = normalise.strip()
    if nu in PROVINCES:
        return nu
    for aiguille, code in _INDEX:
        if f" {aiguille} " in normalise:
            return code
    return defaut


def libelle_zone(code: str) -> str:
    """Libellé lisible d'un code de zone — `RDC` compris."""
    code = (code or "").strip().upper()
    if code == ZONE_INDETERMINEE:
        return "Province non précisée"
    return PROVINCES.get(code, code)
