#!/usr/bin/env bash
# Build the React app and deploy it to Azure Static Web Apps.
# Needs the SWA deployment token in SWA_DEPLOY_TOKEN and VITE_API_URL set.
set -euo pipefail

: "${SWA_DEPLOY_TOKEN:?Set SWA_DEPLOY_TOKEN (az staticwebapp secrets list ...)}"
export VITE_API_URL="${VITE_API_URL:-https://prcheck-dev-api.icyrock-ebce8b31.eastus2.azurecontainerapps.io}"

echo ">> Building frontend (VITE_API_URL=$VITE_API_URL) ..."
pushd frontend >/dev/null
npm ci
npm run build
popd >/dev/null

echo ">> Deploying to Static Web Apps ..."
npx --yes @azure/static-web-apps-cli deploy ./frontend/dist \
  --deployment-token "$SWA_DEPLOY_TOKEN" --env production

echo ">> Done."
