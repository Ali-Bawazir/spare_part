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