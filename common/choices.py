"""Choix partagés entre apps métier (évite la ré-invention par app)."""
from __future__ import annotations

from django.db import models


class FlowStatus(models.TextChoices):
    """Cycle de vie générique d'une opération financière — miroir exact de
    `src/lib/constants.js` `STATUS`, réutilisé par `caisses`, `transactions`,
    `investments.Movement`, `savings` plutôt que ré-inventé par app."""

    DRAFT = "draft", "Brouillon"
    SUBMITTED = "submitted", "Soumis"
    PENDING_VALIDATION = "pending_validation", "En attente de validation"
    APPROVED = "approved", "Approuvé"
    POSTED = "posted", "Comptabilisé"
    REJECTED = "rejected", "Rejeté"
    REVERSED = "reversed", "Annulé (contre-passé)"


class Channel(models.TextChoices):
    """Canal d'une opération d'argent — par quelle « porte » la valeur entre ou sort.

    Nomenclature UNIQUE du concept (principe 6). Elle a vécu en double :
    `savings.SavingsPlan.Channel` (`deposit_mode`, `deposit_channel`, `channel`) et
    `caisses/channels.py` portaient les mêmes valeurs, chacune de son côté — l'en-tête de
    `caisses/channels.py` documentait la dette et demandait ce point de convergence.
    `FlowStatus` ci-dessus est le précédent exact du patron.

    **Les libellés sont ceux de `savings.SavingsPlan.Channel`, au caractère près.** Ce
    n'est pas de la coquetterie : les `choices` sont sérialisés dans les migrations, donc
    faire pointer `savings` sur cette classe ne doit produire AUCUNE migration. Toute
    retouche de libellé ici en fabriquerait une — et casserait le raccordement.

    Chaque app reste maîtresse du SOUS-ENSEMBLE qu'elle accepte : `caisses` ignore
    volontairement `WALLET` (voir `caisses/channels.py`). Le vocabulaire est commun, la
    politique ne l'est pas.
    """

    AGENT = "agent", "Agent"
    MOBILE_MONEY = "mobile_money", "Mobile Money"
    BANK = "bank", "Banque"
    WALLET = "wallet", "Portefeuille"


#: Canaux qui font réellement traverser l'argent par un TIERS (fournisseur de paiement) :
#: la valeur n'est acquise qu'à la confirmation externe. Propriété intrinsèque du canal,
#: pas d'une app — d'où sa place ici plutôt que recopiée par chaque appelant.
CANAUX_EXTERNES = frozenset({Channel.MOBILE_MONEY.value, Channel.BANK.value})

#: Canaux où aucun tiers n'intervient : le staff CONSTATE une remise (`agent`), ou la
#: valeur ne fait que se déplacer à l'intérieur d'AGRICAP (`wallet`).
CANAUX_INTERNES = frozenset({Channel.AGENT.value, Channel.WALLET.value})
