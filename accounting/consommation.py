"""Consommateur de la file d'événements métier — le chaînon manquant entre le métier et
le grand livre.

Constat d'audit à l'origine de ce module : `investments` produit depuis toujours des
`InvestmentEvent` (append-only, avec `consumed_at` / `journal_reference` prévus pour un
consommateur) et **rien ne les lisait**. Le moteur d'écritures existait, le catalogue
existait, la file existait — mais aucun événement réel n'atteignait la comptabilité.

Ce module est ce consommateur. Il ne réécrit AUCUNE mécanique existante :

    investments.InvestmentEvent  ──►  RegleConsommation (mapping EN BASE)
                                          │
                                          ▼
                              catalogue.executer_evenement  (annexe B)
                                          │
                                          ▼
                               services.enregistrer_piece  (invariant par devise)
                                          │
                                          ▼
                    PieceComptable  +  evenement.consumed_at / journal_reference
                                     (dans LA MÊME transaction, jamais l'un sans l'autre)

Quatre règles de conduite, qui expliquent la forme du code :

1. **Idempotence stricte.** `consumed_at` est la garantie : le marquage est un
   compare-and-set (`filter(consumed_at__isnull=True).update(...)`) exécuté DANS la
   transaction qui crée la pièce. Deux consommateurs concurrents ne peuvent pas produire
   deux pièces pour un même événement ; la référence de pièce, déterministe et unique,
   ferme la porte au niveau base.

2. **Un échec est local.** Le lot n'est pas transactionnel : chaque événement a sa propre
   transaction. Un événement qui échoue reste NON consommé (donc rejouable au prochain
   passage) et n'empêche pas les suivants d'entrer au grand livre.

3. **On n'invente jamais une écriture.** Un type d'événement sans schéma dans l'annexe B
   (`SANS_ECRITURE`), ou dont le schéma exige une ventilation que l'événement ne porte
   pas (B12 : capital / rendement « selon l'échéancier »), reste en file et ressort dans
   le rapport. Une écriture comptable fausse est bien pire qu'une écriture absente.

4. **Le code exécute, le paramétrage décide.** Aucun `if event_type == "…"` ni
   `if source == "…"` ici : la file à lire vit dans `SourceEvenements`, le mapping
   « événement → schéma » dans `RegleConsommation`, et TOUT ce qu'un schéma réclame — ses
   montants (`montant_ref`) comme ses comptes à trancher (`$TRESORERIE`, `$CANTONNEMENT`)
   — se lit sur le schéma en base. Brancher une nouvelle file métier est un geste de
   paramétrage, pas un déploiement.

Ce que le consommateur exige d'une file productrice (le CONTRAT, et rien de plus) :
`event_type`, `amount` (> 0, jamais signé — le sens vient du schéma), `currency`,
`occurred_at` (aware), `payload`, `consumed_at`, `journal_reference`. Les champs
supplémentaires (`segregation_account`, `offer`, `subscription`) ne sont lus QUE si le
schéma appliqué les réclame : une file crédit ou épargne n'a pas à les porter.

Le consommateur s'expose en commande de management (`manage.py consume_investment_events`),
jamais en signal : une écriture comptable doit être déclenchée à une heure connue, par un
acteur connu, et pouvoir être rejouée à l'identique.
"""
from __future__ import annotations

from datetime import date as date_cls
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from common.exceptions import BusinessError, ConflictError, NotFoundError, ValidationFailed

from . import catalogue, services
from .definitions import (
    PLACEHOLDER_CANTONNEMENT,
    PLACEHOLDER_TRESORERIE,
    SOURCE_INVESTISSEMENT,
)
from .models import (
    Devise,
    EventEntryTemplate,
    EventEntryTemplateLine,
    PieceComptable,
    RegleConsommation,
    SourceEvenements,
)

#: `investments` cote en « USD » ; `portfolio` dit « CDF » là où l'annexe A dit « FC ».
#: La traduction est explicite et centralisée — jamais devinée ligne à ligne.
DEVISE_EVENEMENT: dict[str, str] = {"USD": Devise.USD, "FC": Devise.FC, "CDF": Devise.FC}

#: Convention de nommage des sous-comptes de cantonnement (annexe A).
PREFIXE_CANTONNEMENT = "419-OFF-"

#: Clé de payload par laquelle un événement peut désigner son compte de trésorerie réel
#: (mobile money, banque, caisse) quand il le connaît — sinon la règle en base tranche.
CLE_TRESORERIE = "compteTresorerie"


class EvenementNonConsommable(BusinessError):
    """Cet événement ne peut pas produire d'écriture MAINTENANT — il reste en file."""

    code = "evenement_non_consommable"
    http_status = 409


class SansEcritureDefinie(EvenementNonConsommable):
    """Aucune écriture n'est DÉFINIE pour cet événement (annexe B muette, ou ventilation
    absente). Ce n'est pas une panne : c'est une dette de spécification, et elle doit
    rester visible plutôt que d'être comblée par une écriture inventée."""

    code = "sans_ecriture_definie"


class DejaConsomme(EvenementNonConsommable):
    """L'événement porte déjà un `consumed_at` : il ne sera jamais rejoué."""

    code = "evenement_deja_consomme"


# --------------------------------------------------------------------------- FILE

#: Champs qu'une file productrice DOIT exposer. Contrôlés à la résolution de la source,
#: une fois, avec un message qui nomme ce qui manque — plutôt qu'un `AttributeError` au
#: milieu d'un lot, sur le premier événement venu.
CONTRAT_FILE = (
    "event_type", "amount", "currency", "occurred_at", "payload",
    "consumed_at", "journal_reference",
)


def source_de(source: str) -> SourceEvenements:
    """Déclaration en base de la file à lire — jamais un nom de modèle en dur.

    Une file inconnue n'est pas une panne du consommateur : c'est un paramétrage qui reste
    à poser. Le message le dit, et dit comment.
    """
    declaration = SourceEvenements.objects.filter(code=source).first()
    if declaration is None:
        raise SansEcritureDefinie(
            f"La file « {source} » n'est pas déclarée en comptabilité : aucun événement n'en "
            "est lu. Déclarez-la (`manage.py parametrer_consommation source --code "
            f"{source} --prefixe …`) — le consommateur ne devine pas quelle table lire."
        )
    if not declaration.actif:
        raise SansEcritureDefinie(
            f"La file « {source} » est désactivée : ses événements s'accumulent, "
            "aucun n'est écrit."
        )
    return declaration


def _modele_evenement(source: str = SOURCE_INVESTISSEMENT):
    """Modèle de file, résolu depuis sa DÉCLARATION en base.

    Import différé et résolution par nom : `accounting` ne dépend au chargement d'aucune app
    productrice (et n'écrit dans leurs modèles que les deux champs de file `consumed_at` /
    `journal_reference`, que chaque producteur déclare explicitement comme étant les nôtres).
    """
    from django.apps import apps

    chemin = source_de(source).chemin_modele
    try:
        modele = apps.get_model(chemin)
    except (LookupError, ValueError) as exc:
        raise SansEcritureDefinie(
            f"La file « {source} » désigne le modèle « {chemin} », que Django ne connaît "
            "pas. Corrigez la déclaration (`SourceEvenements.modele`) : la comptabilité ne "
            "lit que des files qui existent."
        ) from exc

    champs = {champ.name for champ in modele._meta.get_fields()}
    absents = [nom for nom in CONTRAT_FILE if nom not in champs]
    if absents:
        raise SansEcritureDefinie(
            f"La file « {chemin} » ne respecte pas le contrat de consommation : "
            f"{', '.join(absents)} manque(nt). Une file lisible porte {', '.join(CONTRAT_FILE)} "
            "— « consumed_at » et « journal_reference » étant écrits par la comptabilité et "
            "par elle seule."
        )
    return modele


def montants_en_attente(
    *, source: str = "", jusqu_au: date_cls | None = None,
) -> dict[str, Decimal]:
    """Σ des montants NON consommés, par devise du plan comptable.

    C'est le CHIFFRE de ce que le grand livre ne dit pas encore. Un compteur d'événements
    ne suffit pas : « 412 événements en attente » ne se compare à rien, « 128 400 USD en
    attente » se compare au bilan. Tant qu'un schéma manque (B8/B9 : le compte de dette de
    portefeuille n'existe pas à l'annexe A), cette somme EST l'écart connu des états
    financiers — une omission chiffrée, donc arbitrable, au lieu d'une omission abstraite.

    Sans `source`, toutes les files déclarées ET actives sont additionnées. Une file dont le
    modèle est introuvable est ignorée plutôt que fatale : ce contrôle sert à informer un
    état financier, il ne doit jamais l'empêcher de s'afficher.
    """
    totaux, _ = _attente_par_devise(source=source, jusqu_au=jusqu_au)
    return totaux


def montants_en_devise_inconnue(
    *, source: str = "", jusqu_au: date_cls | None = None,
) -> dict[str, Decimal]:
    """Montants en attente dont la devise n'est PAS traduisible en plan comptable.

    Ils ne peuvent pas entrer dans l'écart chiffré — on ne sait pas dans quelle colonne les
    additionner — mais les écarter en silence les rendrait invisibles deux fois : absents du
    grand livre ET absents de la mesure de ce qui manque au grand livre. Ils sortent donc
    séparément, sous leur code brut, pour que l'avertissement puisse les nommer.

    Un tel événement ne se consommera jamais tout seul : il faut corriger la devise à la
    source. C'est un incident, pas une file qui attend son tour.
    """
    _, inconnues = _attente_par_devise(source=source, jusqu_au=jusqu_au)
    return inconnues


def _attente_par_devise(
    *, source: str = "", jusqu_au: date_cls | None = None,
) -> tuple[dict[str, Decimal], dict[str, Decimal]]:
    """(montants traduisibles par devise comptable, montants par devise NON traduisible)."""
    codes = [source] if source else list(
        SourceEvenements.objects.filter(actif=True).values_list("code", flat=True)
    )
    totaux: dict[str, Decimal] = {}
    inconnues: dict[str, Decimal] = {}
    for code in codes:
        try:
            qs = evenements_en_attente(source=code, jusqu_au=jusqu_au)
        except EvenementNonConsommable:
            continue
        for devise_brute, montant in qs.values_list("currency", "amount"):
            if montant is None:
                continue
            brute = (devise_brute or "").upper()
            devise = DEVISE_EVENEMENT.get(brute)
            cible = totaux if devise is not None else inconnues
            cle = devise if devise is not None else (brute or "(vide)")
            cible[cle] = cible.get(cle, Decimal("0.00")) + services.q2(montant)
    return (
        {d: services.q2(t) for d, t in sorted(totaux.items())},
        {d: services.q2(t) for d, t in sorted(inconnues.items())},
    )


def evenements_en_attente(
    *,
    jusqu_au: date_cls | None = None,
    types: list[str] | None = None,
    source: str = SOURCE_INVESTISSEMENT,
):
    """File des événements non consommés, dans l'ordre où ils se sont produits.

    L'ordre chronologique n'est pas cosmétique : la contrepassation d'un remboursement de
    souscription suppose que l'encaissement correspondant ait DÉJÀ été consommé.
    """
    qs = _modele_evenement(source).objects.filter(consumed_at__isnull=True)
    if jusqu_au:
        qs = qs.filter(occurred_at__date__lte=jusqu_au)
    if types:
        qs = qs.filter(event_type__in=types)
    return qs.order_by("occurred_at", "id")


# --------------------------------------------------------------------------- RÈGLES

def regle_pour(type_evenement: str, *, source: str = SOURCE_INVESTISSEMENT) -> RegleConsommation:
    """Règle applicable, ou `SansEcritureDefinie` — jamais un mapping par défaut implicite."""
    regle = RegleConsommation.objects.filter(source=source, type_evenement=type_evenement).first()
    if regle is None:
        raise SansEcritureDefinie(
            f"Aucune règle de consommation pour « {type_evenement} » ({source}) : "
            "l'événement reste en file. Paramétrez-la (`RegleConsommation`) ou chargez "
            "les règles d'amorce avec « manage.py seed_accounting »."
        )
    if not regle.actif:
        raise SansEcritureDefinie(
            f"La règle de consommation de « {type_evenement} » est désactivée : "
            "l'événement reste en file."
        )
    if regle.mode == RegleConsommation.Mode.SANS_ECRITURE:
        raise SansEcritureDefinie(
            f"Aucune écriture définie pour « {type_evenement} ». {regle.note}".strip()
        )
    return regle


# --------------------------------------------------------------------- CONTEXTE

def devise_de(evenement) -> str:
    devise = DEVISE_EVENEMENT.get((evenement.currency or "").upper())
    if devise is None:
        raise EvenementNonConsommable(
            f"Devise « {evenement.currency} » inconnue du plan comptable "
            f"(attendu : {', '.join(sorted(DEVISE_EVENEMENT))}). Aucune écriture n'est "
            "passée sur une devise qu'on ne sait pas nommer."
        )
    return devise


def date_operation_de(evenement) -> date_cls:
    """Date COMPTABLE de l'événement = sa date de survenance, dans le fuseau de
    l'institution. Jamais la date du jour où le consommateur tourne : une écriture porte
    la date de l'opération, pas celle du traitement par lots."""
    return timezone.localdate(evenement.occurred_at)


def _cantonnement(evenement) -> str:
    """Racine du sous-compte de cantonnement de l'offre, dont l'ouverture est une
    responsabilité COMPTABLE (`investments` nomme le compte, il ne le crée pas).

    L'ouverture est idempotente et sans maker-checker : un sous-compte 419-OFF n'étend
    pas le plan comptable, il en décline mécaniquement le compte 419 par offre. C'est
    l'invariant de ségrégation du principe 9 — prouver que l'argent de l'offre X n'a pas
    financé le projet Y.

    Les deux champs lus (`segregation_account`, `offer`) ne font PAS partie du contrat de
    file : ils ne sont demandés qu'aux événements dont le schéma réclame `$CANTONNEMENT`
    (B10→B13). Une file crédit ou épargne n'en porte pas, et n'a pas à en porter.
    """
    reference = (getattr(evenement, "segregation_account", "") or "").strip()
    if not reference and getattr(evenement, "offer_id", None):
        reference = f"{PREFIXE_CANTONNEMENT}{evenement.offer.code}"
    if not reference:
        raise EvenementNonConsommable(
            "Événement sans compte de cantonnement ni offre : impossible de savoir à "
            "quelle levée rattacher l'argent. Aucune écriture n'est passée sans "
            "ségrégation (principe 9)."
        )
    if not reference.startswith(PREFIXE_CANTONNEMENT):
        raise EvenementNonConsommable(
            f"Compte de cantonnement « {reference} » hors convention de l'annexe A "
            f"({PREFIXE_CANTONNEMENT}xxxx)."
        )
    services.creer_sous_compte_cantonnement(offre_ref=reference[len(PREFIXE_CANTONNEMENT):])
    return reference


def _lignes_de_schema(code_schema: str) -> list[EventEntryTemplateLine]:
    """Lignes INCONDITIONNELLES d'un schéma, lues en base.

    Les lignes conditionnelles (B16 : GAIN / PERTE) sont écartées : elles décrivent des
    variantes exclusives, dont une seule s'applique — les compter dans l'équation
    d'équilibre du schéma n'aurait pas de sens.
    """
    template = EventEntryTemplate.objects.filter(code=code_schema, actif=True).first()
    if template is None:
        raise NotFoundError(
            f"Schéma « {code_schema} » introuvable ou inactif : la règle de consommation "
            "désigne un schéma que le catalogue ne contient pas."
        )
    lignes = list(template.lignes.filter(condition=""))
    if not lignes:
        raise NotFoundError(f"Le schéma « {code_schema} » ne porte aucune ligne applicable.")
    return lignes


def _references_de_montant(code_schema: str) -> list[str]:
    """Montants attendus par un schéma, LUS EN BASE (ordre du schéma, sans doublon).

    C'est ce qui évite un dictionnaire `{"B10": "souscription", …}` en dur : ajouter un
    schéma au catalogue suffit à le rendre consommable, sans toucher ce fichier.
    """
    references: list[str] = []
    for ligne in _lignes_de_schema(code_schema):
        if ligne.montant_ref not in references:
            references.append(ligne.montant_ref)
    return references


def placeholders_de(code_schema: str) -> list[str]:
    """Placeholders de compte (`$…`) que CE schéma exige, lus en base.

    C'est ce qui rend le consommateur agnostique du schéma appliqué : B10→B13 réclament un
    cantonnement d'offre, B1→B4 (crédit) et B8/B9 (épargne) n'en réclament aucun. Résoudre
    systématiquement `$CANTONNEMENT` reviendrait à exiger une offre d'investissement d'un
    remboursement de crédit — le mapping en base ne servirait plus à rien.
    """
    references: list[str] = []
    for ligne in _lignes_de_schema(code_schema):
        if ligne.compte_racine.startswith("$") and ligne.compte_racine not in references:
            references.append(ligne.compte_racine)
    return references


def _deduire_montant_manquant(
    code_schema: str, connus: dict[str, Decimal], manquants: list[str],
) -> Decimal | None:
    """Valeur FORCÉE d'un montant absent du payload, par l'équation d'équilibre du schéma.

    Ce n'est pas une ventilation devinée, c'est de l'arithmétique : dans B12
    (débit `retour_total` = crédit `capital_rembourse` + crédit `rendement`), connaître deux
    des trois termes détermine le troisième au centime. Le résultat est ensuite CONFRONTÉ au
    montant réel de l'événement par `_controler_montant` — une déduction fausse ne peut donc
    pas entrer au grand livre.

    Trois garde-fous, et ils sont la raison d'être de cette fonction :

    * **un seul inconnu** — deux inconnus (le cas « je connais le total, pas la ventilation »)
      restent un refus : c'est précisément la répartition capital / rendement qu'on ne devine
      jamais, sous peine de fabriquer un produit financier 719 qui n'a pas existé ;
    * **l'inconnu n'apparaît qu'une fois** dans le schéma — sinon l'équation est ambiguë ;
    * **le résultat est strictement positif** — un montant nul ou négatif signale une
      ventilation incohérente, pas une écriture à passer.
    """
    if len(manquants) != 1:
        return None
    inconnu = manquants[0]

    lignes = _lignes_de_schema(code_schema)
    if sum(1 for ligne in lignes if ligne.montant_ref == inconnu) != 1:
        return None

    au_debit = None
    somme = {EventEntryTemplateLine.Sens.DEBIT: Decimal("0.00"),
             EventEntryTemplateLine.Sens.CREDIT: Decimal("0.00")}
    for ligne in lignes:
        if ligne.montant_ref == inconnu:
            au_debit = ligne.sens == EventEntryTemplateLine.Sens.DEBIT
            continue
        somme[ligne.sens] += connus[ligne.montant_ref]
    if au_debit is None:  # pragma: no cover - garanti par la construction de `manquants`
        return None

    autre_cote = somme[
        EventEntryTemplateLine.Sens.CREDIT if au_debit else EventEntryTemplateLine.Sens.DEBIT
    ]
    meme_cote = somme[
        EventEntryTemplateLine.Sens.DEBIT if au_debit else EventEntryTemplateLine.Sens.CREDIT
    ]
    valeur = services.q2(autre_cote - meme_cote)
    return valeur if valeur > 0 else None


def _valeur_de_payload(payload: dict, reference: str):
    """Cherche `capital_rembourse` puis `capitalRembourse` : `investments` sérialise en
    camelCase, le catalogue nomme en snake_case. On accepte les deux plutôt que d'imposer
    une convention à l'app productrice."""
    if reference in payload:
        return payload[reference]
    morceaux = reference.split("_")
    camel = morceaux[0] + "".join(m.title() for m in morceaux[1:])
    return payload.get(camel)


def montants_de(evenement, code_schema: str) -> dict[str, Decimal]:
    """Montants du contexte, dérivés du schéma.

    * Un seul montant attendu → c'est le montant de l'événement, sans ambiguïté.
    * Plusieurs montants attendus (B12 : `retour_total` / `capital_rembourse` /
      `rendement`) → ils viennent du payload de l'événement. La ventilation « selon
      l'échéancier » ne se déduit pas d'un total : la répartir au jugé fabriquerait un
      produit financier (719) qui n'a jamais existé.

      Un SEUL terme peut manquer, et seulement s'il est déterminé au centime par
      l'équation d'équilibre du schéma (cf. `_deduire_montant_manquant`) : c'est le cas du
      total redondant `retour_total`, que le producteur n'a aucune raison de recalculer.
      Deux termes manquants restent un refus.
    """
    montant = services.q2(evenement.amount)
    if montant <= 0:
        raise EvenementNonConsommable(
            f"Montant non exploitable ({montant}) : une écriture ne naît pas d'un montant "
            "nul ou négatif (le sens se porte par le schéma, jamais par le signe)."
        )

    references = _references_de_montant(code_schema)
    if len(references) == 1:
        return {references[0]: montant}

    payload = evenement.payload or {}
    valeurs: dict[str, Decimal] = {}
    manquants: list[str] = []
    for reference in references:
        brute = _valeur_de_payload(payload, reference)
        if brute is None:
            manquants.append(reference)
        else:
            valeurs[reference] = services.q2(brute)
    if manquants:
        deduit = _deduire_montant_manquant(code_schema, valeurs, manquants)
        if deduit is None:
            raise SansEcritureDefinie(
                f"Le schéma {code_schema} attend une ventilation que l'événement ne porte "
                f"pas (absent(s) de son payload : {', '.join(manquants)}). L'événement reste "
                "en file : la répartition capital / rendement d'un encaissement ne se devine "
                "pas depuis son total."
            )
        valeurs[manquants[0]] = deduit
    return valeurs


def planifier(evenement, *, source: str = SOURCE_INVESTISSEMENT) -> dict:
    """Ce que le consommateur FERAIT de cet événement — sans rien écrire.

    Sert au mode simulation de la commande (`--simulation`) : un comptable doit pouvoir
    lire le plan de consommation avant de le déclencher, exactement comme
    `provisions.analyser_portefeuille` précède `provisions.arreter`.
    """
    prefixe = source_de(source).prefixe_reference
    regle = regle_pour(evenement.event_type, source=source)
    devise = devise_de(evenement)
    date_operation = date_operation_de(evenement)

    if regle.mode == RegleConsommation.Mode.CONTREPASSATION:
        return {
            "regle": regle,
            "mode": regle.mode,
            "schema": "",
            "devise": devise,
            "date_operation": date_operation,
            "montant": services.q2(evenement.amount),
            "reference": f"{prefixe}-{date_operation:%Y%m%d}-CP-{evenement.pk}",
            "piece_origine": _piece_a_contrepasser(evenement, regle, source=source),
        }

    if not regle.schema:
        raise SansEcritureDefinie(
            f"La règle « {evenement.event_type} » est en mode {regle.mode} sans schéma : "
            "rien à appliquer."
        )

    comptes = _comptes_du_schema(evenement, regle)
    montants = montants_de(evenement, regle.schema)
    return {
        "regle": regle,
        "mode": regle.mode,
        "schema": regle.schema,
        "devise": devise,
        "date_operation": date_operation,
        "montant": services.q2(evenement.amount),
        "montants": montants,
        "comptes": comptes,
        "reference": (
            f"{prefixe}-{date_operation:%Y%m%d}-{regle.schema}-{evenement.pk}"
        ),
        "contexte": {"devise": devise, "montants": montants, "comptes": comptes},
    }


#: Comment se résout chaque placeholder de compte que l'annexe B laisse ouvert. Un
#: placeholder inconnu de cette table est un schéma qu'on ne sait pas exécuter — et on le
#: dit, plutôt que de passer une écriture amputée d'une contrepartie.
RESOLVEURS_DE_COMPTE = {
    PLACEHOLDER_TRESORERIE: lambda evenement, regle: (
        (evenement.payload or {}).get(CLE_TRESORERIE) or regle.compte_tresorerie
    ),
    PLACEHOLDER_CANTONNEMENT: lambda evenement, regle: _cantonnement(evenement),
}


def _comptes_du_schema(evenement, regle: RegleConsommation) -> dict[str, str]:
    """Résout LES SEULS placeholders que le schéma réclame — lus sur le schéma en base.

    C'est ce qui rend une file crédit ou épargne consommable sans rien changer ici : B2
    (« remboursement — capital ») ne demande qu'un compte de trésorerie, et exiger de lui
    un cantonnement d'offre — comme le faisait la version « investissement » de ce
    consommateur — condamnait toute autre file avant même son premier événement.
    """
    comptes: dict[str, str] = {}
    for placeholder in placeholders_de(regle.schema):
        resolveur = RESOLVEURS_DE_COMPTE.get(placeholder)
        if resolveur is None:
            raise SansEcritureDefinie(
                f"Le schéma {regle.schema} réclame le compte {placeholder}, que le "
                "consommateur ne sait pas résoudre depuis un événement métier. Aucune "
                "écriture n'est passée sur une contrepartie inconnue."
            )
        resolu = resolveur(evenement, regle)
        if not resolu:
            raise EvenementNonConsommable(
                f"Aucun compte {placeholder} pour « {evenement.event_type} » : l'annexe B "
                "laisse le choix (501/511/53x), il doit être tranché en base "
                "(`RegleConsommation.compte_tresorerie`) ou porté par l'événement "
                f"(« {CLE_TRESORERIE} » dans son payload)."
            )
        comptes[placeholder] = resolu
    return comptes


#: Champ par lequel une contrepassation retrouve l'événement d'origine, essayé dans cet
#: ordre : deux événements qui portent le MÊME rattachement métier décrivent le même
#: engagement (une souscription encaissée puis remboursée, un crédit décaissé puis annulé).
#: Aucune file ne doit tous les porter — la première clé présente fait foi.
CLES_DE_RATTACHEMENT = ("subscription_id", "loan_id", "account_id", "contract_id")


def _rattachement(evenement) -> tuple[str, object]:
    for cle in CLES_DE_RATTACHEMENT:
        valeur = getattr(evenement, cle, None)
        if valeur:
            return cle, valeur
    raise EvenementNonConsommable(
        "Contrepassation sans rattachement métier : impossible d'identifier la pièce à "
        f"annuler (aucun de {', '.join(CLES_DE_RATTACHEMENT)} n'est renseigné). On "
        "n'annule pas au hasard."
    )


def _piece_a_contrepasser(
    evenement, regle: RegleConsommation, *, source: str = SOURCE_INVESTISSEMENT,
) -> PieceComptable:
    """Retrouve la pièce RÉELLEMENT passée pour l'événement d'origine de la souscription.

    On ne reconstruit pas une écriture inverse à la main : on contrepasse la pièce qui
    existe, ce qui garantit que le remboursement annule exactement ce qui avait été
    encaissé, au centime et au compte près (annexe C, P13).
    """
    cle, valeur = _rattachement(evenement)
    origine = (
        _modele_evenement(source).objects
        .filter(**{"event_type": regle.evenement_origine, cle: valeur})
        .exclude(journal_reference="")
        .order_by("occurred_at", "id")
        .first()
    )
    if origine is None:
        raise EvenementNonConsommable(
            f"Aucun événement « {regle.evenement_origine} » consommé pour cette "
            "souscription : il n'y a rien à contrepasser (l'encaissement doit entrer au "
            "grand livre avant son remboursement). L'événement reste en file et sera "
            "repris au prochain passage."
        )
    piece = PieceComptable.objects.filter(reference=origine.journal_reference).first()
    if piece is None:
        raise EvenementNonConsommable(
            f"La pièce « {origine.journal_reference} » référencée par l'encaissement est "
            "introuvable : incident de données, à instruire avant toute contrepassation."
        )

    devise = devise_de(evenement)
    montant_piece = services.q2(
        sum((l.debit for l in piece.lignes.all() if l.devise == devise), Decimal("0.00"))
    )
    montant = services.q2(evenement.amount)
    if montant_piece != montant:
        raise EvenementNonConsommable(
            f"Remboursement de {montant} {devise} contre un encaissement de "
            f"{montant_piece} {devise} (pièce {piece.reference}) : une contrepassation "
            "annule la TOTALITÉ d'une pièce. Un remboursement partiel exige un schéma "
            "propre, à arbitrer — aucune écriture n'a été passée."
        )
    return piece


# ----------------------------------------------------------------- CONSOMMATION

def _controler_montant(piece: PieceComptable, plan: dict) -> None:
    """La pièce doit mouvementer EXACTEMENT le montant annoncé par l'événement.

    C'est le contrôle qui attrape une ventilation incohérente (B12 : un `retour_total`
    de payload qui ne serait pas le montant réellement encaissé). L'équilibre par devise
    est déjà garanti par le moteur ; ici on vérifie la fidélité au fait métier.
    """
    total = services.q2(
        sum((l.debit for l in piece.lignes.all() if l.devise == plan["devise"]), Decimal("0.00"))
    )
    if total != plan["montant"]:
        raise ValidationFailed(
            f"La pièce mouvementerait {total} {plan['devise']} alors que l'événement porte "
            f"{plan['montant']} {plan['devise']} : la ventilation fournie ne décrit pas le "
            "même fait. Aucune écriture n'est enregistrée."
        )


@transaction.atomic
def consommer_evenement(evenement, *, par: str = "", source: str = SOURCE_INVESTISSEMENT) -> PieceComptable:
    """Transforme UN événement en UNE pièce, et le marque — indivisiblement.

    « Pièce générée + événement marqué », jamais l'un sans l'autre : les deux vivent dans
    cette transaction. Si le marquage échoue (consommation concurrente), la pièce est
    annulée avec lui.
    """
    modele = _modele_evenement(source)
    evenement = modele.objects.select_for_update().get(pk=evenement.pk)
    if evenement.consumed_at is not None:
        raise DejaConsomme(
            f"Événement #{evenement.pk} déjà consommé le {evenement.consumed_at} "
            f"(pièce « {evenement.journal_reference} ») : aucun rejeu."
        )

    plan = planifier(evenement, source=source)
    libelle = _libelle(evenement, plan)

    if plan["mode"] == RegleConsommation.Mode.CONTREPASSATION:
        piece, _ = services.contrepasser_piece(
            plan["piece_origine"],
            motif=(
                f"Annulation de « {plan['regle'].evenement_origine} » — événement "
                f"{source}#{evenement.pk}. "
                f"{(evenement.payload or {}).get('reason', '')}".strip()
            ),
            par=par,
            reference_contrepassation=plan["reference"],
            date_operation=plan["date_operation"],
        )
    else:
        piece = _piece_existante(plan["reference"], evenement, source)
        if piece is None:
            piece = catalogue.executer_evenement(
                plan["schema"],
                plan["contexte"],
                reference=plan["reference"],
                date_operation=plan["date_operation"],
                libelle=libelle,
                origine_type=source,
                origine_id=str(evenement.pk),
                par=par,
            )
            _controler_montant(piece, plan)

    marques = modele.objects.filter(pk=evenement.pk, consumed_at__isnull=True).update(
        consumed_at=timezone.now(), journal_reference=piece.reference,
    )
    if marques != 1:
        raise ConflictError(
            f"Événement #{evenement.pk} consommé concurremment : la pièce vient d'être "
            "annulée pour ne pas créer de doublon."
        )
    _journaliser(evenement, piece, plan, par=par, source=source)
    return piece


def _piece_existante(reference: str, evenement, source: str) -> PieceComptable | None:
    """Reprise après incident : la pièce porte déjà la référence déterministe de cet
    événement, mais l'événement n'a pas été marqué. On ADOPTE la pièce (et on marque)
    plutôt que d'en créer une seconde — mais uniquement si elle provient BIEN de cet
    événement. Une homonymie de référence est un incident, pas une reprise.
    """
    piece = PieceComptable.objects.filter(reference=reference).first()
    if piece is None:
        return None
    if piece.origine_type == source and piece.origine_id == str(evenement.pk):
        return piece
    raise ConflictError(
        f"La pièce « {reference} » existe déjà et ne provient pas de l'événement "
        f"#{evenement.pk} (origine : {piece.origine_type}#{piece.origine_id}). "
        "Aucune écriture n'est passée."
    )


def _libelle(evenement, plan: dict) -> str:
    """Libellé lisible par un auditeur : ce qui s'est passé, et sur quel projet/offre.

    Le nom métier vient du CATALOGUE (le schéma sait s'appeler « Encaissement d'une
    souscription investisseur ») et non d'une table de traduction dans ce fichier.
    """
    payload = evenement.payload or {}
    contexte = payload.get("projectCode") or payload.get("offerCode") or ""
    intitule = evenement.event_type
    if plan.get("schema"):
        template = EventEntryTemplate.objects.filter(code=plan["schema"]).first()
        if template is not None:
            intitule = template.libelle
    return f"{intitule} — {contexte}" if contexte else intitule


def _journaliser(evenement, piece: PieceComptable, plan: dict, *, par: str, source: str) -> None:
    """Chaque consommation laisse une trace : quel événement, quelle pièce, quel schéma.

    L'audit ne doit jamais faire échouer une écriture correcte (même parti pris que
    `services._journaliser`).
    """
    try:
        from audit.services import record as audit_record
    except Exception:  # pragma: no cover - l'app audit est toujours installée en pratique
        return
    audit_record(
        actor=par,
        action="accounting.evenement_consomme",
        entity_type=source,
        entity_id=str(evenement.pk),
        details={
            "type_evenement": evenement.event_type,
            "mode": plan["mode"],
            "schema": plan.get("schema", ""),
            "piece": piece.reference,
            "devise": plan["devise"],
            "montant": str(plan["montant"]),
            "date_operation": str(plan["date_operation"]),
        },
    )


def consommer_lot(
    *,
    par: str = "",
    limite: int = 500,
    jusqu_au: date_cls | None = None,
    types: list[str] | None = None,
    source: str = SOURCE_INVESTISSEMENT,
) -> dict:
    """Vide la file, événement par événement, sans jamais s'arrêter au premier caillou.

    Volontairement NON transactionnel au niveau du lot : chaque événement porte sa propre
    transaction. Un échec isolé (donnée incomplète, règle absente, cantonnement manquant)
    laisse SON événement en file et n'empêche pas les autres d'entrer au grand livre —
    sans quoi un seul événement bancal gèlerait toute la comptabilité.
    """
    evenements = list(
        evenements_en_attente(jusqu_au=jusqu_au, types=types, source=source)[: max(0, int(limite))]
    )

    consommes: list[dict] = []
    sans_ecriture: list[dict] = []
    echecs: list[dict] = []

    for evenement in evenements:
        entree = {
            "evenement_id": evenement.pk,
            "type": evenement.event_type,
            "montant": services.q2(evenement.amount),
            "devise": evenement.currency,
            "occurred_at": evenement.occurred_at,
        }
        try:
            piece = consommer_evenement(evenement, par=par, source=source)
        except SansEcritureDefinie as exc:
            sans_ecriture.append({**entree, "motif": str(exc)})
        except Exception as exc:  # noqa: BLE001 - un échec est une DONNÉE du rapport
            echecs.append({**entree, "motif": str(exc), "classe": type(exc).__name__})
        else:
            consommes.append({
                **entree,
                "piece": piece.reference,
                "journal": piece.journal,
                "schema": piece.evenement,
            })

    return {
        "source": source,
        "examines": len(evenements),
        "consommes": consommes,
        "sans_ecriture": sans_ecriture,
        "echecs": echecs,
        # File ENTIÈRE de CETTE source, sans le filtre du lot : un événement écarté par
        # `--jusqu-au` ou par `--limite` reste un événement en attente d'écriture. Un
        # compteur qui se rétrécirait avec le périmètre demandé donnerait l'illusion d'une
        # file vide.
        "restant_en_file": evenements_en_attente(source=source).count(),
        # Le MONTANT de ce qui reste : c'est lui, et non le compte, qui dit de combien les
        # états financiers sont incomplets.
        "montants_en_attente": montants_en_attente(source=source),
    }


def simuler_lot(
    *,
    limite: int = 500,
    jusqu_au: date_cls | None = None,
    types: list[str] | None = None,
    source: str = SOURCE_INVESTISSEMENT,
) -> dict:
    """Mode simulation : ce que produirait `consommer_lot`, sans écrire une seule ligne.

    Attention : la résolution du cantonnement OUVRE les sous-comptes 419-OFF manquants
    (opération idempotente et sans impact sur les soldes) — la commande enveloppe donc la
    simulation dans une transaction annulée, pour qu'un « à blanc » ne laisse rien.
    """
    plans: list[dict] = []
    sans_ecriture: list[dict] = []
    echecs: list[dict] = []
    evenements = list(
        evenements_en_attente(jusqu_au=jusqu_au, types=types, source=source)[: max(0, int(limite))]
    )

    for evenement in evenements:
        entree = {
            "evenement_id": evenement.pk,
            "type": evenement.event_type,
            "montant": services.q2(evenement.amount),
            "devise": evenement.currency,
        }
        try:
            plan = planifier(evenement, source=source)
        except SansEcritureDefinie as exc:
            sans_ecriture.append({**entree, "motif": str(exc)})
        except Exception as exc:  # noqa: BLE001
            echecs.append({**entree, "motif": str(exc), "classe": type(exc).__name__})
        else:
            plans.append({
                **entree,
                "mode": plan["mode"],
                "schema": plan["schema"] or "contrepassation",
                "reference": plan["reference"],
                "date_operation": plan["date_operation"],
                "comptes": plan.get("comptes", {}),
                "montants": plan.get("montants", {}),
            })

    return {
        "source": source,
        "examines": len(evenements),
        "plans": plans,
        "sans_ecriture": sans_ecriture,
        "echecs": echecs,
        "montants_en_attente": montants_en_attente(source=source),
    }
