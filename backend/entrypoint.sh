#!/bin/sh
# Apply migrations before serving. Running them here (rather than a separate job)
# keeps the schema in lockstep with the deployed image with no extra moving parts.
# At min-replicas 1 the startup order is effectively serialized; Django's own
# migration guarding tolerates the occasional overlap on scale-up.
set -e

python manage.py migrate --noinput

exec gunicorn core.wsgi:application \
  --bind "0.0.0.0:${PORT:-8000}" \
  --workers 3 --timeout 60 \
  --access-logfile - --error-logfile -
