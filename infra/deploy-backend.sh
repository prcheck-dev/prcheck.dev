#!/usr/bin/env bash
# Build the backend image in ACR, roll it out to the container app, and migrate.
# Safe to run locally (needs `az login`) or from CI.
set -euo pipefail

RG="${AZURE_RESOURCE_GROUP:-prcheck-dev-rg}"
ACR="${ACR_NAME:-prcheckdevacr0108f4}"
IMAGE="${IMAGE_NAME:-prcheck-dev-api}"
APP="${CONTAINERAPP_NAME:-prcheck-dev-api}"
JOB="${MIGRATION_JOB:-prcheck-dev-migrate}"
TAG="${1:-$(git rev-parse --short HEAD 2>/dev/null || echo latest)}"

echo ">> Building $IMAGE:$TAG in $ACR ..."
az acr build -r "$ACR" -t "$IMAGE:$TAG" -t "$IMAGE:latest" ./backend

echo ">> Rolling out to container app $APP ..."
az containerapp update -n "$APP" -g "$RG" \
  --image "$ACR.azurecr.io/$IMAGE:$TAG"

echo ">> Running migrations (job: $JOB) ..."
az containerapp job start -n "$JOB" -g "$RG"

echo ">> Done."
