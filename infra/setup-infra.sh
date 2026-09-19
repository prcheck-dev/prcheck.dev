#!/usr/bin/env bash
# One-time provisioning that finishes the live deployment.
#
# The base resources (resource group, ACR, Postgres, Container Apps env, Static
# Web App) already exist -- see infra/README.md. This script wires up the parts
# that handle secrets, which is why it is run by a human with `az login` rather
# than baked into an agent transcript:
#
#   * rotates the Postgres admin password (so nothing pre-existing is needed)
#   * creates the API container app with DATABASE_URL + DJANGO_SECRET_KEY secrets
#   * grants the app's managed identity read access to the EXISTING key vault and
#     references github-client-id / github-client-secret from it (no plaintext)
#   * creates + runs the migration job
#
# Re-running is safe-ish: it uses `create` calls that will error if a resource
# already exists. Delete the app/job first if you need a clean re-run.
set -euo pipefail

# ---- config ---------------------------------------------------------------- #
RG=prcheck-dev-rg
LOC=eastus2
ACR=prcheckdevacr0108f4
IMAGE=prcheck-dev-api
ENVNAME=prcheck-dev-env
APP=prcheck-dev-api
JOB=prcheck-dev-migrate

PG=prcheck-dev-pg-0108f4
PGHOST=$PG.postgres.database.azure.com
PGADMIN=pcadmin
PGDB=prcheck

# The existing vault that holds the GitHub OAuth credentials.
KV_URI=https://prcheck-kv-siiadi.vault.azure.net/secrets
KV_NAME=prcheck-kv-siiadi

FEHOST=black-coast-04fbadc0f.3.azurestaticapps.net
API_FQDN=$APP.icyrock-ebce8b31.eastus2.azurecontainerapps.io
# ---------------------------------------------------------------------------- #

echo ">> Rotating Postgres admin password ..."
PGPW=$(openssl rand -base64 24 | tr -d '/+=' | cut -c1-24)
az postgres flexible-server update -g "$RG" -n "$PG" --admin-password "$PGPW" >/dev/null
DBURL="postgresql://$PGADMIN:$PGPW@$PGHOST:5432/$PGDB?sslmode=require"

echo ">> Generating Django secret key ..."
DJ_SECRET=$(python3 -c "import secrets;print(secrets.token_urlsafe(64))")

echo ">> Creating container app $APP ..."
az containerapp create \
  --name "$APP" --resource-group "$RG" --environment "$ENVNAME" \
  --image "$ACR.azurecr.io/$IMAGE:latest" \
  --registry-server "$ACR.azurecr.io" --registry-identity system \
  --ingress external --target-port 8000 \
  --min-replicas 1 --max-replicas 3 --cpu 0.5 --memory 1.0Gi \
  --secrets "django-secret-key=$DJ_SECRET" "database-url=$DBURL" \
  --env-vars \
    "DJANGO_DEBUG=False" \
    "DJANGO_SECRET_KEY=secretref:django-secret-key" \
    "DATABASE_URL=secretref:database-url" \
    "DJANGO_ALLOWED_HOSTS=$API_FQDN" \
    "DJANGO_CSRF_TRUSTED_ORIGINS=https://$API_FQDN" \
    "CORS_ALLOWED_ORIGINS=https://$FEHOST" \
    "GITHUB_OAUTH_REDIRECT_URI=https://$API_FQDN/api/auth/github/callback/" \
    "AUTH_COOKIE_SECURE=True" "AUTH_COOKIE_SAMESITE=Strict"

echo ">> Granting the app's identity read access to $KV_NAME ..."
APP_PRINCIPAL=$(az containerapp show -n "$APP" -g "$RG" \
  --query identity.principalId -o tsv)
az keyvault set-policy -n "$KV_NAME" --object-id "$APP_PRINCIPAL" \
  --secret-permissions get >/dev/null

echo ">> Referencing GitHub OAuth secrets from the key vault ..."
az containerapp secret set -n "$APP" -g "$RG" --secrets \
  "github-client-id=keyvaultref:$KV_URI/github-client-id,identityref:system" \
  "github-client-secret=keyvaultref:$KV_URI/github-client-secret,identityref:system"
az containerapp update -n "$APP" -g "$RG" --set-env-vars \
  "GITHUB_OAUTH_CLIENT_ID=secretref:github-client-id" \
  "GITHUB_OAUTH_CLIENT_SECRET=secretref:github-client-secret"

echo ">> Creating migration job $JOB ..."
az containerapp job create \
  --name "$JOB" --resource-group "$RG" --environment "$ENVNAME" \
  --trigger-type Manual --replica-timeout 600 --replica-retry-limit 1 \
  --image "$ACR.azurecr.io/$IMAGE:latest" \
  --registry-server "$ACR.azurecr.io" --registry-identity system \
  --cpu 0.5 --memory 1.0Gi \
  --secrets "django-secret-key=$DJ_SECRET" "database-url=$DBURL" \
  --env-vars \
    "DJANGO_DEBUG=False" \
    "DJANGO_SECRET_KEY=secretref:django-secret-key" \
    "DATABASE_URL=secretref:database-url" \
    "DJANGO_ALLOWED_HOSTS=$API_FQDN" "SECURE_SSL_REDIRECT=False" \
  --command "python" "manage.py" "migrate" "--noinput"

echo ">> Running first migration ..."
az containerapp job start -n "$JOB" -g "$RG"

echo
echo ">> Done. API:      https://$API_FQDN/api/health/"
echo ">> Frontend:       https://$FEHOST"
echo ">> Remember to add https://$API_FQDN/api/auth/github/callback/"
echo "   to the GitHub OAuth App's callback URLs."
