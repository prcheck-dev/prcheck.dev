#!/usr/bin/env bash
# Store the GitHub App credentials in Key Vault and wire the container app to use
# the App for BOTH user login (OAuth) and the reviewer bot (installation tokens).
#
# Reads local, gitignored files:
#   infra/app_handover.md      app_id= / client_id= / client_secret=
#   infra/github_app_key.pem   the App private key
#   infra/gh_app_webhook.txt   the webhook secret
#
# Run from the repo root with `az login` done:  ./infra/setup-github-app.sh
set -euo pipefail

RG=prcheck-dev-rg
APP=prcheck-dev-api
KV=prcheck-dev-kv-0108f4
KV_URI="https://$KV.vault.azure.net/secrets"

APP_ID=$(grep -E '^app_id=' infra/app_handover.md | cut -d= -f2- | tr -d ' \r')
CLIENT_ID=$(grep -E '^client_id=' infra/app_handover.md | cut -d= -f2- | tr -d ' \r')
CLIENT_SECRET=$(grep -E '^client_secret=' infra/app_handover.md | cut -d= -f2- | tr -d ' \r')
WEBHOOK_SECRET=$(tr -d ' \t\r\n' < infra/gh_app_webhook.txt)
# Base64 so the multi-line PEM survives env-var injection cleanly.
PK_B64=$(base64 -i infra/github_app_key.pem | tr -d '\n')

for v in APP_ID CLIENT_ID CLIENT_SECRET WEBHOOK_SECRET PK_B64; do
  [ -n "${!v}" ] || { echo "!! $v is empty — check the infra files." >&2; exit 1; }
done
echo ">> App ID: $APP_ID (client id + secret + key + webhook loaded)"

echo ">> Writing secrets into $KV ..."
az keyvault secret set --vault-name "$KV" -n github-app-private-key-b64 --value "$PK_B64"        >/dev/null
az keyvault secret set --vault-name "$KV" -n github-client-id           --value "$CLIENT_ID"     >/dev/null
az keyvault secret set --vault-name "$KV" -n github-client-secret       --value "$CLIENT_SECRET" >/dev/null
az keyvault secret set --vault-name "$KV" -n github-webhook-secret      --value "$WEBHOOK_SECRET">/dev/null

echo ">> Pointing container app secrets at the vault (managed identity) ..."
az containerapp secret set -n "$APP" -g "$RG" --secrets \
  "github-app-private-key-b64=keyvaultref:$KV_URI/github-app-private-key-b64,identityref:system" \
  "github-client-id=keyvaultref:$KV_URI/github-client-id,identityref:system" \
  "github-client-secret=keyvaultref:$KV_URI/github-client-secret,identityref:system" \
  "github-webhook-secret=keyvaultref:$KV_URI/github-webhook-secret,identityref:system" >/dev/null

echo ">> Binding env vars ..."
az containerapp update -n "$APP" -g "$RG" --set-env-vars \
  "PRCHECK_GITHUB_APP_ID=$APP_ID" \
  "PRCHECK_GITHUB_APP_PRIVATE_KEY_B64=secretref:github-app-private-key-b64" \
  "GITHUB_OAUTH_CLIENT_ID=secretref:github-client-id" \
  "GITHUB_OAUTH_CLIENT_SECRET=secretref:github-client-secret" \
  "PRCHECK_GITHUB_WEBHOOK_SECRET=secretref:github-webhook-secret" >/dev/null

echo
API=https://api.prcheck.dev
echo ">> Done. Smoke test:"
echo "   health : $(curl -s -m 20 $API/api/health/)"
echo "   login  : HTTP $(curl -s -m 20 -o /dev/null -w '%{http_code}' $API/api/auth/github/login/)"
echo
echo ">> Make sure the GitHub App has:"
echo "     Callback URL : $API/api/auth/github/callback/"
echo "     Webhook URL  : $API/api/reviews/webhook/github/  (secret = gh_app_webhook.txt)"
echo "     Permissions  : Contents:read, Pull requests:read+write, Metadata:read"
echo "     Events       : Pull request     and the App installed on your repo(s)"
