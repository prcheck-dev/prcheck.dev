#!/usr/bin/env bash
# Resync the database credential: reset the Postgres admin password and update
# the container app's (and migration job's) DATABASE_URL secret to match, then
# restart and run migrations. Fixes "password authentication failed for pcadmin".
set -euo pipefail

RG=prcheck-dev-rg
APP=prcheck-dev-api
PG=prcheck-dev-pg-0108f4
JOB=prcheck-dev-migrate
PGHOST=$PG.postgres.database.azure.com
PGADMIN=pcadmin
PGDB=prcheck

NEWPW=$(openssl rand -base64 24 | tr -d '/+=' | cut -c1-24)
DBURL="postgresql://$PGADMIN:$NEWPW@$PGHOST:5432/$PGDB?sslmode=require"

echo ">> Resetting Postgres admin password ..."
az postgres flexible-server update -g "$RG" -n "$PG" --admin-password "$NEWPW" >/dev/null

echo ">> Updating container app database-url secret ..."
az containerapp secret set -n "$APP" -g "$RG" --secrets "database-url=$DBURL" >/dev/null

echo ">> Forcing a new revision so the app picks it up ..."
az containerapp update -n "$APP" -g "$RG" --set-env-vars "DB_RESYNC_TS=$(date +%s)" >/dev/null

echo ">> Syncing + running the migration job (if present) ..."
if az containerapp job show -n "$JOB" -g "$RG" >/dev/null 2>&1; then
  az containerapp job secret set -n "$JOB" -g "$RG" --secrets "database-url=$DBURL" >/dev/null 2>&1 \
    || echo "   (could not update job secret automatically; migrations may need a manual run)"
  az containerapp job start -n "$JOB" -g "$RG" >/dev/null || true
else
  echo "   migration job not found — if tables are missing, create it via setup-infra.sh"
fi

echo
echo ">> Done. Re-test by reopening PR #1 (Close then Reopen) or opening a new PR."
echo "   Watch logs:  az containerapp logs show -n $APP -g $RG --tail 30 --type console"
