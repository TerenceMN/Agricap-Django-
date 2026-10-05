"""Services du grand livre — `post_journal_entry` est LE point d'entrée unique d'écriture
comptable (idempotence, invariant Σdébit=Σcrédit validé ici, audit)."""
from __future__ import annotations

from decimal import Decimal
from datetime import date as date_cls

from django.db import transaction

from audit.services import record as audit_record
from common import idempotency
from common.exceptions import NotFoundError, ValidationFailed
from common.parsing import to_decimal

from . import serializers
from .models import ChartAccount, JournalEntry, JournalLine


@transaction.atomic
def post_journal_entry(*, date: date_cls, piece_ref: str, code: str, currency: str,
                        lines: list[dict], description: str = "", idempotency_key: str,
                        by: str = "") -> JournalEntry:
    if not lines:
        raise ValidationFailed("Une écriture doit comporter au moins une ligne.")

    rec = idempotency.begin(
        scope="ledger.post_entry", key=idempotency_key,
        params={"date": str(date), "piece_ref": piece_ref, "code": code, "lines": lines}, by=by,
    )

    total_debit = sum((to_decimal(l.get("debit")) for l in lines), start=to_decimal("0"))
    total_credit = sum((to_decimal(l.get("credit")) for l in lines), start=to_decimal("0"))
    if total_debit != total_credit:
        raise ValidationFailed(f"Écriture déséquilibrée : débit={total_debit} ≠ crédit={total_credit}.")
    if total_debit <= 0:
        raise ValidationFailed("Une écriture ne peut pas être vide (montants nuls).")
    if JournalEntry.objects.filter(date=date, piece_ref=piece_ref).exists():
        raise ValidationFailed(f"La pièce « {piece_ref} » existe déjà pour le {date}.")

    account_codes = [l["account"] for l in lines]
    accounts = {a.code: a for a in ChartAccount.objects.filter(code__in=account_codes)}
    missing = set(account_codes) - set(accounts)
    if missing:
        raise NotFoundError(f"Compte(s) introuvable(s) dans le plan comptable : {', '.join(sorted(missing))}")

    entry = JournalEntry.objects.create(
        date=date, piece_ref=piece_ref, code=code, currency=currency,
        description=description, created_by=by,
    )
    JournalLine.objects.bulk_create([
        JournalLine(
            entry=entry, account=accounts[l["account"]],
            debit=to_decimal(l.get("debit")), credit=to_decimal(l.get("credit")), user_sub=by,
        )
        for l in lines
    ])

    audit_record(actor=by, action="ledger.post_entry", entity_type="JournalEntry", entity_id=str(entry.pk),
                 details={"piece_ref": piece_ref, "total": str(total_debit)})
    idempotency.complete(rec, response=serializers.entry_row(entry),
                          entity_type="JournalEntry", entity_id=str(entry.pk))
    return entry


@transaction.atomic
def reverse_journal_entry(*, entry_id: int, reason: str, by: str = "") -> JournalEntry:
    original = JournalEntry.objects.prefetch_related("lines").filter(pk=entry_id).first()
    if not original:
        raise NotFoundError("Écriture introuvable.")
    contra_ref = f"{original.piece_ref}-REV"
    if JournalEntry.objects.filter(date=original.date, piece_ref=contra_ref).exists():
        raise ValidationFailed("Cette écriture a déjà été contre-passée.")
    contra = JournalEntry.objects.create(
        date=original.date, piece_ref=contra_ref, code=original.code, currency=original.currency,
        description=f"Contre-passation de {original.piece_ref} : {reason}", created_by=by,
    )
    JournalLine.objects.bulk_create([
        JournalLine(entry=contra, account=l.account, debit=l.credit, credit=l.debit, user_sub=by)
        for l in original.lines.all()
    ])
    audit_record(actor=by, action="ledger.reverse_entry", entity_type="JournalEntry", entity_id=str(contra.pk),
                 details={"original_id": entry_id, "reason": reason})
    return contra


def _account_balances(*, as_of: date_cls | None = None) -> dict[str, dict]:
    """Agrège les lignes du grand livre par compte — base commune de `trial_balance` ET de
    `financial_statements` (Decimal conservé jusqu'ici, converti en float seulement à la
    sérialisation finale de chaque fonction appelante)."""
    qs = JournalLine.objects.select_related("account", "entry")
    if as_of:
        qs = qs.filter(entry__date__lte=as_of)
    totals: dict[str, dict] = {}
    for line in qs:
        acc = totals.setdefault(line.account.code, {
            "code": line.account.code, "name": line.account.name, "nature": line.account.nature,
            "debit": to_decimal("0"), "credit": to_decimal("0"),
        })
        acc["debit"] += line.debit
        acc["credit"] += line.credit
    return totals


def trial_balance(*, as_of: date_cls | None = None, scope=None) -> list[dict]:
    """Balance générale (lecture seule). `scope` (une `agencies.Agency`) est accepté pour
    signature-compat avec `agencies.services.reconciliation_report` — non filtrant pour
    l'instant faute d'un lien compte/agence dans le plan comptable (renvoie la balance
    globale)."""
    totals = _account_balances(as_of=as_of)
    return [
        {**v, "debit": float(v["debit"]), "credit": float(v["credit"]), "balance": float(v["debit"] - v["credit"])}
        for v in sorted(totals.values(), key=lambda x: x["code"])
    ]


def _normal_rows(totals: dict[str, dict], natures: tuple[str, ...]) -> list[dict]:
    """Lignes filtrées par nature, soldées dans le sens NORMAL de cette nature (positif =
    direction attendue) : actif/charge = débit-crédit ; passif/produit = crédit-débit. Un
    compte contra (ex. 491 Dépréciations des comptes clients, nature=ACTIF mais alimenté au
    crédit) ressort alors en solde négatif et se soustrait naturellement du total de sa
    section — pas besoin de le traiter à part."""
    rows = []
    for v in totals.values():
        if v["nature"] not in natures:
            continue
        signed = (v["debit"] - v["credit"]) if v["nature"] in ("ACTIF", "CHARGE") else (v["credit"] - v["debit"])
        rows.append({
            "code": v["code"], "name": v["name"], "nature": v["nature"],
            "debit": float(v["debit"]), "credit": float(v["credit"]), "balance": float(signed),
        })
    return sorted(rows, key=lambda r: r["code"])


def _prefix_normal_sum(totals: dict[str, dict], *prefixes: str) -> Decimal:
    """Somme des soldes NORMAUX (positif = sens attendu selon la NATURE de chaque compte,
    charge ou produit) de tous les comptes dont le code COMMENCE PAR l'un des préfixes —
    capture automatiquement les divisionnaires AGRICAP à 4 chiffres (ex. préfixe "706" inclut
    aussi "7061 Commissions sur transactions") sans avoir à les lister un par un, et évite
    à l'appelant de devoir se souvenir quels préfixes sont des charges vs des produits."""
    total = Decimal("0")
    for code, v in totals.items():
        if not code.startswith(prefixes):
            continue
        raw = v["debit"] - v["credit"]
        total += raw if v["nature"] in ("ACTIF", "CHARGE") else -raw
    return total


def financial_statements(*, kind: str, as_of: date_cls | None = None) -> dict:
    """Bilan/Compte de résultat/SIG/Flux de trésorerie/Provisions/Créances — calculés depuis
    le grand livre (remplace les calculs client-side historiques d'`Accounting.jsx`)."""
    totals = _account_balances(as_of=as_of)

    if kind == "bilan":
        return {"actif": _normal_rows(totals, ("ACTIF",)), "passif": _normal_rows(totals, ("PASSIF",))}

    if kind == "resultat":
        return {"charges": _normal_rows(totals, ("CHARGE",)), "produits": _normal_rows(totals, ("PRODUIT",))}

    if kind == "sig":
        return _resultat_activites_agricap(totals)

    if kind == "cashflow":
        return _cashflow_statement(as_of=as_of)

    if kind == "provisions":
        # Famille "49 Dépréciations et risques provisionnés (tiers)" = le provisionnement
        # de risque de crédit réellement porté par AGRICAP (491/4911) — pas les comptes de
        # provisions génériques d'une entreprise commerciale (151-198 provisions
        # réglementées, 851-864 dotations/reprises HAO) qu'AGRICAP ne mouvemente jamais.
        return {"rows": _accounts_report(totals, code_prefix="49")}

    if kind == "creances":
        # Famille "41 Clients" = les créances liées à l'activité de crédit/épargne
        # d'AGRICAP — pas tout compte actif de la classe 4 (TVA récupérable, avances
        # personnel, fournisseurs débiteurs... des comptes de tiers génériques sans lien
        # avec le portefeuille de crédit).
        rows = _accounts_report(totals, code_prefix="41")
        for row in rows:
            row["risque"] = "douteus" in row["name"].lower() or "souffrance" in row["name"].lower()
        return {"rows": rows}

    by_class: dict[str, list] = {}
    for row in trial_balance(as_of=as_of):
        by_class.setdefault(row["code"][0] if row["code"] else "?", []).append(row)
    return {"classes": by_class}


def _resultat_activites_agricap(totals: dict[str, dict]) -> dict:
    """Résultat des activités d'AGRICAP FINTECH — structure d'un établissement de crédit
    (Produit Net des activités, charges générales d'exploitation, coût du risque), PAS le
    Tableau des Soldes Intermédiaires de Gestion générique SYSCOHADA (Marge commerciale,
    Production de l'exercice, Valeur ajoutée...) : ces notions supposent l'achat/la revente
    ou la fabrication de biens physiques, sans objet pour un prêteur qui ne vend rien.

    Chaque terme `ps(...)` est déjà un montant NORMAL (positif = sens attendu, cf.
    `_prefix_normal_sum`). Comptes de classe 6/7 volontairement exclus car spécifiques au
    négoce/à la production, jamais mouvementés par AGRICAP en pratique (restent visibles,
    au besoin, dans les onglets Balance/Compte de résultat qui ne font aucune exclusion) :
    601/602/603 (achats/variation stocks marchandises), 701-705 (ventes marchandises/
    produits fabriqués), 72 et 73 (production immobilisée / variation stocks produits)."""
    ps = lambda *prefixes: _prefix_normal_sum(totals, *prefixes)

    produits_financiers = ps("77")                        # 771 intérêts de prêts, 776 gains de change...
    charges_financieres = ps("67")                        # 671 intérêts des emprunts, 676 pertes de change...
    marge_financiere = produits_financiers - charges_financieres

    commissions_percues = ps("706")                       # 7061 Commissions sur transactions
    commissions_payees = ps("631")                        # Frais bancaires / Mobile Money
    commissions_nettes = commissions_percues - commissions_payees

    # 70 hors 706 (déjà compté ci-dessus) : ventes/produits accessoires marginaux pour un
    # prêteur mais pas explicitement exclus (ex. 707 produits accessoires reste plausible).
    autres_produits = (ps("70") - ps("706")) + ps("71") + ps("75") + ps("78") + ps("79")
    produit_net_activites = marge_financiere + commissions_nettes + autres_produits

    charges_personnel = ps("66")
    # 63 hors 631 (déjà compté dans les commissions payées, pas à soustraire deux fois).
    achats_et_charges_generales = (ps("60") - ps("601") - ps("602") - ps("603")) \
        + ps("61") + ps("62") + (ps("63") - commissions_payees) + ps("64")
    autres_charges = ps("65") - ps("659")                 # 659 isolé plus bas (coût du risque)
    dotations_amortissements = ps("68")
    charges_generales_exploitation = charges_personnel + achats_et_charges_generales \
        + autres_charges + dotations_amortissements

    resultat_brut_exploitation = produit_net_activites - charges_generales_exploitation

    # Coût du risque = dotations aux provisions/dépréciations liées au risque de crédit.
    cout_du_risque = ps("659") + ps("691") + ps("697")
    resultat_exploitation = resultat_brut_exploitation - cout_du_risque

    produits_hao = ps("82") + ps("84") + ps("86") + ps("88")
    charges_hao = ps("81") + ps("83") + ps("85")
    resultat_hao = produits_hao - charges_hao

    resultat_net = resultat_exploitation + resultat_hao - ps("87") - ps("89")

    def row(label: str, value: Decimal) -> dict:
        return {"label": label, "amount": float(value)}

    return {"rows": [
        row("Produits financiers (intérêts de prêts, gains de change)", produits_financiers),
        row("Charges financières (intérêts versés, pertes de change)", charges_financieres),
        row("Marge financière", marge_financiere),
        row("Commissions nettes (transactions, Mobile Money)", commissions_nettes),
        row("Autres produits d'activité", autres_produits),
        row("Produit net des activités", produit_net_activites),
        row("Charges générales d'exploitation", charges_generales_exploitation),
        row("Résultat brut d'exploitation", resultat_brut_exploitation),
        row("Coût du risque (provisions de crédit)", cout_du_risque),
        row("Résultat d'exploitation", resultat_exploitation),
        row("Résultat hors activités ordinaires", resultat_hao),
        row("Résultat net de l'exercice", resultat_net),
    ]}


def _cashflow_statement(*, as_of: date_cls | None = None) -> dict:
    """Flux de trésorerie simplifié : pour chaque écriture touchant un compte de trésorerie
    (classe 5), le mouvement net de trésorerie de cette écriture est classé selon la classe
    de sa CONTREPARTIE dominante (la ligne non-trésorerie du plus gros montant) — classe 2 =
    investissement, classe 1 = financement, classe 8 = hors activités ordinaires, le reste
    (3/4/6/7) = exploitation. Une écriture trésorerie<->trésorerie (ex. caisse vers banque)
    est un mouvement interne, exclue du flux (elle ne change pas la trésorerie globale)."""
    qs = JournalLine.objects.select_related("account", "entry")
    if as_of:
        qs = qs.filter(entry__date__lte=as_of)
    by_entry: dict[int, list] = {}
    for line in qs:
        by_entry.setdefault(line.entry_id, []).append(line)

    buckets = {"exploitation": Decimal("0"), "investissement": Decimal("0"),
               "financement": Decimal("0"), "hao": Decimal("0")}
    for lines in by_entry.values():
        treasury = [l for l in lines if l.account.class_no == 5]
        other = [l for l in lines if l.account.class_no != 5]
        if not treasury or not other:
            continue
        net = sum((l.debit - l.credit for l in treasury), start=Decimal("0"))
        dominant = max(other, key=lambda l: l.debit + l.credit)
        category = {2: "investissement", 1: "financement", 8: "hao"}.get(dominant.account.class_no, "exploitation")
        buckets[category] += net

    variation = sum(buckets.values(), start=Decimal("0"))
    return {
        "categories": [
            {"key": "exploitation", "label": "Flux de trésorerie liés à l'activité opérationnelle",
             "amount": float(buckets["exploitation"])},
            {"key": "investissement", "label": "Flux de trésorerie liés aux opérations d'investissement",
             "amount": float(buckets["investissement"])},
            {"key": "financement", "label": "Flux de trésorerie liés aux opérations de financement",
             "amount": float(buckets["financement"])},
            {"key": "hao", "label": "Flux liés aux opérations hors activités ordinaires",
             "amount": float(buckets["hao"])},
        ],
        "variationTresorerie": float(variation),
    }


def _accounts_report(totals: dict[str, dict], *, name_contains: tuple[str, ...] = (),
                      class_no: int | None = None, nature: str | None = None,
                      code_prefix: str | None = None) -> list[dict]:
    """Liste TOUS les comptes du plan (postés ou non) correspondant au filtre, complétée par
    leur solde s'ils ont des écritures — contrairement à `trial_balance`/`_normal_rows` qui
    ne listent que les comptes déjà mouvementés, utile pour voir d'un coup d'œil quels
    comptes de provisions/créances existent même à 0."""
    qs = ChartAccount.objects.all()
    if class_no is not None:
        qs = qs.filter(class_no=class_no)
    if nature is not None:
        qs = qs.filter(nature=nature)
    if code_prefix is not None:
        qs = qs.filter(code__startswith=code_prefix)
    rows = []
    for account in qs:
        if name_contains and not any(term in account.name.lower() for term in name_contains):
            continue
        v = totals.get(account.code)
        debit = v["debit"] if v else Decimal("0")
        credit = v["credit"] if v else Decimal("0")
        signed = (debit - credit) if account.nature in ("ACTIF", "CHARGE") else (credit - debit)
        rows.append({
            "code": account.code, "name": account.name, "nature": account.nature,
            "debit": float(debit), "credit": float(credit), "balance": float(signed),
        })
    return sorted(rows, key=lambda r: r["code"])
