"""
Parsing des valeurs du référentiel v3 (texte français) vers des nombres.

Le référentiel stocke des plages sous forme littérale, avec virgule décimale,
séparateur de milliers par espace, tiret cadratin/demi-cadratin, et parfois un
préfixe/suffixe (« IC 1,8–2,2 », « 2,5–3,5 USD/kg vif », « 600–1 200 / an »,
« 8 % »). On n'invente jamais : une valeur illisible renvoie None (→ le contrôle
concerné deviendra `NON ÉVALUABLE`, CAHIER §1/§4).
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

# Tirets acceptés comme séparateurs de plage : hyphen, non-breaking hyphen,
# figure dash, en dash, em dash, minus sign.
_DASHES = "-‐‑‒–—−"
_NUMBER_RE = re.compile(r"[-+]?\d[\d   ]*(?:[.,]\d+)?")


def to_number(value) -> float | None:
    """Convertit une valeur cellule (nombre déjà typé ou texte FR) en float."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    m = _NUMBER_RE.search(text)
    if not m:
        return None
    return _clean_number(m.group(0))


def _clean_number(token: str) -> float | None:
    # Retire les séparateurs de milliers (espaces, espaces insécables) puis
    # normalise la virgule décimale.
    t = token.replace(" ", "").replace(" ", "").replace(" ", "")
    t = t.replace(",", ".")
    # Un éventuel point de milliers résiduel (rare ici) : garder le dernier point.
    if t.count(".") > 1:
        head, _, tail = t.rpartition(".")
        t = head.replace(".", "") + "." + tail
    try:
        return float(t)
    except ValueError:
        return None


def to_range(value) -> tuple[float | None, float | None]:
    """
    Extrait (min, max) d'une plage textuelle. Une valeur unique donne (v, v).
    « 8 % » → (0.08, 0.08). Renvoie (None, None) si rien d'exploitable.
    """
    if value is None or value == "":
        return (None, None)
    if isinstance(value, (int, float)):
        return (float(value), float(value))

    text = str(value).strip()
    is_percent = "%" in text
    # Isole tous les nombres présents (gère préfixes « IC », suffixes « USD/kg »).
    numbers = [_clean_number(m.group(0)) for m in _NUMBER_RE.finditer(text)]
    numbers = [n for n in numbers if n is not None]
    if not numbers:
        return (None, None)

    if len(numbers) == 1:
        lo = hi = numbers[0]
    else:
        # Plage explicite « a–b » : bornes = premier et deuxième nombre.
        lo, hi = numbers[0], numbers[1]
        if lo > hi:
            lo, hi = hi, lo

    if is_percent:
        lo = lo / 100.0 if lo is not None else None
        hi = hi / 100.0 if hi is not None else None
    return (lo, hi)


# --- Variante exacte (Decimal) ---------------------------------------------
# `to_number`/`to_range` restent la porte d'entrée des concepts INDICATIFS du
# référentiel (rendements, plages de coûts, pertes) : ce sont des ordres de
# grandeur affichés, jamais des termes de calcul financier.
#
# Les paramètres de la feuille 16, eux, gouvernent des DÉCISIONS : seuils DSCR,
# score global minimum, poids du scoring, décote de garantie. Le principe 4 les
# veut en `Decimal`, et un `Decimal` obtenu depuis un `float` a déjà perdu :
# `Decimal(float("1.20"))` vaut 1,1999999999999999555910790149937383830547332763671875.
# On parse donc le texte DIRECTEMENT en `Decimal`, sans intermédiaire binaire.

_CENT = Decimal(100)
#: Ne garde du jeton que chiffres, séparateurs et signe (retire tous les
#: espaces, y compris insécables et fins, sans avoir à les énumérer).
_NON_NUMERIC_RE = re.compile(r"[^0-9.,+\-]")


def _clean_decimal(token: str) -> Decimal | None:
    t = _NON_NUMERIC_RE.sub("", token).replace(",", ".")
    # Un éventuel point de milliers résiduel : garder le dernier point.
    if t.count(".") > 1:
        head, _, tail = t.rpartition(".")
        t = head.replace(".", "") + "." + tail
    try:
        return Decimal(t)
    except InvalidOperation:
        return None


def to_decimal(value) -> Decimal | None:
    """`to_number`, en exact : « 1,20 » → `Decimal("1.20")`, jamais un `float`."""
    if value is None or value == "":
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        # Cellule Excel déjà typée : `str()` donne la représentation courte
        # (repr) du flottant, donc « 1.2 » et non ses 17 décimales binaires.
        return Decimal(str(value))
    m = _NUMBER_RE.search(str(value).strip())
    if not m:
        return None
    return _clean_decimal(m.group(0))


def to_decimal_range(value) -> tuple[Decimal | None, Decimal | None]:
    """`to_range`, en exact. « 8 % » → (`Decimal("0.08")`, `Decimal("0.08")`)."""
    if value is None or value == "":
        return (None, None)
    if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
        d = to_decimal(value)
        return (d, d)

    text = str(value).strip()
    is_percent = "%" in text
    numbers = [_clean_decimal(m.group(0)) for m in _NUMBER_RE.finditer(text)]
    numbers = [n for n in numbers if n is not None]
    if not numbers:
        return (None, None)

    if len(numbers) == 1:
        lo = hi = numbers[0]
    else:
        lo, hi = numbers[0], numbers[1]
        if lo > hi:
            lo, hi = hi, lo

    if is_percent:
        lo, hi = lo / _CENT, hi / _CENT
    return (lo, hi)


def has_range_separator(value) -> bool:
    """Vrai si le texte contient un séparateur de plage (deux bornes attendues)."""
    if value is None:
        return False
    return any(d in str(value) for d in _DASHES if d != "-") or "-" in str(value)
