"""Arrondi monétaire du système — deux décimales, résidu conservé (principe 4 bis).

`arrondir()` REND le sous-centime au lieu de l'absorber : un `quantize()` seul est une
perte silencieuse qu'on ne retrouve qu'au rapprochement, sans savoir quelle opération
l'a produite. La signature force l'appelant à décider — l'oublier devient visible.

Invariant : `montant + residu == exact`, sans tolérance.

Le résidu est SIGNÉ (`ROUND_HALF_UP`) : l'institution tantôt conserve la fraction,
tantôt l'avance. Une troncature systématique en sa faveur prélèverait une fraction de
centime sur chaque opération client — cela se décide, ça ne s'installe pas par un
choix d'arrondi.
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from .parsing import to_decimal

#: Quantum des montants monétaires — deux décimales, toutes devises.
CENTIME = Decimal("0.01")

#: Quantum de CONSERVATION du résidu, huit décimales.
#:
#: Le résidu d'une division est potentiellement infini en décimales ; il faut bien
#: l'arrêter quelque part pour le stocker. Huit décimales bornent la perte de second
#: ordre à 1e-8 unité monétaire par opération — soit un centime tous les millions
#: d'opérations, et cette perte-là est écrite ici plutôt que subie ailleurs.
RESIDU_QUANTUM = Decimal("0.00000001")

ZERO = Decimal("0")


def arrondir(valeur) -> tuple[Decimal, Decimal]:
    """`(montant au centime, résidu signé)` — avec `montant + residu == exact`.

    >>> arrondir("12.3456")
    (Decimal('12.35'), Decimal('-0.0044'))
    >>> arrondir("12.3400")
    (Decimal('12.34'), Decimal('0.0000'))

    Le résidu rendu est EXACT (non quantifié) : c'est au moment de l'écrire au compte
    qu'il est arrêté à `RESIDU_QUANTUM`, pour que la troncature de stockage ait un seul
    endroit et un seul responsable.
    """
    exact = to_decimal(valeur)
    montant = exact.quantize(CENTIME, rounding=ROUND_HALF_UP)
    return montant, exact - montant


def arrondir_residu(valeur) -> Decimal:
    """Résidu arrêté à la précision de stockage (`RESIDU_QUANTUM`)."""
    return to_decimal(valeur).quantize(RESIDU_QUANTUM, rounding=ROUND_HALF_UP)


def est_au_centime(valeur) -> bool:
    """Le montant tient-il exactement sur deux décimales ?

    Utilisé par les gardes de sérialiseur : un montant qui arrive du client avec trois
    décimales n'est pas arrondi en silence, il est refusé — sinon le client croit avoir
    déposé 10,005 et le système en a inscrit 10,01 sans le lui dire.
    """
    montant = to_decimal(valeur)
    return montant == montant.quantize(CENTIME, rounding=ROUND_HALF_UP)


def quantifier(valeur, quantum: Decimal = CENTIME) -> Decimal:
    """Quantize `ROUND_HALF_UP` pour les grandeurs NON monétaires (ratios, pourcentages).

    Pour un MONTANT, utiliser `arrondir()` : elle rend le résidu, que cette fonction-ci
    laisse tomber. Le nom distinct est délibéré — c'est ce qui empêche d'arrondir un
    montant en croyant faire un calcul de ratio.

    Rassemble trois copies identiques (`analytics/services._q`, `investments/metrics._q`,
    `credits/needs_sheet._q`), dont deux étaient rigoureusement le même corps.
    """
    return Decimal(valeur).quantize(quantum, rounding=ROUND_HALF_UP)


def en_float(valeur) -> float:
    """Frontière de sérialisation `Decimal` → `float`. `None` devient `0.0`.

    Contrepartie de `en_float_optionnel`, qui préserve le `None`. Les DEUX existent
    parce que le dépôt portait les deux contrats sous le MÊME nom `_f` dans quatre
    fichiers : `portfolio/serializers` et `analytics/services` rendaient `0.0`,
    `credits/scoring` et `credits/dataio_simulator` rendaient `None`. Un appelant qui
    se trompait de module changeait silencieusement la réponse de son API — d'où deux
    noms explicites plutôt qu'un nom court et ambigu.

    Choisir `en_float` quand l'absence de valeur SIGNIFIE zéro (un solde non mouvementé
    vaut 0) ; `en_float_optionnel` quand elle signifie « non renseigné » (un score non
    calculé n'est pas un score de 0).
    """
    return float(valeur) if valeur is not None else 0.0


def en_float_optionnel(valeur) -> float | None:
    """Frontière de sérialisation `Decimal` → `float`, en préservant `None`."""
    return float(valeur) if valeur is not None else None


def repartir(total, parts: int) -> list[Decimal]:
    """Répartit `total` en `parts` montants au centime dont la somme fait EXACTEMENT `total`.

    Le reliquat va à la DERNIÈRE part — même convention que l'échéancier du module
    crédit, où la dernière échéance est ajustée pour que le capital restant dû tombe
    rigoureusement à zéro. Aucun résidu à loger ici : rien n'est perdu, c'est une
    répartition, pas une conversion.
    """
    if parts <= 0:
        raise ValueError("Le nombre de parts doit être strictement positif.")
    exact = to_decimal(total)
    part, _ = arrondir(exact / Decimal(parts))
    montants = [part] * (parts - 1)
    montants.append(exact.quantize(CENTIME, rounding=ROUND_HALF_UP) - part * (parts - 1))
    return montants
