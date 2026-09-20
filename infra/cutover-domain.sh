#!/usr/bin/env bash
# Cut prcheck.dev + www + api.prcheck.dev over from the OLD system to the NEW one.
#
# DNS lives at Cloudflare, so this runs in two phases with a manual pause:
#   ./infra/cutover-domain.sh prepare   # deploy FE, free the domains, print DNS
#   <paste the printed records into Cloudflare, DNS-only / grey cloud>
#   ./infra/cutover-domain.sh bind      # validate + issue managed certs
#
# NOTE: `prepare` removes the custom domains from the OLD prod resources. Those
# hostnames go offline until the new bindings + DNS are live. This is the
# intended cutover.
set -euo pipefail

OLD_RG=prcheck-rg
NEW_RG=prcheck-dev-rg
OLD_SWA=prcheck-web
NEW_SWA=prcheck-dev-web
OLD_API=prcheck-api
NEW_API=prcheck-dev-api
NEW_ENV=prcheck-dev-env

NEW_API_FQDN=prcheck-dev-api.icyrock-ebce8b31.eastus2.azurecontainerapps.io
NEW_SWA_HOST=black-coast-04fbadc0f.3.azurestaticapps.net
# api.prcheck.dev domain-verification id for the NEW container app:
ASUID=0DD480A253D063E83A4586413D5024DF84E36BF0A6519F336E37EA3EAAC52FBC

ACTION="${1:-prepare}"

case "$ACTION" in
prepare)
  echo ">> [1/4] Building + deploying the React app to the NEW static web app ..."
  export VITE_API_URL=https://api.prcheck.dev
  ( cd frontend && npm ci && npm run build )
  SWA_TOKEN=$(az staticwebapp secrets list -n "$NEW_SWA" -g "$NEW_RG" \
    --query properties.apiKey -o tsv)
  npx --yes @azure/static-web-apps-cli deploy ./frontend/dist \
    --deployment-token "$SWA_TOKEN" --env production

  echo ">> [2/4] Removing the domains from the OLD prod resources (cutover) ..."
  az staticwebapp hostname delete -n "$OLD_SWA" -g "$OLD_RG" --hostname prcheck.dev --yes || true
  az staticwebapp hostname delete -n "$OLD_SWA" -g "$OLD_RG" --hostname www.prcheck.dev --yes || true
  az containerapp hostname delete -n "$OLD_API" -g "$OLD_RG" --hostname api.prcheck.dev --yes || true

  echo ">> [3/4] Registering the apex on the NEW static web app (for its TXT token) ..."
  az staticwebapp hostname set -n "$NEW_SWA" -g "$NEW_RG" --hostname prcheck.dev \
    --validation-method dns-txt-token --no-wait || true
  sleep 8
  APEX_TOKEN=$(az staticwebapp hostname show -n "$NEW_SWA" -g "$NEW_RG" \
    --hostname prcheck.dev --query validationToken -o tsv)
  if [ -z "$APEX_TOKEN" ]; then
    echo "!! Could not get the apex validation token. The domain is probably still" >&2
    echo "!! linked to the old static web app. Confirm it was removed, then re-run." >&2
    exit 1
  fi

  cat <<EOF

>> [4/4] ADD THESE RECORDS IN CLOUDFLARE  (set each to DNS-only / grey cloud)
==============================================================================
  TXT    @               ${APEX_TOKEN}
  CNAME  @         ->     ${NEW_SWA_HOST}
  CNAME  www       ->     ${NEW_SWA_HOST}
  CNAME  api       ->     ${NEW_API_FQDN}
  TXT    asuid.api        ${ASUID}
------------------------------------------------------------------------------
  * Delete the OLD apex A record and the OLD 'api' CNAME first.
  * Grey cloud (DNS-only) is required so Azure can issue the TLS certs.
  * Cloudflare flattens the apex CNAME to A records automatically.

  Once the records resolve (check: dig +short api.prcheck.dev), run:
      ./infra/cutover-domain.sh bind
==============================================================================
EOF
  ;;

bind)
  echo ">> Validating apex + www on the new static web app ..."
  az staticwebapp hostname set -n "$NEW_SWA" -g "$NEW_RG" --hostname prcheck.dev \
    --validation-method dns-txt-token
  az staticwebapp hostname set -n "$NEW_SWA" -g "$NEW_RG" --hostname www.prcheck.dev \
    --validation-method cname-delegation --no-wait || true

  echo ">> Binding api.prcheck.dev + managed cert on the new container app ..."
  az containerapp hostname add -n "$NEW_API" -g "$NEW_RG" --hostname api.prcheck.dev || true
  az containerapp hostname bind -n "$NEW_API" -g "$NEW_RG" --hostname api.prcheck.dev \
    --environment "$NEW_ENV" --validation-method CNAME

  echo
  echo ">> Cutover complete. Verify:"
  echo "     curl https://api.prcheck.dev/api/health/"
  echo "     open  https://prcheck.dev"
  echo ">> Then add this callback URL to the GitHub OAuth App:"
  echo "     https://api.prcheck.dev/api/auth/github/callback/"
  ;;

*)
  echo "usage: $0 [prepare|bind]" >&2
  exit 2
  ;;
esac
