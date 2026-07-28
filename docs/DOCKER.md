# MMS — Docker deployment

This document describes how to run MMS (Bawazir Factory Maintenance) in Docker
for the **company-server production deployment**.

> **Phase 1 (current):** PostgreSQL only. Redis/Celery land in Phase 2.
> **For local development with hot-reload:** run the app natively with
> `python manage.py runserver`; this Docker stack is *not* a dev override.

---

## 1. Prerequisites

- Docker Engine 24+ (or Docker Desktop on macOS/Windows).
- `docker compose` v2 (the modern `docker compose` CLI; not the legacy
  `docker-compose` Python tool).
- A Linux host (recommended) or macOS with Docker Desktop.
- ~1.5 GB free disk for the image + Postgres volume.

Verify your toolchain:

```bash
docker --version
docker compose version
```

---

## 2. Quick start (production deploy on the company server)

```bash
git clone <repo-url> /opt/mms
cd /opt/mms

# Copy the env template and fill in real values
cp .env.example .env
$EDITOR .env                          # REQUIRED: set DB_PASSWORD, SECRET_KEY, ALLOWED_HOSTS

# Build and start the stack
docker compose up -d --build

# Watch the logs to confirm boot succeeded
docker compose logs -f web
```

Within ~30 seconds the `web` container should report:

```
[entrypoint] PostgreSQL is up (waited 0s).
[entrypoint] Running database migrations...
[entrypoint] Collecting static files...
[entrypoint] MMS_CREATE_SUPERUSER=1 — Created superuser 'admin' (...)
[entrypoint] Handing off to: gunicorn mms.wsgi:application --bind 0.0.0.0:8000 --workers N ...
[INFO] Booting worker with pid: ...
```

Verify health:

```bash
curl -fsS http://localhost:8000/health/
# {"status": "ok", "db": "ok"}
```

Open the app in a browser: <http://localhost:8000/accounts/login/>
(Use the company's actual hostname if you're behind a reverse proxy.)

---

## 3. Architecture

```
   Browser  ──HTTP──▶  :8000  mms_web  (gunicorn, 3 workers)
                          │
                          │  psycopg2
                          ▼
                          :5432  mms_db  (PostgreSQL 15-alpine)
```

Two containers. No source bind-mount. The image is self-contained. Static files
and uploaded media are persisted via named volumes so they survive `docker compose
down` (but are removed by `docker compose down -v`).

| Service | Image | Port (host) | Volume | Purpose |
|---|---|---|---|---|
| `db`  | `postgres:15-alpine` | `127.0.0.1:5432` | `mms_mms_postgres_data` | PostgreSQL data |
| `web` | `mms-web` (built from `Dockerfile`) | `8000` | `mms_mms_static`, `mms_mms_media` | Django app (gunicorn) |

> Volume names are prefixed `mms_` because the compose file sets
> `name: mms` at the top level. Don't rename this without also renaming
> the volume references inside the `web` service.

---

## 4. Environment variables (`.env`)

The compose stack reads every var from `.env` via `env_file: .env`. **Never
commit `.env`** — only `.env.example`.

| Var | Required? | Default | Purpose |
|---|---|---|---|
| `DB_NAME` | yes | `mms_db` | Postgres database name |
| `DB_USER` | yes | `mms_user` | Postgres role |
| `DB_PASSWORD` | yes | — | Postgres role password |
| `DB_HOST` | no | `db` (compose) / `localhost` (native) | Postgres host |
| `DB_PORT` | no | `5432` | Postgres port |
| `SECRET_KEY` | yes | — | Django secret key. Min 50 chars. Generate with `python -c "import secrets; print(secrets.token_urlsafe(50))"` |
| `DEBUG` | yes | `False` | Production must be `False` |
| `ALLOWED_HOSTS` | yes | `localhost,127.0.0.1` | Comma-separated hostnames Django will serve. **Must include the company hostname.** |
| `GUNICORN_WORKERS` | no | `3` | Number of gunicorn worker processes |
| `MMS_CREATE_SUPERUSER` | no | `0` | If `1`, auto-create a superuser on first boot if none exists |
| `DJANGO_SUPERUSER_USERNAME` | when `MMS_CREATE_SUPERUSER=1` | — | Superuser username |
| `DJANGO_SUPERUSER_EMAIL` | when `MMS_CREATE_SUPERUSER=1` | — | Superuser email |
| `DJANGO_SUPERUSER_PASSWORD` | when `MMS_CREATE_SUPERUSER=1` | — | Superuser password. **Change after first login.** |
| `MMS_USE_SQLITE` | no | empty | Set to `1` to use SQLite instead of Postgres (CI / tests ONLY). Do not use in Docker. |

---

## 5. Common commands

```bash
# ─── Lifecycle ─────────────────────────────────────────────────────────────
docker compose up -d --build       # build (if needed) and start detached
docker compose down                # stop and remove containers (keeps volumes)
docker compose down -v             # DANGER: also removes the Postgres volume
docker compose restart web         # restart just the web service

# ─── Logs ─────────────────────────────────────────────────────────────────
docker compose logs -f web         # follow web logs
docker compose logs --tail=200 db  # last 200 lines of db logs
docker compose logs web 2>&1 | grep entrypoint   # only entrypoint messages

# ─── Django management ───────────────────────────────────────────────────
docker compose exec web python manage.py shell
docker compose exec web python manage.py createsuperuser
docker compose exec web python manage.py changepassword admin

# ─── Database ────────────────────────────────────────────────────────────
docker compose exec db psql -U mms_user -d mms_db   # open psql
docker compose exec db pg_dump -U mms_user mms_db > backup_$(date +%F).sql
docker compose exec -T db pg_restore -U mms_user -d mms_db < backup.sql

# ─── Health ──────────────────────────────────────────────────────────────
curl -fsS http://localhost:8000/health/                # JSON 200/503
docker compose ps                                      # STATUS (healthy / unhealthy)
docker inspect mms_web --format '{{json .State.Health}}'  # full health detail

# ─── Debugging ───────────────────────────────────────────────────────────
docker compose exec web bash                           # shell into web container
docker compose logs web --since=10m                    # last 10 minutes
docker compose exec web python manage.py check --deploy  # Django deploy check
```

---

## 6. Health endpoint

`GET /health/` returns JSON, no auth required, no CSRF.

| HTTP | Body | Meaning |
|---|---|---|
| `200` | `{"status": "ok", "db": "ok"}` | App is up and the DB is reachable. |
| `503` | `{"status": "degraded", "db": "error", "db_error": "..."}` | App is up but the DB is unreachable. |

Used by:
- `docker compose` healthcheck (every 30s, 3 retries, 30s start period).
- Uptime monitors (Pingdom, UptimeRobot, k8s readiness probe, etc.).

`AuthenticationMiddleware` runs before the view, but the view never reads
`request.user` so it stays cheap.

---

## 7. Deploy to the company server (runbook)

```bash
# On the company server, as the deployment user (NOT root):

# 1. Install Docker (one-time, if not already present).
#    See https://docs.docker.com/engine/install/ for your OS.

# 2. Clone the repo
sudo mkdir -p /opt/mms
sudo chown $USER:$USER /opt/mms
git clone <repo-url> /opt/mms
cd /opt/mms

# 3. Configure
cp .env.example .env
$EDITOR .env
#    Required: DB_PASSWORD, SECRET_KEY, ALLOWED_HOSTS=mms.company.local
#    Optional: GUNICORN_WORKERS=4 (match CPU cores), MMS_CREATE_SUPERUSER=1

# 4. First boot
docker compose up -d --build
sleep 30
docker compose ps   # both should be "healthy"

# 5. Set the initial admin password
docker compose exec web python manage.py changepassword admin

# 6. (Optional) Seed demo data — ONLY for sandbox / test environments
docker compose exec web python manage.py seed_demo --full

# 7. Open in browser
xdg-open http://mms.company.local/   # or just navigate manually
```

### After deploy: monitoring checklist

- [ ] `curl http://mms.company.local/health/` returns `{"status":"ok"}`.
- [ ] `docker compose ps` shows both containers as `healthy`.
- [ ] Log in as `admin` with the password you just set, change it.
- [ ] Open `/issues/`, switch language to العربية, verify Arabic UI.
- [ ] Open `/work-orders/emergency/`, verify form labels are in Arabic.
- [ ] Open `/pm/templates/1/edit/`, verify checklist is editable.
- [ ] Confirm `/admin/` works (Django admin).

### Production backup (manual, until Phase 2 automates it)

```bash
docker compose exec db pg_dump -U mms_user mms_db > /opt/mms/backups/backup_$(date +%F).sql
# Add to cron:
# 0 2 * * * cd /opt/mms && docker compose exec -T db pg_dump -U mms_user mms_db > /opt/mms/backups/backup_$(date +\%F).sql
```

---

## 8. SQLite fallback (CI / tests)

For local testing without Postgres, set `MMS_USE_SQLITE=1`:

```bash
# Run the test suite against an in-memory (or on-disk) SQLite
MMS_USE_SQLITE=1 python manage.py test

# Or, one-off:
MMS_USE_SQLITE=1 python manage.py shell
```

A warning is emitted (in DEBUG mode only) if neither `MMS_USE_SQLITE` nor
`DB_*` are set, so dev environments don't silently fall back.

**Do not** set `MMS_USE_SQLITE=1` in the docker compose stack — the
entrypoint will fall back to a non-persistent in-container SQLite,
losing all data on restart.

---

## 9. Verified on

| Date | Docker | OS | Operator | Notes |
|---|---|---|---|---|
| 2026-07-08 | 28.4.0 | macOS 15.6.1 (Docker Desktop) | dev | Full 14-point verification: see commit message. 923MB image, 50 Postgres tables, all 428 tests pass, Arabic pages render correctly. |

---

## 10. Phase 2 (deferred)

When users have validated the production deployment, the following will be
added in a follow-up PR (out of scope for this initial Docker setup):

- `redis` service in `docker-compose.yml`.
- `celery` worker + beat services.
- `mms/celery.py` app file wiring `os.environ["DJANGO_SETTINGS_MODULE"]`.
- `flower` (Celery monitoring UI).
- Automated pg_dump via cron / systemd timer.
- Reverse-proxy guidance (nginx / Caddy / Traefik) for HTTPS termination.
- Monitoring integration (Prometheus metrics endpoint, log shipping).
