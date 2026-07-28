from django.contrib.auth.models import AbstractUser
from django.db import models
from django.utils.translation import gettext_lazy as _


class User(AbstractUser):
    """System actors per scope document."""

    class Role(models.TextChoices):
        OPERATOR = "operator", _("Operator / Labor")
        SUPERVISOR = "supervisor", _("Supervisor")
        TECHNICIAN = "technician", _("Technician")
        MANAGER = "manager", _("Maintenance Manager (Storekeeper)")
        PROCUREMENT = "procurement", _("Procurement Officer")
        SUPER_ADMIN = "super_admin", _("Super Admin")

    role = models.CharField(
        max_length=32,
        choices=Role.choices,
        default=Role.OPERATOR,
        db_index=True,
    )

    def is_super_admin_role(self) -> bool:
        return self.role == self.Role.SUPER_ADMIN or self.is_superuser

    def has_any_role(self, *roles: str) -> bool:
        if self.is_super_admin_role():
            return True
        return self.role in roles
