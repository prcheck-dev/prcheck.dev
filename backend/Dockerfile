# syntax=docker/dockerfile:1
FROM python:3.13-slim

# Fail fast, no .pyc, unbuffered logs.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8000

WORKDIR /app

# System deps for psycopg / building wheels are already covered by the binary
# wheels; keep the image lean.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Collect static assets at build time so the image is self-contained.
# A throwaway key/env is fine here; nothing secret is baked in.
RUN DJANGO_DEBUG=False DJANGO_SECRET_KEY=build-only \
    DJANGO_ALLOWED_HOSTS=localhost SECURE_SSL_REDIRECT=False \
    python manage.py collectstatic --noinput

# Run as a non-root user.
RUN useradd --system --uid 1001 appuser && chown -R appuser /app
USER appuser

EXPOSE 8000

# entrypoint.sh migrates, then execs gunicorn.
CMD ["sh", "./entrypoint.sh"]
