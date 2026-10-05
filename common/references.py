"""Référence métier d'une transaction — `AG-CD-AG-BUK-01-AIRTEL-000042`.

    AG - <pays> - <code agence> - <opérateur> - <séquence>

`HQ` si aucune agence, `NA` si le segment ne s'applique pas : aucun segment n'est
jamais omis, c'est ce qui rend deux références comparables à l'œil.

Elle ne se découpe PAS par `-` : le code agence en contient déjà (« AG-BUK-01 »),
donc le nombre de tirets varie d'une agence à l'autre. Elle est faite pour être lue
et recherchée, pas parsée — agence, opérateur et séquence restent atteignables par
les relations.

Stockée et non calculée : une propriété calculée changerait rétroactivement au
renommage d'une agence, et la référence notée par un client il y a six mois ne
correspondrait plus à rien (principe 3).
"""
from __future__ import annotations

import unicodedata

from django.conf import settings

#: Préfixe institutionnel.
PREFIXE = "AG"

#: Segment sans valeur pour cette opération (espèces : pas d'opérateur).
SEGMENT_ABSENT = "NA"

#: Aucune agence rattachée — siège. Même sens que `TreasuryAccount.agency = null`.
AGENCE_SIEGE = "HQ"

#: Largeur de la séquence. 6 chiffres = 999 999 lignes par registre avant
#: débordement ; au-delà, le format s'allonge naturellement au lieu de tronquer
#: (un identifiant tronqué cesse d'être unique, ce qui est pire que long).
LARGEUR_SEQUENCE = 6


def code_pays() -> str:
    """Code pays d'exploitation. `CD` par défaut (République démocratique du Congo).

    En `settings` et non en base : il identifie le DÉPLOIEMENT, comme l'issuer de
    l'IdP. Une institution ne change pas de pays sans redéploiement, et le mettre
    en table donnerait la fausse impression qu'on peut le basculer à chaud sur un
    historique déjà émis.
    """
    return str(getattr(settings, "INSTITUTION_COUNTRY", "CD") or "CD").upper()


def segment(valeur, *, defaut: str = SEGMENT_ABSENT, compact: bool = False) -> str:
    """Normalise un segment : majuscules, sans accent, sans caractère parasite.

    `compact=False` CONSERVE les tirets internes — `AG-BUK-01` doit rester lisible
    tel quel, c'est le code que l'agence porte officiellement.

    `compact=True` les supprime : un nom commercial d'opérateur (« Airtel Money »)
    n'a pas de structure interne à préserver, et y laisser un tiret ne ferait
    qu'ajouter de l'ambiguïté à la lecture.
    """
    if valeur is None:
        return defaut
    texte = unicodedata.normalize("NFD", str(valeur))
    texte = "".join(c for c in texte if unicodedata.category(c) != "Mn")
    texte = "".join(c if c.isalnum() else "-" for c in texte.upper())
    parties = [part for part in texte.split("-") if part]
    if not parties:
        return defaut
    return "".join(parties) if compact else "-".join(parties)


def composer(*, agence, operateur, sequence: int) -> str:
    """Assemble la référence. Aucun segment n'est omis, jamais."""
    return "-".join([
        PREFIXE,
        code_pays(),
        segment(agence, defaut=AGENCE_SIEGE),
        segment(operateur, defaut=SEGMENT_ABSENT, compact=True),
        f"{int(sequence):0{LARGEUR_SEQUENCE}d}",
    ])


def contexte_ordre(order) -> tuple[str | None, str | None]:
    """`(code agence, opérateur)` d'un ordre de paiement.

    L'agence vient du compte de trésorerie qui porte l'opération ; l'opérateur du
    PARTENAIRE rattaché à ce compte (`partners.Partner`), seul endroit où le nom
    commercial d'un opérateur Mobile Money existe.

    Tant que la table `Partner` n'est pas peuplée, l'opérateur rend `None` — donc
    `NA` dans la référence. Ce n'est pas une approximation : c'est l'aveu que
    l'institution n'a pas encore enregistré ses opérateurs. Y mettre « MM » à la
    place ferait passer une FAMILLE de canal pour un opérateur, et un rapprochement
    avec Airtel ne se fait pas contre « MM ».
    """
    if order is None:
        return None, None
    compte = getattr(order, "treasury_account", None)
    agence = getattr(getattr(compte, "agency", None), "code", None)
    partenaire = getattr(compte, "partner", None)
    return agence, getattr(partenaire, "name", None)


def reference_mouvement(mouvement, *, ordre=None) -> str:
    """Référence d'un mouvement de portefeuille.

    `ordre` est le contexte que l'appelant fournit quand il le connaît. C'est
    nécessaire : un `PaymentOrder` pointe vers son mouvement APRÈS l'avoir créé,
    donc au moment où le mouvement s'enregistre, le lien inverse n'existe pas
    encore. Sans ce paramètre, tout flux externe naîtrait en `HQ-NA` et il
    faudrait un second passage pour le corriger — c'est-à-dire réécrire une
    référence déjà émise.

    À défaut de contexte, on tente les liens inverses (utile à la reprise
    d'historique), puis on retombe sur les codes explicites.
    """
    if ordre is None:
        ordre = _premier(getattr(mouvement, "payment_order", None))
    if ordre is None:
        retrait = _premier(getattr(mouvement, "withdrawal_request", None))
        ordre = getattr(retrait, "payout_order", None) if retrait else None
    agence, operateur = contexte_ordre(ordre)
    return composer(agence=agence, operateur=operateur, sequence=mouvement.pk)


def reference_ordre(order) -> str:
    """Référence d'un ordre de paiement.

    Elle s'ajoute à `PaymentOrder.reference` (« AGC… ») sans la remplacer : cette
    dernière est la clé technique d'idempotence et de rapprochement fournisseur,
    celle-ci est la référence MÉTIER, lisible et structurée. Les confondre
    obligerait à changer la clé d'idempotence pour changer un libellé.
    """
    agence, operateur = contexte_ordre(order)
    return composer(agence=agence, operateur=operateur, sequence=order.pk)


def _premier(gestionnaire):
    """Premier objet d'un manager inverse, ou `None`. Tolère un lien absent."""
    if gestionnaire is None:
        return None
    try:
        return gestionnaire.all()[:1][0]
    except (AttributeError, IndexError):
        return None
