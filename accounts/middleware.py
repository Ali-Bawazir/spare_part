"""Idle session timeout middleware.

Stamps ``request.session['_last_seen']`` AFTER the view returns — only
on successful (2xx/3xx) responses, so failed requests don't extend
the session.

Skips /static/, the auth endpoints themselves, /admin/jsi18n/, and
/favicon.ico so asset hits and the login/logout flow never touch the
session. Media is served by the CDN, not Django, so no skip needed
there.

On expiry: ``logout(request)`` (which also flushes the session) + 302
to ``?expired=1`` on the login page. No AJAX detection, no JSON, no
special headers — server-rendered app per design decision.

Must be placed after AuthenticationMiddleware (so ``request.user`` is
populated).
"""
from time import time
from urllib.parse import urlencode

from django.conf import settings
from django.contrib.auth import logout
from django.shortcuts import redirect
from django.urls import reverse


_SKIP_PREFIXES = (
    "/static/",
    "/accounts/login/",
    "/accounts/logout/",
    "/admin/jsi18n/",
    "/favicon.ico",
)


class IdleSessionTimeoutMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response
        self.timeout = getattr(settings, "SESSION_IDLE_TIMEOUT_SECONDS", 14400)

    def __call__(self, request):
        if (
            request.user.is_authenticated
            and not request.path.startswith(_SKIP_PREFIXES)
        ):
            now = time()
            last_seen = request.session.get("_last_seen")
            if last_seen is not None and now - last_seen > self.timeout:
                logout(request)
                return redirect(f"{reverse('login')}?{urlencode({'expired': 1})}")

        response = self.get_response(request)

        if (
            request.user.is_authenticated
            and not request.path.startswith(_SKIP_PREFIXES)
            and 200 <= response.status_code < 400
        ):
            now = time()
            last_seen = request.session.get("_last_seen", 0)
            if now - last_seen >= 60:
                request.session["_last_seen"] = now
        return response