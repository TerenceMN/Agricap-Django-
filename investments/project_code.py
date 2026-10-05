"""Code d'un projet d'investissement — `AG-CD-KIN-01`.

    AG - <pays> - <zone> - <séquence>

Même grammaire que la référence de transaction (`common/references.py`) et que le
code d'agence (`AG-BUK-01`) : préfixe institutionnel, pays de déploiement, zone,
séquence. Le préfixe et le pays sont donc LUS dans `common.references` et non
recopiés — une institution qui changerait de pays le changerait à un seul endroit.

**Généré, jamais saisi.** Un code saisi à la main est un code que deux personnes
composent différemment, qui finit avec des espaces, des accents, une casse variable
et parfois un doublon. Ici il se déduit de la localisation du projet et de ce qui
existe déjà en base.

**Séquence par ZONE et non globale** : `AG-CD-KIN-01` et `AG-CD-NKV-01` coexistent.
Le numéro dit « le premier projet de cette province », ce qui est l'information
qu'un gestionnaire de portefeuille lit réellement.

**La séquence ne se réutilise pas.** Elle repart du plus grand numéro DÉJÀ ÉMIS sur
la zone, pas du nombre de projets vivants : un projet annulé (P13) garde son code, et
le suivant prend le numéro d'après. Deux projets ne portent jamais le même code, même
à des années d'écart — c'est la condition pour que le journal d'audit, qui référence
les projets par leur code (`workflow._audit_transition`), reste lisible.
"""
from __future__ import annotations

import re

from common import references
from common.provinces import code_zone

__all__ = ["LARGEUR_SEQUENCE", "prefixe", "prochain_numero", "generer"]

#: Largeur du numéro de séquence. Deux chiffres suffisent à la lecture courante ;
#: au-delà de 99, le format s'allonge naturellement (`…-100`) au lieu de tronquer —
#: un identifiant tronqué cesse d'être unique, ce qui est pire que long.
LARGEUR_SEQUENCE = 2


def prefixe(localisation="") -> str:
    """`AG-CD-KIN` — tout le code sauf sa séquence."""
    return "-".join([references.PREFIXE, references.code_pays(), code_zone(localisation)])


def prochain_numero(prefixe_zone: str) -> int:
    """Plus grand numéro déjà émis sur cette zone, plus un.

    Lu sur les CODES et non sur un compteur en table : le code est la seule vérité
    de ce qui a été émis, et un compteur séparé se désynchronise dès la première
    reprise de données ou le premier projet créé avec un code explicite.
    """
    from .models import Project

    motif = re.compile(rf"^{re.escape(prefixe_zone)}-(\d+)$")
    maximum = 0
    codes = Project.objects.filter(code__startswith=f"{prefixe_zone}-").values_list(
        "code", flat=True)
    for code in codes:
        trouve = motif.match(code or "")
        if trouve:
            maximum = max(maximum, int(trouve.group(1)))
    return maximum + 1


def generer(*, localisation="") -> str:
    """Code du prochain projet de cette zone.

    N'est PAS une réservation : entre ce calcul et l'insertion, un autre poste peut
    prendre le même numéro. C'est l'unicité portée par la base (`Project.code` est
    `unique`) qui tranche, et `services.create_project` réessaie sur collision.
    """
    zone = prefixe(localisation)
    return f"{zone}-{prochain_numero(zone):0{LARGEUR_SEQUENCE}d}"
