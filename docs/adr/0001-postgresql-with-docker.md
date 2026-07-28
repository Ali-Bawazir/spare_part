# PostgreSQL with Docker Containerization

SQLite is insufficient for production: it lacks row-level locking, serializable
transactions, and advisory locks needed for concurrent stock operations and
future background jobs.

## Decision

Migrate to PostgreSQL, running in Docker via docker-compose alongside the Django
application container. Redis for Celery (Phase 2) also containerized.
All configuration via environment variables — no hardcoded credentials.

## Consequences

- psycopg2-binary driver; CONN_MAX_AGE=60 for connection pooling
- Migrations via JSON dumpdata/loaddata (NOT management commands alone, to preserve WO number sequence)
- Docker setup scaffolded from scratch: docker-compose.yml, Dockerfile, .env.example
- PostgreSQL is the single source of truth; SQLite is retired
- Rollback: revert DATABASES config in settings.py and re-loaddata from backup.json

## Operational consequences (2026-07-08)

The Docker setup was hardened for the company-server production deploy.
Changes from the original (Phase 1.1) implementation:

- **Production web server is gunicorn** (not `runserver`). `GUNICORN_WORKERS`
  env var (default 3) controls worker count. Configurable per-host without
  rebuilding the image.
- **Non-root runtime.** Image builds a `mms` user (UID 1000) and runs as that
  user. The image is ~923 MB and contains Python 3.11 + gunicorn 21+ + libpq5 +
  libgl1 (for opencv) + tesseract reserved for Phase 2.
- **Health endpoint** at `GET /health/` returns 200 `{"status":"ok","db":"ok"}` or
  503 `{"status":"degraded","db":"error","db_error":"..."}`. Used by both the
  docker healthcheck and any external uptime monitor.
- **Entrypoint sequence** (in `entrypoint.sh`):
  1. Wait for PostgreSQL (psycopg2 probe, max 30s).
  2. `python manage.py migrate --noinput`.
  3. `python manage.py collectstatic --noinput`.
  4. Optional: `MMS_CREATE_SUPERUSER=1` + `DJANGO_SUPERUSER_*` envvars → create
     superuser IF none exists (idempotent).
  5. `exec "$@"` to gunicorn.
- **Demo data is NOT auto-seeded.** Operator runs
  `docker compose exec web python manage.py seed_demo --full` manually.
- **SQLite fallback is opt-in via `MMS_USE_SQLITE=1`.** Used by tests and CI
  only. A warning is emitted in DEBUG mode if neither SQLite nor DB_* envvars
  are set.
- **Redis service removed from compose.** Celery/Redis land in Phase 2.
- **`docker-compose.yml` is the single source of truth for deploy.** No
  `docker-compose.override.yml` and no source bind-mount. Devs run the app
  natively with `python manage.py runserver` for hot-reload.
- **`.env` is git-ignored.** Only `.env.example` is committed. The compose file
  uses `${VAR:?msg}` syntax to fail-fast with a clear error if any required
  var is missing.
- **Volumes** are named (`mms_mms_postgres_data`, `mms_mms_static`,
  `mms_mms_media`). The double `mms_` prefix comes from the top-level
  `name: mms` directive in compose; do not rename without updating the
  service volume references.
- **Arabic content stores correctly** because the Postgres initdb args
  include `--encoding=UTF8 --lc-collate=C --lc-ctype=C`. Without this,
  Arabic collation and sort order would be wrong on some images.

## Verification

After deployment, verify all of:

- [x] `docker compose up -d --build` → both services `healthy` within 30s.
- [x] `curl /health/` → `{"status":"ok","db":"ok"}` with HTTP 200.
- [x] `MMS_USE_SQLITE=1 python manage.py test` → 428 tests pass.
- [x] `/accounts/login/` returns Arabic title when cookie `django_language=ar`
      is set.
- [x] Auth'd pages (e.g. `/work-orders/emergency/`) render Arabic form labels
      and sidebar in the language set via `/i18n/setlang/`.

## Phase 2 (deferred)

- Redis service in compose (returns with Celery).
- `celery` worker + beat services.
- Automated `pg_dump` backup via cron.
- Reverse-proxy / TLS termination guidance.
- Monitoring integration (Prometheus, log shipping).

See [`docs/DOCKER.md`](../DOCKER.md) for the deployment runbook.
