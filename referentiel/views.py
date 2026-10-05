"""API référentiel : plages, configuration institution et versions.

ANTI-GAMING (principe 7). Ces endpoints étaient tous ouverts à
`IsAuthenticated`, donc à N'IMPORTE QUEL utilisateur connecté — le rôle
``client`` porte la capacité ``read``. Un demandeur pouvait donc lire les seuils
DSCR, la couverture minimale, le score global minimum, les cinq poids du
scoring et les plages min/max du référentiel : soit exactement les règles du
moteur, de quoi calibrer un dossier pour franchir la barre plutôt que pour
réussir. Le principe 7 l'interdit mot pour mot (« Il ne voit JAMAIS : les
barèmes, les seuils, les tolérances par module, les plages du référentiel »).

La « transparence » que visait ce module est celle du PERSONNEL qui instruit —
elle est préservée : `IsStaff` laisse passer les rôles internes. Aucun écran
client ne consommait ces endpoints (vérifié dans `src/`), le resserrage ne
casse donc aucun parcours.

`chains` reste ouvert : c'est le catalogue des 14 cultures (code, libellé,
spécialité), pas une règle de décision.
"""
from __future__ import annotations

from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from accounts.permissions import IsStaff

from .chains import CHAINS
from .models import InstitutionConfig, ReferenceRange, ReferentielVersion


@api_view(["GET"])
@permission_classes([IsStaff])
def ranges(request):
    """Plages de la version active, filtrables par ?chain=09."""
    version = ReferentielVersion.active()
    if not version:
        return Response({"version": None, "ranges": []})
    qs = ReferenceRange.objects.filter(version=version)
    chain = request.GET.get("chain")
    if chain:
        qs = qs.filter(chain_code=chain)
    return Response({
        "version": version.label,
        "ranges": [{
            "chain_code": r.chain_code, "chain_libelle": r.chain_libelle, "name": r.name,
            "systeme": r.systeme, "unite": r.unite, "cycle_months": r.cycle_months,
            "parametre_cle": r.parametre_cle, "rendement": [r.rendement_min, r.rendement_max],
            "cout": [r.cout_min, r.cout_max], "prix": [r.prix_min, r.prix_max],
            "perte_max": r.perte_max, "statut": r.statut, "a_valider": r.a_valider,
        } for r in qs],
    })


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def chains(request):
    """Les 14 chaînes de valeur (référence indicative AGRICAP)."""
    return Response([{"code": c.code, "libelle": c.libelle, "specialite": c.specialite} for c in CHAINS])


@api_view(["GET"])
@permission_classes([IsStaff])
def config(request):
    """Configuration institution active (seuils/pondérations, §8.1).

    `decote_garantie` (décote appliquée aux actifs gagés, cf.
    `assets/services.py::valeur_apres_decote`) est exposée ICI pour que la file de
    vérification des actifs (agent de terrain) affiche le taux en vigueur —
    donnée STAFF, protégée par `IsStaff` comme le reste des paramètres du moteur
    (principe 7 : elle ne transite jamais vers un rôle client). La valeur retenue
    d'un actif reste calculée serveur ; le taux n'est ici qu'informatif.

    Ces champs sont des `Decimal` en base (principe 4) et sont servis TELS QUELS :
    le rendu JSON de DRF les sérialise en nombres, ce qu'attend le front
    (`AssetVerification.tsx` teste `typeof cfg.decote_garantie === 'number'`).
    Ne pas les faire passer par un serializer DRF sans neutraliser
    `COERCE_DECIMAL_TO_STRING` — l'écran cesserait d'afficher la décote en
    silence. Verrouillé par `ConfigDecoteExpositionTests`.
    """
    c = InstitutionConfig.active()
    return Response({
        "seuil_dscr": c.seuil_dscr, "seuil_dscr_stresse": c.seuil_dscr_stresse,
        "couverture_min": c.couverture_min, "score_global_min": c.score_global_min,
        "poids": {"technique": c.poids_technique, "financier": c.poids_financier,
                  "stress": c.poids_stress, "comportemental": c.poids_comportemental,
                  "garanties": c.poids_garanties},
        "taux_interet_annuel": c.taux_interet_annuel, "plafond_delegue": c.plafond_delegue,
        "decote_garantie": c.decote_garantie,
        "phase_deploiement": c.phase_deploiement,
    })


@api_view(["GET"])
@permission_classes([IsStaff])
def versions(request):
    """Historique des versions du référentiel typé."""
    return Response([{
        "id": v.id, "label": v.label, "imported_at": v.imported_at, "is_active": v.is_active,
        "n_ranges": v.ranges.count(),
    } for v in ReferentielVersion.objects.all()])
