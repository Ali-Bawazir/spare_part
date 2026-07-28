#!/usr/bin/env bash
# MMS entrypoint — runs once per container start.
# Responsibilities:
#   1. Wait for PostgreSQL to be reachable (with bounded retry).
#   2. Run `migrate` to bring the schema up to date.
#   3. Run `collectstatic` to refresh /app/staticfiles.
#   4. Optionally create the first superuser (idempotent).
#   5. `exec` the CMD passed by docker (default: gunicorn).
#
# Note: Demo data is NOT auto-seeded. After first boot, run:
#     docker compose exec web python manage.py seed_demo --full

set -euo pipefail

DB_HOST="${DB_HOST:-db}"
DB_PORT="${DB_PORT:-5432}"
DB_USER="${DB_USER:-mms_user}"
DB_NAME="${DB_NAME:-mms_db}"

# ─── 1. Wait for DB ──────────────────────────────────────────────────────────
echo "[entrypoint] Waiting for PostgreSQL at ${DB_HOST}:${DB_PORT} (db=${DB_NAME}, user=${DB_USER})..."

WAITED=0
MAX_WAIT=30
until python - <<'PY' 2>/dev/null
import os, sys
try:
    import psycopg2
except ImportError:
    sys.exit("psycopg2 is required in the image")
psycopg2.connect(
    host=os.environ.get("DB_HOST", "db"),
    port=int(os.environ.get("DB_PORT", "5432")),
    user=os.environ.get("DB_USER", "mms_user"),
    password=os.environ.get("DB_PASSWORD", ""),
    dbname=os.environ.get("DB_NAME", "mms_db"),
    connect_timeout=2,
).close()
PY
do
    WAITED=$((WAITED + 1))
    if [ "$WAITED" -ge "$MAX_WAIT" ]; then
        echo "[entrypoint] ERROR: PostgreSQL not reachable after ${MAX_WAIT}s." >&2
        exit 1
    fi
    sleep 1
done
echo "[entrypoint] PostgreSQL is up (waited ${WAITED}s)."

# ─── 2. Migrations ──────────────────────────────────────────────────────────
echo "[entrypoint] Running database migrations..."
python manage.py migrate --noinput

# ─── 3. Static files ────────────────────────────────────────────────────────
echo "[entrypoint] Collecting static files..."
python manage.py collectstatic --noinput

# ─── 4. Optional superuser ───────────────────────────────────────────────────
if [ "${MMS_CREATE_SUPERUSER:-0}" = "1" ]; then
    echo "[entrypoint] MMS_CREATE_SUPERUSER=1 — checking for existing superuser..."
    python - <<'PY'
import os
import django
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "mms.settings")
django.setup()
from django.contrib.auth import get_user_model
User = get_user_model()
if User.objects.filter(is_superuser=True).exists():
    print("[entrypoint] A superuser already exists; skipping creation.")
else:
    username = os.environ.get("DJANGO_SUPERUSER_USERNAME", "admin")
    email = os.environ.get("DJANGO_SUPERUSER_EMAIL", "")
    password = os.environ.get("DJANGO_SUPERUSER_PASSWORD", "")
    if not password:
        print("[entrypoint] ERROR: MMS_CREATE_SUPERUSER=1 but DJANGO_SUPERUSER_PASSWORD is empty.", flush=True)
        raise SystemExit(1)
    User.objects.create_superuser(username=username, email=email, password=password)
    print(f"[entrypoint] Created superuser '{username}' (email={email!r}). Change the password on first login.")
PY
else
    echo "[entrypoint] MMS_CREATE_SUPERUSER != 1 — skipping superuser creation."
fi

# ─── 5. Hand off to CMD ─────────────────────────────────────────────────────
# If the first arg is `gunicorn`, rebuild the arg list to substitute
# `--workers <literal>` with the value of $GUNICORN_WORKERS (default 3).
# The input is the CMD array from compose; output is the same array with
# the workers count replaced. If the operator never passed `--workers`,
# the value still gets appended so gunicorn picks it up.
if [ "${1:-}" = "gunicorn" ]; then
    WORKERS="${GUNICORN_WORKERS:-3}"
    new_args=(gunicorn)
    i=1
    found_workers=0
    while [ $i -lt $# ]; do
        i=$((i + 1))
        arg="${!i}"
        if [ "$arg" = "--workers" ]; then
            new_args+=("--workers" "$WORKERS")
            i=$((i + 1))  # skip the literal value
            found_workers=1
        else
            new_args+=("$arg")
        fi
    done
    if [ "$found_workers" = "0" ]; then
        new_args+=("--workers" "$WORKERS")
    fi
    set -- "${new_args[@]}"
fi

echo "[entrypoint] Handing off to: $*"
exec "$@"
