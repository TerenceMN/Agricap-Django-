"""
Logique métier du portefeuille de crédits : génération de référence, création d'un
dossier, configuration taux/maturité (avec audit), mouvements financiers, transitions
de statut (actions du menu), et agrégats du tableau de bord (résumé + alertes).
"""
from __future__ import annotations

import logging
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from django.db import transaction
from django.utils import timezone

from audit.services import record as audit_record
from common.exceptions import InsufficientFundsError, ValidationFailed

from . import rates
from . import repayment
from . import schedule as schedule_module
from .models import Loan, LoanConfigHistory, LoanGuarantee, LoanNote, LoanSubWallet, LoanTransaction
from .schedule import add_months, build_schedule, schedule_totals
from common.parsing import to_decimal

logger = logging.getLogger(__name__)


# --- Utilitaires de conversion tolérants -----------------------------------
def _int(value, default=0) -> int:
    """Entier tolérant (« 12 », « 12,0 ») — SANS passer par `float`.

    `int(float("12.0"))` marchait, mais faisait transiter par un binaire flottant une
    durée qui détermine le nombre d'échéances du client (principe 4).
    """
    try:
        return int(Decimal(str(value).replace(",", ".").replace(" ", "")))
    except (TypeError, ValueError, InvalidOperation):
        return default


def _date(value):
    if not value:
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


# Libellés / codes acceptés → code canonique de statut.
_STATUS_MAP = {
    "en traitement": Loan.Status.EN_TRAITEMENT, "en_traitement": Loan.Status.EN_TRAITEMENT,
    "approuvé": Loan.Status.APPROUVE, "approuve": Loan.Status.APPROUVE, "approuvé": Loan.Status.APPROUVE,
    "en cours": Loan.Status.EN_COURS, "en_cours": Loan.Status.EN_COURS, "active": Loan.Status.EN_COURS,
    "défaut": Loan.Status.DEFAUT, "defaut": Loan.Status.DEFAUT,
    "clôturé": Loan.Status.CLOTURE, "cloture": Loan.Status.CLOTURE, "clôturé": Loan.Status.CLOTURE,
    "rejeté": Loan.Status.REJETE, "rejete": Loan.Status.REJETE, "cancelled": Loan.Status.REJETE,
    "suspendu": Loan.Status.SUSPENDU, "suspended": Loan.Status.SUSPENDU,
    "bloqué": Loan.Status.BLOQUE, "bloque": Loan.Status.BLOQUE, "blocked": Loan.Status.BLOQUE,
}


def status_code(value, default=Loan.Status.EN_TRAITEMENT) -> str:
    if not value:
        return default
    v = str(value).strip().lower()
    for code in Loan.Status.values:
        if v == code.lower():
            return code
    return _STATUS_MAP.get(v, default)


def generate_reference() -> str:
    """Référence lisible CRD-AAAA-NNN, séquentielle par année (saisie manuelle)."""
    year = timezone.localdate().year
    prefix = f"CRD-{year}-"
    n = Loan.objects.filter(reference__startswith=prefix).count() + 1
    while Loan.objects.filter(reference=f"{prefix}{n:03d}").exists():
        n += 1
    return f"{prefix}{n:03d}"


# --- Pont avec le moteur d'analyse (credit) --------------------------------
# La décision d'analyse (§8.3) initialise le statut de gestion.
_APP_STATUS_TO_LOAN = {
    "draft":               Loan.Status.EN_TRAITEMENT,
    "submitted":           Loan.Status.EN_TRAITEMENT,
    "in_analysis":         Loan.Status.EN_TRAITEMENT,
    "adjourned":           Loan.Status.EN_TRAITEMENT,
    "approved":            Loan.Status.APPROUVE,
    "pending_disbursement": Loan.Status.APPROUVE,
    "active":              Loan.Status.EN_COURS,
    "rejected":            Loan.Status.REJETE,
    "closed":              Loan.Status.CLOTURE,
}


def derniere_analyse(app):
    """Dernière `AnalyseCredit` du dossier d'instruction, ou `None`.

    Import différé et défensif : le portefeuille reste autonome (un dossier peut
    être saisi manuellement, sans analyse).
    """
    if app is None:
        return None
    try:
        from credits.scoring import derniere_analyse as _derniere

        return _derniere(app)
    except Exception:  # noqa: BLE001 — absence d'analyse = cas nominal, pas une erreur
        return None


def taux_annuel_du_dossier(loan: Loan):
    """Taux ANNUEL sur lequel le dossier a été SCORÉ, ou `None`.

    C'est la référence qui permet de reconnaître un report du taux annuel dans le
    champ mensuel (cf. `portfolio.rates.valider_taux_mensuel`).
    """
    if not loan or not loan.application_id:
        return None
    analyse = derniere_analyse(loan.application)
    return getattr(analyse, "taux_annuel", None) if analyse else None


def _taux_de_l_analyse(app) -> tuple[Decimal | None, str, str]:
    """(taux ANNUEL à appliquer, provenance, avertissement) depuis l'analyse.

    Le taux SERVI est `taux_propose` (grille de tarification unique) quand l'analyse
    en porte un ; à défaut, `taux_annuel`, celui avec lequel l'échéancier et le DSCR
    ont été calculés. Quand les deux diffèrent, le dossier a été scoré à un prix et
    facturé à un autre : le fait est journalisé, jamais absorbé.
    """
    analyse = derniere_analyse(app)
    if analyse is None:
        return None, "", ""
    scoré = getattr(analyse, "taux_annuel", None)
    servi = getattr(analyse, "taux_propose", None)
    if servi is not None:
        avertissement = ""
        if scoré is not None and Decimal(servi) != Decimal(scoré):
            avertissement = (
                f"Taux servi {servi} %/an ≠ taux d'analyse {scoré} %/an : "
                f"le DSCR du dossier a été calculé à {scoré} %/an."
            )
        return Decimal(servi), f"analyse #{analyse.pk} (taux proposé)", avertissement
    if scoré is not None:
        return Decimal(scoré), f"analyse #{analyse.pk} (taux d'analyse)", ""
    return None, "", ""


def _differe_de_l_analyse(analyse, duree_mois: int) -> dict:
    """Différé SCORÉ du dossier, repris tel quel — ou refus explicite.

    Le DSCR du dossier a été calculé sur ce différé : ne pas le reprendre revient à
    faire rembourser dès le premier mois un client dont la capacité de remboursement
    a été mesurée après récolte. Un différé incohérent avec la durée retenue côté
    gestion n'est pas rogné en silence : il remonte en erreur.
    """
    if analyse is None:
        return {}
    mois = int(getattr(analyse, "differe_mois", 0) or 0)
    if mois <= 0:
        return {}
    mode = getattr(analyse, "mode_differe", None) or schedule_module.MODE_INTERETS_SEULS
    schedule_module._valider_differe(mois, int(duree_mois or 0), mode, "monthly")
    return {"deferral_months": mois, "deferral_mode": mode}


@transaction.atomic
def create_from_application(app, *, by: str = ""):
    """
    Crée (ou resynchronise) un dossier de GESTION à partir d'une CreditApplication
    (nouveau module credits). Les montants et le score viennent directement des champs
    du modèle ; les champs de gestion déjà édités (gestionnaire, dates) sont préservés.

    Le TAUX est recopié de l'analyse, en ANNUEL et sans conversion manuelle : c'est
    le seul moyen de garantir que le prêt facture ce qui a été scoré. Il ne restait
    auparavant aucune trace du taux dans le dossier de gestion (`rate` = 0 jusqu'à
    saisie), ce qui obligeait le gestionnaire à le retaper — depuis un écran qui
    l'affiche en ANNUEL, dans un champ qui l'attend en MENSUEL.
    """
    sr = app.score_result or {}
    score = sr.get("score") or 0
    schedule = sr.get("scheduleDraft") or []
    duree = len(schedule) if schedule else 12

    analyse = derniere_analyse(app)
    if analyse is not None and getattr(analyse, "duree_mois", None):
        duree = int(analyse.duree_mois)
    taux_annuel, provenance, avertissement = _taux_de_l_analyse(app)
    differe = _differe_de_l_analyse(analyse, duree)

    snapshot = dict(
        operator=getattr(app.client, "full_name", None) or "—",
        category=getattr(app.value_chain, "label", None) or "",
        amount_requested=app.amount_requested or Decimal("0"),
        amount_approved=app.amount_approved or Decimal("0"),
        score=int(score or 0),
        guarantee=app.guarantee_type or "",
        source="Credits",
    )

    existing = Loan.objects.filter(application=app).first()
    if existing:
        for key, val in snapshot.items():
            setattr(existing, key, val)
        # Le taux n'écrase une saisie de gestion que s'il n'y en a jamais eu :
        # une resynchronisation ne re-tarife pas un prêt déjà configuré.
        if taux_annuel is not None and not existing.annual_rate:
            _appliquer_taux_analyse(existing, taux_annuel, provenance, avertissement, by=by)
        if differe and not existing.deferral_months:
            for champ, valeur in differe.items():
                setattr(existing, champ, valeur)
        existing.save()
        return existing

    loan = Loan(
        reference=app.code, application=app,
        duration_months=duree or 12,
        status=_APP_STATUS_TO_LOAN.get(app.status, Loan.Status.EN_TRAITEMENT),
        created_by=by, **snapshot, **differe,
    )
    if taux_annuel is not None:
        loan.annual_rate = taux_annuel
    loan.save()
    if differe:
        LoanConfigHistory.objects.create(
            loan=loan, action="Différé repris de l'analyse", user=by or "Système",
            details=f"{differe['deferral_months']} mois de différé "
                    f"({differe['deferral_mode']}) — le DSCR du dossier a été "
                    f"calculé sur ce différé.",
        )
    if taux_annuel is not None:
        _journaliser_taux(loan, taux_annuel, provenance, avertissement, by=by)
    else:
        LoanConfigHistory.objects.create(
            loan=loan, action="Taux non repris", user=by or "Système",
            details="Aucune analyse ne porte de taux : le taux du prêt reste à "
                    "configurer explicitement (aucune valeur devinée).",
        )
    return loan


def _appliquer_taux_analyse(loan: Loan, taux_annuel: Decimal, provenance: str,
                            avertissement: str, *, by: str) -> None:
    loan.annual_rate = taux_annuel
    _journaliser_taux(loan, taux_annuel, provenance, avertissement, by=by)


def _journaliser_taux(loan: Loan, taux_annuel: Decimal, provenance: str,
                      avertissement: str, *, by: str) -> None:
    """Trace la provenance du taux : un chiffre financier sans auteur n'existe pas."""
    details = (f"Taux repris de l'{provenance} : {taux_annuel} %/an "
               f"= {rates.mensuel_stocke(taux_annuel)} %/mois")
    if avertissement:
        details = f"{details}. {avertissement}"
    LoanConfigHistory.objects.create(
        loan=loan, action="Taux repris de l'analyse", user=by or "Système",
        details=details[:255],
    )


def sync_from_applications(*, by: str = "") -> int:
    """Garantit qu'un dossier de gestion existe pour CHAQUE demande analysée."""
    from credits.models import CreditApplication
    linked = set(Loan.objects.exclude(application__isnull=True).values_list("application_id", flat=True))
    created = 0
    for app in CreditApplication.objects.all():
        if app.id not in linked:
            create_from_application(app, by=by)
            created += 1
    return created


def _recompute_due_date(loan: Loan) -> None:
    if loan.start_date and loan.duration_months:
        loan.due_date = add_months(loan.start_date, int(loan.duration_months))


def _lire_taux(data: dict, loan: Loan | None = None) -> dict:
    """Extrait le taux d'un payload d'API, en NOMMANT son unité.

    Deux clés distinctes, jamais interchangeables :
      - `annualRate` / `annual_rate` : taux ANNUEL (celui des écrans d'analyse) ;
      - `rate` : taux MENSUEL (l'unité historique du champ).
    Le taux mensuel est confronté au taux annuel du dossier scoré quand il existe :
    une saisie égale à ce taux annuel est le report fautif, et elle est refusée.
    """
    if data.get("annualRate") not in (None, "") or data.get("annual_rate") not in (None, ""):
        annuel = data.get("annualRate") if data.get("annualRate") not in (None, "") \
            else data.get("annual_rate")
        return {"annual_rate": rates.valider_taux_annuel(annuel)}
    if "rate" in data and data.get("rate") not in (None, ""):
        mensuel = rates.valider_taux_mensuel(
            data.get("rate"), taux_annuel_dossier=taux_annuel_du_dossier(loan))
        return {"rate": mensuel, "annual_rate": rates.annuel_depuis_mensuel(mensuel)}
    return {}


def _lire_differe(data: dict, loan: Loan | None = None) -> dict:
    """Extrait le différé d'un payload et le VALIDE contre la durée et la périodicité.

    Le contrôle a lieu à l'écriture, pas au calcul : un dossier ne doit pas pouvoir
    être enregistré dans un état dont l'échéancier lèverait une erreur à l'affichage.
    """
    fourni = {}
    if data.get("deferralMonths") is not None or data.get("deferral_months") is not None:
        brut = data.get("deferralMonths")
        if brut is None:
            brut = data.get("deferral_months")
        fourni["deferral_months"] = max(0, _int(brut, 0))
    mode = data.get("deferralMode") or data.get("deferral_mode")
    if mode:
        if mode not in schedule_module.MODES_DIFFERE:
            raise ValidationFailed(
                f"Mode de différé « {mode} » inconnu "
                f"(attendu : {', '.join(schedule_module.MODES_DIFFERE)})."
            )
        fourni["deferral_mode"] = mode
    if not fourni:
        return {}

    # Cohérence évaluée sur l'état RÉSULTANT (payload + valeurs déjà en base).
    differe = fourni.get("deferral_months",
                         getattr(loan, "deferral_months", 0) if loan else 0)
    duree = _int(data.get("duration") or data.get("duration_months"),
                 getattr(loan, "duration_months", 0) if loan else 0)
    frequence = (data.get("frequency")
                 or (getattr(loan, "frequency", "monthly") if loan else "monthly"))
    schedule_module._valider_differe(
        differe, duree, fourni.get("deferral_mode",
                                   getattr(loan, "deferral_mode", None) if loan
                                   else schedule_module.MODE_INTERETS_SEULS),
        frequence)
    return fourni


@transaction.atomic
def create_loan(data: dict, *, by: str = "") -> Loan:
    taux = _lire_taux(data)
    differe = _lire_differe(data)
    loan = Loan(
        reference=generate_reference(),
        date=_date(data.get("date")) or timezone.localdate(),
        operator=(data.get("operator") or "").strip(),
        category=(data.get("category") or data.get("type") or "").strip(),
        amount_requested=to_decimal(data.get("amountRequested") or data.get("amount_requested")),
        amount_approved=to_decimal(data.get("amountApproved") or data.get("amount_approved")),
        currency=(data.get("currency") or "USD").upper()[:3],
        duration_months=_int(data.get("duration") or data.get("duration_months"), 12),
        frequency=(data.get("frequency") or "monthly"),
        start_date=_date(data.get("startDate") or data.get("start_date")),
        manager=(data.get("manager") or "").strip(),
        investor=(data.get("investor") or "").strip(),
        source=(data.get("source") or "App").strip(),
        status=status_code(data.get("status"), Loan.Status.EN_TRAITEMENT),
        score=_int(data.get("score"), 0),
        guarantee=(data.get("guarantee") or "").strip(),
        borrower_sub=(data.get("borrowerSub") or "").strip(),
        created_by=by,
        **taux, **differe,
    )
    _recompute_due_date(loan)
    loan.save()
    return loan


# --- Espace client (Credits.jsx) : auto-service, sous-portefeuilles par module ----------
@transaction.atomic
def submit_client_application(data: dict, *, by: str) -> Loan:
    """Le client soumet lui-même sa demande (formulaire multi-étapes : demande initiale +
    simulateur de modules + garanties). Devient un dossier de gestion `Loan` standard
    (visible aussi côté admin), avec `borrower_sub=by` pour le scoper à l'espace client."""
    loan = Loan.objects.create(
        reference=generate_reference(),
        operator=(data.get("demandeur") or "").strip(),
        category=(data.get("culture") or "").strip(),
        amount_requested=to_decimal(data.get("montant")),
        currency=(data.get("currency") or "USD").upper()[:3],
        status=Loan.Status.EN_TRAITEMENT,
        score=_int(data.get("score"), 0),
        source="App",
        borrower_sub=by,
        created_by=by,
    )
    modules = data.get("modules") or {}
    for key, mod in modules.items():
        if not mod.get("active"):
            continue
        amount = to_decimal(mod.get("cost")) * to_decimal(mod.get("financing"), "100") / Decimal("100")
        LoanSubWallet.objects.create(
            loan=loan, module_key=key, label=mod.get("label") or key,
            allocated_amount=amount, balance=amount,
        )
    loan.amount_approved = sum((sw.allocated_amount for sw in loan.subwallets.all()), Decimal("0"))
    loan.save(update_fields=["amount_approved"])

    for g in data.get("guarantees") or []:
        LoanGuarantee.objects.create(
            loan=loan, type=g.get("type", ""), label=g.get("label", ""),
            description=g.get("description", ""), value=to_decimal(g.get("value")) if g.get("value") else None,
        )
    loan.guarantee = ", ".join(g.get("label", "") for g in data.get("guarantees") or [] if g.get("label"))
    loan.save(update_fields=["guarantee"])

    audit_record(actor=by, action="portfolio.client_application.submit", entity_type="Loan",
                 entity_id=loan.reference, details={"amount": str(loan.amount_approved)})
    return loan


def client_loans(sub: str) -> list:
    return list(Loan.objects.filter(borrower_sub=sub))


def client_loan_detail(loan: Loan) -> dict:
    from . import serializers
    return {
        **serializers.loan_row(loan),
        "subwallets": [serializers.subwallet_row(sw) for sw in loan.subwallets.all()],
        "guarantees": [serializers.guarantee_row(g) for g in loan.guarantee_items.all()],
        "transactions": serializers.transactions_with_balance(loan),
        **schedule_for(loan),
    }


@transaction.atomic
def pay_from_subwallet(loan: Loan, subwallet_id: int, amount, beneficiary: str, description: str, *,
                        by: str) -> LoanTransaction:
    subwallet = loan.subwallets.filter(pk=subwallet_id).first()
    if not subwallet:
        raise ValidationFailed("Sous-portefeuille introuvable.")
    amt = to_decimal(amount)
    if amt <= 0:
        raise ValidationFailed("Montant invalide.")
    if amt > subwallet.balance:
        raise InsufficientFundsError("Solde du sous-portefeuille insuffisant.")
    subwallet.balance -= amt
    subwallet.save(update_fields=["balance"])
    tx = LoanTransaction.objects.create(
        loan=loan, subwallet=subwallet, kind=LoanTransaction.Kind.DISBURSEMENT,
        label=description or f"Paiement — {subwallet.label}", amount=amt, currency=loan.currency,
        payment_method="Virement interne", reference=beneficiary, verified_by=by,
    )
    if loan.status in (Loan.Status.APPROUVE, Loan.Status.EN_TRAITEMENT):
        loan.status = Loan.Status.EN_COURS
        loan.save(update_fields=["status"])
    audit_record(actor=by, action="portfolio.subwallet.pay", entity_type="LoanTransaction",
                 entity_id=str(tx.pk), details={"subwallet": subwallet.module_key, "amount": str(amt),
                                                 "beneficiary": beneficiary})
    return tx


@transaction.atomic
def rebalance_subwallets(loan: Loan, from_id: int, to_id: int, amount, *, by: str) -> None:
    if from_id == to_id:
        raise ValidationFailed("Les deux sous-portefeuilles doivent être différents.")
    # Verrouillage ordonné par pk (comme `caisses.transfer_funds`) — évite un deadlock si
    # deux réajustements concurrents portent sur la même paire dans l'ordre inverse.
    ids = sorted([from_id, to_id])
    locked = {sw.pk: sw for sw in loan.subwallets.select_for_update().filter(pk__in=ids)}
    from_sw, to_sw = locked.get(from_id), locked.get(to_id)
    if not from_sw or not to_sw:
        raise ValidationFailed("Sous-portefeuille introuvable.")
    amt = to_decimal(amount)
    if amt <= 0:
        raise ValidationFailed("Montant invalide.")
    if amt > from_sw.balance:
        raise InsufficientFundsError("Solde du sous-portefeuille source insuffisant.")
    from_sw.balance -= amt
    from_sw.allocated_amount -= amt
    to_sw.balance += amt
    to_sw.allocated_amount += amt
    from_sw.save(update_fields=["balance", "allocated_amount"])
    to_sw.save(update_fields=["balance", "allocated_amount"])
    LoanConfigHistory.objects.create(
        loan=loan, action="Réajustement entre modules", user=by or "Client",
        details=f"{from_sw.label} → {to_sw.label} : {amt} {loan.currency}",
    )
    audit_record(actor=by, action="portfolio.subwallet.rebalance", entity_type="Loan", entity_id=loan.reference,
                 details={"from": from_sw.module_key, "to": to_sw.module_key, "amount": str(amt)})


@transaction.atomic
def update_loan(loan: Loan, data: dict) -> Loan:
    """Mise à jour partielle générique d'un dossier."""
    fields = {
        "operator": "operator", "category": "category", "manager": "manager",
        "investor": "investor", "source": "source", "guarantee": "guarantee",
    }
    for key, attr in fields.items():
        if key in data or attr in data:
            setattr(loan, attr, (data.get(key) if key in data else data.get(attr)) or "")
    if "amountApproved" in data or "amount_approved" in data:
        loan.amount_approved = to_decimal(data.get("amountApproved") or data.get("amount_approved"))
    if "score" in data:
        loan.score = _int(data.get("score"), loan.score)
    for champ, valeur in _lire_taux(data, loan).items():
        setattr(loan, champ, valeur)
    if "status" in data:
        loan.status = status_code(data.get("status"), loan.status)
    if "startDate" in data or "start_date" in data:
        loan.start_date = _date(data.get("startDate") or data.get("start_date"))
    if "duration" in data or "duration_months" in data:
        loan.duration_months = _int(data.get("duration") or data.get("duration_months"), loan.duration_months)
    _recompute_due_date(loan)
    loan.save()
    return loan


@transaction.atomic
def apply_config(loan: Loan, data: dict, *, by: str = "", action: str = "Modification") -> dict:
    """
    Applique taux/durée/fréquence/statut/date d'effet + enregistre une entrée d'audit.
    Renvoie l'échéancier recalculé.

    Le taux passe par `_lire_taux` : c'est l'écran « Taux & maturité », donc le point
    d'entrée exact où la confusion mensuel/annuel se commettait.
    """
    for champ, valeur in _lire_taux(data, loan).items():
        setattr(loan, champ, valeur)
    for champ, valeur in _lire_differe(data, loan).items():
        setattr(loan, champ, valeur)
    loan.duration_months = _int(data.get("duration") or data.get("duration_months"), loan.duration_months)
    if data.get("frequency"):
        loan.frequency = data["frequency"]
    if data.get("startDate") or data.get("start_date"):
        loan.start_date = _date(data.get("startDate") or data.get("start_date"))
    if data.get("status"):
        loan.status = status_code(data.get("status"), loan.status)
    _recompute_due_date(loan)
    loan.save()

    details = (f"Taux: {loan.rate}%/mois (= {loan.annual_rate}%/an), "
               f"Durée: {loan.duration_months} mois, Statut: {loan.get_status_display()}")
    LoanConfigHistory.objects.create(loan=loan, action=action, user=by or "Système", details=details)
    return schedule_for(loan)


#: Bases d'amortissement possibles — la réponse dit TOUJOURS laquelle a servi.
BASE_DECAISSE = "decaisse_valide"
BASE_APPROUVE = "montant_approuve"


def base_amortissable(loan: Loan) -> dict:
    """Capital à amortir, sa provenance, et les faits qui la nuancent.

    Le prêt s'amortissait sur `amount_approved`, jamais sur ce qui était réellement
    sorti : un décaissement partiel produisait un échéancier sur un capital que le
    client n'avait pas reçu — il remboursait de l'argent jamais versé.

    Règle retenue :
      - dès qu'un décaissement VALIDÉ existe, c'est LUI qui s'amortit ;
      - tant que rien n'est sorti, l'échéancier reste PRÉVISIONNEL sur le montant
        approuvé, et il le dit.
    Aucune des deux bases n'est servie sans son étiquette : un écran ne peut pas
    présenter un prévisionnel comme un contrat.
    """
    decaisse = loan.disbursed_validated
    approuve = loan.amount_approved or loan.amount_requested or Decimal("0")
    anomalies: list[str] = []

    if decaisse > 0:
        base, source = decaisse, BASE_DECAISSE
        if approuve and decaisse != approuve:
            sens = "partiel" if decaisse < approuve else "supérieur à l'approbation"
            anomalies.append(
                f"Décaissement {sens} : {decaisse} {loan.currency} sortis pour "
                f"{approuve} approuvés. L'échéancier amortit le décaissé — l'écart "
                f"de {abs(approuve - decaisse)} {loan.currency} est à instruire."
            )
        tranches = [t for t in loan.transactions.all()
                    if t.kind == LoanTransaction.Kind.DISBURSEMENT
                    and t.status == LoanTransaction.Status.VALIDE and t.amount]
        jours = {t.date for t in tranches if t.date}
        if len(jours) > 1:
            anomalies.append(
                f"{len(tranches)} décaissements étalés sur {len(jours)} dates : "
                f"l'échéancier amortit leur TOTAL depuis la date d'effet du prêt. "
                f"Un amortissement tranche par tranche (intérêts courus depuis la "
                f"date de chaque versement) est une décision de méthode qui n'a pas "
                f"été arbitrée — signalé, jamais tranché en silence."
            )
    else:
        base, source = approuve, BASE_APPROUVE

    return {"principal": base, "source": source, "anomalies": anomalies,
            "approuve": approuve, "decaisse": decaisse}


def date_d_effet(loan: Loan):
    """Date d'effet de l'échéancier — jamais antérieure à la sortie des fonds.

    À défaut de `start_date` saisie, on prend la date du PREMIER décaissement validé
    plutôt que `loan.date` (la date de la DEMANDE) : faire courir les intérêts depuis
    une demande, c'est facturer un argent qui n'était pas encore sorti.
    """
    return (loan.start_date or loan.first_disbursement_date or loan.date
            or timezone.localdate())


def schedule_for(loan: Loan) -> dict:
    """Échéancier réel du dossier — 100 % `Decimal` de bout en bout (principe 4).

    Les champs du modèle sont déjà des `DecimalField` : on les transmet TELS QUELS,
    sans passer par `float()`. `build_schedule` refuse d'ailleurs un `float`, ce qui
    verrouille le chemin par une erreur bruyante plutôt que par un centime silencieux.

    Le taux transmis est `monthly_rate_pct` (= taux annuel ÷ 12 en pleine précision),
    pas la colonne `rate` arrondie : c'est la condition pour que le calendrier PAYÉ
    tombe au centime sur le calendrier SCORÉ.
    """
    base = base_amortissable(loan)
    duree = int(loan.duration_months or 0)
    start = date_d_effet(loan)
    rows = build_schedule(base["principal"], loan.monthly_rate_pct, duree,
                          loan.frequency, start, loan.currency,
                          deferral_months=loan.deferral_months,
                          deferral_mode=loan.deferral_mode)
    return {"schedule": rows, "totals": schedule_totals(rows, duree, loan.currency),
            "currency": loan.currency,
            "deferralMonths": loan.deferral_months,
            "deferralMode": loan.deferral_mode,
            # Le capital amorti et SA PROVENANCE : sans elles, l'écran ne peut pas
            # distinguer un échéancier contractuel d'une projection.
            "principal": base["principal"],
            "principalSource": base["source"],
            "startDate": start.isoformat() if start else "",
            "anomalies": base["anomalies"]}


@transaction.atomic
def add_transaction(loan: Loan, data: dict, *, by: str = "") -> LoanTransaction:
    kind = (data.get("kind") or "").upper()
    if kind not in LoanTransaction.Kind.values:
        kind = LoanTransaction.Kind.OTHER
    tx = LoanTransaction.objects.create(
        loan=loan,
        date=_date(data.get("date")) or timezone.localdate(),
        kind=kind,
        label=(data.get("label") or data.get("type") or "").strip(),
        amount=None if data.get("amount") in (None, "") else to_decimal(data.get("amount")),
        currency=(data.get("currency") or loan.currency).upper()[:3],
        original_amount=None if data.get("originalAmount") in (None, "") else to_decimal(data.get("originalAmount")),
        original_currency=(data.get("originalCurrency") or "").upper()[:3],
        payment_method=(data.get("paymentMethod") or "").strip(),
        reference=(data.get("ref") or data.get("reference") or "").strip(),
        status=data.get("statusCode") or _txn_status(data.get("status")),
        verified_by=(data.get("verifiedBy") or by or "").strip(),
    )
    # Un premier décaissement fait passer le dossier « en cours ».
    if kind == LoanTransaction.Kind.DISBURSEMENT and loan.status in (
        Loan.Status.APPROUVE, Loan.Status.EN_TRAITEMENT
    ):
        loan.status = Loan.Status.EN_COURS
        loan.save(update_fields=["status"])

    # La file comptable est alimentée DANS la transaction du mouvement : un
    # encaissement qui existerait sans son événement serait un franc entré sans
    # trace au grand livre — la divergence la plus dangereuse, parce qu'elle est
    # silencieuse.
    emettre_evenements_comptables(loan, tx, by=by)
    return tx


def _deja_regle_avant(loan: Loan, tx: LoanTransaction) -> Decimal:
    """Total des remboursements VALIDÉS antérieurs à `tx`.

    C'est lui qui décale l'imputation : le deuxième versement d'un client ne
    rembourse pas les mêmes intérêts que le premier. Les mouvements « en attente »
    sont exclus — on ne ventile que de l'argent réellement encaissé.
    """
    total = Decimal("0.00")
    for autre in loan.transactions.all():
        if (autre.pk != tx.pk
                and autre.kind == LoanTransaction.Kind.REPAYMENT
                and autre.status == LoanTransaction.Status.VALIDE
                and autre.amount):
            total += -autre.amount
    return total


def emettre_evenements_comptables(loan: Loan, tx: LoanTransaction, *, by: str = "") -> list:
    """Publie les événements B2 / B3 / B4 correspondant à un mouvement encaissé.

    - REMBOURSEMENT → B2 (capital) + B3 (intérêts), ventilés par imputation sur
      l'échéancier réel. JAMAIS un total : les deux natures ne mouvementent ni les
      mêmes comptes ni les mêmes classes, et la répartition ne se devine pas.
    - FRAIS → B4 (commission), qui est déjà d'une seule nature.

    Un mouvement « en attente » n'émet rien : l'argent n'est pas encore entré.
    Une ventilation indisponible n'émet rien non plus — elle laisse une trace dans
    l'historique du dossier, et la file comptable attend. Un événement en attente
    vaut mieux qu'une écriture fausse.
    """
    if tx.status != LoanTransaction.Status.VALIDE or not tx.amount:
        return []

    try:
        from credits.events import emettre_commission, emettre_echeance
    except Exception:  # noqa: BLE001 — module d'événements absent : rien n'est inventé
        logger.warning(
            "Événements comptables indisponibles (import `credits.events`) : "
            "le mouvement %s du prêt %s n'alimente pas la file.", tx.pk, loan.reference)
        return []

    commun = dict(
        currency=tx.currency or loan.currency,
        application=loan.application if loan.application_id else None,
        loan_id=loan.pk, loan_reference=loan.reference,
        occurred_at=_occurred_at(tx), actor_sub=by or tx.verified_by or "",
    )

    if tx.kind == LoanTransaction.Kind.FEE:
        evenement = emettre_commission(
            amount=abs(tx.amount), reference=f"FEE-{loan.reference}-{tx.pk}",
            libelle=tx.label or "", **commun)
        return [evenement] if evenement is not None else []

    if tx.kind != LoanTransaction.Kind.REPAYMENT:
        return []

    montant = -tx.amount           # les remboursements sont stockés NÉGATIFS
    ventilation = repayment.ventiler_remboursement(
        loan, montant=montant, deja_regle=_deja_regle_avant(loan, tx))

    if not ventilation["disponible"]:
        LoanConfigHistory.objects.create(
            loan=loan, action="Remboursement non ventilé — aucun événement comptable",
            user=by or "Système", details=ventilation["motif"][:255],
        )
        logger.warning("Prêt %s, mouvement %s : %s",
                       loan.reference, tx.pk, ventilation["motif"])
        return []

    if ventilation["motif"]:
        # Ventilation possible mais partielle (versement supérieur au solde) :
        # les jambes imputables sont émises, le reliquat est tracé et n'est
        # attribué à aucun compte.
        LoanConfigHistory.objects.create(
            loan=loan, action="Remboursement partiellement imputé",
            user=by or "Système", details=ventilation["motif"][:255],
        )

    return emettre_echeance(
        capital=ventilation["capital"], interets=ventilation["interets"],
        reference=f"RBT-{loan.reference}-{tx.pk}",
        echeances=[{**l, "capital": str(l["capital"]), "interets": str(l["interets"])}
                   for l in ventilation["lignes_imputees"]],
        **commun,
    )


def _occurred_at(tx: LoanTransaction):
    """Horodatage de l'événement : la DATE du mouvement, pas celle de la saisie.

    Une régularisation saisie en septembre pour un encaissement d'août doit
    s'écrire sur août, sinon l'exercice comptable ne reflète plus les faits.

    MIDI local, et non minuit : `accounting.consommation` date sa pièce avec
    `timezone.localdate(occurred_at)`, qui reconvertit correctement — mais minuit
    local vaut 23 h la VEILLE en UTC (Kinshasa = UTC+1), et toute lecture qui
    ferait `.date()` sur la valeur stockée daterait la pièce du jour précédent.
    Midi ne peut basculer de jour sous aucun décalage horaire, et évite au
    passage les minuits inexistants des fuseaux à heure d'été.
    """
    if not tx.date:
        return None
    valeur = datetime.combine(tx.date, time(12, 0))
    return timezone.make_aware(valeur) if timezone.is_naive(valeur) else valeur


def _txn_status(value, default=LoanTransaction.Status.VALIDE) -> str:
    if not value:
        return default
    v = str(value).strip().lower()
    mapping = {"validé": "VALIDE", "valide": "VALIDE", "en attente": "EN_ATTENTE",
               "en_attente": "EN_ATTENTE", "non applicable": "NON_APPLICABLE"}
    for code in LoanTransaction.Status.values:
        if v == code.lower():
            return code
    return mapping.get(v, default)


# --- Actions du menu (réaffecter, prolonger, pause, clôturer, annuler…) -----
@transaction.atomic
def run_action(loan: Loan, action: str, data: dict, *, by: str = "") -> dict:
    action = (action or "").lower()
    label = None

    if action == "reassign":
        loan.manager = (data.get("manager") or "").strip() or loan.manager
        loan.save(update_fields=["manager"])
        label = f"Réaffectation gestionnaire → {loan.manager}"

    elif action == "extend":
        months = _int(data.get("months"), 0)
        if months:
            loan.duration_months += months
        if data.get("dueDate") or data.get("due_date"):
            loan.due_date = _date(data.get("dueDate") or data.get("due_date"))
        else:
            _recompute_due_date(loan)
        loan.save(update_fields=["duration_months", "due_date"])
        label = f"Prolongation échéance (+{months} mois)"

    elif action in ("pause", "suspend"):
        loan.status = Loan.Status.SUSPENDU
        loan.save(update_fields=["status"])
        label = "Mise en pause / suspension"

    elif action == "block":
        loan.status = Loan.Status.BLOQUE
        loan.rate = Decimal("0")
        loan.save(update_fields=["status", "rate"])
        label = "Blocage (taux 0%)"

    elif action == "resume":
        loan.status = Loan.Status.EN_COURS
        loan.save(update_fields=["status"])
        label = "Réactivation"

    elif action == "close":
        loan.status = Loan.Status.CLOTURE
        loan.save(update_fields=["status"])
        label = "Clôture du dossier"

    elif action in ("cancel", "reject"):
        loan.status = Loan.Status.REJETE
        loan.save(update_fields=["status"])
        label = "Annulation / rejet"

    elif action == "default":
        loan.status = Loan.Status.DEFAUT
        loan.save(update_fields=["status"])
        label = "Passage en défaut"

    elif action == "approve":
        loan.status = Loan.Status.APPROUVE
        if data.get("amountApproved") or data.get("amount_approved"):
            loan.amount_approved = to_decimal(data.get("amountApproved") or data.get("amount_approved"))
        loan.save(update_fields=["status", "amount_approved"])
        label = "Approbation du dossier"

    elif action == "reminder":
        add_transaction(loan, {
            "kind": "REMINDER", "label": "Relance envoyée",
            "paymentMethod": data.get("channel") or "Notification",
            "status": "Non applicable", "amount": None,
        }, by=by)
        label = "Relance / notification"

    elif action == "note":
        text = (data.get("text") or "").strip()
        if text:
            LoanNote.objects.create(loan=loan, author=by or "Admin", text=text)
        label = "Note ajoutée"

    elif action == "disburse":
        add_transaction(loan, {
            "kind": "DISBURSEMENT",
            "label": data.get("label") or "Décaissement",
            "amount": data.get("amount"),
            "currency": data.get("currency") or loan.currency,
            "paymentMethod": data.get("paymentMethod") or "Virement bancaire",
            "ref": data.get("ref"),
            "status": data.get("status") or "Validé",
            "verifiedBy": by,
        }, by=by)
        label = "Décaissement enregistré"

    else:
        return {"ok": False, "detail": f"Action inconnue : {action}"}

    LoanConfigHistory.objects.create(loan=loan, action=label, user=by or "Admin", details=data.get("note") or "")
    return {"ok": True, "detail": label}


# --- Tableau de bord --------------------------------------------------------
def summary() -> list[dict]:
    """Cartes de synthèse (agrégats du portefeuille)."""
    qs = Loan.objects.all()
    total = qs.count()
    approved = qs.filter(status__in=[Loan.Status.APPROUVE, Loan.Status.EN_COURS])
    # Agrégats monétaires en `Decimal` : un cumul de portefeuille en `float` dérive
    # d'autant plus qu'il y a de dossiers (principe 4).
    disbursed_total = sum((l.disbursed for l in qs), Decimal("0"))
    outstanding_total = sum((l.outstanding for l in qs), Decimal("0"))
    en_defaut = qs.filter(status=Loan.Status.DEFAUT).count()
    en_traitement = qs.filter(status=Loan.Status.EN_TRAITEMENT).count()
    clotures = qs.filter(status=Loan.Status.CLOTURE).count()

    def money(v: Decimal) -> str:
        # Arrondi d'AFFICHAGE explicite : le formatage `Decimal` par défaut applique
        # l'arrondi bancaire du contexte, pas la règle du centime du module.
        entier = Decimal(v).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        return f"${entier:,}".replace(",", " ")

    return [
        {"title": "Dossiers", "value": str(total), "icon": "BarChart"},
        {"title": "En traitement", "value": str(en_traitement), "icon": "Calendar"},
        {"title": "Actifs", "value": str(approved.count()), "icon": "CheckSquare"},
        {"title": "Décaissé (cumul)", "value": money(disbursed_total), "icon": "DollarSign"},
        {"title": "Encours", "value": money(outstanding_total), "icon": "Repeat"},
        {"title": "En défaut", "value": str(en_defaut), "icon": "AlertTriangle",
         "trendValue": f"{clotures} clôturés", "trendDirection": "up"},
    ]


def alerts() -> list[dict]:
    """Dossiers nécessitant une attention (échéance dépassée, défaut, à décaisser)."""
    today = timezone.localdate()
    out = []
    for loan in Loan.objects.exclude(status__in=[Loan.Status.CLOTURE, Loan.Status.REJETE]):
        if loan.status == Loan.Status.DEFAUT:
            out.append({"reference": loan.reference, "operator": loan.operator,
                        "level": "danger", "message": "Dossier en défaut."})
        elif loan.due_date and loan.due_date < today:
            out.append({"reference": loan.reference, "operator": loan.operator,
                        "level": "warning",
                        "message": f"Échéance dépassée ({loan.due_date.isoformat()})."})
        elif loan.status == Loan.Status.APPROUVE and loan.disbursed == 0:
            out.append({"reference": loan.reference, "operator": loan.operator,
                        "level": "info", "message": "Approuvé, en attente de décaissement."})
    return out


def calendar_entries():
    """Prochaines échéances (agrégées sur les dossiers actifs)."""
    out = []
    for loan in Loan.objects.filter(status__in=[Loan.Status.EN_COURS, Loan.Status.APPROUVE]):
        data = schedule_for(loan)
        for row in data["schedule"]:
            out.append({
                "reference": loan.reference, "operator": loan.operator,
                "currency": loan.currency, **row,
            })
    out.sort(key=lambda r: r["date"])
    return out
