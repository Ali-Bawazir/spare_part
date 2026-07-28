import getpass

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.utils.translation import gettext_lazy as _

from accounts.staff_admin import assign_staff_model_permissions


class Command(BaseCommand):
    help = _(
        "Create a real MMS user (production-style): username, role, optional staff flag, "
        "and optional Django admin permissions for maintenance/inventory/procurement."
    )

    def add_arguments(self, parser):
        parser.add_argument("username", help=_("Login name (no spaces)."))
        parser.add_argument(
            "--role",
            required=True,
            choices=[
                "operator",
                "supervisor",
                "technician",
                "manager",
                "procurement",
                "super_admin",
            ],
            help=_("MMS role."),
        )
        parser.add_argument("--email", default="", help=_("Email address."))
        parser.add_argument("--first-name", default="", dest="first_name")
        parser.add_argument("--last-name", default="", dest="last_name")
        parser.add_argument(
            "--staff",
            action="store_true",
            help=_("Allow login to /admin/ (still needs model permissions unless --grant-admin-apps)."),
        )
        parser.add_argument(
            "--superuser",
            action="store_true",
            help=_("Django superuser (full admin). Prefer createsuperuser unless you know you need this."),
        )
        parser.add_argument(
            "--grant-admin-apps",
            action="store_true",
            help=_("If --staff, assign maintenance+inventory+procurement permissions so /admin/ is not empty."),
        )
        parser.add_argument(
            "--password",
            default="",
            help=_("Unsafe for shell history; omit to be prompted securely."),
        )
        parser.add_argument(
            "--no-input",
            action="store_true",
            help=_("With --password only; fail if password empty."),
        )

    def handle(self, *args, **options):
        User = get_user_model()
        username = options["username"].strip()
        if not username:
            raise CommandError(_("username is required."))

        if User.objects.filter(username=username).exists():
            raise CommandError(_("User %s already exists.") % username)

        role = options["role"]
        pwd = (options.get("password") or "").strip()
        if options["no_input"]:
            if not pwd:
                raise CommandError(_("--no-input requires --password."))
        else:
            if not pwd:
                p1 = getpass.getpass(_("Password: "))
                p2 = getpass.getpass(_("Password (again): "))
                if p1 != p2:
                    raise CommandError(_("Passwords do not match."))
                pwd = p1
            if len(pwd) < 8:
                self.stderr.write(self.style.WARNING(_("Password is shorter than 8 characters (not recommended).")))

        u = User(
            username=username,
            email=options.get("email") or "",
            first_name=options.get("first_name") or "",
            last_name=options.get("last_name") or "",
            role=role,
            is_staff=bool(options["staff"]),
            is_superuser=bool(options["superuser"]),
            is_active=True,
        )
        u.set_password(pwd)
        u.save()

        if u.is_staff and options["grant_admin_apps"]:
            assign_staff_model_permissions(User, usernames=(username,))
            self.stdout.write(
                self.style.SUCCESS(
                    _("Granted maintenance/inventory/procurement admin permissions to %s.") % username
                )
            )
        elif u.is_staff and not options["grant_admin_apps"]:
            self.stdout.write(
                self.style.WARNING(
                    _(
                        "User is staff but has no model permissions yet — /admin/ may be empty. Run:\n"
                        "  python manage.py grant_staff_admin_permissions --usernames=%s"
                    )
                    % username
                )
            )

        self.stdout.write(
            self.style.SUCCESS(
                _(
                    "Created user %(username)r role=%(role)s staff=%(staff)s superuser=%(superuser)s. "
                    "MMS login: same username/password at site root (not /admin/ unless staff)."
                )
                % {
                    "username": username,
                    "role": role,
                    "staff": u.is_staff,
                    "superuser": u.is_superuser,
                }
            )
        )
