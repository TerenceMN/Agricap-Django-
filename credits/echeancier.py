"""
Échéancier prévisionnel — capital constant, intérêts dégressifs sur capital
restant dû, avec différé (SPEC Moteur d'analyse, annexe A).

C'est la pièce financière centrale du moteur : le service de la dette qu'elle
produit est le dénominateur du DSCR (critères C2 et C3), et c'est ce tableau que
le client voit comme calendrier de remboursement.

Règles non négociables (principe 4) :
  - `Decimal` partout, `float` nulle part dans le calcul ;
  - quantize explicite à 0,01 avec `ROUND_HALF_UP` à chaque montant produit ;
  - intérêts du mois calculés sur le solde de DÉBUT de mois, avant paiement ;
  - dernière tranche de capital ajustée au solde exact → CRD final rigoureusement
    nul, quels que soient les arrondis intermédiaires.

Écart signalé avec la SPEC §4 (pseudo-code) : celui-ci calcule la tranche
d'amortissement `A = capital / N` AVANT la phase de différé. En franchise totale
les intérêts sont capitalisés, donc le capital à amortir n'est plus `C` mais
`CRD` en fin de différé — le pseudo-code ne solderait pas le prêt. L'annexe A.2
donne d'ailleurs A = 477,59 = 1 432,78 / 3, et non 1 330 / 3. On suit l'annexe.

Second écart : le pseudo-code renvoie des `float` dans les lignes. On conserve
des `Decimal` ; `serialiser_echeancier()` produit la vue JSON (chaînes) pour
l'API, sans jamais faire transiter un binaire flottant.
"""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

CENT = Decimal("0.01")
ZERO = Decimal("0.00")

MODE_INTERETS_SEULS = "interets_seuls"
MODE_FRANCHISE_TOTALE = "franchise_totale"
MODES = (MODE_INTERETS_SEULS, MODE_FRANCHISE_TOTALE)

PHASE_DIFFERE = "differe"
PHASE_AMORTISSEMENT = "amortissement"


class EcheancierError(ValueError):
    """Paramètres de prêt inexploitables — jamais d'échéancier « best effort »."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


def q2(value: Decimal) -> Decimal:
    """Quantize monétaire unique du module : 0,01 / ROUND_HALF_UP."""
    return Decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


def _dec(value, name: str) -> Decimal:
    try:
        return Decimal(str(value))
    except Exception as exc:  # noqa: BLE001 — remonté en erreur métier explicite
        raise EcheancierError("PARAMETRE_INVALIDE", f"{name} n'est pas un nombre : {value!r}") from exc


def construire_echeancier(
    capital,
    taux_annuel,
    duree_mois: int,
    differe_mois: int = 0,
    mode_differe: str = MODE_INTERETS_SEULS,
) -> list[dict]:
    """Tableau d'amortissement mois par mois.

    Args:
        capital: montant décaissé (C).
        taux_annuel: taux nominal en pourcentage (18 = 18 %/an).
        duree_mois: durée totale D, différé inclus.
        differe_mois: F, nombre de mois de différé (0 = aucun).
        mode_differe:
            `interets_seuls`   — le client paie les intérêts, le capital reste
                                 intact (standard AGRICAP v4) ;
            `franchise_totale` — rien n'est payé, les intérêts sont capitalisés
                                 et grossissent le capital à amortir.

    Returns:
        Une ligne par mois : `mois`, `phase`, `capital`, `interets`,
        `interets_capitalises`, `echeance`, `crd` — tous `Decimal` quantizés.
    """
    capital = _dec(capital, "capital")
    taux_annuel = _dec(taux_annuel, "taux_annuel")

    if capital <= 0:
        raise EcheancierError("CAPITAL_INVALIDE", "Le capital doit être strictement positif.")
    if taux_annuel < 0:
        raise EcheancierError("TAUX_INVALIDE", "Le taux annuel ne peut pas être négatif.")
    if duree_mois <= 0:
        raise EcheancierError("DUREE_INVALIDE", "La durée doit être d'au moins 1 mois.")
    if differe_mois < 0:
        raise EcheancierError("DIFFERE_INVALIDE", "Le différé ne peut pas être négatif.")
    if differe_mois >= duree_mois:
        raise EcheancierError(
            "DIFFERE_TROP_LONG",
            f"Le différé ({differe_mois} mois) doit être strictement inférieur à la "
            f"durée totale ({duree_mois} mois) : il faut au moins un mois pour "
            f"amortir le capital.",
        )
    if mode_differe not in MODES:
        raise EcheancierError(
            "MODE_DIFFERE_INCONNU",
            f"Mode de différé « {mode_differe} » inconnu (attendu : {', '.join(MODES)}).",
        )

    taux_mensuel = taux_annuel / Decimal(100) / Decimal(12)
    n_amort = duree_mois - differe_mois

    lignes: list[dict] = []
    crd = q2(capital)

    # ── Phase 1 — différé ────────────────────────────────────────────────────
    for mois in range(1, differe_mois + 1):
        interets = q2(crd * taux_mensuel)
        if mode_differe == MODE_FRANCHISE_TOTALE:
            crd = q2(crd + interets)
            lignes.append({
                "mois": mois, "phase": PHASE_DIFFERE,
                "capital": ZERO, "interets": ZERO,
                "interets_capitalises": interets, "echeance": ZERO, "crd": crd,
            })
        else:
            lignes.append({
                "mois": mois, "phase": PHASE_DIFFERE,
                "capital": ZERO, "interets": interets,
                "interets_capitalises": ZERO, "echeance": interets, "crd": crd,
            })

    # ── Phase 2 — amortissement ──────────────────────────────────────────────
    # La tranche est calculée sur le CRD RÉEL en fin de différé : en franchise
    # totale il inclut les intérêts capitalisés.
    tranche = q2(crd / Decimal(n_amort))

    for mois in range(differe_mois + 1, duree_mois + 1):
        interets = q2(crd * taux_mensuel)
        # Dernier mois : on solde exactement, quels que soient les centimes
        # accumulés par les arrondis des tranches précédentes.
        part_capital = crd if mois == duree_mois else tranche
        crd = q2(crd - part_capital)
        lignes.append({
            "mois": mois, "phase": PHASE_AMORTISSEMENT,
            "capital": q2(part_capital), "interets": interets,
            "interets_capitalises": ZERO,
            "echeance": q2(part_capital + interets), "crd": crd,
        })

    return lignes


def totaux_echeancier(lignes: list[dict]) -> dict[str, Decimal]:
    """Grandeurs dérivées : coût du crédit, service de la dette, capital remboursé.

    `service_dette` est le dénominateur du DSCR — c'est la somme des mensualités
    réellement payées, pas la somme capital + intérêts théoriques.
    """
    somme = lambda cle: q2(sum((l[cle] for l in lignes), ZERO))  # noqa: E731
    return {
        "capital_rembourse": somme("capital"),
        "interets_payes": somme("interets"),
        "interets_capitalises": somme("interets_capitalises"),
        "service_dette": somme("echeance"),
        "crd_final": lignes[-1]["crd"] if lignes else ZERO,
        "nb_echeances": len(lignes),
    }


def serialiser_echeancier(lignes: list[dict]) -> list[dict]:
    """Vue JSON : les montants sortent en chaînes décimales, jamais en `float`.

    Un binaire flottant sérialisé perd la garantie du centime que tout le module
    s'astreint à tenir ; le front formate la chaîne, il ne recalcule rien.
    """
    return [
        {
            "mois": l["mois"],
            "phase": l["phase"],
            "capital": str(l["capital"]),
            "interets": str(l["interets"]),
            "interets_capitalises": str(l["interets_capitalises"]),
            "echeance": str(l["echeance"]),
            "crd": str(l["crd"]),
        }
        for l in lignes
    ]
