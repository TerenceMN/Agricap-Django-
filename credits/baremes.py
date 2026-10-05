"""
Barèmes de score éditables par le comité (`BaremeScore`) — principe 8.

Édition maker-checker avec prévisualisation de l'impact sur le golden set AVANT
activation (CONVENTIONS.md §7.1.5, CONTRAT §5). Le comité propose une révision
(`BaremeRevision`, append-only) ; l'impact sur les dossiers déjà analysés est
calculé et figé à la proposition ; un SECOND membre l'active (maker ≠ checker),
ce qui bascule `BaremeScore` sur la nouvelle courbe et archive la précédente.

La prévisualisation ne persiste RIEN et ne relit aucune feuille de besoins : elle
réévalue la courbe proposée sur les abscisses (DSCR, écart moyen, ratio de
couverture, score global) déjà calculées et stockées dans chaque `AnalyseCredit`.
Elle est honnête sur sa couverture — elle dit combien de dossiers du golden set
sont réellement évaluables, et n'invente aucune valeur absente (principe 1).
"""
from __future__ import annotations

import logging
from decimal import Decimal, ROUND_HALF_UP

from django.db import transaction
from django.utils import timezone

logger = logging.getLogger(__name__)

DIXIEME = Decimal("0.1")

#: Critères qu'un barème pilote, dans l'ordre canonique de `credits.analyse`.
#: DECISION ne pilote aucun score de critère : il déplace la recommandation et la
#: lettre (seuils + grille), recalculées à part. TAUX non plus : il ne touche ni
#: au score ni au verdict, seulement au PRIX — dont l'impact se prévisualise
#: différemment (taux avant / après, et non score avant / après).
AFFECTED_CRITERIA = {
    "DSCR": ("dscr", "stress"),
    "ECART_TECHNIQUE": ("technique",),
    "COUVERTURE_GARANTIES": ("garanties",),
    "DECISION": (),
    "TAUX": (),
}

#: Barèmes qui ne sont pas des courbes mais des jeux de règles (`parametres`).
CODES_REGLES = ("DECISION", "TAUX")

#: Taille du golden set : dernières analyses réelles, une par dossier.
GOLDEN_LIMIT = 200


# ── Exceptions (convention `credits.workflow.WorkflowError`) ──────────────────

class BaremeError(Exception):
    code = "BAREME_ERROR"
    http_status = 422

    def __init__(self, message: str, errors: list[dict] | None = None) -> None:
        super().__init__(message)
        self.errors = errors or []

    def as_errors(self) -> list[dict]:
        return self.errors or [{"code": self.code, "message": str(self)}]


class BaremeIntrouvable(BaremeError):
    code = "BAREME_INTROUVABLE"
    http_status = 404


class BaremeContenuInvalide(BaremeError):
    code = "BAREME_CONTENU_INVALIDE"


class BaremeRevisionIntrouvable(BaremeError):
    code = "BAREME_REVISION_INTROUVABLE"
    http_status = 404


class BaremeRevisionEtat(BaremeError):
    code = "BAREME_REVISION_ETAT"
    http_status = 409


class BaremeMakerChecker(BaremeError):
    code = "MAKER_CHECKER_VIOLATION"
    http_status = 409


# ── Quantize / helpers (miroir de `credits.analyse`) ──────────────────────────

def _q1(value) -> Decimal:
    return Decimal(str(value)).quantize(DIXIEME, rounding=ROUND_HALF_UP)


def _points(score, poids) -> Decimal:
    """`points = score × poids / 100`, arrondi au dixième — comme `analyse._points`."""
    return _q1(Decimal(str(score)) * Decimal(str(poids)) / Decimal(100))


def _eval_curve(points, x) -> Decimal:
    """Évalue une courbe (liste `{x, y}`) via la logique Decimal de `BaremeScore`."""
    from credits.models import BaremeScore

    return BaremeScore(code="_preview", points=points).evaluer(x)


def is_courbe(code: str) -> bool:
    return code not in CODES_REGLES


# ── Validation du contenu proposé ─────────────────────────────────────────────

def valider_contenu(code: str, points, parametres) -> None:
    """Garde-fou minimal : une courbe exploitable, un jeu de règles non vide."""
    if code == "TAUX":
        _valider_grille_taux(parametres)
        return
    if code in CODES_REGLES:
        if not isinstance(parametres, dict) or not parametres:
            raise BaremeContenuInvalide(
                f"Le barème {code} exige des paramètres (seuils de décision, "
                "grille de lettres) — la courbe `points` ne le décrit pas."
            )
        return

    if not isinstance(points, list) or len(points) < 2:
        raise BaremeContenuInvalide(
            "Une courbe de barème exige au moins deux points {x, y}."
        )
    xs = []
    for p in points:
        try:
            x = Decimal(str(p["x"]))
            y = Decimal(str(p["y"]))
        except Exception as exc:  # noqa: BLE001
            raise BaremeContenuInvalide(
                "Chaque point de courbe doit porter un x et un y numériques."
            ) from exc
        if not (Decimal(0) <= y <= Decimal(100)):
            raise BaremeContenuInvalide(
                f"Un score de courbe (y) doit rester dans [0, 100] : trouvé {y}."
            )
        xs.append(x)
    if len(set(xs)) != len(xs):
        raise BaremeContenuInvalide(
            "Deux points de la courbe partagent la même abscisse x."
        )


def _valider_grille_taux(parametres) -> None:
    """La grille de tarification : des bandes de score, des ajustements en points.

    Contrôles volontairement serrés — c'est un PRIX qui se règle ici, et une
    grille mal formée qui passerait en production tarifierait tous les dossiers
    d'une même bande à côté :
      - au moins une bande, et une qui parte de 0 (sinon un score bas n'est
        couvert par aucun palier et sort sans ajustement) ;
      - bandes distinctes, dans [0, 100] ;
      - ajustements bornés à ±20 points de taux : au-delà, c'est une faute de
        frappe (2 000 au lieu de 20) plus probablement qu'une décision de comité.
    """
    if not isinstance(parametres, dict):
        raise BaremeContenuInvalide("Le barème TAUX exige un objet de paramètres.")
    grille = parametres.get("grille")
    if not isinstance(grille, list) or not grille:
        raise BaremeContenuInvalide(
            "Le barème TAUX exige une « grille » d'au moins une bande "
            "{score_min, ajustement}."
        )
    seuils = []
    for bande in grille:
        try:
            seuil = Decimal(str(bande["score_min"]))
            ajustement = Decimal(str(bande["ajustement"]))
        except Exception as exc:  # noqa: BLE001
            raise BaremeContenuInvalide(
                "Chaque bande de la grille de taux doit porter un « score_min » et "
                "un « ajustement » numériques."
            ) from exc
        if not (Decimal(0) <= seuil <= Decimal(100)):
            raise BaremeContenuInvalide(
                f"Une bande de score doit rester dans [0, 100] : trouvé {seuil}."
            )
        if abs(ajustement) > Decimal(20):
            raise BaremeContenuInvalide(
                f"Ajustement de taux hors de portée : {ajustement} points. "
                "Au-delà de ±20 points, corrigez la saisie plutôt que la grille."
            )
        seuils.append(seuil)
    if len(set(seuils)) != len(seuils):
        raise BaremeContenuInvalide(
            "Deux bandes de la grille de taux partagent le même « score_min »."
        )
    if Decimal(0) not in seuils:
        raise BaremeContenuInvalide(
            "La grille de taux doit couvrir les scores les plus bas : ajoutez une "
            "bande « score_min: 0 », sinon un dossier faible ne serait tarifé par "
            "aucun palier."
        )
    ratio = parametres.get("plancher_ratio_base")
    if ratio is not None:
        try:
            valeur = Decimal(str(ratio))
        except Exception as exc:  # noqa: BLE001
            raise BaremeContenuInvalide(
                "« plancher_ratio_base » doit être un nombre (part du taux de base)."
            ) from exc
        if not (Decimal(0) < valeur <= Decimal(1)):
            raise BaremeContenuInvalide(
                f"« plancher_ratio_base » doit rester dans ]0, 1] : trouvé {valeur}."
            )


# ── Golden set ────────────────────────────────────────────────────────────────

def _golden_analyses(limit: int = GOLDEN_LIMIT):
    """Dernière `AnalyseCredit` de chaque dossier — le golden set de calibrage."""
    from credits.models import AnalyseCredit

    vus: set[int] = set()
    out = []
    qs = (AnalyseCredit.objects
          .select_related("application")
          .order_by("-execute_le", "-id"))
    for a in qs.iterator():
        if a.application_id in vus:
            continue
        vus.add(a.application_id)
        out.append(a)
        if len(out) >= limit:
            break
    return out


def _new_score_for_criterion(code, points, parametres, analyse, criterion):
    """Score (0–100) du critère sous la courbe PROPOSÉE, ou (None, False) si l'x
    nécessaire n'est pas disponible dans l'analyse stockée (jamais inventé)."""
    crit = (analyse.criteres or {}).get(criterion, {})
    details = crit.get("details", {}) if isinstance(crit, dict) else {}

    if code == "DSCR" and criterion == "dscr":
        return _q1(_eval_curve(points, analyse.dscr or 0)), True
    if code == "DSCR" and criterion == "stress":
        return _q1(_eval_curve(points, analyse.dscr_stress or 0)), True
    if code == "ECART_TECHNIQUE" and criterion == "technique":
        pct = details.get("ecartMoyenPct")
        if pct is None:
            return None, False
        return _q1(_eval_curve(points, Decimal(str(pct)) / Decimal(100))), True
    if code == "COUVERTURE_GARANTIES" and criterion == "garanties":
        ratio = details.get("ratioCouverture")
        if ratio is None:
            return None, False
        score = _q1(_eval_curve(points, Decimal(str(ratio))))
        # Réplique le plafond « garanties non constituées / absentes » de
        # `scorer_garanties` : sans lui, la prévisualisation surestimerait le score.
        plafond = Decimal(str((parametres or {}).get("plafond_non_constituees", "60")))
        garanties = details.get("garanties") or []
        if not garanties or not details.get("constituees"):
            score = min(score, plafond)
        return score, True
    return None, False


def previsualiser_impact(bareme, *, points=None, parametres=None) -> dict:
    """Impact d'un barème proposé sur le golden set (dernières analyses réelles).

    Pour un barème-courbe : réévalue le(s) critère(s) piloté(s) sur l'x stocké,
    recompose le score global (global − points avant + points après) et rejoue la
    recommandation et la lettre. Pour DECISION : rejoue recommandation et lettre à
    partir du score global stocké et des seuils proposés. Rien n'est persisté.
    """
    from credits.analyse import recommander, score_lettre
    from credits.models import BaremeScore

    code = bareme.code
    points = points if points is not None else bareme.points
    parametres = parametres if parametres is not None else bareme.parametres
    courbe = is_courbe(code)

    if code == "TAUX":
        # Une grille de taux ne déplace ni score ni recommandation : elle déplace
        # un PRIX. Prévisualiser son impact en « score avant / après » afficherait
        # 0 partout et laisserait croire qu'un recalibrage est sans conséquence.
        return _previsualiser_taux(bareme, parametres)

    decision_actif = BaremeScore.objects.filter(code="DECISION", actif=True).first()
    regles_actives = (decision_actif.parametres if decision_actif else {}) or {}
    if code == "DECISION":
        regles_new = parametres or {}
        regles_old = regles_actives
    else:
        regles_new = regles_old = regles_actives

    golden = _golden_analyses()
    impacts: list[dict] = []
    deltas: list[Decimal] = []
    nb_eval = nb_score_change = nb_reco_flip = nb_lettre_flip = 0

    for a in golden:
        old_global = Decimal(str(a.score_global))
        new_global = old_global
        evaluable = True

        if courbe:
            for crit in AFFECTED_CRITERIA[code]:
                crit_data = (a.criteres or {}).get(crit, {})
                poids = Decimal(str(crit_data.get("poids", 0)))
                old_pts = Decimal(str(crit_data.get("points", 0)))
                new_score, ok = _new_score_for_criterion(
                    code, points, parametres, a, crit)
                if not ok:
                    evaluable = False
                    break
                new_global = new_global - old_pts + _points(new_score, poids)
            if not evaluable:
                impacts.append({
                    "applicationCode": a.application.code, "evaluable": False,
                })
                continue
            new_global = _q1(new_global)

        hors_plage = a.indicateurs_hors_plage or []
        old_reco = a.recommandation
        new_reco = recommander(new_global, a.dscr or 0, hors_plage, regles_new)
        old_lettre = score_lettre(old_global, regles_old)
        new_lettre = score_lettre(new_global, regles_new)

        nb_eval += 1
        delta = _q1(new_global - old_global)
        deltas.append(delta)
        if delta != 0:
            nb_score_change += 1
        if new_reco != old_reco:
            nb_reco_flip += 1
        if new_lettre != old_lettre:
            nb_lettre_flip += 1

        impacts.append({
            "applicationCode": a.application.code,
            "evaluable": True,
            "scoreGlobalAvant": float(old_global),
            "scoreGlobalApres": float(new_global),
            "deltaScore": float(delta),
            "recommandationAvant": old_reco,
            "recommandationApres": new_reco,
            "recommandationChange": new_reco != old_reco,
            "lettreAvant": old_lettre,
            "lettreApres": new_lettre,
        })

    # Grille d'échantillon : lisible même golden set vide (base fraîche).
    sample = _sample_grid(bareme.points, points) if courbe else []

    delta_moyen = (sum(deltas, Decimal(0)) / Decimal(len(deltas))) if deltas else Decimal(0)
    delta_max = max((abs(d) for d in deltas), default=Decimal(0))

    return {
        "baremeCode": code,
        "type": "courbe" if courbe else "regles",
        "goldenSet": {
            "nbDossiers": len(golden),
            "nbEvalues": nb_eval,
            "source": "AnalyseCredit — dernière analyse par dossier",
        },
        "sampleGrid": sample,
        "impacts": impacts,
        "resume": {
            "nbScoreChange": nb_score_change,
            "nbRecommandationFlip": nb_reco_flip,
            "nbLettreFlip": nb_lettre_flip,
            "deltaScoreMoyen": float(_q1(delta_moyen)),
            "deltaScoreMax": float(_q1(delta_max)),
        },
    }


def _previsualiser_taux(bareme, parametres) -> dict:
    """Impact d'une grille de tarification proposée : des taux, pas des scores.

    Rejoue `proposer_taux` sur le score global et le taux de BASE figés dans
    chaque analyse du golden set. Une analyse antérieure à la grille unique n'a
    pas de taux de base figé : elle est déclarée non évaluable plutôt que
    re-tarifée sur une assiette devinée.
    """
    from credits.analyse import proposer_taux

    golden = _golden_analyses()
    impacts: list[dict] = []
    deltas: list[Decimal] = []
    nb_eval = nb_change = 0

    for a in golden:
        fige = (a.baremes_appliques or {}).get("_tarification") or {}
        base = fige.get("tauxBase")
        if base is None or a.taux_propose is None:
            impacts.append({"applicationCode": a.application.code, "evaluable": False})
            continue
        avant = Decimal(str(a.taux_propose))
        apres = proposer_taux(a.score_global, base, parametres)["_taux"]
        delta = (apres - avant).quantize(Decimal("0.01"))
        nb_eval += 1
        deltas.append(delta)
        if delta != 0:
            nb_change += 1
        impacts.append({
            "applicationCode": a.application.code,
            "evaluable": True,
            "scoreGlobal": float(a.score_global),
            "tauxBase": float(Decimal(str(base))),
            "tauxAvant": float(avant),
            "tauxApres": float(apres),
            "deltaTaux": float(delta),
        })

    # Grille lisible même sur une base fraîche : ce que la nouvelle grille donne
    # sur un taux de base de 18 %, aux scores charnières.
    ancienne = bareme.parametres or {}
    sample = []
    for score in (Decimal("95"), Decimal("85"), Decimal("75"), Decimal("70"),
                  Decimal("60"), Decimal("55"), Decimal("40")):
        avant = proposer_taux(score, Decimal("18"), ancienne)["_taux"]
        apres = proposer_taux(score, Decimal("18"), parametres)["_taux"]
        sample.append({
            "x": float(score),
            "scoreAvant": float(avant),      # « taux avant », clé du contrat existant
            "scoreApres": float(apres),
            "delta": float((apres - avant).quantize(Decimal("0.01"))),
        })

    delta_moyen = (sum(deltas, Decimal(0)) / Decimal(len(deltas))) if deltas else Decimal(0)
    delta_max = max((abs(d) for d in deltas), default=Decimal(0))
    return {
        "baremeCode": "TAUX",
        "type": "regles",
        "unite": "points de taux annuel",
        "goldenSet": {
            "nbDossiers": len(golden),
            "nbEvalues": nb_eval,
            "source": "AnalyseCredit — dernière analyse par dossier",
        },
        "sampleGrid": sample,
        "impacts": impacts,
        "resume": {
            "nbScoreChange": nb_change,
            "nbRecommandationFlip": 0,
            "nbLettreFlip": 0,
            "deltaScoreMoyen": float(delta_moyen.quantize(Decimal("0.01"))),
            "deltaScoreMax": float(delta_max.quantize(Decimal("0.01"))),
        },
    }


def _sample_grid(anciens_points, nouveaux_points) -> list[dict]:
    """Ancienne vs nouvelle courbe sur les abscisses clés + points milieux."""
    if not nouveaux_points:
        return []
    try:
        xs = sorted({Decimal(str(p["x"])) for p in nouveaux_points})
    except Exception:  # noqa: BLE001
        return []

    grille: list[Decimal] = []
    for i, x in enumerate(xs):
        grille.append(x)
        if i + 1 < len(xs):
            grille.append((x + xs[i + 1]) / 2)

    sample = []
    for x in grille:
        sa = _q1(_eval_curve(anciens_points, x)) if anciens_points else None
        sn = _q1(_eval_curve(nouveaux_points, x))
        sample.append({
            "x": float(x),
            "scoreAvant": float(sa) if sa is not None else None,
            "scoreApres": float(sn),
            "delta": float(_q1(sn - sa)) if sa is not None else None,
        })
    return sample


# ── Sérialiseurs (staff seulement — principe 7) ───────────────────────────────

def serialize_revision(rev, *, with_preview: bool = False) -> dict:
    data = {
        "id": rev.pk,
        "baremeCode": rev.bareme_code,
        "version": rev.version,
        "status": rev.status,
        "comment": rev.comment or None,
        "proposedBySub": rev.proposed_by_sub,
        "proposedAt": rev.proposed_at.isoformat() if rev.proposed_at else None,
        "decidedBySub": rev.decided_by_sub or None,
        "decidedAt": rev.decided_at.isoformat() if rev.decided_at else None,
    }
    if with_preview:
        data["points"] = rev.points
        data["parametres"] = rev.parametres
        data["impactPreview"] = rev.impact_preview
    return data


def serialize_bareme(bareme, *, include_history: bool = False) -> dict:
    from credits.models import BaremeRevision

    revisions = list(bareme.revisions.all()) if include_history else []
    pending = next(
        (r for r in revisions if r.status == BaremeRevision.Status.DRAFT), None)
    data = {
        "code": bareme.code,
        "libelle": bareme.libelle or None,
        "type": "courbe" if is_courbe(bareme.code) else "regles",
        "points": bareme.points,
        "parametres": bareme.parametres,
        "actif": bareme.actif,
        "version": bareme.version,
        "updatedAt": bareme.updated_at.isoformat() if bareme.updated_at else None,
        "pendingRevision": serialize_revision(pending, with_preview=True) if pending else None,
    }
    if include_history:
        data["revisions"] = [serialize_revision(r) for r in revisions]
    return data


# ── Édition maker-checker ─────────────────────────────────────────────────────

@transaction.atomic
def proposer_revision(*, code: str, points=None, parametres=None,
                      comment: str = "", proposed_by_sub: str) -> "object":
    """Maker : propose une édition de barème + fige son impact sur le golden set.

    N'active RIEN. Une seule révision en attente par barème à la fois.
    """
    from credits.models import BaremeScore, BaremeRevision

    try:
        bareme = BaremeScore.objects.get(code=code)
    except BaremeScore.DoesNotExist as exc:
        raise BaremeIntrouvable(f"Aucun barème de code « {code} ».") from exc

    new_points = points if points is not None else bareme.points
    new_params = parametres if parametres is not None else bareme.parametres
    valider_contenu(code, new_points, new_params)

    if bareme.revisions.filter(status=BaremeRevision.Status.DRAFT).exists():
        raise BaremeRevisionEtat(
            "Une révision de ce barème est déjà en attente d'activation. "
            "Activez-la ou rejetez-la avant d'en proposer une autre."
        )

    preview = previsualiser_impact(bareme, points=new_points, parametres=new_params)

    revision = BaremeRevision.objects.create(
        bareme=bareme,
        bareme_code=code,
        points=new_points,
        parametres=new_params,
        version=bareme.version + 1,
        status=BaremeRevision.Status.DRAFT,
        impact_preview=preview,
        comment=comment or "",
        proposed_by_sub=proposed_by_sub or "",
    )

    from audit.services import record as audit_record
    audit_record(
        actor=proposed_by_sub or "", action="credits.bareme.propose",
        entity_type="BaremeRevision", entity_id=str(revision.pk),
        details={
            "baremeCode": code,
            "version": revision.version,
            "resume": preview.get("resume"),
            "comment": comment or "",
        },
    )
    return revision


@transaction.atomic
def activer_revision(*, revision_id: int, activated_by_sub: str) -> "object":
    """Checker (≠ maker) : active une révision brouillon.

    Bascule `BaremeScore` sur la nouvelle courbe, incrémente sa version et archive
    la révision précédemment active. Append-only : rien n'est supprimé.
    """
    from credits.models import BaremeRevision

    try:
        rev = (BaremeRevision.objects
               .select_related("bareme")
               .get(pk=revision_id))
    except BaremeRevision.DoesNotExist as exc:
        raise BaremeRevisionIntrouvable(
            f"Révision de barème #{revision_id} introuvable."
        ) from exc

    if rev.status != BaremeRevision.Status.DRAFT:
        raise BaremeRevisionEtat(
            f"La révision #{revision_id} n'est pas en attente d'activation "
            f"(statut « {rev.status} »)."
        )
    if activated_by_sub and activated_by_sub == rev.proposed_by_sub:
        raise BaremeMakerChecker(
            "L'activation d'un barème revient à un autre membre du comité que "
            "celui qui l'a proposé (maker ≠ checker)."
        )

    bareme = rev.bareme
    # Archive l'ancienne révision active (QuerySet.update : ne touche que `status`,
    # champ mutable — le garde-fou d'immuabilité du contenu reste intact).
    bareme.revisions.filter(status=BaremeRevision.Status.ACTIVE).update(
        status=BaremeRevision.Status.ARCHIVED)

    bareme.points = rev.points
    bareme.parametres = rev.parametres
    bareme.version = rev.version
    bareme.actif = True
    bareme.save(update_fields=["points", "parametres", "version", "actif", "updated_at"])

    rev.status = BaremeRevision.Status.ACTIVE
    rev.decided_by_sub = activated_by_sub or ""
    rev.decided_at = timezone.now()
    rev.save()

    from audit.services import record as audit_record
    audit_record(
        actor=activated_by_sub or "", action="credits.bareme.activate",
        entity_type="BaremeRevision", entity_id=str(rev.pk),
        details={
            "baremeCode": bareme.code,
            "version": rev.version,
            "proposePar": rev.proposed_by_sub,
            "activePar": activated_by_sub or "",
        },
    )
    return rev
