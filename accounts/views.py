from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models.deletion import ProtectedError
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _

from accounts.forms import MMSUserCreateForm, MMSUserEditForm
from accounts.models import User
from accounts.staff_admin import assign_staff_model_permissions, revoke_mms_staff_model_permissions

from maintenance.services import log_audit


def _require_super_admin(request):
    if not request.user.is_authenticated:
        return False
    return bool(getattr(request.user, "is_super_admin_role", lambda: False)())


def _mms_user_delete_block_reason(actor: User, target: User) -> str:
    if target.pk == actor.pk:
        return _("You cannot delete your own account.")
    if target.is_super_admin_role():
        others = User.objects.exclude(pk=target.pk)
        if not any(u.is_super_admin_role() for u in others):
            return _("Cannot delete the last Super Admin or Django superuser. Promote another account first.")
    return ""


def _mms_user_deactivate_block_reason(actor: User, target: User) -> str:
    if target.pk == actor.pk:
        return _("You cannot deactivate your own account.")
    if not target.is_active:
        return _("This account is already inactive.")
    if target.is_super_admin_role():
        others = User.objects.exclude(pk=target.pk).filter(is_active=True)
        if not any(u.is_super_admin_role() for u in others):
            return _("Cannot deactivate the last active Super Admin or Django superuser.")
    return ""


@login_required
def mms_user_list(request):
    if not _require_super_admin(request):
        messages.error(request, _("Only Super Admins (or Django superusers) can manage users."))
        return redirect("dashboard")
    users = list(User.objects.all().order_by("username")[:500])
    for u in users:
        u.mms_delete_block = _mms_user_delete_block_reason(request.user, u)
    return render(request, "accounts/mms_user_list.html", {"users": users})


@login_required
def mms_user_create(request):
    if not _require_super_admin(request):
        messages.error(request, _("Only Super Admins (or Django superusers) can create users."))
        return redirect("dashboard")

    if request.method == "POST":
        form = MMSUserCreateForm(request.POST)
        if form.is_valid():
            u = User(
                username=form.cleaned_data["username"],
                email=form.cleaned_data.get("email") or "",
                role=form.cleaned_data["role"],
                is_staff=form.cleaned_data.get("is_staff", False),
                is_superuser=False,
                is_active=True,
            )
            u.set_password(form.cleaned_data["password1"])
            u.save()
            log_audit(
                actor=request.user,
                action="mms_user_created",
                entity="User",
                object_id=u.pk,
                payload={"username": u.username, "role": u.role},
            )
            if u.is_staff and form.cleaned_data.get("grant_admin_apps"):
                assign_staff_model_permissions(User, usernames=(u.username,))
            messages.success(request, _("User %s created.") % u.username)
            return redirect("mms_user_list")
    else:
        form = MMSUserCreateForm()
    return render(request, "accounts/mms_user_create.html", {"form": form})


@login_required
def mms_user_edit(request, pk):
    if not _require_super_admin(request):
        messages.error(request, _("Only Super Admins (or Django superusers) can edit users."))
        return redirect("dashboard")

    target = get_object_or_404(User, pk=pk)

    if request.method == "POST":
        form = MMSUserEditForm(request.POST, instance=target, actor=request.user)
        if form.is_valid():
            old_role = target.role
            old_staff = target.is_staff
            old_active = target.is_active
            user = form.save(commit=False)
            pwd = form.cleaned_data.get("password1")
            if pwd:
                user.set_password(pwd)
            user.save()
            uname = user.username
            if user.is_staff and form.cleaned_data.get("grant_admin_apps"):
                assign_staff_model_permissions(User, usernames=(uname,))
            else:
                revoke_mms_staff_model_permissions(User, usernames=(uname,))
            log_audit(
                actor=request.user,
                action="mms_user_updated",
                entity="User",
                object_id=str(user.pk),
                payload={
                    "username": user.username,
                    "role": {"from": old_role, "to": user.role},
                    "is_staff": {"from": old_staff, "to": user.is_staff},
                    "is_active": {"from": old_active, "to": user.is_active},
                    "password_changed": bool(pwd),
                },
            )
            messages.success(request, _("User %s updated.") % user.username)
            return redirect("mms_user_list")
    else:
        form = MMSUserEditForm(instance=target, actor=request.user)

    return render(request, "accounts/mms_user_edit.html", {"form": form, "target": target})


@login_required
def mms_user_delete(request, pk):
    if not _require_super_admin(request):
        messages.error(request, _("Only Super Admins (or Django superusers) can delete users."))
        return redirect("dashboard")

    target = get_object_or_404(User, pk=pk)
    block = _mms_user_delete_block_reason(request.user, target)
    if block:
        messages.error(request, block)
        return redirect("mms_user_list")

    if request.method == "POST":
        username = target.username
        uid = target.pk
        try:
            target.delete()
        except ProtectedError:
            messages.warning(
                request,
                _(
                    "This user cannot be removed from the database while maintenance, inventory, or "
                    "procurement records still reference them. Use \"Deactivate account\" below to block "
                    "sign-in and keep history, or reassign those records in Django admin and try delete again."
                ),
            )
            return redirect("mms_user_delete", pk=pk)
        log_audit(
            actor=request.user,
            action="mms_user_deleted",
            entity="User",
            object_id=str(uid),
            payload={"username": username},
        )
        messages.success(request, _("User %s was deleted.") % username)
        return redirect("mms_user_list")

    return render(
        request,
        "accounts/mms_user_confirm_delete.html",
        {
            "target": target,
            "mms_user_deactivate_block": _mms_user_deactivate_block_reason(request.user, target),
        },
    )


@login_required
def mms_user_deactivate(request, pk):
    if not _require_super_admin(request):
        messages.error(request, _("Only Super Admins (or Django superusers) can deactivate users."))
        return redirect("dashboard")

    target = get_object_or_404(User, pk=pk)
    block = _mms_user_deactivate_block_reason(request.user, target)
    if block:
        messages.error(request, block)
        return redirect("mms_user_list")

    if request.method != "POST":
        return redirect("mms_user_delete", pk=pk)

    username = target.username
    uid = target.pk
    target.is_active = False
    target.save(update_fields=["is_active"])
    log_audit(
        actor=request.user,
        action="mms_user_deactivated",
        entity="User",
        object_id=str(uid),
        payload={"username": username},
    )
    messages.success(
        request,
        _(
            "User %s is now inactive (they cannot sign in; records that reference them are unchanged)."
        ) % username,
    )
    return redirect("mms_user_list")
