#!/usr/bin/env bash
# Populate the new Key Vault with the app's runtime secrets, wire the container
# app to reference them via its managed identity, and restart.
#
# The vault (prcheck-dev-kv-0108f4) and the app's read access already exist; this
# script only sets secret *values* and points the app at them, which is why a
# human runs it (with `az login`) rather than an agent.
#
# Provide the values you control as environment variables, then run:
#   GH_OAUTH_CLIENT_ID=xxx \
#   GH_OAUTH_CLIENT_SECRET=xxx \
#   PR_GITHUB_TOKEN=ghp_xxx \
#   ./infra/setup-secrets.sh
#
# GH_OAUTH_*        : from a fresh GitHub OAuth App (callback
#                     https://api.prcheck.dev/api/auth/github/callback/)
# PR_GITHUB_TOKEN   : fine-grained PAT, Contents:read + Pull requests:read/write
# WEBHOOK_SECRET    : optional; any random string, also set on the GitHub webhook
# The Azure OpenAI key is fetched automatically from your AI Services account.
set -euo pipefail

RG=prcheck-dev-rg
APP=prcheck-dev-api
KV=prcheck-dev-kv-0108f4
KV_URI="https://$KV.vault.azure.net/secrets"

AOAI_ACCOUNT=db-6569201-resource-1201
AOAI_RG=db-6569201

: "${GH_OAUTH_CLIENT_ID:?set GH_OAUTH_CLIENT_ID}"
: "${GH_OAUTH_CLIENT_SECRET:?set GH_OAUTH_CLIENT_SECRET}"
: "${PR_GITHUB_TOKEN:?set PR_GITHUB_TOKEN}"
WEBHOOK_SECRET="${WEBHOOK_SECRET:-$(openssl rand -hex 24)}"

echo ">> Fetching the Azure OpenAI key ..."
AOAI_KEY=$(az cognitiveservices account keys list -n "$AOAI_ACCOUNT" -g "$AOAI_RG" \
  --query key1 -o tsv)

echo ">> Writing secret values into $KV ..."
az keyvault secret set --vault-name "$KV" -n github-client-id     --value "$GH_OAUTH_CLIENT_ID"     >/dev/null
az keyvault secret set --vault-name "$KV" -n github-client-secret --value "$GH_OAUTH_CLIENT_SECRET" >/dev/null
az keyvault secret set --vault-name "$KV" -n prcheck-github-token --value "$PR_GITHUB_TOKEN"        >/dev/null
az keyvault secret set --vault-name "$KV" -n github-webhook-secret --value "$WEBHOOK_SECRET"        >/dev/null
az keyvault secret set --vault-name "$KV" -n openai-api-key       --value "$AOAI_KEY"               >/dev/null

echo ">> Pointing the container app's secrets at the vault (managed identity) ..."
az containerapp secret set -n "$APP" -g "$RG" --secrets \
  "github-client-id=keyvaultref:$KV_URI/github-client-id,identityref:system" \
  "github-client-secret=keyvaultref:$KV_URI/github-client-secret,identityref:system" \
  "prcheck-github-token=keyvaultref:$KV_URI/prcheck-github-token,identityref:system" \
  "github-webhook-secret=keyvaultref:$KV_URI/github-webhook-secret,identityref:system" \
  "openai-api-key=keyvaultref:$KV_URI/openai-api-key,identityref:system" >/dev/null

echo ">> Binding env vars to those secrets ..."
az containerapp update -n "$APP" -g "$RG" --set-env-vars \
  "GITHUB_OAUTH_CLIENT_ID=secretref:github-client-id" \
  "GITHUB_OAUTH_CLIENT_SECRET=secretref:github-client-secret" \
  "PRCHECK_GITHUB_TOKEN=secretref:prcheck-github-token" \
  "PRCHECK_GITHUB_WEBHOOK_SECRET=secretref:github-webhook-secret" \
  "PRCHECK_OPENAI_API_KEY=secretref:openai-api-key" >/dev/null

echo
echo ">> Done. Smoke test:"
API=https://prcheck-dev-api.icyrock-ebce8b31.eastus2.azurecontainerapps.io
echo "   health : $(curl -s -m 20 $API/api/health/)"
echo "   login  : HTTP $(curl -s -m 20 -o /dev/null -w '%{http_code}' $API/api/auth/github/login/) (302 = OAuth configured)"
echo
echo ">> Testing the Azure OpenAI deployment directly ..."
AOAI_URL="https://$AOAI_ACCOUNT.cognitiveservices.azure.com/openai/deployments/gpt-5.2/chat/completions?api-version=2025-04-01-preview"
code=$(curl -s -o /tmp/aoai_test.json -w "%{http_code}" "$AOAI_URL" \
  -H "api-key: $AOAI_KEY" -H "content-type: application/json" \
  -d '{"messages":[{"role":"user","content":"reply with ok"}],"max_completion_tokens":16}')
if [ "$code" = "200" ]; then
  echo "   Azure OpenAI gpt-5.2: OK (HTTP 200)"
else
  echo "   Azure OpenAI gpt-5.2: HTTP $code — check PRCHECK_OPENAI_API_VERSION."
  head -c 400 /tmp/aoai_test.json; echo
fi
echo
echo ">> Reviewer now uses Azure OpenAI (gpt-5.2). Trigger a real review with a"
echo "   logged-in session:  POST $API/api/reviews/ {\"repo\":\"owner/name\",\"pr_number\":N}"
