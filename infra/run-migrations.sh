#!/usr/bin/env bash
# Apply Django migrations to the deployed API's database.
#
# The running container app already has DATABASE_URL configured, so we just exec
# the migrate command inside a live replica -- no secrets are handled here.
set -euo pipefail

RG="${AZURE_RESOURCE_GROUP:-prcheck-dev-rg}"
APP="${CONTAINERAPP_NAME:-prcheck-dev-api}"

echo ">> Running migrations inside $APP ..."
# If this drops you into an interactive shell instead of running the command,
# type:  python manage.py migrate --noinput   then  exit
az containerapp exec -n "$APP" -g "$RG" --command "python manage.py migrate --noinput"
echo ">> Done."
