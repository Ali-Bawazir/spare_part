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
