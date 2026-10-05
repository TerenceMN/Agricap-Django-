"""
Indicateurs ESG — référentiel versionné, mesures datées, vérification terrain.

Trois règles gouvernent tout le fichier, et chacune répond à une manière connue de
fabriquer un faux chiffre d'impact :

1. **Un critère porte son unité, son sens et son poids.** Sans l'unité, on
   additionne des tonnes et des emplois ; sans le sens, on ne sait pas si 12 est
   bon ; sans le poids voté, la « note globale » est celle de son auteur. Le
   référentiel est donc versionné et activé par un checker ≠ maker, comme les
   filières `ValueChain`.

2. **Une valeur non vérifiée ne compte pas.** Même cycle et même vocabulaire que
   les actifs gageables (`assets.Asset`) : `value` est DÉCLARÉE, `valeur_retenue`
   est fixée par le vérificateur, et seule cette dernière entre dans un agrégat.
   Une déclaration reste lisible — elle n'est simplement pas comptée.

3. **Pas de moyenne sans effectif ni couverture** (§4.6). Un score ESG moyen sur
   3 projets renseignés parmi 20 est un chiffre trompeur : l'agrégat est donc
   toujours servi avec son effectif ET son taux de couverture, et il n'est pas
   servi du tout quand aucune mesure vérifiée n'existe.

L'agrégat investisseur est pondéré par les montants ENCAISSÉS : c'est l'argent
réellement placé qui donne à un projet son poids dans l'impact d'un portefeuille,
pas le nombre de lignes.
"""
from __future__ import annotations

from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from audit.services import record as audit_record
from common.exceptions import ConflictError, ValidationFailed
from common.money import quantifier

from .models import (
    EsgCriterion, Investor, Project, ProjectEsgMeasure, Subscription,
)

RATIO = Decimal("0.0001")

#: Extensions acceptées pour une pièce probante ESG — liste blanche, comme les
#: pièces du dossier (`services.joindre_piece_analyse`).
EXTENSIONS_PIECES = (".pdf", ".xlsx", ".csv", ".png", ".jpg", ".jpeg")
TAILLE_MAX_PIECE = 5 * 1024 * 1024   # 5 Mo

#: Statuts pendant lesquels une mesure ESG se collecte : dès l'approbation du
#: dossier (P05) puis toute la vie du projet financé, au rythme des rapports de
#: performance. En prospection, le dossier n'est pas constitué ; une mesure
#: d'impact n'aurait pas d'objet.
#:
#: Le point de départ était P03 (« due diligence »), étape retirée avec le
#: scoring : P05 est la première étape qui la suit dans le parcours actuel.
STATUTS_COLLECTE = (
    Project.Status.P05, Project.Status.P06,
    Project.Status.P07, Project.Status.P08, Project.Status.P09, Project.Status.P10,
    Project.Status.P11, Project.Status.P12,
)


# ── Référentiel : versionné, maker ≠ checker ─────────────────────────────────

def criteres_actifs():
    return EsgCriterion.objects.filter(status=EsgCriterion.Status.ACTIVE)


def critere_row(c: EsgCriterion) -> dict:
    return {
        "id": c.pk, "code": c.code, "version": c.version, "label": c.label,
        "pillar": c.pillar, "pillarLabel": c.get_pillar_display(), "unit": c.unit,
        "direction": c.direction, "directionLabel": c.get_direction_display(),
        "weight": float(c.weight), "status": c.status,
        "minValue": float(c.min_value) if c.min_value is not None else None,
        "maxValue": float(c.max_value) if c.max_value is not None else None,
        "createdBy": c.created_by, "activatedBy": c.activated_by,
        "activatedAt": c.activated_at.isoformat() if c.activated_at else None,
    }


@transaction.atomic
def declarer_critere(*, code: str, label: str, unit: str, direction: str, weight,
                     pillar: str = EsgCriterion.Pillar.ENVIRONMENT,
                     min_value=None, max_value=None, by: str = "") -> EsgCriterion:
    """Pose une NOUVELLE version d'un critère, en brouillon.

    Corriger un critère n'écrase pas l'ancien : les mesures déjà collectées l'ont
    été contre lui, et une pondération réécrite sous les pieds d'un historique rend
    tout agrégat passé irreproductible.
    """
    code = (code or "").strip().upper()
    if not code:
        raise ValidationFailed("Le code du critère est requis.", code="CRITERION_CODE_REQUIRED")
    if not (label or "").strip():
        raise ValidationFailed("Le libellé du critère est requis.",
                                code="CRITERION_LABEL_REQUIRED")
    if not (unit or "").strip():
        raise ValidationFailed(
            "L'unité de mesure est requise : sans elle, deux projets ne se comparent pas.",
            code="CRITERION_UNIT_REQUIRED")
    if direction not in EsgCriterion.Direction.values:
        raise ValidationFailed(
            f"Sens d'amélioration inconnu : « {direction} ». Valeurs admises : "
            f"{', '.join(EsgCriterion.Direction.values)}.", code="CRITERION_DIRECTION_UNKNOWN")
    if pillar not in EsgCriterion.Pillar.values:
        raise ValidationFailed(f"Pilier inconnu : « {pillar} ».", code="CRITERION_PILLAR_UNKNOWN")
    poids = Decimal(str(weight or 0))
    if poids <= Decimal("0"):
        raise ValidationFailed("Le poids d'un critère doit être strictement positif.",
                                code="CRITERION_WEIGHT_INVALID")

    derniere = EsgCriterion.objects.filter(code=code).order_by("-version").first()
    critere = EsgCriterion.objects.create(
        code=code, version=(derniere.version + 1) if derniere else 1, label=label.strip(),
        pillar=pillar, unit=unit.strip(), direction=direction, weight=poids,
        min_value=Decimal(str(min_value)) if min_value is not None else None,
        max_value=Decimal(str(max_value)) if max_value is not None else None,
        created_by=by or "",
    )
    audit_record(actor=by, action="investments.esg.criterion.declare",
                 entity_type="EsgCriterion", entity_id=f"{code}@{critere.version}",
                 details={"label": critere.label, "unit": critere.unit,
                          "weight": str(poids), "direction": direction})
    return critere


@transaction.atomic
def activer_criteres(*, criterion_ids, by: str = "") -> list[EsgCriterion]:
    """Active un JEU de critères d'un seul acte — checker ≠ maker, poids = 100.

    L'activation porte sur un ENSEMBLE et non sur un critère isolé, parce que la
    pondération est une propriété de l'ensemble : activer un critère à 60 points puis
    un autre à 40 supposerait un état intermédiaire à 60 où toute moyenne pondérée
    serait fausse. Un référentiel se vote entier, comme un barème.

    L'invariant est vérifié sur l'état FINAL : les versions remplacées sont retirées
    du décompte, celles qu'on active y entrent.
    """
    ids = [int(i) for i in (criterion_ids or [])]
    if not ids:
        raise ValidationFailed("Aucun critère à activer.", code="CRITERIA_IDS_REQUIRED")
    if not (by or "").strip():
        raise ValidationFailed("L'activation exige un acteur identifié.",
                                code="CRITERION_ACTIVATOR_REQUIRED")

    candidats = list(EsgCriterion.objects.select_for_update().filter(pk__in=ids))
    if len(candidats) != len(set(ids)):
        raise ValidationFailed("Critère introuvable.", code="CRITERION_NOT_FOUND")
    for critere in candidats:
        if critere.status == EsgCriterion.Status.ARCHIVED:
            raise ConflictError(
                f"Le critère {critere.code} v{critere.version} est archivé : il ne se "
                "réactive pas, on pose une nouvelle version.")
        if critere.created_by and critere.created_by == by:
            raise ConflictError(
                f"Maker ≠ checker : le critère {critere.code} a été déclaré par le même "
                "acteur. Son activation revient à quelqu'un d'autre.",
                code="CRITERION_MAKER_IS_CHECKER")

    codes = {c.code for c in candidats}
    remplaces = list(EsgCriterion.objects.filter(code__in=codes,
                                                  status=EsgCriterion.Status.ACTIVE)
                     .exclude(pk__in=ids))
    conserves = criteres_actifs().exclude(pk__in=[r.pk for r in remplaces]).exclude(pk__in=ids)
    total = (sum((Decimal(c.weight) for c in conserves), Decimal("0"))
             + sum((Decimal(c.weight) for c in candidats), Decimal("0")))
    if total != Decimal("100.00"):
        raise ValidationFailed(
            f"La somme des poids des critères actifs ferait {total} points au lieu de "
            "100. Une pondération qui ne somme pas à 100 n'est pas une pondération.",
            code="CRITERIA_WEIGHTS_SUM_INVALID",
        )

    for ancien in remplaces:
        ancien.status = EsgCriterion.Status.ARCHIVED
        ancien.save(update_fields=["status"])
    maintenant = timezone.now()
    for critere in candidats:
        if critere.status == EsgCriterion.Status.ACTIVE:
            continue
        critere.status = EsgCriterion.Status.ACTIVE
        critere.activated_by = by
        critere.activated_at = maintenant
        critere.save(update_fields=["status", "activated_by", "activated_at"])
    audit_record(actor=by, action="investments.esg.referential.activate",
                 entity_type="EsgCriterion",
                 entity_id=",".join(sorted(f"{c.code}@{c.version}" for c in candidats)),
                 details={"activated": [f"{c.code}@{c.version}" for c in candidats],
                          "archived": [f"{c.code}@{c.version}" for c in remplaces],
                          "weightsTotal": str(total)})
    return candidats


def activer_critere(*, criterion_id: int, by: str = "") -> EsgCriterion:
    """Active UNE version de critère — cas particulier de `activer_criteres`.

    N'aboutit que si l'ensemble résultant somme toujours à 100 : c'est le cas normal
    du remplacement d'une version par la suivante à poids égal.
    """
    return activer_criteres(criterion_ids=[criterion_id], by=by)[0]


# ── Mesures : déclaration append-only, puis vérification ─────────────────────

def _valider_piece(fichier):
    """Liste blanche d'extensions et taille bornée — même garde que le PV du comité."""
    if fichier is None:
        return "", None
    nom = (getattr(fichier, "name", "") or "").strip()
    if not nom.lower().endswith(EXTENSIONS_PIECES):
        raise ValidationFailed(
            f"Extension refusée. Formats acceptés : {', '.join(EXTENSIONS_PIECES)}.",
            code="ESG_FILE_EXTENSION")
    if getattr(fichier, "size", 0) > TAILLE_MAX_PIECE:
        raise ValidationFailed(
            f"Pièce trop volumineuse (maximum {TAILLE_MAX_PIECE // (1024 * 1024)} Mo).",
            code="ESG_FILE_TOO_LARGE")
    return nom, fichier


@transaction.atomic
def enregistrer_mesure(*, project: Project, criterion_code: str, value, collected_on,
                       source: str, fichier=None, by: str = "") -> ProjectEsgMeasure:
    """Enregistre une mesure DÉCLARÉE avec sa pièce probante.

    Elle n'entre dans aucun agrégat tant qu'un vérificateur ne l'a pas retenue :
    déclarer n'est pas constater. La mesure est append-only — corriger, c'est en
    enregistrer une nouvelle, et l'écart entre les deux reste lisible.
    """
    if project.status not in STATUTS_COLLECTE:
        raise ConflictError(
            f"Le projet {project.code} est en {project.status} : une mesure d'impact se "
            "collecte à partir de l'approbation du dossier (P05).",
        )
    critere = EsgCriterion.objects.filter(code=(criterion_code or "").strip().upper(),
                                           status=EsgCriterion.Status.ACTIVE).first()
    if critere is None:
        raise ValidationFailed(
            f"Aucun critère ACTIF ne porte le code « {criterion_code} » : une mesure se "
            "prend contre le référentiel en vigueur, pas contre un critère inventé.",
            code="CRITERION_NOT_ACTIVE")
    if not (source or "").strip():
        raise ValidationFailed(
            "La source de la mesure est requise : un chiffre d'impact sans provenance "
            "n'est pas vérifiable.", code="MEASURE_SOURCE_REQUIRED")
    if collected_on is None:
        raise ValidationFailed(
            "La date de collecte est requise : un impact se constate à une date.",
            code="MEASURE_DATE_REQUIRED")
    if collected_on > timezone.localdate():
        raise ValidationFailed("La date de collecte ne peut pas être dans le futur.",
                                code="MEASURE_DATE_FUTURE")
    try:
        montant = Decimal(str(value))
    except Exception:
        raise ValidationFailed(f"Valeur illisible : « {value} ».",
                                code="MEASURE_VALUE_INVALID") from None
    if critere.min_value is not None and montant < Decimal(critere.min_value):
        raise ValidationFailed(
            f"Valeur {montant} {critere.unit} sous la borne de plausibilité du critère "
            f"({critere.min_value}). Corrigez la saisie ou justifiez l'écart.",
            code="MEASURE_VALUE_OUT_OF_RANGE")
    if critere.max_value is not None and montant > Decimal(critere.max_value):
        raise ValidationFailed(
            f"Valeur {montant} {critere.unit} au-dessus de la borne de plausibilité du "
            f"critère ({critere.max_value}). Corrigez la saisie ou justifiez l'écart.",
            code="MEASURE_VALUE_OUT_OF_RANGE")

    nom, piece = _valider_piece(fichier)
    mesure = ProjectEsgMeasure.objects.create(
        project=project, criterion=critere, value=montant, collected_on=collected_on,
        source=source.strip(), file=piece, original_name=nom, declared_by=by or "",
    )
    audit_record(actor=by, action="investments.esg.measure.declare",
                 entity_type="ProjectEsgMeasure", entity_id=str(mesure.pk),
                 details={"project": project.code, "criterion": critere.code,
                          "value": str(montant), "collectedOn": collected_on.isoformat(),
                          "source": mesure.source, "file": nom})
    return mesure


@transaction.atomic
def verifier_mesure(*, measure_id: int, decision: str, valeur_retenue=None,
                    motif: str = "", by: str = "") -> ProjectEsgMeasure:
    """Vérification terrain — `verifie` (avec valeur retenue) ou `rejete` (avec motif).

    Une mesure ne se vérifie qu'une fois : la contre-vérifier, c'est enregistrer une
    NOUVELLE mesure et la vérifier à son tour. Le vérificateur ne peut pas être celui
    qui a déclaré — constater sa propre déclaration ne constate rien.
    """
    mesure = ProjectEsgMeasure.objects.select_for_update().select_related(
        "criterion", "project").filter(pk=measure_id).first()
    if mesure is None:
        raise ValidationFailed("Mesure introuvable.", code="MEASURE_NOT_FOUND")
    if mesure.status != ProjectEsgMeasure.Status.DECLARE:
        raise ConflictError(
            f"Cette mesure est déjà « {mesure.get_status_display().lower()} » : une "
            "vérification ne se rejoue pas, elle se refait sur une nouvelle mesure.")
    if not (by or "").strip():
        raise ValidationFailed("La vérification exige un acteur identifié.",
                                code="MEASURE_VERIFIER_REQUIRED")
    if mesure.declared_by and mesure.declared_by == by:
        raise ConflictError(
            "Le vérificateur ne peut pas être le déclarant : constater sa propre "
            "déclaration ne la vérifie pas.",
            code="MEASURE_VERIFIER_IS_DECLARANT")

    if decision == ProjectEsgMeasure.Status.REJETE:
        if not (motif or "").strip():
            raise ValidationFailed("Un rejet exige un motif.", code="MEASURE_REASON_REQUIRED")
        mesure.status = ProjectEsgMeasure.Status.REJETE
        mesure.motif_rejet = motif.strip()[:255]
        mesure.valeur_retenue = None
    elif decision == ProjectEsgMeasure.Status.VERIFIE:
        retenue = Decimal(str(valeur_retenue)) if valeur_retenue is not None else Decimal(mesure.value)
        if retenue < Decimal("0"):
            raise ValidationFailed("La valeur retenue ne peut pas être négative.",
                                    code="MEASURE_RETAINED_INVALID")
        mesure.status = ProjectEsgMeasure.Status.VERIFIE
        mesure.valeur_retenue = retenue
    else:
        raise ValidationFailed(
            f"Décision inconnue : « {decision} ». Valeurs admises : verifie, rejete.",
            code="MEASURE_DECISION_UNKNOWN")

    mesure.verifie_par_sub = by
    mesure.verifie_le = timezone.now()
    mesure.save(update_fields=["status", "valeur_retenue", "motif_rejet",
                                "verifie_par_sub", "verifie_le"])
    audit_record(actor=by, action="investments.esg.measure.verify",
                 entity_type="ProjectEsgMeasure", entity_id=str(mesure.pk),
                 details={"project": mesure.project.code, "criterion": mesure.criterion.code,
                          "decision": mesure.status, "declared": str(mesure.value),
                          "retained": str(mesure.valeur_retenue or ""),
                          "reason": mesure.motif_rejet})
    return mesure


def mesure_row(m: ProjectEsgMeasure) -> dict:
    return {
        "id": m.pk, "projectCode": m.project.code,
        "criterionCode": m.criterion.code, "criterionLabel": m.criterion.label,
        "criterionVersion": m.criterion.version, "unit": m.criterion.unit,
        "direction": m.criterion.direction, "pillar": m.criterion.pillar,
        "declaredValue": float(m.value),
        "retainedValue": float(m.valeur_retenue) if m.valeur_retenue is not None else None,
        "collectedOn": m.collected_on.isoformat(), "source": m.source,
        "status": m.status, "statusLabel": m.get_status_display(),
        "fileName": m.original_name,
        "fileUrl": m.file.url if m.file else None,
        "declaredBy": m.declared_by, "verifiedBy": m.verifie_par_sub,
        "verifiedAt": m.verifie_le.isoformat() if m.verifie_le else None,
        "rejectionReason": m.motif_rejet,
        "createdAt": m.created_at.isoformat(),
    }


def _derniere_mesure_retenue(project: Project, critere: EsgCriterion):
    """La mesure VÉRIFIÉE la plus récente d'un critère sur un projet.

    « La plus récente » se lit sur la date de COLLECTE, pas sur la date de saisie :
    une mesure de mars saisie en juin reste une mesure de mars.
    """
    return (ProjectEsgMeasure.objects
            .filter(project=project, criterion=critere,
                    status=ProjectEsgMeasure.Status.VERIFIE)
            .order_by("-collected_on", "-created_at", "-id").first())


def projet_esg(project: Project) -> dict:
    """`GET /investments/projects/<code>/esg` — les mesures datées d'un projet.

    Sert la valeur RETENUE de chaque critère actif, la couverture du référentiel par
    ce projet, et l'historique complet (déclarations comprises) : l'écart entre une
    valeur déclarée et une valeur retenue est une information, pas un brouillon.
    """
    criteres = list(criteres_actifs())
    lignes = []
    renseignes = 0
    for critere in criteres:
        mesure = _derniere_mesure_retenue(project, critere)
        if mesure is not None:
            renseignes += 1
        lignes.append({
            "criterionCode": critere.code, "criterionLabel": critere.label,
            "pillar": critere.pillar, "unit": critere.unit,
            "direction": critere.direction, "weight": float(critere.weight),
            "retainedValue": float(mesure.valeur_retenue) if mesure else None,
            "collectedOn": mesure.collected_on.isoformat() if mesure else None,
            "source": mesure.source if mesure else "",
            "verifiedBy": mesure.verifie_par_sub if mesure else "",
            "measureId": mesure.pk if mesure else None,
            "unavailableReason": None if mesure else
            "Aucune mesure vérifiée sur ce critère pour ce projet.",
        })
    historique = (ProjectEsgMeasure.objects.filter(project=project)
                  .select_related("criterion", "project"))
    return {
        "projectCode": project.code,
        "criteriaCount": len(criteres),
        "measuredCriteriaCount": renseignes,
        "coverage": (float(quantifier(Decimal(renseignes) / Decimal(len(criteres)), RATIO))
                     if criteres else None),
        "criteria": lignes,
        "measures": [mesure_row(m) for m in historique],
        "totalRows": historique.count(),
        "referentialUnavailableReason": (
            "Aucun critère ESG n'est activé dans le référentiel : rien n'est mesurable "
            "tant que le comité n'en a pas voté." if not criteres else None
        ),
    }


# ── Agrégat investisseur, pondéré par les montants ENCAISSÉS ─────────────────

def bloc_investisseur(investor: Investor) -> dict:
    """Le bloc `esg` de `GET /investments/metrics/mine`.

    Chaque critère est agrégé sur les projets où l'investisseur a de l'argent, pondéré
    par le montant ENCAISSÉ sur chacun — c'est l'argent placé qui donne son poids à un
    projet, pas le nombre de lignes de souscription. La moyenne pondérée n'est servie
    qu'avec :

    - son **effectif** (nombre de projets qui portent une valeur retenue) ;
    - son **taux de couverture** (part des montants encaissés effectivement couverts).

    Un critère sans aucune mesure vérifiée n'est pas servi à 0 : il est servi à `null`
    avec son motif. Zéro tonne de CO₂ évitée et « nous ne savons pas » ne sont pas la
    même information.
    """
    criteres = list(criteres_actifs())
    subs = list(
        Subscription.objects.filter(investor=investor, status__in=Subscription.FUNDED_STATUSES)
        .select_related("offer__project")
    )
    poids_projet: dict[int, Decimal] = {}
    projets: dict[int, Project] = {}
    for sub in subs:
        projet = sub.offer.project
        projets[projet.pk] = projet
        poids_projet[projet.pk] = poids_projet.get(projet.pk, Decimal("0")) + Decimal(
            sub.settled_amount)
    base_totale = sum(poids_projet.values(), Decimal("0"))

    lignes = []
    for critere in criteres:
        numerateur = Decimal("0")
        base = Decimal("0")
        effectif = 0
        for projet_id, montant in poids_projet.items():
            mesure = _derniere_mesure_retenue(projets[projet_id], critere)
            if mesure is None or montant <= 0:
                continue
            numerateur += Decimal(mesure.valeur_retenue) * montant
            base += montant
            effectif += 1
        moyenne = (numerateur / base) if base > 0 else None
        lignes.append({
            "criterionCode": critere.code, "criterionLabel": critere.label,
            "pillar": critere.pillar, "pillarLabel": critere.get_pillar_display(),
            "unit": critere.unit, "direction": critere.direction,
            "directionLabel": critere.get_direction_display(),
            "weight": float(critere.weight),
            "weightedAverage": float(quantifier(moyenne, Decimal("0.0001")))
            if moyenne is not None else None,
            "projectsCovered": effectif,
            "projectsTotal": len(poids_projet),
            "coveredAmount": float(quantifier(base)),
            "basisAmount": float(quantifier(base_totale)),
            "coverage": (float(quantifier(base / base_totale, RATIO))
                         if base_totale > 0 else None),
            "unavailableReason": None if moyenne is not None else (
                "Aucune mesure vérifiée de ce critère sur les projets que vous financez."
            ),
        })

    couverts = sum(1 for ligne in lignes if ligne["weightedAverage"] is not None)
    return {
        "criteriaCount": len(criteres),
        "measuredCriteriaCount": couverts,
        "projectsCount": len(poids_projet),
        "basisAmount": float(quantifier(base_totale)),
        "weightingBasis": "Montants encaissés par projet.",
        "criteria": lignes,
        "unavailableReason": (
            "Aucun critère ESG n'est activé dans le référentiel de l'institution."
            if not criteres else
            "Aucune position financée : il n'y a pas encore d'impact à agréger."
            if not poids_projet else
            "Aucune mesure ESG vérifiée sur les projets que vous financez."
            if couverts == 0 else None
        ),
    }
