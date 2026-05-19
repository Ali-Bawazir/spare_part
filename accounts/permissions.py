from functools import wraps

from django.conf import settings
from django.contrib import messages
from django.shortcuts import redirect


def role_required(*roles):
    """Superuser or super_admin role bypasses."""

    def decorator(view_func):
        @wraps(view_func)
        def _wrapped(request, *args, **kwargs):
            if not request.user.is_authenticated:
                return redirect(f"{settings.LOGIN_URL}?next={request.path}")
            u = request.user
            if u.is_superuser or getattr(u, "role", None) == "super_admin":
                return view_func(request, *args, **kwargs)
            if getattr(u, "role", None) in roles:
                return view_func(request, *args, **kwargs)
            messages.error(request, "You do not have permission to access this page.")
            return redirect("dashboard")

        return _wrapped

    return decorator
