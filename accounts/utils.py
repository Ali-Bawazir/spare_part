"""Account / role display helpers.

Keep these in one place so the role names shown to users stay consistent
across templates, the admin, and notifications.
"""

from __future__ import annotations

from django.utils.translation import gettext_lazy as _

# Friendly display names.  The DB-level `User.Role.PROCUREMENT` value is
# kept for migration safety — see CONTEXT.md (Phase 1 caveat).
ROLE_DISPLAY_NAMES = {
    "operator": _("Operator"),
    "supervisor": _("Supervisor"),
    "technician": _("Technician"),
    "manager": _("Maintenance Manager"),
    "procurement": _("Maintenance Supply Officer"),
    "super_admin": _("Super Admin"),
}


def role_display_name(role_code: str) -> str:
    """Return the friendly display name for a role code.

    Unknown / empty roles return the input unchanged so we never crash
    on legacy data.
    """
    if not role_code:
        return ""
    return ROLE_DISPLAY_NAMES.get(role_code, role_code)


def user_role_display(user) -> str:
    """Return the display name for a user's role."""
    if user is None:
        return ""
    role_code = getattr(user, "role", "") or ""
    return role_display_name(role_code)
