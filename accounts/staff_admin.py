"""Assign Django auth permissions so staff users see apps in /admin/."""

from typing import Iterable, Optional, Sequence, Type

from django.contrib.auth import get_user_model
from django.contrib.auth.models import AbstractBaseUser, Permission

MMS_STAFF_APP_LABELS = ("maintenance", "inventory", "procurement")


def mms_staff_admin_permissions_queryset():
    return Permission.objects.filter(content_type__app_label__in=MMS_STAFF_APP_LABELS)


def user_has_full_mms_staff_bundle(user) -> bool:
    """True if user holds every permission assign_staff_model_permissions would set."""
    qs = mms_staff_admin_permissions_queryset()
    expected = set(qs.values_list("pk", flat=True))
    if not expected:
        return False
    have = set(user.user_permissions.filter(pk__in=expected).values_list("pk", flat=True))
    return have == expected


def revoke_mms_staff_model_permissions(
    User: Optional[Type[AbstractBaseUser]] = None,
    *,
    usernames: Sequence[str],
) -> None:
    """Remove maintenance/inventory/procurement model permissions; keep any other user_permissions."""
    User = User or get_user_model()
    mms_pks = list(mms_staff_admin_permissions_queryset().values_list("pk", flat=True))
    if not mms_pks:
        return
    for username in usernames:
        u = User.objects.filter(username=username).first()
        if not u:
            continue
        keep = u.user_permissions.exclude(pk__in=mms_pks)
        u.user_permissions.set(keep)


def assign_staff_model_permissions(
    User: Optional[Type[AbstractBaseUser]] = None,
    *,
    usernames: Optional[Sequence[str]] = None,
) -> None:
    """
    Django's admin index only lists apps where the user has at least one model
    permission for that app. `is_staff=True` alone is not enough.

    MMS-specific rules (read-only procurement, etc.) still come from ModelAdmin
    and ProcurementMaintenanceReadOnlyMixin.

    Args:
        User: User model (defaults to get_user_model()).
        usernames: Accounts to update (default: manager, procurement for demos).
    """
    User = User or get_user_model()
    perms = list(mms_staff_admin_permissions_queryset())
    if not perms:
        return
    names: Iterable[str] = usernames if usernames is not None else ("manager", "procurement")
    for username in names:
        u = User.objects.filter(username=username).first()
        if u and u.is_staff:
            u.user_permissions.set(perms)
