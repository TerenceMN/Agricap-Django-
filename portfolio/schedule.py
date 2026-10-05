"""
Échéancier RÉEL du prêt décaissé (taux & maturité) — le calendrier que le client
rembourse effectivement, par opposition à l'échéancier PRÉVISIONNEL du moteur
d'analyse (`credits/echeancier.py`), qui sert au scoring.

Méthode : intérêt simple sur le solde restant dû en début de période ; amortissement
constant du principal (ou remboursement in fine « bullet »). Le taux est MENSUEL
(en %) — c'est l'unité du champ `Loan.rate`, et c'est le premier écart de méthode
avec le prévisionnel, qui raisonne en taux ANNUEL (cf. « Écarts assumés » ci-dessous).

Règles non négociables (principe 4), alignées mot pour mot sur `credits/echeancier.py` :
  - `Decimal` partout, `float` nulle part — un `float` en entrée lève `TypeError`,
    il n'est jamais converti en silence ;
  - quantize explicite à 0,01 avec `ROUND_HALF_UP` sur chaque montant produit
    (`round()` de Python fait de l'arrondi BANCAIRE : sur 250 × 0,05 %/mois il
    renvoyait 0,12 là où la règle du centime donne 0,13) ;
  - intérêts de la période calculés sur le solde de DÉBUT de période, avant paiement ;
  - dernière tranche de capital ajustée au solde exact → CRD final rigoureusement nul
    ET Σ principal = capital (en `float`, 1 000 amorti sur 3 échéances ne rendait que
    999,99 : le centime résiduel n'était porté par aucune ligne) ;
  - tout montant porte sa devise : chaque ligne et les totaux la transportent.

Écarts de MÉTHODE assumés avec `credits/echeancier.py` (faits à connaître, pas des
bugs d'arrondi — ils sont remontés au fondateur, pas masqués ici) :
  1. RÉSOLU (cf. `portfolio/rates.py`) — l'unité du taux était MENSUELLE ici et
     ANNUELLE dans le prévisionnel, sans que rien ne le dise : `Loan` porte désormais
     les deux champs (`rate` mensuel, `annual_rate` annuel), maintenus cohérents à
     l'écriture, un taux mensuel implausible est refusé, et `services.schedule_for`
     transmet `annual_rate / 12` en pleine précision. Ce module reste, lui, en
     taux MENSUEL : c'est son unité d'entrée, et elle est nommée dans la signature ;
  2. RÉSOLU — le DIFFÉRÉ du prévisionnel (intérêts seuls / franchise totale) est
     désormais appliqué ici aussi. Il ne l'était pas : un dossier scoré avec 5 mois
     de différé — donc dont le DSCR a été calculé SUR ce différé — était remboursé
     dès le premier mois une fois décaissé. Le différé n'a de sens qu'en mensuel
     (l'unité du prévisionnel) : sur une autre périodicité il est REFUSÉ, jamais
     approximé ;
  3. ce module gère des périodicités (mensuel/trimestriel/annuel/in fine) que le
     prévisionnel ignore (mensuel strict) — écart assumé, pas un défaut ;
  4. RÉSOLU (cf. `services.base_amortissable`) — la base amortie était
     `amount_approved`, jamais le décaissé réel : un décaissement partiel faisait
     rembourser un capital jamais reçu. Elle est désormais le total des
     décaissements VALIDÉS dès qu'il en existe un, et la réponse d'API dit
     TOUJOURS laquelle des deux bases a servi (`principalSource`).
Hors ces points, et à paramètres équivalents (mensuel, taux mensuel = taux annuel
/ 12, même différé), les deux moteurs produisent les MÊMES chiffres — c'est
verrouillé par un test de non-régression croisé (`portfolio/tests.py`).
"""
from __future__ import annotations

import calendar
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from common.exceptions import ValidationFailed

CENT = Decimal("0.01")
ZERO = Decimal("0.00")
CENT_POURCENT = Decimal("100")
MOIS_PAR_AN = Decimal("12")

#: Périodicités de remboursement → pas en mois. « bullet » (in fine) est traité à
#: part : son pas vaut la durée totale du prêt.
FREQ_MONTHS = {"monthly": 1, "quarterly": 3, "annual": 12}

#: Modes de différé — MÊMES CODES que `credits.echeancier` (un test croisé vérifie
#: l'égalité littérale : deux nomenclatures pour un même concept, c'est le
#: principe 6 qui saute).
MODE_INTERETS_SEULS = "interets_seuls"
MODE_FRANCHISE_TOTALE = "franchise_totale"
MODES_DIFFERE = (MODE_INTERETS_SEULS, MODE_FRANCHISE_TOTALE)

PHASE_DIFFERE = "differe"
PHASE_AMORTISSEMENT = "amortissement"


class EcheancierInvalide(ValidationFailed):
    """Paramètres inexploitables — jamais d'échéancier « best effort ».

    Pendant de `credits.echeancier.EcheancierError`, mais rattaché à la hiérarchie
    métier commune : la vue relaie un 400 structuré au lieu d'un 500.
    """

    code = "ECHEANCIER_INVALIDE"


def add_months(d: date, months: int) -> date:
    """Ajoute `months` mois à une date (borne le jour à la fin de mois)."""
    m = d.month - 1 + months
    y = d.year + m // 12
    m = m % 12 + 1
    day = min(d.day, calendar.monthrange(y, m)[1])
    return date(y, m, day)


def q2(value: Decimal) -> Decimal:
    """Quantize monétaire unique du module : 0,01 / ROUND_HALF_UP.

    Le pendant exact de `credits.echeancier.q2` : les deux échéanciers d'un même
    prêt ne peuvent pas diverger d'un centime par différence de règle d'arrondi.
    """
    return Decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


def _dec(value, nom: str) -> Decimal:
    """Conversion stricte vers `Decimal`.

    Un `float` est REFUSÉ, jamais converti : accepter `0.1` (binaire = 0,1000…0055)
    rouvrirait silencieusement la porte que ce module vient de fermer. L'appelant
    doit fournir un `Decimal`, une chaîne ou un entier.
    """
    if isinstance(value, float):
        raise TypeError(
            f"{nom} reçu en float ({value!r}) : le chemin de l'échéancier est en "
            f"Decimal (principe 4). Passez un Decimal, une chaîne ou un entier."
        )
    if value in (None, ""):
        return ZERO
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, ArithmeticError) as exc:
        raise ValueError(f"{nom} n'est pas un nombre exploitable : {value!r}") from exc


def _pas_et_nombre(duration: int, frequency: str) -> tuple[int, int]:
    """Pas de la périodicité (en mois) et nombre d'échéances.

    Le plafond est calculé en arithmétique ENTIÈRE (`-(-a // b)`) : `ceil(a / b)`
    passerait par une division flottante — 8/3 = 2,666…65 — sur le chemin qui
    détermine le nombre d'échéances du client.
    """
    pas = duration if frequency == "bullet" else FREQ_MONTHS.get(frequency, 1)
    pas = pas or 1
    return pas, max(1, -(-duration // pas))


def _valider_differe(deferral_months: int, duration_months: int, mode: str,
                     frequency: str) -> int:
    """Contrôles du différé — mêmes refus que `credits.echeancier`, plus un.

    Le refus supplémentaire est la périodicité : le prévisionnel est mensuel strict,
    et « 5 mois de différé sur un prêt trimestriel » n'a pas de traduction unique
    (5 mois ne tombent sur aucune échéance). On refuse plutôt que d'inventer une
    méthode que personne n'a validée — et le message dit quoi faire.
    """
    differe = int(deferral_months or 0)
    if differe < 0:
        raise EcheancierInvalide("Le différé ne peut pas être négatif.")
    if differe == 0:
        return 0
    if mode not in MODES_DIFFERE:
        raise EcheancierInvalide(
            f"Mode de différé « {mode} » inconnu (attendu : {', '.join(MODES_DIFFERE)})."
        )
    if frequency != "monthly":
        raise EcheancierInvalide(
            f"Un différé de {differe} mois est demandé sur un échéancier "
            f"« {frequency} » : le différé n'est défini qu'en périodicité mensuelle "
            f"(c'est l'unité de l'échéancier prévisionnel qui a scoré le dossier). "
            f"Passez le prêt en mensuel, ou ramenez le différé à 0 — jamais de "
            f"conversion implicite."
        )
    if differe >= duration_months:
        raise EcheancierInvalide(
            f"Le différé ({differe} mois) doit être strictement inférieur à la durée "
            f"totale ({duration_months} mois) : il faut au moins un mois pour amortir "
            f"le capital."
        )
    return differe


def build_schedule(principal, monthly_rate_pct, duration_months: int,
                   frequency: str, start_date: date, currency: str = "USD",
                   deferral_months: int = 0,
                   deferral_mode: str = MODE_INTERETS_SEULS) -> list[dict]:
    """Tableau d'amortissement du prêt décaissé.

    Args:
        principal: capital amorti (`Decimal`, chaîne ou entier — jamais un `float`).
        monthly_rate_pct: taux MENSUEL en pourcentage (1.5 = 1,5 %/mois).
        duration_months: durée TOTALE du prêt, en mois, différé inclus.
        frequency: `monthly` | `quarterly` | `annual` | `bullet` (in fine).
        start_date: date d'effet ; toutes les échéances sont calées SUR ELLE.
        currency: devise portée par chaque ligne (aucun montant nu).
        deferral_months: différé, en mois (0 = aucun) — mensuel uniquement.
        deferral_mode: `interets_seuls` (le client paie les intérêts, le capital
            reste intact) ou `franchise_totale` (rien n'est payé, les intérêts
            sont capitalisés et grossissent le capital à amortir).

    Returns:
        Une ligne par échéance : `number`, `date`, `phase`, `principal`,
        `interest`, `interest_capitalized`, `total`, `balance`, `currency` — tous
        les montants en `Decimal` quantizés à 0,01. Liste vide si le prêt n'a pas
        de capital ou pas de durée (dossier pas encore configuré) — jamais
        d'échéancier « best effort ».
    """
    principal = q2(_dec(principal, "principal"))
    taux = _dec(monthly_rate_pct, "monthly_rate_pct")
    duration = int(duration_months or 0)
    if principal <= 0 or duration <= 0:
        return []

    differe = _valider_differe(deferral_months, duration, deferral_mode, frequency)
    pas, nombre = _pas_et_nombre(duration, frequency)
    taux_periode = taux / CENT_POURCENT * Decimal(pas)

    rows: list[dict] = []
    balance = principal

    def _echeance(i: int) -> str:
        """Date de la i-ème échéance, calée sur la DATE D'EFFET.

        Chaîner `add_months` sur la date précédente faisait dériver le calendrier :
        un prêt démarré le 31 janvier voyait sa 1re échéance au 28 février… puis
        TOUTES les suivantes au 28, parce que le 28 devenait la nouvelle ancre.
        Ancrer sur `start_date` rend 28/02, 31/03, 30/04, 31/05 — le calendrier
        que le contrat décrit.
        """
        return add_months(start_date, i * pas).isoformat()

    # ── Phase 1 — différé (mensuel uniquement, pas = 1) ──────────────────────
    for i in range(1, differe + 1):
        interest = q2(balance * taux_periode)
        if deferral_mode == MODE_FRANCHISE_TOTALE:
            # Rien n'est payé : les intérêts grossissent le capital à amortir.
            balance = q2(balance + interest)
            rows.append({
                "number": i, "date": _echeance(i), "phase": PHASE_DIFFERE,
                "principal": ZERO, "interest": ZERO,
                "interest_capitalized": interest, "total": ZERO,
                "balance": balance, "currency": currency,
            })
        else:
            rows.append({
                "number": i, "date": _echeance(i), "phase": PHASE_DIFFERE,
                "principal": ZERO, "interest": interest,
                "interest_capitalized": ZERO, "total": interest,
                "balance": balance, "currency": currency,
            })

    # ── Phase 2 — amortissement ─────────────────────────────────────────────
    # La tranche est calculée sur le solde RÉEL en fin de différé : en franchise
    # totale il inclut les intérêts capitalisés, sinon le prêt ne se solderait pas.
    n_amort = nombre - differe
    tranche = q2(balance / Decimal(n_amort))
    for i in range(differe + 1, nombre + 1):
        # Intérêts sur le solde de DÉBUT de période, avant imputation du capital.
        interest = q2(balance * taux_periode)
        if frequency == "bullet":
            principal_payment = balance if i == nombre else ZERO
        else:
            principal_payment = tranche
            if i == nombre:
                # Dernière échéance : on solde EXACTEMENT le résidu d'arrondi des
                # tranches précédentes → CRD final nul et Σ principal = capital.
                principal_payment = balance
        balance = q2(balance - principal_payment)
        rows.append({
            "number": i,
            "date": _echeance(i),
            "phase": PHASE_AMORTISSEMENT,
            "principal": q2(principal_payment),
            "interest": interest,
            "interest_capitalized": ZERO,
            "total": q2(principal_payment + interest),
            "balance": balance,
            "currency": currency,
        })
    return rows


def schedule_totals(rows: list[dict], duration_months: int, currency: str = "USD") -> dict:
    """Agrégats de l'échéancier.

    `apr` est un TAUX MOYEN ANNUEL NOMINAL — intérêts totaux rapportés au capital,
    ramenés à l'année — et NON un TAEG : il n'intègre ni frais de dossier, ni
    commissions, ni actualisation des flux. Le libellé « TAEG » de l'écran
    d'échéancier est donc trompeur (écart signalé, correction côté front hors
    périmètre de ce lot).

    `total_interest_capitalized` n'est PAS dans `total_payments` : en franchise
    totale, ces intérêts ne sont jamais payés comme tels — ils sont devenus du
    capital, et ils ressortent donc dans `total_principal`, qui dépasse alors le
    montant décaissé. C'est la même convention que `credits.echeancier`
    (`capital_rembourse` ≠ capital initial en franchise totale).
    """
    total_principal = q2(sum((r["principal"] for r in rows), ZERO))
    total_interest = q2(sum((r["interest"] for r in rows), ZERO))
    total_capitalise = q2(sum((r.get("interest_capitalized", ZERO) for r in rows), ZERO))
    annees = Decimal(int(duration_months or 12)) / MOIS_PAR_AN
    apr = ZERO
    if total_principal > 0 and annees > 0:
        apr = q2(total_interest / total_principal / annees * CENT_POURCENT)
    return {
        "total_principal": total_principal,
        "total_interest": total_interest,
        "total_interest_capitalized": total_capitalise,
        "total_payments": q2(total_principal + total_interest),
        "apr": apr,
        "final_balance": rows[-1]["balance"] if rows else ZERO,
        "currency": currency,
    }
