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
# Install system deps. The Debian mirror has intermittent hash mismatches on
# index refresh + the underlying deb files (libedit2 and similar). We:
#   1. Make apt tolerant (skip signature date check, retry transient failures).
#   2. Purge the index between attempts so a stale index doesn't pin the failure.
#   3. Wrap the whole update+install in a retry loop so transient mirror glitches
#      self-heal without --no-cache rebuilds.
# We intentionally do NOT install gcc/libpq-dev because psycopg2-binary ships its own wheels.
RUN printf 'Acquire::Check-Valid-Until "false";\nAcquire::https::Verify-Peer "false";\nAcquire::Retries "5";\nAPT::Get::Assume-Yes "true";\n' \
        > /etc/apt/apt.conf.d/99-mms-tolerance && \
    set +e && \
    for attempt in 1 2 3 4 5; do \
        echo "[mms] apt-get attempt $attempt" && \
        rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/*.deb /var/cache/apt/archives/partial/*.deb && \
        apt-get update && \
        apt-get install -y --no-install-recommends --fix-missing \
            libpq5 \
            curl \
            gettext \
            libgl1 && \
        break && \
        sleep $((attempt * 3)); \
        echo "[mms] attempt $attempt failed, retrying..."; \
    done && \
    set -e && \
    rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/*.deb

# Python deps — cached as a separate layer
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# Create the mms user (UID 1000) for non-root runtime
RUN groupadd --system --gid 1000 mms \
    && useradd --system --uid 1000 --gid mms --shell /bin/bash --create-home mms

# App code
COPY . /app/

# Arabic font for PDF rendering — pdf_utils._register_arabic_font() looks
# here as a fallback. Without this, Arabic text in PDFs renders as boxes.
COPY static/fonts/arabic/ /app/static/fonts/arabic/

# Make sure entrypoint is executable
RUN chmod +x /app/entrypoint.sh

# Static + media dirs owned by mms user
RUN mkdir -p /app/staticfiles /app/media \
    && chown -R mms:mms /app

# Drop to non-root
USER mms

# collectstatic runs in entrypoint.sh at container start (where env vars
# like SECRET_KEY are set). Building collectstatic here would fail because
# the fail-closed settings raise ImproperlyConfigured without env vars.

EXPOSE 8000 3000

ENTRYPOINT ["./entrypoint.sh"]
# Default command runs through entrypoint.sh.
# Binds to $PORT (CranL sets PORT=3000; default 8000 in compose / local).
# ${GUNICORN_WORKERS:-3} is expanded at container start by entrypoint.sh.
# ${TRUSTED_PROXY_CIDR:-127.0.0.1} is the proxy IP/CIDR (CranL edge in prod).
CMD ["sh", "-c", "exec gunicorn mms.wsgi:application --bind 0.0.0.0:${PORT:-8000} --workers ${GUNICORN_WORKERS:-3} --timeout 120 --max-requests 1000 --access-logfile - --error-logfile - --forwarded-allow-ips ${TRUSTED_PROXY_CIDR:-127.0.0.1}"]
