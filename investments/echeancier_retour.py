"""
Échéancier de RETOUR d'un projet financé — le producteur qui manquait (B12).

`RepaymentSchedule` existait, `GET /investments/schedules` le servait, la garde
P09→P10 l'exigeait et l'Annexe D lisait dessus le « prochain paiement »… mais
**aucun service ne créait jamais de ligne**. Conséquence : P10 était inatteignable
autrement qu'en écrivant à la main en base, et « prochain paiement » valait
toujours `null` pour l'investisseur. Ce module est ce producteur, et il rend au
passage B12 consommable : la ventilation capital / rendement d'un encaissement se
LIT sur l'échéancier, elle ne se devine pas depuis un total.

Discipline reprise de `credits/echeancier.py` (référence du projet, lue et non
recopiée) :

  - `Decimal` partout, `float` nulle part ;
  - quantize explicite à 0,01 / `ROUND_HALF_UP` sur chaque montant produit ;
  - **dernière ligne ajustée au solde exact** : Σ capital = capital décaissé au
    centime (CRD final rigoureusement nul) et Σ coupons = intérêt total au centime,
    quels que soient les arrondis intermédiaires ;
  - paramètres inexploitables → refus argumenté (`code` + message), jamais
    d'échéancier « best effort ».

**Ce qui n'est PAS repris, et pourquoi.** `credits/echeancier.py` amortit le
capital par tranches constantes avec intérêts dégressifs sur le CRD : c'est la
mécanique d'un PRÊT. Une offre d'investissement AGRICAP est un TITRE, et ce module
en applique la convention déjà écrite ailleurs dans l'app plutôt que d'en inventer
une seconde (principe 6) :

  - le coupon vient de `obligations.coupon_periodique` — intérêt simple sur le
    nominal, seule convention de coupon du module, déjà figée dans
    `ObligationPosition.coupon_amount` et déjà servie aux investisseurs ;
  - le capital est remboursé **in fine**, ce qui est la conséquence de la
    précédente : un coupon constant sur le nominal et un capital amorti en cours
    de route seraient deux conventions contradictoires pour le même titre. C'est
    aussi l'hypothèse de `metrics._valuation` (« dette saine valorisée au pair,
    intérêts courus au taux de coupon figé à la souscription »).

**`Kind.BULLET` reste inutilisé, volontairement.** Une ligne unique « capital +
intérêts » serait inventilable pour B12 : le schéma comptable ventile entre le
cantonnement 419-OFF (capital) et le compte 719 (rendement), et une ligne de
nature mixte obligerait le consommateur à répartir au jugé — exactement ce qu'il
refuse de faire. Une offre in fine produit donc DEUX lignes à la même date, une
COUPON et une CAPITAL, chacune de nature unique.

Cas chiffré (test `EcheancierRetourTests`) : 9 000 USD à 9 %, trimestriel,
24 mois → coupon annuel 810,00 ; 8 coupons de 202,50 ; capital 9 000,00 à
24 mois ; total encaissable 10 620,00 ; CRD final 0,00.
"""
from __future__ import annotations

import calendar
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from django.db import transaction

from audit.services import record as audit_record
from common.exceptions import BusinessError

from .models import Offer, Project, RepaymentSchedule

CENT = Decimal("0.01")
ZERO = Decimal("0.00")

#: Natures de ligne qui rendent du CAPITAL (crédit 419-OFF en B12). `BULLET` y
#: figure pour les lignes héritées d'une saisie manuelle antérieure à ce module :
#: une ligne « in fine » est du capital, jamais un produit.
KINDS_CAPITAL = (RepaymentSchedule.Kind.CAPITAL, RepaymentSchedule.Kind.BULLET)
#: Natures de ligne qui rendent du RENDEMENT (crédit 719 en B12).
KINDS_RENDEMENT = (RepaymentSchedule.Kind.COUPON,)


def q2(value) -> Decimal:
    """Quantize monétaire unique du module (identique à `credits.echeancier.q2`)."""
    return Decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


class EcheancierRetourError(BusinessError):
    """Termes d'offre inexploitables : on refuse, on n'invente pas d'échéancier.

    Même contrat `{code, message}` que `workflow.WorkflowError` et
    `obligations.ObligationError` : le `code` est consommé par le front, le message
    explique sans se parser.
    """

    code = "RETURN_SCHEDULE_ERROR"
    http_status = 422

    def as_errors(self) -> list[dict]:
        return [{"code": self.code, "message": self.message}]


class EcheancierDejaGenere(EcheancierRetourError):
    """Un échéancier de retour existe déjà : il ne se régénère pas en silence."""

    code = "RETURN_SCHEDULE_ALREADY_EXISTS"
    http_status = 409


def _ajouter_mois(depart: date, mois: int) -> date:
    """`depart` décalé de `mois` mois, jour tronqué à la fin du mois cible.

    Dette croisée assumée : `portfolio/schedule.py` et `credits/disbursement.py`
    portent chacun leur copie de ce calcul de calendrier. En importer une
    coupleraient `investments` au cycle de vie d'un prêt (`portfolio`) pour trois
    lignes de `calendar` ; le vrai correctif est un utilitaire commun, hors du
    périmètre de ce module. Signalé plutôt que contourné.
    """
    total = depart.month - 1 + mois
    annee = depart.year + total // 12
    mois_cible = total % 12 + 1
    return date(annee, mois_cible, min(depart.day, calendar.monthrange(annee, mois_cible)[1]))


def _periodes(frequence: str, maturite_mois: int) -> list[tuple[int, int]]:
    """Découpage de la maturité en périodes de coupon : `(mois d'échéance, durée)`.

    Une maturité qui n'est pas un multiple de la période produit une période
    « brisée » (stub) en fin de vie : 10 mois en trimestriel = 3 + 3 + 3 + 1, et le
    dernier coupon est calculé prorata temporis sur son mois unique. On ne rallonge
    pas le titre pour arrondir sa durée.
    """
    from .obligations import PERIODES_PAR_AN

    if frequence == Offer.Frequency.BULLET:
        return [(maturite_mois, maturite_mois)]
    par_an = PERIODES_PAR_AN.get(frequence)
    if not par_an:
        raise EcheancierRetourError(
            f"Fréquence de paiement inconnue sur l'offre : « {frequence} ». "
            f"Valeurs admises : {', '.join(Offer.Frequency.values)}."
        )
    pas = 12 // par_an
    pleines = maturite_mois // pas
    reste = maturite_mois % pas
    periodes = [((i + 1) * pas, pas) for i in range(pleines)]
    if reste:
        periodes.append((maturite_mois, reste))
    if not periodes:
        # Maturité plus courte qu'une période de coupon : un seul coupon prorata
        # temporis à l'échéance, plutôt qu'un titre sans aucun coupon.
        periodes = [(maturite_mois, maturite_mois)]
    return periodes


def construire_echeancier_retour(
    *,
    capital,
    taux_annuel,
    frequence: str,
    maturite_mois: int,
    date_depart: date,
) -> list[dict]:
    """Lignes de l'échéancier de retour d'une offre — aucun accès base.

    Args:
        capital: nominal effectivement décaissé sur l'offre (base du coupon ET
            montant à rendre au cantonnement).
        taux_annuel: `Offer.coupon_rate`, en points de pourcentage (9 = 9 %/an).
        frequence: `Offer.payment_frequency`.
        maturite_mois: `Offer.maturity_months`, durée totale du titre.
        date_depart: date de décaissement — l'obligation de retour du promoteur
            naît le jour où il reçoit l'argent, pas le jour de la souscription.

    Returns:
        Une ligne par flux : `mois`, `due_date`, `kind`, `montant`, `crd` (capital
        restant dû APRÈS la ligne). Ordre chronologique, coupon avant capital à
        date égale.
    """
    from .obligations import coupon_periodique

    capital = q2(_dec(capital, "capital"))
    taux_annuel = _dec(taux_annuel, "taux_annuel")

    if capital <= ZERO:
        raise EcheancierRetourError("Le capital de l'échéancier doit être strictement positif.")
    if taux_annuel < 0:
        raise EcheancierRetourError("Le taux de coupon d'une offre ne peut pas être négatif.")
    if maturite_mois <= 0:
        raise EcheancierRetourError(
            f"Maturité inexploitable ({maturite_mois} mois) : sans durée, l'offre ne "
            "porte aucune date de retour et l'échéancier ne peut pas être construit. "
            "Corrigez les termes de l'offre avant de décaisser."
        )

    # Intérêt TOTAL de la vie du titre — intérêt simple sur le nominal, convention
    # `obligations.coupon_periodique` (branche in fine : annuel × mois / 12).
    total_interets = coupon_periodique(
        montant=capital, taux_annuel=taux_annuel, frequence=Offer.Frequency.BULLET,
        maturite_mois=maturite_mois,
    )

    lignes: list[dict] = []
    reste_interets = total_interets
    periodes = _periodes(frequence, maturite_mois)
    for index, (mois, _duree) in enumerate(periodes):
        if index == len(periodes) - 1:
            # Dernier coupon ajusté au solde exact : Σ coupons = intérêt total au
            # centime, quels que soient les arrondis des périodes précédentes.
            # C'est aussi la branche du stub de fin de vie et celle de l'in fine,
            # qui n'ont pas d'autre coupon que ce solde.
            coupon = reste_interets
        else:
            # Toutes les périodes non finales sont des périodes PLEINES : le stub
            # éventuel est construit en dernier par `_periodes`.
            coupon = min(coupon_periodique(
                montant=capital, taux_annuel=taux_annuel, frequence=frequence,
                maturite_mois=maturite_mois,
            ), reste_interets)
        reste_interets = q2(reste_interets - coupon)
        if coupon > ZERO:
            # Une ligne à zéro n'est pas une échéance : une offre à 0 % ne produit
            # aucun coupon, elle ne produit pas des coupons nuls.
            lignes.append({
                "mois": mois, "due_date": _ajouter_mois(date_depart, mois),
                "kind": RepaymentSchedule.Kind.COUPON, "montant": q2(coupon),
                "crd": capital,
            })

    lignes.append({
        "mois": maturite_mois, "due_date": _ajouter_mois(date_depart, maturite_mois),
        "kind": RepaymentSchedule.Kind.CAPITAL, "montant": capital, "crd": ZERO,
    })
    lignes.sort(key=lambda l: (l["due_date"], l["kind"] == RepaymentSchedule.Kind.CAPITAL))
    return lignes


def _dec(value, nom: str) -> Decimal:
    try:
        return Decimal(str(value))
    except Exception as exc:  # noqa: BLE001 — remonté en erreur métier explicite
        raise EcheancierRetourError(f"{nom} n'est pas un nombre : {value!r}") from exc


def totaux(lignes: list[dict]) -> dict[str, Decimal]:
    """Grandeurs de contrôle : capital rendu, rendement servi, CRD final."""
    capital = q2(sum((l["montant"] for l in lignes
                      if l["kind"] in KINDS_CAPITAL), ZERO))
    rendement = q2(sum((l["montant"] for l in lignes
                        if l["kind"] in KINDS_RENDEMENT), ZERO))
    return {
        "capital": capital,
        "rendement": rendement,
        "total": q2(capital + rendement),
        "crd_final": lignes[-1]["crd"] if lignes else ZERO,
        "nb_echeances": len(lignes),
    }


# ── Production en base ────────────────────────────────────────────────────────

def _repartition(offres: list[Offer], base_total: Decimal) -> list[tuple[Offer, Decimal]]:
    """Répartit le décaissement entre les offres du projet, au prorata de l'ENCAISSÉ.

    Le capital à rendre à chaque cantonnement 419-OFF est la part que ce
    cantonnement a réellement financée (ségrégation des fonds, principe 9). La
    dernière offre absorbe le reliquat pour que Σ des parts = montant décaissé au
    centime — même discipline que la dernière échéance ajustée au solde.
    """
    finances = [o for o in offres if Decimal(o.funded_amount) > ZERO]
    if not finances:
        raise EcheancierRetourError(
            "Aucune offre de ce projet n'a encaissé de souscription : il n'existe "
            "aucun cantonnement auquel rendre le capital décaissé."
        )
    base = sum((Decimal(o.funded_amount) for o in finances), ZERO)
    parts: list[tuple[Offer, Decimal]] = []
    cumul = ZERO
    for offre in finances[:-1]:
        part = q2(base_total * Decimal(offre.funded_amount) / base)
        parts.append((offre, part))
        cumul += part
    parts.append((finances[-1], q2(base_total - cumul)))
    return [(o, p) for o, p in parts if p > ZERO]


@transaction.atomic
def generer_pour_projet(*, project: Project, base_total, date_depart: date,
                        by: str = "") -> list[RepaymentSchedule]:
    """Crée l'échéancier de retour des offres d'un projet décaissé.

    Appelé par `funding.disburse`, DANS sa transaction : un décaissement sans
    échéancier de retour laisserait un projet incapable d'atteindre P10 et un
    encaissement B12 inventilable. Si les termes d'une offre ne permettent pas de
    construire l'échéancier, le décaissement échoue avec le motif — refuser est
    conforme, inventer ne l'est pas.

    L'échéancier ne se régénère pas : un second appel est un conflit, pas une
    mise à jour silencieuse (les échéances déjà servies à l'investisseur ne
    changent pas sous ses pieds).
    """
    montant = q2(_dec(base_total, "montant décaissé"))
    if montant <= ZERO:
        raise EcheancierRetourError("Le montant décaissé doit être positif.")

    offres = list(project.offers.order_by("pk"))
    if RepaymentSchedule.objects.filter(offer__in=offres).exists():
        raise EcheancierDejaGenere(
            f"Le projet {project.code} porte déjà un échéancier de retour : il n'est "
            "pas régénéré. Une échéance annoncée à un investisseur ne se réécrit pas."
        )

    creees: list[RepaymentSchedule] = []
    for offre, part in _repartition(offres, montant):
        lignes = construire_echeancier_retour(
            capital=part, taux_annuel=offre.coupon_rate, frequence=offre.payment_frequency,
            maturite_mois=offre.maturity_months, date_depart=date_depart,
        )
        creees.extend(RepaymentSchedule.objects.bulk_create([
            RepaymentSchedule(offer=offre, due_date=l["due_date"], amount_due=l["montant"],
                              kind=l["kind"], status=RepaymentSchedule.Status.PENDING)
            for l in lignes
        ]))
        controle = totaux(lignes)
        audit_record(
            actor=by, action="investments.repayment_schedule.generate", entity_type="Offer",
            entity_id=offre.code,
            details={"projectCode": project.code, "capital": str(controle["capital"]),
                     "rendement": str(controle["rendement"]),
                     "total": str(controle["total"]), "crdFinal": str(controle["crd_final"]),
                     "echeances": controle["nb_echeances"],
                     "couponRate": str(offre.coupon_rate),
                     "frequency": offre.payment_frequency,
                     "maturityMonths": offre.maturity_months,
                     "startDate": date_depart.isoformat()},
        )
    return creees


# ── Ventilation d'un encaissement (B12) ───────────────────────────────────────

def _lignes_ouvertes(project: Project):
    """Échéances non soldées du projet, dans l'ordre où elles sont dues.

    `select_for_update` : deux encaissements concurrents ne peuvent pas imputer
    deux fois la même échéance (même parti pris que le verrou de gage du module
    crédit).
    """
    return list(
        RepaymentSchedule.objects.select_for_update()
        .filter(offer__project=project)
        .exclude(status__in=(RepaymentSchedule.Status.PAID,
                             RepaymentSchedule.Status.CANCELLED))
        .order_by("due_date", "kind", "pk")
    )


def ventiler_retour(*, project: Project, montant, movement=None, imputer: bool = True) -> dict:
    """Ventile un encaissement de retour entre CAPITAL et RENDEMENT, selon l'échéancier.

    C'est la donnée que le schéma comptable B12 attend et qu'un total ne porte pas :
    « 501 → 419-OFF + 719, selon l'échéancier ». La répartition est le résultat
    d'une IMPUTATION déterministe, jamais d'une clé de répartition devinée :

    1. les échéances ouvertes sont servies dans l'ordre de leur date d'exigibilité
       (la plus ancienne d'abord), une échéance pouvant être partiellement servie ;
    2. chaque échéance porte une nature UNIQUE — COUPON → rendement (719),
       CAPITAL/BULLET → capital (419-OFF) ;
    3. le reliquat éventuel, une fois tout l'échéancier soldé, est un produit :
       le principal dû est déjà intégralement imputé, ce qui arrive en plus n'en
       est pas. Il est signalé (`surplus`), jamais fondu dans le capital.

    **Sans échéancier, pas de ventilation.** Le retour d'un projet décaissé avant
    l'existence de ce producteur ne porte aucune répartition connue : on renvoie
    `disponible=False` avec son motif, et l'événement comptable reste en file
    plutôt que de fabriquer un produit 719 qui n'a jamais existé.

    Args:
        imputer: `False` pour simuler sans rien écrire (lecture d'un plan).
    """
    total = q2(_dec(montant, "montant encaissé"))
    if total <= ZERO:
        raise EcheancierRetourError("Le montant à ventiler doit être positif.")

    lignes = _lignes_ouvertes(project)
    if not lignes and not RepaymentSchedule.objects.filter(offer__project=project).exists():
        return {
            "disponible": False,
            "retour_total": total,
            "capital_rembourse": ZERO,
            "rendement": ZERO,
            "surplus": ZERO,
            "lignes_imputees": [],
            "motif": (
                f"Aucun échéancier de retour n'est enregistré sur les offres du projet "
                f"{project.code} : la ventilation capital / rendement de cet encaissement "
                "ne peut pas être établie et n'est pas devinée."
            ),
        }

    capital = ZERO
    rendement = ZERO
    reste = total
    imputees: list[dict] = []
    for ligne in lignes:
        if reste <= ZERO:
            break
        du = q2(Decimal(ligne.amount_due) - Decimal(ligne.amount_paid))
        if du <= ZERO:
            continue
        part = du if du <= reste else reste
        reste = q2(reste - part)
        if ligne.kind in KINDS_CAPITAL:
            capital = q2(capital + part)
        else:
            rendement = q2(rendement + part)
        if imputer:
            ligne.amount_paid = q2(Decimal(ligne.amount_paid) + part)
            champs = ["amount_paid"]
            if ligne.amount_paid >= Decimal(ligne.amount_due):
                ligne.status = RepaymentSchedule.Status.PAID
                champs.append("status")
            if movement is not None:
                ligne.paid_movement = movement
                champs.append("paid_movement")
            ligne.save(update_fields=champs)
        imputees.append({"id": ligne.pk, "offer": ligne.offer_id, "kind": ligne.kind,
                         "dueDate": ligne.due_date.isoformat(), "montant": str(part)})

    surplus = reste
    if surplus > ZERO:
        rendement = q2(rendement + surplus)

    # Invariant dur du schéma B12 : la somme des deux jambes créditrices vaut
    # exactement le débit de trésorerie. Une pièce déséquilibrée est un rollback.
    if q2(capital + rendement) != total:
        raise EcheancierRetourError(
            f"Ventilation incohérente : capital {capital} + rendement {rendement} ≠ "
            f"encaissement {total}. Aucun événement n'est produit."
        )

    return {
        "disponible": True,
        "retour_total": total,
        "capital_rembourse": capital,
        "rendement": rendement,
        "surplus": surplus,
        "lignes_imputees": imputees,
        "motif": "",
    }
