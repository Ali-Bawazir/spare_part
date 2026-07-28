"""Shared Django admin behavior aligned with scope document (PDF v1.1)."""

from accounts.models import User


def _is_procurement_officer_staff(request) -> bool:
    u = request.user
    return (
        bool(getattr(u, "is_authenticated", False))
        and u.is_staff
        and getattr(u, "role", None) == User.Role.PROCUREMENT
        and not u.is_superuser
    )


class ProcurementMaintenanceReadOnlyMixin:
    """
    Procurement Officer may view maintenance/inventory data but must not
    edit it in admin (PDF: cannot modify maintenance work orders / stock issue).
    """

    def has_add_permission(self, request):
        if _is_procurement_officer_staff(request):
            return False
        return super().has_add_permission(request)

    def has_change_permission(self, request, obj=None):
        if _is_procurement_officer_staff(request):
            return False
        return super().has_change_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        if _is_procurement_officer_staff(request):
            return False
        return super().has_delete_permission(request, obj)


class MMSAdminPermission:
    """
    Role-based permission mixin for Django admin.
    Enforces access matrix per app_label derived from request.user.role.
    """

    _ROLE_PERMS = {
        "maintenance": {
            "operator": "none",
            "supervisor": "view",
            "technician": "view",
            "procurement": "view",
            "manager": "full",
            "super_admin": "full",
        },
        "inventory": {
            "operator": "none",
            "supervisor": "view",
            "technician": "view",
            "procurement": "view",
            "manager": "full",
            "super_admin": "full",
        },
        "procurement": {
            "operator": "none",
            "supervisor": "view",
            "technician": "none",
            "procurement": "full",
            "manager": "view",
            "super_admin": "full",
        },
    }

    def has_module_permission(self, request):
        if request is None:
            return False
        u = request.user
        if not getattr(u, "is_active", False) or not getattr(u, "is_authenticated", False):
            return False
        role = getattr(u, "role", None)
        if role is None:
            return False
        if self.opts.app_label not in self._ROLE_PERMS:
            return getattr(u, "is_superuser", False) and role == User.Role.SUPER_ADMIN
        perms = self._ROLE_PERMS[self.opts.app_label]
        level = perms.get(role, "none")
        return level != "none"

    def has_view_permission(self, request, obj=None):
        if request is None:
            return False
        if self._has_full_perm(request):
            return True
        if self._has_view_perm(request):
            return True
        return super().has_view_permission(request, obj) if hasattr(super(), "has_view_permission") else False

    def has_add_permission(self, request):
        if request is None:
            return False
        if not self._has_full_perm(request):
            return False
        return super().has_add_permission(request) if hasattr(super(), "has_add_permission") else True

    def has_change_permission(self, request, obj=None):
        if request is None:
            return False
        if not self._has_full_perm(request):
            return False
        return super().has_change_permission(request, obj) if hasattr(super(), "has_change_permission") else True

    def has_delete_permission(self, request, obj=None):
        if request is None:
            return False
        if not self._has_full_perm(request):
            return False
        return super().has_delete_permission(request, obj) if hasattr(super(), "has_delete_permission") else True

    def _has_full_perm(self, request) -> bool:
        if request is None:
            return False
        u = request.user
        if not getattr(u, "is_active", False) or not getattr(u, "is_authenticated", False):
            return False
        role = getattr(u, "role", None)
        if role is None:
            return False
        if getattr(u, "is_superuser", False) and role == User.Role.SUPER_ADMIN:
            return True
        if self.opts.app_label not in self._ROLE_PERMS:
            return False
        perms = self._ROLE_PERMS[self.opts.app_label]
        return perms.get(role) == "full"

    def _has_view_perm(self, request) -> bool:
        if request is None:
            return False
        u = request.user
        if not getattr(u, "is_active", False) or not getattr(u, "is_authenticated", False):
            return False
        role = getattr(u, "role", None)
        if role is None:
            return False
        if self.opts.app_label not in self._ROLE_PERMS:
            return False
        perms = self._ROLE_PERMS[self.opts.app_label]
        level = perms.get(role, "none")
        return level in ("view", "full")