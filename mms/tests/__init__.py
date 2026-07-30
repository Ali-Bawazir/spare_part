"""Test package init.

django-axes 5.x installs AxesStandaloneBackend as the first
``AUTHENTICATION_BACKENDS`` entry to gate ``authenticate()`` calls. But
its ``get_user()`` returns ``None`` (inherited from ``BaseBackend``),
which means ``Client.force_login(user)`` produces a session with
``_auth_user_backend = 'axes.backends.AxesStandaloneBackend'`` that
``AuthenticationMiddleware`` cannot resolve — every subsequent
request looks like an anonymous user and ``@login_required`` redirects
to /accounts/login/.

Production login (POST /login) is unaffected because Django calls
``auth_login(request, user)`` with the backend that successfully
authenticated, which is ``ModelBackend`` (axes returns None on
success).

Patch ``get_user`` once at import time so all tests can keep using
``force_login`` (or any other auth helper) without breaking.
"""
from axes.backends import AxesStandaloneBackend
from django.contrib.auth.backends import ModelBackend


_original_get_user = AxesStandaloneBackend.get_user


def _get_user(self, user_id):  # noqa: ANN001
    user = _original_get_user(self, user_id)
    if user is not None:
        return user
    return ModelBackend().get_user(user_id)


AxesStandaloneBackend.get_user = _get_user