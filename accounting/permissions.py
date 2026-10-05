"""Gardes d'accès de l'API comptable.

Deux règles, posées ici une fois pour toutes plutôt que répétées dans chaque vue :

1. **La comptabilité n'a pas de vue client.** `HasCapability("read")` ne suffit PAS à
   protéger le grand livre : dans `rbac.role_registry`, les rôles de type « Client »
   (`client`, `agri_op`, `invest`, `partner`) portent `read=True`. Sans un garde `IsStaff`
   cumulé, un investisseur lirait les écritures de l'institution (principe 7 de MKOPO —
   anti-gaming, et principe 7 de HAZINA — asymétrie maîtrisée).

2. **Une vue multi-méthodes n'a pas une seule permission.** `GET /taux` est de la lecture,
   `POST /taux` est un acte. `CapaciteSelonMethode` rend la matrice explicite dans la
   déclaration de la vue, au lieu de la cacher dans un `if request.method` du corps —
   « toute vue sans permission explicite est un bug » (CONVENTIONS.md §5).

Ce garde-là n'a RIEN de comptable : il ne parle que de méthodes HTTP et de capacités RBAC.
Il vivait ici parce que le socle comptable en a eu besoin le premier, et il s'y est retrouvé
dupliqué mot pour mot dans `rbac.permissions` — deux implémentations du même concept, soit
le principe 6 en défaut. `rbac` est la bonne maison (aucune app ne doit dépendre de
`accounting` pour un garde générique) : ce module n'en garde qu'un **ré-export**, pour que
les vues comptables continuent d'importer leurs gardes depuis un seul endroit.
"""
from __future__ import annotations

from accounts.permissions import IsStaff
from rbac.permissions import CapaciteSelonMethode, HasCapability

__all__ = ["CapaciteSelonMethode", "LIRE", "SAISIR", "VALIDER", "PARAMETRER"]


#: Combinaisons prêtes à l'emploi (toujours cumulées avec `IsStaff`).
LIRE = [IsStaff, HasCapability("read")]
SAISIR = [IsStaff, HasCapability("create")]
VALIDER = [IsStaff, HasCapability("validate")]
PARAMETRER = [IsStaff, HasCapability("config")]
