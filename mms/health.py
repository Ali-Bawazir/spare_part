"""Lightweight health endpoint for compose healthcheck and uptime monitors.

Returns:
    200 {"status": "ok", "db": "ok"}     — DB reachable.
    503 {"status": "degraded", ...}      — DB unreachable.

No auth, no CSRF, no per-request DB-side caching. Designed to be hit often
(by container orchestrators and external uptime probes).
"""
from __future__ import annotations

import logging

from django.db import connection
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET

log = logging.getLogger(__name__)


@csrf_exempt
@require_GET
def health(_request):
    """Return 200 if the app is up and the DB is reachable, 503 otherwise."""
    db_status = "ok"
    db_error: str | None = None
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except Exception as exc:  # noqa: BLE001 — health endpoint must catch all
        db_status = "error"
        db_error = f"{type(exc).__name__}: {exc}"
        log.warning("health: DB check failed: %s", db_error)

    payload = {
        "status": "ok" if db_status == "ok" else "degraded",
        "db": db_status,
    }
    if db_error:
        payload["db_error"] = db_error

    http_status = 200 if db_status == "ok" else 503
    return JsonResponse(payload, status=http_status)
