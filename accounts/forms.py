from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

from accounts.staff_admin import user_has_full_mms_staff_bundle

User = get_user_model()


def _other_active_super_admin_users_exist(exclude_pk: int) -> bool:
    return any(
        u.is_super_admin_role()
        for u in User.objects.exclude(pk=exclude_pk).filter(is_active=True)
    )


class MMSUserCreateForm(forms.Form):
    """Create a factory user from the MMS UI (super admin only)."""

    username = forms.CharField(max_length=150, label=_("Username"))
    email = forms.EmailField(required=False)
    role = forms.ChoiceField(choices=User.Role.choices, label=_("MMS role"))
    is_staff = forms.BooleanField(
        required=False,
        initial=False,
        label=_("Staff"),
        help_text=_("Allows sign-in to Django /admin/ when combined with app permissions below."),
    )
    grant_admin_apps = forms.BooleanField(
        required=False,
        initial=False,
        label=_("Grant admin app permissions"),
        help_text=_(
            "When Staff is checked, assigns Maintenance, Inventory, and Procurement permissions "
            "so /admin/ lists those apps."
        ),
    )
    password1 = forms.CharField(widget=forms.PasswordInput, label=_("Password"))
    password2 = forms.CharField(widget=forms.PasswordInput, label=_("Password (again)"))

    def clean_username(self):
        username = self.cleaned_data["username"].strip()
        if User.objects.filter(username=username).exists():
            raise ValidationError(_("A user with this username already exists."))
        return username

    def clean(self):
        data = super().clean()
        p1 = data.get("password1")
        p2 = data.get("password2")
        if p1 and p2 and p1 != p2:
            raise ValidationError(_("The two password fields do not match."))
        if p1:
            tmp = User(username=data.get("username") or "user")
            validate_password(p1, user=tmp)
        if data.get("grant_admin_apps") and not data.get("is_staff"):
            raise ValidationError(_("Grant admin app permissions only applies when Staff is checked."))
        return data


class MMSUserEditForm(forms.ModelForm):
    """Edit a factory user from the MMS UI (super admin only)."""

    grant_admin_apps = forms.BooleanField(
        required=False,
        label=_("Grant admin app permissions"),
        help_text=_(
            "When Staff is checked, assigns Maintenance, Inventory, and Procurement permissions "
            "so /admin/ lists those apps."
        ),
    )
    password1 = forms.CharField(
        widget=forms.PasswordInput(render_value=False),
        required=False,
        label=_("New password"),
    )
    password2 = forms.CharField(
        widget=forms.PasswordInput(render_value=False),
        required=False,
        label=_("New password (again)"),
    )

    class Meta:
        model = User
        fields = (
            "username",
            "email",
            "first_name",
            "last_name",
            "role",
            "is_staff",
            "is_active",
        )

    def __init__(self, *args, actor=None, **kwargs):
        self.actor = actor
        super().__init__(*args, **kwargs)
        if self.instance and self.instance.pk:
            self.fields["grant_admin_apps"].initial = user_has_full_mms_staff_bundle(self.instance)
        sf = self.fields.get("is_staff")
        if sf:
            sf.label = _("Staff")
            sf.help_text = _("Allows sign-in to Django /admin/ when the user also has app permissions.")
        ia = self.fields.get("is_active")
        if ia:
            ia.label = _("Active")
            ia.help_text = _("Inactive users cannot sign in. Prefer deactivating over deleting an account.")
        role_f = self.fields.get("role")
        if role_f:
            role_f.label = _("MMS role")

    def clean_username(self):
        username = self.cleaned_data["username"].strip()
        qs = User.objects.filter(username=username)
        if self.instance.pk:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise ValidationError(_("A user with this username already exists."))
        return username

    def clean(self):
        data = super().clean()
        p1 = data.get("password1")
        p2 = data.get("password2")
        if p1 or p2:
            if p1 != p2:
                raise ValidationError(_("The two password fields do not match."))
            if p1:
                validate_password(p1, user=self.instance)
        if data.get("grant_admin_apps") and not data.get("is_staff"):
            raise ValidationError(_("Grant admin app permissions only applies when Staff is checked."))

        if "role" not in self.cleaned_data or "is_active" not in self.cleaned_data:
            return data

        role = self.cleaned_data["role"]
        is_active = self.cleaned_data["is_active"]
        will_be_super_admin = (
            role == User.Role.SUPER_ADMIN or getattr(self.instance, "is_superuser", False)
        ) and is_active
        was_super_admin = self.instance.is_super_admin_role()

        if self.actor and self.instance.pk == self.actor.pk and was_super_admin and not will_be_super_admin:
            raise ValidationError(_("You cannot remove your own Super Admin access or deactivate yourself here."))

        if was_super_admin and not will_be_super_admin:
            if not _other_active_super_admin_users_exist(self.instance.pk):
                raise ValidationError(
                    _(
                        "Another active Super Admin or Django superuser is required before "
                        "demoting, deactivating, or changing this account."
                    )
                )

        return data
