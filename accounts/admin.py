from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin

from .models import User


@admin.register(User)
class UserAdmin(DjangoUserAdmin):
    """Super Admin manages users (scope doc). Django superuser always has access."""

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
        ("MMS role", {"fields": ("role",)}),
    )
    add_fieldsets = DjangoUserAdmin.add_fieldsets + (
        ("MMS role", {"fields": ("role",)}),
    )

    def has_module_permission(self, request):
        u = request.user
        if not u.is_active or not u.is_authenticated:
            return False
        return bool(getattr(u, "is_super_admin_role", lambda: False)())

    def has_add_permission(self, request):
        return self.has_module_permission(request)

    def has_change_permission(self, request, obj=None):
        return self.has_module_permission(request)

    def has_delete_permission(self, request, obj=None):
        return self.has_module_permission(request)

    def has_view_permission(self, request, obj=None):
        return self.has_module_permission(request)
