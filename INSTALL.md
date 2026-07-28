# MMS — Installation Guide

Factory Maintenance & Spare Parts Management System. Django 4.2 monolith with PostgreSQL, Gunicorn, WhiteNoise.

## Local development (SQLite)

```bash
git clone <repo>
cd spare_part
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Edit .env: set DEBUG=True and SECRET_KEY to any dev string
python3 manage.py migrate
python3 manage.py createsuperuser
python3 manage.py seed_demo --full   # optional sample data
python3 manage.py runserver
```

Visit `http://localhost:8000`. Login with the credentials you set.

## Local development (PostgreSQL via Docker)

```bash
docker compose up --build
docker compose exec web python manage.py migrate
docker compose exec web python manage.py createsuperuser
docker compose exec web python manage.py seed_demo --full   # optional
```

Visit `http://localhost:8000`.

## Test suite

```bash
# SQLite (fast, CI):
MMS_USE_SQLITE=1 python3 manage.py test

# PostgreSQL (matches production):
DB_NAME=mms_test DB_USER=mms DB_PASSWORD=postgres DB_HOST=localhost \
    python3 manage.py test
```

## Production (CranL)

See `RUNBOOK.md` for production deployment steps. Critical env vars:

```
SECRET_KEY=<openssl rand -base64 64>
DEBUG=False
ALLOWED_HOSTS=<your-fqdn>
DB_NAME=... DB_USER=... DB_PASSWORD=... DB_HOST=... DB_PORT=5432
GUNICORN_WORKERS=4
TRUSTED_PROXY_CIDR=<CranL edge CIDR>
TIME_ZONE=Asia/Riyadh
LANGUAGE_CODE=en
MMS_CREATE_SUPERUSER=0   # set to 1 only for the first boot
```

## File layout

- `mms/` — Django project (settings, URLs, WSGI, health endpoint)
- `accounts/` — custom user model + role-based decorator
- `maintenance/` — issues, work orders, downtime, PM, ERO, tools, notifications, QR
- `inventory/` — spare parts, stock, reservations, consumables, reusable tools
- `procurement/` — purchase requests, purchase orders, suppliers
- `docs/` — design docs, ADRs, scenarios