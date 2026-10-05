"""Permissions FX — le garde est celui de `rbac`, pas une réécriture locale.

`GET /api/fx/rates` et `POST /api/fx/rates` partagent une URL (contrat consommé par
`src/services/api.ts`), mais pas la même exigence : lire un taux est un droit de personnel,
en publier un est un acte de configuration institutionnelle. Une permission déclarative
sensible à la méthode évite le contre-modèle « contrôle dans le corps de la vue » proscrit
par CONVENTIONS.md §5.

`CapabilityByMethod` était une TROISIÈME écriture du même mécanisme (avec
`rbac.permissions.CapaciteSelonMethode` et son jumeau comptable) — principe 6 en défaut.
Elle n'est plus qu'un **adaptateur d'une ligne** : elle traduit sa signature « safe / unsafe »
en la matrice explicite par méthode que `rbac` sait déjà appliquer. Le nom est conservé parce
que `fx/views.py` l'importe ; les futures vues FX gagneraient à appeler directement
`CapaciteSelonMethode(GET="read", POST="config")`, plus lisible qu'un couple safe/unsafe.

Seul écart de comportement, volontaire et dans le sens de la fermeture : un verbe exotique
(TRACE, CONNECT) était auparavant traité comme « unsafe » — donc évalué — alors qu'il est
désormais refusé d'emblée, le défaut de `rbac` étant fermé.
"""
from __future__ import annotations

from rbac.permissions import CapaciteSelonMethode

__all__ = ["CapabilityByMethod"]

#: Découpage de DRF (`rest_framework.permissions.SAFE_METHODS`), rendu explicite pour
#: pouvoir le traduire en matrice par méthode.
_METHODES_SURES = ("GET", "HEAD", "OPTIONS")
_METHODES_ECRITURE = ("POST", "PUT", "PATCH", "DELETE")


def CapabilityByMethod(*, safe: str = "read", unsafe: str = "config"):
    """Capacité `safe` sur les méthodes de lecture, `unsafe` sur celles d'écriture."""
    return CapaciteSelonMethode(
        **{methode: safe for methode in _METHODES_SURES},
        **{methode: unsafe for methode in _METHODES_ECRITURE},
    )
