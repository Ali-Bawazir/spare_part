# syntax=docker/dockerfile:1
# MMS (Bawazir Factory Maintenance) — production image
# Base: python:3.11-slim (Debian Bookworm slim; current stable)
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    DEBIAN_FRONTEND=noninteractive

WORKDIR /app

# System deps:
#   libpq5     — Postgres client (psycopg2-binary links against it)
#   curl       — for the /health/ endpoint used by docker healthcheck
#   gettext    — required by Django's i18n runtime
#   libgl1     — required by opencv-python-headless
# We intentionally do NOT install gcc/libpq-dev because psycopg2-binary ships its own wheels.
# tesseract-ocr is reserved for Phase 2 OCR pipelines; add here when needed.
# Retry once on transient 404s from the mirror (Bookworm repo has occasional
# stale index files). The most common failure is "Unable to fetch some archives,
# maybe run apt-get update or try with --fix-missing?"
RUN for i in 1 2 3; do \
        apt-get update && break || sleep 2; \
    done \
    && apt-get install -y --no-install-recommends --fix-missing \
        libpq5 \
        curl \
        gettext \
        libgl1 \
    && rm -rf /var/lib/apt/lists/*

# Python deps — cached as a separate layer
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# Create the mms user (UID 1000) for non-root runtime
RUN groupadd --system --gid 1000 mms \
    && useradd --system --uid 1000 --gid mms --shell /bin/bash --create-home mms

# App code
COPY . /app/

# Make sure entrypoint is executable
RUN chmod +x /app/entrypoint.sh

# Static + media dirs owned by mms user
RUN mkdir -p /app/staticfiles /app/media \
    && chown -R mms:mms /app

# Drop to non-root
USER mms

# Pre-collect staticfiles at build time so first boot is fast
# (idempotent — collectstatic at startup will be a no-op if nothing changed)
RUN python manage.py collectstatic --noinput || true

EXPOSE 8000

ENTRYPOINT ["./entrypoint.sh"]
# Default command runs through entrypoint.sh; ${GUNICORN_WORKERS:-3} is expanded
# at container start by entrypoint.sh so the var is read from the env, not the image.
CMD ["gunicorn", "mms.wsgi:application", \
     "--bind", "0.0.0.0:8000", \
     "--workers", "${GUNICORN_WORKERS:-3}", \
     "--access-logfile", "-", \
     "--error-logfile", "-", \
     "--forwarded-allow-ips", "*"]
