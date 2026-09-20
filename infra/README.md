# Infrastructure & deployment

All deployment lives here, outside `frontend/` and `backend/`. CI/CD workflows
are under `.github/workflows/` (GitHub only reads workflows from that path); the
scripts they call live here and are runnable by hand too.

## Azure resources (resource group `prcheck-dev-rg`)

This is a **brand-new** resource group, separate from the live `prcheck-rg`.

| Resource | Name | Notes |
|----------|------|-------|
| Container Registry | `prcheckdevacr0108f4` | Hosts the backend image |
| PostgreSQL Flexible Server | `prcheck-dev-pg-0108f4` | `centralindia` (eastus2 is region-restricted on this subscription); DB `prcheck` |
| Container Apps env | `prcheck-dev-env` | `eastus2` |
| Container App (API) | `prcheck-dev-api` | Django + gunicorn, port 8000 |
| Container Apps Job | `prcheck-dev-migrate` | Runs `manage.py migrate` |
| Static Web App | `prcheck-dev-web` | React frontend (Free tier) |

- **API URL:** `https://prcheck-dev-api.icyrock-ebce8b31.eastus2.azurecontainerapps.io`
- **Frontend URL:** `https://black-coast-04fbadc0f.3.azurestaticapps.net`

### GitHub OAuth secrets

The API reads `GITHUB_OAUTH_CLIENT_ID` / `GITHUB_OAUTH_CLIENT_SECRET` from the
**existing** key vault `prcheck-kv-siiadi` (secrets `github-client-id` /
`github-client-secret`) via the container app's managed identity. Nothing is
copied and no plaintext is exposed; the only change to the old resource group is
a read-only access-policy grant for the new app's identity.

## First-time setup

The base resources above are already created. To finish wiring the secret-bearing
pieces (container app, migration job, key-vault references) run, from the repo
root with `az login` done:

```bash
./infra/setup-infra.sh
```

This rotates the DB password itself, so you don't need any previously generated
value.

## CI/CD

Two workflows, both trigger on **push** (path-filtered) and **manually**
(`workflow_dispatch` from the Actions tab):

- **`backend.yml`** — runs Django tests, builds the image in ACR (tagged with the
  commit SHA), rolls it out to the container app, and runs migrations.
- **`frontend.yml`** — builds the Vite app and uploads it to Static Web Apps.

### Required GitHub configuration

Settings → Secrets and variables → Actions:

**Secrets**
| Name | How to get it |
|------|---------------|
| `AZURE_CREDENTIALS` | `az ad sp create-for-rbac --name prcheck-dev-cicd --role contributor --scopes /subscriptions/<SUB_ID>/resourceGroups/prcheck-dev-rg --sdk-auth` (paste the JSON) |
| `AZURE_STATIC_WEB_APPS_API_TOKEN` | `az staticwebapp secrets list -n prcheck-dev-web -g prcheck-dev-rg --query properties.apiKey -o tsv` |

**Variables**
| Name | Value |
|------|-------|
| `VITE_API_URL` | `https://api.prcheck.dev` (or the Azure FQDN before cutover) |

The review engine's model and GitHub integration settings are runtime
configuration on the API container (`PRCHECK_*`, `ANTHROPIC_*`, or
`PRCHECK_OPENAI_*`); do not commit those values or place them in workflow
variables. The existing deployment keeps OAuth credentials in Key Vault.

## Custom domains (prcheck.dev)

DNS is managed at **Cloudflare** (not Azure). The apex/`api` currently point at
the old system, so switching them over is a **cutover**: run
[`cutover-domain.sh`](cutover-domain.sh), which frees the domains from the old
resources, deploys the frontend to the new SWA, and prints the exact Cloudflare
records to paste.

```bash
az login
./infra/cutover-domain.sh prepare   # deploys FE, frees domains, prints DNS records
# ... paste the printed records into Cloudflare (DNS-only / grey cloud) ...
./infra/cutover-domain.sh bind      # validates + issues managed TLS certs
```

Target mapping:

| Host | Points to |
|------|-----------|
| `prcheck.dev` (apex) | `prcheck-dev-web` (Static Web App) |
| `www.prcheck.dev` | `prcheck-dev-web` |
| `api.prcheck.dev` | `prcheck-dev-api` (Container App) |

GitHub OAuth callback after cutover:
`https://api.prcheck.dev/api/auth/github/callback/`

## Manual deploys (without CI)

```bash
az login
./infra/deploy-backend.sh            # build + roll out + migrate
SWA_DEPLOY_TOKEN=... ./infra/deploy-frontend.sh
```

## Post-deploy: GitHub OAuth callback

For GitHub login to work, add this callback URL to the GitHub OAuth App:

```
https://prcheck-dev-api.icyrock-ebce8b31.eastus2.azurecontainerapps.io/api/auth/github/callback/
```
