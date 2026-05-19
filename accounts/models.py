from django.contrib.auth.models import AbstractUser
from django.db import models


class User(AbstractUser):
    """System actors per scope document."""

    class Role(models.TextChoices):
        OPERATOR = "operator", "Operator / Labor"
        SUPERVISOR = "supervisor", "Supervisor"
        TECHNICIAN = "technician", "Technician"
        MANAGER = "manager", "Maintenance Manager (Storekeeper)"
        PROCUREMENT = "procurement", "Procurement Officer"
        SUPER_ADMIN = "super_admin", "Super Admin"

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
