"""Provisionnement du risque de crédit (principe 6 de HAZINA).

« Le provisionnement est mécanique, la reprise aussi. »

Chaîne complète, sans aucun geste manuel :

    portfolio.Loan  ──(échéancier Decimal + imputation des règlements)──►  jours de retard
         │                                                                      │
         │                                                       ClasseRisque (grille EN BASE)
         ▼                                                                      ▼
    encours restant dû ─────────────────────────────────────────►  encours × taux = provision
         │                                                                      │
         │  classe « en souffrance » ──► B5 : 413 → 416 (déclassement)          │
         └──────────────────────────────────────────────────────────► B6 dotation / B7 reprise

Trois choix de conception, explicités parce qu'ils engagent des chiffres :

1. **L'échéancier n'est PAS recalculé ici.** Il l'a été : ce module portait sa propre
   version en `Decimal` — la quatrième du projet — écrite parce que
   `portfolio.schedule.build_schedule` travaillait alors en `float`, ce que le principe 4
   interdit sur un chemin qui décide d'une provision.

   Cette raison a disparu (`portfolio/schedule.py` est passé en `Decimal`, commit 459e17b),
   et avec elle la seule justification d'un deuxième calcul. On délègue donc désormais au
   calendrier que le CLIENT REMBOURSE (`portfolio.services.schedule_for`), traduit dans le
   vocabulaire comptable et rien de plus. Ce n'est pas qu'une déduplication : provisionner
   sur un calendrier qui n'est pas celui du contrat, c'est compter des jours de retard que
   le client ne doit pas. La délégation fait entrer d'un coup trois corrections que la
   réimplémentation ne connaissait pas — le DIFFÉRÉ (un prêt à 5 mois de différé était
   compté en retard dès le premier mois, donc provisionné à tort), le taux de calcul en
   pleine précision (`annual_rate / 12` plutôt que la colonne `rate` arrondie), et l'ancrage
   des échéances sur la date d'effet (un prêt démarré un 31 ne dérive plus au 28).

2. **Les jours de retard se lisent sur la plus ancienne échéance non intégralement réglée**,
   après imputation des règlements dans l'ordre des échéances (intérêts d'abord, puis
   capital) — pas sur `Loan.due_date`, qui est la fin du prêt et non son premier impayé.

3. **La devise du portefeuille est traduite.** `portfolio.Loan.Currency` dit « CDF », le plan
   comptable de l'annexe A dit « FC ». La traduction est explicite et centralisée ici ;
   la divergence de nomenclature est signalée au fondateur (principe 6 de MKOPO).
"""
from __future__ import annotations

from datetime import date as date_cls
from decimal import Decimal, ROUND_HALF_UP

from django.db import transaction

from common.exceptions import ConflictError, NotFoundError, ValidationFailed

from . import catalogue, services
from .models import (
    ArreteProvision,
    ClasseRisque,
    ClassementCredit,
    Devise,
    LigneArreteProvision,
)

#: `portfolio` nomme le franc congolais « CDF », l'annexe A le nomme « FC ».
DEVISE_PORTEFEUILLE: dict[str, str] = {"CDF": Devise.FC, "USD": Devise.USD, "FC": Devise.FC}

COMPTE_ENCOURS_SAIN = "413"
COMPTE_ENCOURS_SOUFFRANCE = "416"
COMPTE_PROVISION = "137"

EVENEMENT_DECLASSEMENT = "B5"
EVENEMENT_DOTATION = "B6"
EVENEMENT_REPRISE = "B7"
#: Symétrique de B5, HORS annexe B (cf. `definitions.CATALOGUE["B17"]`) : sans lui, un
#: crédit revenu à bonne fin restait indéfiniment en souffrance au grand livre et le PAR
#: comptable ne redescendait jamais.
EVENEMENT_RECLASSEMENT = "B17"


# --------------------------------------------------------------------- GRILLE PAR

def grille(*, actives_seulement: bool = True) -> list[ClasseRisque]:
    qs = ClasseRisque.objects.all()
    if actives_seulement:
        qs = qs.filter(actif=True)
    return list(qs.order_by("jours_min", "ordre"))


def verifier_couverture(classes: list[ClasseRisque] | None = None) -> list[ClasseRisque]:
    """La grille doit couvrir [0, ∞[ SANS TROU ni recouvrement.

    Un trou, c'est un crédit sans classe donc sans provision : le risque disparaîtrait des
    états financiers par un simple effet de bord de paramétrage. Ce contrôle s'exécute avant
    tout arrêté et il est verrouillé par un test.
    """
    classes = classes if classes is not None else grille()
    if not classes:
        raise ValidationFailed(
            "Aucune classe de risque active : chargez la grille "
            "(« manage.py seed_accounting ») ou paramétrez-la avant tout arrêté."
        )
    if classes[0].jours_min != 0:
        raise ValidationFailed(
            f"La grille PAR ne démarre pas à 0 jour (première borne : "
            f"{classes[0].jours_min}) — les crédits sains n'auraient pas de classe."
        )
    for precedente, suivante in zip(classes, classes[1:]):
        if precedente.jours_max is None:
            raise ValidationFailed(
                f"La classe {precedente.code} est terminale (sans borne supérieure) mais "
                f"{suivante.code} la suit : deux classes se recouvrent."
            )
        if suivante.jours_min != precedente.jours_max + 1:
            raise ValidationFailed(
                f"Discontinuité entre {precedente.code} (…{precedente.jours_max} j) et "
                f"{suivante.code} ({suivante.jours_min} j…) : la grille doit être contiguë."
            )
    if classes[-1].jours_max is not None:
        raise ValidationFailed(
            f"La dernière classe {classes[-1].code} est bornée à {classes[-1].jours_max} j : "
            "aucune classe ne couvre les retards au-delà."
        )
    return classes


def classer(jours_retard: int, classes: list[ClasseRisque]) -> ClasseRisque:
    for classe in classes:
        if classe.contient(jours_retard):
            return classe
    raise ValidationFailed(  # pragma: no cover - impossible après verifier_couverture
        f"Aucune classe de risque ne couvre {jours_retard} jours de retard."
    )


# ------------------------------------------------------------------- ÉCHÉANCIER

def _traduire(lignes: list[dict]) -> list[dict]:
    """Traduit les lignes de `portfolio.schedule` dans le vocabulaire comptable.

    C'est TOUT ce qui reste de l'ancienne réimplémentation : une correspondance de noms et
    un `date.fromisoformat`. Aucune arithmétique — s'il en réapparaissait une ici, la dette
    serait reconstituée.

    `interest_capitalized` n'est délibérément PAS additionné aux intérêts : en franchise
    totale, les intérêts capitalisés ne sont pas EXIGIBLES à l'échéance (ils grossissent le
    capital). Les compter comme dus ferait naître un impayé — donc un retard, donc une
    provision — sur une échéance que le contrat dit non payable.
    """
    return [
        {
            "numero": ligne["number"],
            "date": date_cls.fromisoformat(ligne["date"]),
            "capital": services.q2(ligne["principal"]),
            "interets": services.q2(ligne["interest"]),
            "total": services.q2(ligne["total"]),
            "crd": services.q2(ligne["balance"]),
            "phase": ligne.get("phase", ""),
        }
        for ligne in lignes
    ]


def echeancier(
    *,
    principal,
    taux_mensuel_pct,
    duree_mois: int,
    frequence: str,
    date_debut: date_cls,
    differe_mois: int = 0,
    mode_differe: str = "",
) -> list[dict]:
    """Échéancier du prêt, EMPRUNTÉ à `portfolio` et traduit — jamais recalculé.

    Invariants (garantis par `portfolio.schedule`, verrouillés par les tests des deux
    côtés) : Σ capital = principal, et le CRD après la dernière échéance est rigoureusement
    nul — le résidu d'arrondi est absorbé par la dernière échéance.
    """
    from portfolio import schedule as echeancier_portefeuille

    if services.q2(principal) <= 0 or int(duree_mois or 0) <= 0:
        return []

    options = {}
    if differe_mois:
        options["deferral_months"] = int(differe_mois)
        if mode_differe:
            options["deferral_mode"] = mode_differe
    return _traduire(echeancier_portefeuille.build_schedule(
        services.q2(principal),
        services.to_decimal(taux_mensuel_pct),
        int(duree_mois or 0),
        frequence,
        date_debut,
        **options,
    ))


def imputer(echeances: list[dict], total_regle: Decimal) -> dict:
    """Impute les règlements dans l'ordre des échéances : intérêts d'abord, puis capital.

    Retourne le capital effectivement remboursé et la date de la plus ancienne échéance
    non intégralement réglée (`None` si tout est à jour).

    DETTE CONNUE — CETTE FONCTION EST UN DOUBLON EN SURSIS (signalée par l'agent
    `portfolio`, et il a raison). `portfolio.repayment` applique la même règle pour
    ventiler les remboursements en événements B2/B3 : l'imputation est une propriété de
    l'ÉCHÉANCIER, que `portfolio` produit et dont ce module dépend déjà depuis la fusion
    (`_echeancier_du_credit`). C'est donc CETTE copie-ci qui doit partir, exactement comme
    la 4ᵉ réimplémentation d'échéancier avant elle — et pas l'inverse : un module métier ne
    peut pas dépendre de la comptabilité pour appliquer sa propre règle.

    Ce qui manque pour la retirer, demandé au producteur : une fonction PUBLIQUE (la sienne
    est `_imputer`, donc privée — consommer un souligné, c'est signer pour une rupture sans
    préavis) qui rende AUSSI `premiere_echeance_impayee`. C'est le seul champ que sa version
    ne calcule pas, et c'est celui dont dépendent les jours de retard, donc la provision.

    En attendant, la divergence est impossible sans bruit : `portfolio.tests`
    (`ImputationUniqueTests`) verrouille l'égalité des deux implémentations au centime sur
    huit montants, y compris la date du premier impayé. C'est un filet, pas une
    architecture — il ne dispense pas du retrait, il le rend sûr.
    """
    reste = services.q2(total_regle)
    capital_rembourse = Decimal("0.00")
    interets_regles = Decimal("0.00")
    premiere_impayee = None
    for echeance in echeances:
        du_interets = echeance["interets"]
        du_capital = echeance["capital"]
        paye_interets = min(reste, du_interets)
        reste -= paye_interets
        paye_capital = min(reste, du_capital)
        reste -= paye_capital
        interets_regles += paye_interets
        capital_rembourse += paye_capital
        if (paye_interets < du_interets or paye_capital < du_capital) and premiere_impayee is None:
            premiere_impayee = echeance["date"]
    return {
        "capital_rembourse": services.q2(capital_rembourse),
        "interets_regles": services.q2(interets_regles),
        "avance": services.q2(reste),
        "premiere_echeance_impayee": premiere_impayee,
    }


# ------------------------------------------------------- ANALYSE DU PORTEFEUILLE

def _flux_du_credit(loan) -> tuple[Decimal, Decimal]:
    """(décaissé, réglé) sur les seules transactions VALIDÉES.

    Divergence assumée avec `portfolio.Loan.disbursed`, qui additionne aussi les
    mouvements « en attente » : une provision ne se calcule pas sur de l'argent dont on
    n'est pas sûr qu'il soit sorti. L'écart est exposé dans le rapprochement.
    """
    from portfolio.models import LoanTransaction

    decaisse = Decimal("0.00")
    regle = Decimal("0.00")
    for transaction_ in loan.transactions.all():
        if transaction_.status != LoanTransaction.Status.VALIDE or transaction_.amount is None:
            continue
        montant = services.to_decimal(transaction_.amount)
        if transaction_.kind == LoanTransaction.Kind.DISBURSEMENT:
            decaisse += montant
        elif transaction_.kind == LoanTransaction.Kind.REPAYMENT:
            regle += -montant  # les remboursements sont stockés négatifs
    return services.q2(decaisse), services.q2(regle)


def _echeancier_du_credit(loan, anomalies: list[str]) -> list[dict]:
    """Le calendrier que CE client rembourse — celui de `portfolio`, pas un jumeau.

    On passe par `portfolio.services.schedule_for` plutôt que de rassembler nous-mêmes les
    paramètres du prêt : c'est la seule façon d'être certain que la provision est calculée
    sur le MÊME échéancier que celui affiché au client et opposé au dossier. Reconstituer
    les arguments à l'identique aurait suffi aujourd'hui, et aurait divergé au premier
    paramètre ajouté au contrat de prêt (le différé vient d'en être la démonstration ; la
    bascule de la base amortie du montant APPROUVÉ vers le total DÉCAISSÉ VALIDÉ, arrivée
    dans la foulée, en est la seconde — l'ancienne anomalie « approuvé ≠ décaissé » que ce
    module signalait n'a plus d'objet, les deux parlent désormais du même argent).

    Un échéancier inexploitable (paramètres refusés par `portfolio`) devient une ANOMALIE
    remontée dans le rapport, pas une exception : un dossier mal configuré ne doit pas
    empêcher l'arrêté de provision de tout le portefeuille.
    """
    from portfolio.services import BASE_APPROUVE, schedule_for

    try:
        reponse = schedule_for(loan)
    except Exception as exc:  # noqa: BLE001 - un refus de `portfolio` est une DONNÉE
        anomalies.append(
            f"Échéancier refusé par le portefeuille ({type(exc).__name__} : {exc}) — "
            "retard non calculable, crédit classé sur 0 jour de retard. Le paramétrage du "
            "prêt est à corriger à la source."
        )
        return []

    # Les anomalies du portefeuille (décaissements étalés, date d'effet déduite…) sont
    # REPRISES telles quelles : elles expliquent l'échéancier sur lequel la provision est
    # calculée, et un auditeur doit les lire au même endroit que le chiffre.
    anomalies.extend(reponse.get("anomalies") or [])
    # CONTRÔLE DE COHÉRENCE, et non information : ce test est censé ne JAMAIS être vrai ici.
    #
    # `analyser_credit` ne nous appelle que si elle a vu des décaissements validés, et
    # `portfolio` n'étiquette « montant_approuve » que s'il n'en voit AUCUN — les deux
    # définitions sont volontairement les mêmes (`Loan.disbursed_validated` le dit dans sa
    # docstring). Si elles se répondent différemment, ce n'est donc pas un cas de figure,
    # c'est que les deux modules ne comptent plus le même argent : la provision serait
    # calculée sur une base que la comptabilité croit connaître et ne connaît plus.
    #
    # On compare la CONSTANTE de `portfolio`, jamais son littéral. Un test qui gèle la chaîne
    # chez lui attrape « quelqu'un change la valeur » — mais pas le refactor qui arrive
    # vraiment : renommer la valeur ET mettre le test à jour dans le même geste, ce à quoi
    # ressemble un refactor propre. Les deux suites resteraient vertes et cette comparaison
    # deviendrait silencieusement fausse. Un import ne peut pas être « mis à jour de bonne
    # foi » dans le mauvais sens : la valeur suit, ou il casse. (Arbitrage rendu par l'agent
    # `portfolio`, propriétaire de la constante.)
    if reponse.get("principalSource") == BASE_APPROUVE:
        anomalies.append(
            "INCOHÉRENCE : le portefeuille amortit le montant APPROUVÉ (échéancier "
            "prévisionnel, donc aucun décaissement validé de son point de vue) alors que la "
            "comptabilité en constate. Les deux modules ne comptent plus le même argent — "
            "les jours de retard portent sur un calendrier qui n'est pas celui du contrat. "
            "À instruire avant de s'appuyer sur cette provision."
        )
    return _traduire(reponse["schedule"])


def analyser_credit(loan, *, as_of: date_cls, classes: list[ClasseRisque]) -> dict | None:
    """Classification d'UN crédit à une date. `None` = hors périmètre du risque."""
    from portfolio.models import Loan

    if loan.status in (Loan.Status.REJETE, Loan.Status.CLOTURE):
        return None

    decaisse, regle = _flux_du_credit(loan)
    if decaisse <= 0:
        return None  # rien n'est sorti : aucune exposition

    devise = DEVISE_PORTEFEUILLE.get((loan.currency or "").upper())
    anomalies: list[str] = []
    if devise is None:
        devise = Devise.FC
        anomalies.append(
            f"Devise « {loan.currency} » inconnue du plan comptable — rattachée à FC par "
            "défaut ; à corriger à la source."
        )

    signalees = len(anomalies)
    echeances = _echeancier_du_credit(loan, anomalies)
    if not echeances:
        if len(anomalies) == signalees:  # échéancier vide, et non refusé (déjà signalé)
            anomalies.append(
                "Échéancier vide (durée ou montant nul) : retard non calculable, crédit "
                "classé sur 0 jour de retard."
            )
        imputation = {
            "capital_rembourse": min(regle, decaisse),
            "interets_regles": Decimal("0.00"),
            "avance": Decimal("0.00"),
            "premiere_echeance_impayee": None,
        }
    else:
        imputation = imputer(echeances, regle)

    premiere = imputation["premiere_echeance_impayee"]
    jours_retard = (as_of - premiere).days if premiere and premiere < as_of else 0
    jours_retard = max(0, jours_retard)

    encours = services.q2(decaisse - imputation["capital_rembourse"])
    if encours < 0:
        anomalies.append(
            f"Capital imputé ({imputation['capital_rembourse']}) supérieur au décaissé "
            f"({decaisse}) : exposition ramenée à 0, écart à instruire."
        )
        encours = Decimal("0.00")

    classe = classer(jours_retard, classes)
    return {
        "loan_id": loan.pk,
        "loan_reference": loan.reference,
        "operateur": loan.operator,
        "statut_portefeuille": loan.status,
        "devise": devise,
        "decaisse": decaisse,
        "regle": regle,
        "capital_rembourse": imputation["capital_rembourse"],
        "encours": encours,
        "jours_retard": jours_retard,
        "premiere_echeance_impayee": premiere,
        "classe": classe,
        "taux_provision": classe.taux_provision,
        "provision": services.q2(encours * classe.taux_provision),
        "en_souffrance": classe.en_souffrance,
        "anomalies": anomalies,
    }


def analyser_portefeuille(*, as_of: date_cls) -> dict:
    """Classification complète, EN LECTURE SEULE — aucune écriture n'est produite ici.

    C'est le mode « simulation » : le comité voit ce que coûterait l'arrêté avant de le
    déclencher.
    """
    from portfolio.models import Loan

    classes = verifier_couverture()
    credits: list[dict] = []
    for loan in Loan.objects.all().prefetch_related("transactions"):
        analyse = analyser_credit(loan, as_of=as_of, classes=classes)
        if analyse is not None:
            credits.append(analyse)

    totaux: dict[str, dict] = {}
    for analyse in credits:
        par_devise = totaux.setdefault(analyse["devise"], {})
        bucket = par_devise.setdefault(analyse["classe"].code, {
            "classe": analyse["classe"].code,
            "libelle": analyse["classe"].libelle,
            "taux_provision": analyse["classe"].taux_provision,
            "nombre": 0,
            "encours": Decimal("0.00"),
            "provision": Decimal("0.00"),
        })
        bucket["nombre"] += 1
        bucket["encours"] += analyse["encours"]
        bucket["provision"] += analyse["provision"]

    par_code = {c.code: c for c in classes}
    synthese = []
    for devise in sorted(totaux):
        lignes = [totaux[devise][c.code] for c in classes if c.code in totaux[devise]]
        encours_total = sum((l["encours"] for l in lignes), Decimal("0.00"))
        provision_totale = sum((l["provision"] for l in lignes), Decimal("0.00"))
        # PAR30 au sens usuel : encours porté par des crédits à ≥ 30 jours de retard.
        en_retard = sum(
            (l["encours"] for l in lignes if par_code[l["classe"]].jours_min >= 30),
            Decimal("0.00"),
        )
        synthese.append({
            "devise": devise,
            "lignes": lignes,
            "nombre_credits": sum(l["nombre"] for l in lignes),
            "encours_total": services.q2(encours_total),
            "provision_requise": services.q2(provision_totale),
            "encours_a_risque_30j": services.q2(en_retard),
            "par30_ratio": (
                services.q2(en_retard / encours_total * 100) if encours_total else Decimal("0.00")
            ),
            "provision_comptabilisee": services.q2(
                -services.solde_compte(COMPTE_PROVISION, devise=devise, as_of=as_of)
            ),
            "encours_comptable": services.q2(
                services.solde_compte(COMPTE_ENCOURS_SAIN, devise=devise, as_of=as_of)
                + services.solde_compte(COMPTE_ENCOURS_SOUFFRANCE, devise=devise, as_of=as_of)
            ),
        })

    return {
        "as_of": as_of,
        "grille": classes,
        "credits": credits,
        "total_rows": len(credits),
        "synthese": synthese,
        "anomalies": [
            {"loan_reference": c["loan_reference"], "messages": c["anomalies"]}
            for c in credits if c["anomalies"]
        ],
    }


# ------------------------------------------------------------------- ARRÊTÉ

def _dernier_classement(loan_id: int, *, avant: date_cls) -> ClassementCredit | None:
    return (
        ClassementCredit.objects
        .filter(loan_id=loan_id, date_arrete__lt=avant)
        .order_by("-date_arrete", "-id")
        .first()
    )


@transaction.atomic
def arreter(
    *,
    date_arrete: date_cls,
    par: str,
    prefixe_reference: str = "PROV",
) -> dict:
    """Clôture de provisionnement : déclassements B5 puis dotation B6 / reprise B7.

    Idempotence : un arrêté existant à la même date et pour la même devise est refusé
    (409). On ne « rejoue » pas une clôture — on en fait une nouvelle à une date
    postérieure, ou on contrepasse la pièce.
    """
    if not par:
        raise ValidationFailed("Un arrêté de provision s'exécute sous une identité connue.")

    analyse = analyser_portefeuille(as_of=date_arrete)
    devises = [ligne["devise"] for ligne in analyse["synthese"]]
    deja = ArreteProvision.objects.filter(date_arrete=date_arrete, devise__in=devises)
    if deja.exists():
        raise ConflictError(
            f"Un arrêté existe déjà au {date_arrete} pour "
            f"{', '.join(sorted(a.devise for a in deja))}."
        )

    declassements = _declasser(analyse, date_arrete=date_arrete, par=par,
                              prefixe_reference=prefixe_reference)
    reclassements = _reclasser(analyse, date_arrete=date_arrete, par=par,
                               prefixe_reference=prefixe_reference)
    arretes = _provisionner(analyse, date_arrete=date_arrete, par=par,
                           prefixe_reference=prefixe_reference)
    _enregistrer_classements(analyse, date_arrete=date_arrete, par=par,
                             pieces_declassement=declassements["pieces_par_credit"],
                             pieces_reclassement=reclassements["pieces_par_credit"])

    return {
        "date_arrete": date_arrete,
        "declassements": declassements["details"],
        "reclassements": reclassements["details"],
        "arretes": arretes,
        "anomalies": analyse["anomalies"] + reclassements["anomalies"],
    }


def _declasser(analyse: dict, *, date_arrete, par, prefixe_reference) -> dict:
    """B5 — 413 → 416 pour tout crédit qui ENTRE en souffrance, jamais deux fois.

    Un crédit déjà déclassé lors d'un arrêté précédent n'est pas re-déclassé : le
    dernier classement connu fait foi.
    """
    a_declasser = []
    for credit in analyse["credits"]:
        if not credit["en_souffrance"] or credit["encours"] <= 0:
            continue
        precedent = _dernier_classement(credit["loan_id"], avant=date_arrete)
        if precedent is not None and precedent.en_souffrance:
            continue
        a_declasser.append(credit)

    # Garde-fou : on ne déclasse pas plus d'encours sain que le grand livre n'en porte.
    par_devise: dict[str, Decimal] = {}
    for credit in a_declasser:
        par_devise[credit["devise"]] = par_devise.get(credit["devise"], Decimal("0.00")) + credit["encours"]
    for devise, montant in par_devise.items():
        solde = services.solde_compte(COMPTE_ENCOURS_SAIN, devise=devise, as_of=date_arrete)
        if solde < montant:
            raise ValidationFailed(
                f"Déclassement impossible en {devise} : le compte "
                f"{COMPTE_ENCOURS_SAIN}{devise} porte {solde} au grand livre alors que "
                f"{montant} doivent passer en souffrance. L'encours du portefeuille n'est "
                "pas (ou pas entièrement) comptabilisé dans `accounting` — les événements "
                "de crédit (B1/B2) doivent être branchés sur le moteur d'écritures avant "
                "de pouvoir déclasser. Aucun déclassement n'a été passé."
            )

    pieces_par_credit: dict[int, object] = {}
    details = []
    for credit in a_declasser:
        piece = catalogue.executer_evenement(
            EVENEMENT_DECLASSEMENT,
            {"devise": credit["devise"], "montants": {"encours": credit["encours"]}},
            reference=f"{prefixe_reference}-{date_arrete:%Y%m%d}-B5-{credit['loan_reference']}",
            date_operation=date_arrete,
            libelle=f"Déclassement en souffrance — {credit['loan_reference']} "
                    f"({credit['jours_retard']} j de retard)",
            origine_type="portfolio.Loan",
            origine_id=str(credit["loan_id"]),
            par=par,
        )
        pieces_par_credit[credit["loan_id"]] = piece
        details.append({
            "loan_reference": credit["loan_reference"],
            "devise": credit["devise"],
            "encours": credit["encours"],
            "jours_retard": credit["jours_retard"],
            "classe": credit["classe"].code,
            "piece": piece.reference,
        })
    return {"details": details, "pieces_par_credit": pieces_par_credit}


def _dernier_declassement(loan_id: int, *, avant: date_cls) -> ClassementCredit | None:
    """Dernier arrêté ayant RÉELLEMENT passé une pièce B5 pour ce crédit."""
    return (
        ClassementCredit.objects
        .filter(loan_id=loan_id, date_arrete__lt=avant, piece_declassement__isnull=False)
        .order_by("-date_arrete", "-id")
        .first()
    )


def _reclasser(analyse: dict, *, date_arrete, par, prefixe_reference) -> dict:
    """B17 — 416 → 413 pour tout crédit qui SORT de la souffrance.

    Le montant reclassé est celui qui avait été DÉCLASSÉ (l'encours porté par la pièce B5),
    et non l'encours courant. Raison : depuis le déclassement, les remboursements de capital
    ont continué de créditer 413 (schéma B2, qui ne connaît pas le compte 416). Rendre à 413
    exactement ce que B5 lui avait pris rétablit donc mécaniquement l'identité
    « 413 + 416 = encours », et laisse 416 à zéro pour ce crédit — au lieu d'y abandonner un
    résidu que plus aucune écriture n'irait chercher.

    Cas chiffré (celui de `tests_provisions`) : décaissement 1 200, capital remboursé 100 →
    413 = 1 100 ; déclassement B5 → 413 = 0, 416 = 1 100 ; retour à bonne fin → B17 de
    1 100 → 413 = 1 100, 416 = 0. Si les remboursements postérieurs avaient été
    comptabilisés (B2 non branché à ce jour), 413 afficherait l'encours réel.

    Le déclassement ne se rejoue pas (`_declasser`) ; le reclassement non plus : il n'a lieu
    qu'à la TRANSITION « souffrance → sain » constatée par rapport au dernier classement.
    """
    a_reclasser = []
    anomalies: list[dict] = []
    for credit in analyse["credits"]:
        if credit["en_souffrance"]:
            continue
        precedent = _dernier_classement(credit["loan_id"], avant=date_arrete)
        if precedent is None or not precedent.en_souffrance:
            continue
        declassement = _dernier_declassement(credit["loan_id"], avant=date_arrete)
        if declassement is None:
            anomalies.append({
                "loan_reference": credit["loan_reference"],
                "messages": [
                    "Crédit revenu sain alors qu'aucune pièce de déclassement B5 n'est "
                    "traçable : rien n'a été reclassé (on ne devine pas ce qui a été "
                    "porté en 416). À instruire manuellement."
                ],
            })
            continue
        a_reclasser.append((credit, services.q2(declassement.encours)))

    # Garde-fou symétrique de celui du déclassement : on ne rend jamais à 413 plus
    # d'encours en souffrance que le grand livre n'en porte réellement.
    par_devise: dict[str, Decimal] = {}
    for credit, montant in a_reclasser:
        par_devise[credit["devise"]] = par_devise.get(credit["devise"], Decimal("0.00")) + montant
    for devise, montant in par_devise.items():
        solde = services.solde_compte(COMPTE_ENCOURS_SOUFFRANCE, devise=devise, as_of=date_arrete)
        if solde < montant:
            raise ValidationFailed(
                f"Reclassement impossible en {devise} : le compte "
                f"{COMPTE_ENCOURS_SOUFFRANCE}{devise} porte {solde} au grand livre alors que "
                f"{montant} doivent revenir en encours sain. Aucun reclassement n'a été passé."
            )

    pieces_par_credit: dict[int, object] = {}
    details = []
    for credit, montant in a_reclasser:
        piece = catalogue.executer_evenement(
            EVENEMENT_RECLASSEMENT,
            {"devise": credit["devise"], "montants": {"encours": montant}},
            reference=f"{prefixe_reference}-{date_arrete:%Y%m%d}-B17-{credit['loan_reference']}",
            date_operation=date_arrete,
            libelle=f"Retour à bonne fin — {credit['loan_reference']} "
                    f"(classe {credit['classe'].code}, {credit['jours_retard']} j de retard)",
            origine_type="portfolio.Loan",
            origine_id=str(credit["loan_id"]),
            par=par,
        )
        pieces_par_credit[credit["loan_id"]] = piece
        details.append({
            "loan_reference": credit["loan_reference"],
            "devise": credit["devise"],
            "encours_reclasse": montant,
            "encours_courant": credit["encours"],
            "jours_retard": credit["jours_retard"],
            "classe": credit["classe"].code,
            "piece": piece.reference,
        })
    return {"details": details, "pieces_par_credit": pieces_par_credit, "anomalies": anomalies}


def _provisionner(analyse: dict, *, date_arrete, par, prefixe_reference) -> list[dict]:
    """B6/B7 — le stock de provision converge vers la cible, par devise.

    On raisonne en STOCK (137 doit valoir la provision requise), pas en flux : c'est ce
    qui rend la reprise automatique et symétrique de la dotation.
    """
    resultats = []
    for ligne_devise in analyse["synthese"]:
        devise = ligne_devise["devise"]
        requise = ligne_devise["provision_requise"]
        anterieure = services.q2(
            -services.solde_compte(COMPTE_PROVISION, devise=devise, as_of=date_arrete)
        )
        ecart = services.q2(requise - anterieure)

        piece = None
        dotation = reprise = Decimal("0.00")
        if ecart > 0:
            dotation = ecart
            piece = catalogue.executer_evenement(
                EVENEMENT_DOTATION,
                {"devise": devise, "montants": {"dotation": dotation}},
                reference=f"{prefixe_reference}-{date_arrete:%Y%m%d}-B6-{devise}",
                date_operation=date_arrete,
                libelle=f"Dotation aux provisions {devise} — arrêté du {date_arrete}",
                origine_type="accounting.ArreteProvision",
                origine_id=f"{date_arrete}:{devise}",
                par=par,
            )
        elif ecart < 0:
            reprise = -ecart
            piece = catalogue.executer_evenement(
                EVENEMENT_REPRISE,
                {"devise": devise, "montants": {"reprise": reprise}},
                reference=f"{prefixe_reference}-{date_arrete:%Y%m%d}-B7-{devise}",
                date_operation=date_arrete,
                libelle=f"Reprise de provisions {devise} — arrêté du {date_arrete}",
                origine_type="accounting.ArreteProvision",
                origine_id=f"{date_arrete}:{devise}",
                par=par,
            )

        arrete = ArreteProvision.objects.create(
            date_arrete=date_arrete,
            devise=devise,
            provision_requise=requise,
            provision_anterieure=anterieure,
            dotation=dotation,
            reprise=reprise,
            encours_portefeuille=ligne_devise["encours_total"],
            encours_comptable=ligne_devise["encours_comptable"],
            nombre_credits=ligne_devise["nombre_credits"],
            piece=piece,
            cree_par=par,
        )
        for detail in ligne_devise["lignes"]:
            classe = ClasseRisque.objects.get(code=detail["classe"])
            LigneArreteProvision.objects.create(
                arrete=arrete,
                classe=classe,
                nombre_credits=detail["nombre"],
                encours=services.q2(detail["encours"]),
                taux_applique=classe.taux_provision,
                provision=services.q2(detail["provision"]),
            )
        _journaliser_arrete(arrete, par=par)
        resultats.append({
            "devise": devise,
            "provision_requise": requise,
            "provision_anterieure": anterieure,
            "dotation": dotation,
            "reprise": reprise,
            "piece": piece.reference if piece else None,
            "encours_portefeuille": ligne_devise["encours_total"],
            "encours_comptable": ligne_devise["encours_comptable"],
            "ecart_encours": services.q2(
                ligne_devise["encours_comptable"] - ligne_devise["encours_total"]
            ),
        })
    return resultats


def _enregistrer_classements(analyse, *, date_arrete, par, pieces_declassement,
                             pieces_reclassement=None) -> None:
    pieces_reclassement = pieces_reclassement or {}
    for credit in analyse["credits"]:
        ClassementCredit.objects.create(
            date_arrete=date_arrete,
            loan_id=credit["loan_id"],
            loan_reference=credit["loan_reference"],
            classe=credit["classe"],
            jours_retard=credit["jours_retard"],
            encours=credit["encours"],
            devise=credit["devise"],
            en_souffrance=credit["en_souffrance"],
            piece_declassement=pieces_declassement.get(credit["loan_id"]),
            piece_reclassement=pieces_reclassement.get(credit["loan_id"]),
            cree_par=par,
        )


def _journaliser_arrete(arrete: ArreteProvision, *, par: str) -> None:
    try:
        from audit.services import record as audit_record
    except Exception:  # pragma: no cover
        return
    audit_record(
        actor=par, action="accounting.arrete_provision",
        entity_type="ArreteProvision", entity_id=str(arrete.pk),
        details={
            "date_arrete": str(arrete.date_arrete), "devise": arrete.devise,
            "provision_requise": str(arrete.provision_requise),
            "provision_anterieure": str(arrete.provision_anterieure),
            "dotation": str(arrete.dotation), "reprise": str(arrete.reprise),
            "piece": arrete.piece.reference if arrete.piece else None,
        },
    )


def historique(*, devise: str = "", limite: int = 100) -> list[ArreteProvision]:
    qs = ArreteProvision.objects.select_related("piece").prefetch_related("lignes__classe")
    if devise:
        if devise not in Devise.values:
            raise ValidationFailed(f"Devise « {devise} » inconnue.")
        qs = qs.filter(devise=devise)
    return list(qs[:limite])


def classements(*, date_arrete: date_cls | None = None, loan_reference: str = "",
                limite: int = 500) -> list[ClassementCredit]:
    qs = ClassementCredit.objects.select_related("classe", "piece_declassement")
    if date_arrete:
        qs = qs.filter(date_arrete=date_arrete)
    if loan_reference:
        qs = qs.filter(loan_reference=loan_reference)
    return list(qs[:limite])


@transaction.atomic
def modifier_classe(code: str, *, par: str, **champs) -> ClasseRisque:
    """Paramétrage du comité : taux et bornes se changent EN BASE, jamais par déploiement.

    La grille résultante est re-vérifiée immédiatement : un changement de borne qui
    créerait un trou est refusé avant d'exister.
    """
    classe = ClasseRisque.objects.filter(code=code).first()
    if classe is None:
        raise NotFoundError(f"Classe de risque « {code} » introuvable.")
    autorises = {"libelle", "jours_min", "jours_max", "taux_provision", "en_souffrance",
                 "ordre", "actif"}
    inconnus = set(champs) - autorises
    if inconnus:
        raise ValidationFailed(f"Champs non modifiables : {', '.join(sorted(inconnus))}.")

    if "taux_provision" in champs:
        taux = services.to_decimal(champs["taux_provision"])
        if taux < 0 or taux > 1:
            raise ValidationFailed(
                "Le taux de provision est une FRACTION de l'encours : 0 ≤ taux ≤ 1 "
                "(0.25 = 25 %)."
            )
        # Quantize à la précision de STOCKAGE : sans ça, l'objet renvoyé porterait
        # « 0.6 » alors que la base contient « 0.6000 » — deux écritures du même taux
        # selon qu'on relit ou non (principe 11 : jamais deux chiffres pour une grandeur).
        champs["taux_provision"] = taux.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)

    for nom, valeur in champs.items():
        setattr(classe, nom, valeur)
    classe.modifie_par = par
    classe.save()

    # Re-contrôle IMMÉDIAT dans la transaction : une borne qui créerait un trou est
    # annulée avant d'exister (`@transaction.atomic` fait le rollback).
    verifier_couverture()

    try:
        from audit.services import record as audit_record

        audit_record(
            actor=par, action="accounting.classe_risque_modifiee",
            entity_type="ClasseRisque", entity_id=classe.code,
            details={nom: str(valeur) for nom, valeur in champs.items()},
        )
    except Exception:  # pragma: no cover
        pass
    return classe
