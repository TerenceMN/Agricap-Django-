# AGRICAP FINTECH — backend Django

API REST de la plateforme financière AGRICAP : crédit agricole (analyse de dossier, garanties, comité, décaissement), épargne, caisses et transactions, investissements, comptabilité, conformité KYC et service client.

L'authentification est déléguée à l'IdP AGRICAP (OIDC) : le backend ne gère aucun mot de passe, il valide le jeton Bearer auprès de `/userinfo`.

## Prérequis

- Python 3.12
- PostgreSQL en production (SQLite suffit en développement)

## Installation

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # Linux / macOS
pip install -r requirements.txt
copy .env.example .env           # puis renseigner les valeurs
python manage.py migrate
python manage.py runserver 8000
```

Les variables d'environnement attendues sont décrites dans `.env.example` : clé Django, hôtes autorisés, URL de l'IdP, origines CORS, dossier des classeurs Excel de référence et paramètres de la plateforme de paiement Makuta.

## Référentiels

Les barèmes et référentiels technico-économiques sont chargés depuis des classeurs Excel, puis versionnés en base. Les commandes d'amorçage se trouvent dans `credits/management/commands/` et `dataio/management/commands/`.

## Documentation de l'API

Le schéma OpenAPI et l'interface Swagger sont servis sur `/api/docs/`.

## Organisation

| Application | Rôle |
| --- | --- |
| `accounts`, `rbac`, `audit` | utilisateurs provisionnés depuis l'IdP, rôles et permissions, journal d'audit |
| `credits`, `referentiel`, `reference_data`, `dataio` | dossiers de crédit, moteur d'analyse, référentiels et templates de feuilles de besoins |
| `savings`, `caisses`, `transactions`, `ledger`, `fx` | épargne, caisses, mouvements, grand livre, taux de change |
| `investments`, `portfolio`, `accounting` | projets d'investissement, portefeuille de prêts, comptabilité |
| `compliance`, `contracts`, `assets`, `suppliers`, `partners`, `agencies` | KYC, contrats, actifs gageables, fournisseurs, partenaires, agences |
| `alerts`, `analytics`, `notifications`, `support` | alertes, indicateurs, SMS, tickets du service client |
| `config`, `common` | réglages Django et utilitaires partagés |

## Production

PostgreSQL et gunicorn derrière un serveur web. La configuration de la base se fait par variables d'environnement, voir `config/settings.py` (section `DATABASES`).
