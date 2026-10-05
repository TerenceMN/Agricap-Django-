"""Rendu texte partagé par les commandes de `caisses` (`check_makuta`,
`reconcile_payment_orders`).

Une seule fonction, mais elle mérite son module : la sortie de `check_makuta` est destinée à
être transmise telle quelle au fournisseur, et une checklist dont les guillemets fermants
tombent en début de ligne se lit comme un brouillon.
"""
from __future__ import annotations

#: Signes qui ne commencent JAMAIS une ligne en typographie française. Un « mot » composé
#: uniquement de ces signes (« » », « ?). », « : ») reste collé à ce qui le précède, quitte à
#: dépasser la largeur de quelques caractères.
_CLOSING_PUNCTUATION = frozenset("»:;!?).,…")

#: Symétrique : un guillemet ou une parenthèse ouvrante ne termine JAMAIS une ligne.
_OPENING_PUNCTUATION = frozenset("«(")


def _never_starts_a_line(word: str) -> bool:
    return bool(word) and all(character in _CLOSING_PUNCTUATION for character in word)


def _never_ends_a_line(line: str) -> bool:
    return bool(line) and all(character in _OPENING_PUNCTUATION for character in line.split()[-1])


def wrap(text: str, width: int) -> list[str]:
    """Découpage sur les espaces. Volontairement pas `textwrap` : on veut garder la
    ponctuation française détachée (« ... » : ...) sans qu'elle passe à la ligne seule."""
    lines: list[str] = []
    current = ""
    for word in str(text).split():
        candidate = f"{current} {word}".strip()
        if (len(candidate) > width and current
                and not _never_starts_a_line(word) and not _never_ends_a_line(current)):
            lines.append(current)
            current = word
        else:
            current = candidate
    lines.append(current)
    return lines or [""]
