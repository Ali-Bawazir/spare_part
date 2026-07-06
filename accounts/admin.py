from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin
from django.utils.translation import gettext_lazy as _

from accounts.models import User


@admin.register(User)
class UserAdmin(DjangoUserAdmin):
    """Only SUPER_ADMIN may add/change/delete users; MANAGER may view."""

    list_display = (
        "username",
        "email",
        "role",
        "is_staff",
        "is_superuser",
        "is_active",
        "last_login",
    )
    list_filter = ("role", "is_staff", "is_superuser", "is_active")
    search_fields = ("username", "first_name", "last_name", "email")
    ordering = ("username",)

    fieldsets = DjangoUserAdmin.fieldsets + (
        (_("MMS role"), {"fields": ("role",)}),
    )
    add_fieldsets = DjangoUserAdmin.add_fieldsets + (
        (_("MMS role"), {"fields": ("role",)}),
    )

    def has_module_permission(self, request):
        u = request.user
        if not getattr(u, "is_active", False) or not getattr(u, "is_authenticated", False):
            return False
        role = getattr(u, "role", None)
        if role == User.Role.SUPER_ADMIN:
            return True
        if role == User.Role.MANAGER:
            return True
        return False

    def has_add_permission(self, request):
        u = request.user
        if not getattr(u, "is_active", False) or not getattr(u, "is_authenticated", False):
            return False
        role = getattr(u, "role", None)
        return role == User.Role.SUPER_ADMIN

    def has_change_permission(self, request, obj=None):
        u = request.user
        if not getattr(u, "is_active", False) or not getattr(u, "is_authenticated", False):
            return False
        role = getattr(u, "role", None)
        return role == User.Role.SUPER_ADMIN

    def has_delete_permission(self, request, obj=None):
        u = request.user
        if not getattr(u, "is_active", False) or not getattr(u, "is_authenticated", False):
            return False
        role = getattr(u, "role", None)
        return role == User.Role.SUPER_ADMIN

    def has_view_permission(self, request, obj=None):
        u = request.user
        if not getattr(u, "is_active", False) or not getattr(u, "is_authenticated", False):
            return False
        role = getattr(u, "role", None)
        return role in (User.Role.SUPER_ADMIN, User.Role.MANAGER)