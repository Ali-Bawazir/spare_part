import getpass

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from accounts.staff_admin import assign_staff_model_permissions


class Command(BaseCommand):
    help = (
        "Create a real MMS user (production-style): username, role, optional staff flag, "
        "and optional Django admin permissions for maintenance/inventory/procurement."
    )

    def add_arguments(self, parser):
        parser.add_argument("username", help="Login name (no spaces).")
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
            help="MMS role.",
        )
        parser.add_argument("--email", default="", help="Email address.")
        parser.add_argument("--first-name", default="", dest="first_name")
        parser.add_argument("--last-name", default="", dest="last_name")
        parser.add_argument(
            "--staff",
            action="store_true",
            help="Allow login to /admin/ (still needs model permissions unless --grant-admin-apps).",
        )
        parser.add_argument(
            "--superuser",
            action="store_true",
            help="Django superuser (full admin). Prefer createsuperuser unless you know you need this.",
        )
        parser.add_argument(
            "--grant-admin-apps",
            action="store_true",
            help="If --staff, assign maintenance+inventory+procurement permissions so /admin/ is not empty.",
        )
        parser.add_argument(
            "--password",
            default="",
            help="Unsafe for shell history; omit to be prompted securely.",
        )
        parser.add_argument(
            "--no-input",
            action="store_true",
            help="With --password only; fail if password empty.",
        )

    def handle(self, *args, **options):
        User = get_user_model()
        username = options["username"].strip()
        if not username:
            raise CommandError("username is required.")

        if User.objects.filter(username=username).exists():
            raise CommandError(f"User {username!r} already exists.")

        role = options["role"]
        pwd = (options.get("password") or "").strip()
        if options["no_input"]:
            if not pwd:
                raise CommandError("--no-input requires --password.")
        else:
            if not pwd:
                p1 = getpass.getpass("Password: ")
                p2 = getpass.getpass("Password (again): ")
                if p1 != p2:
                    raise CommandError("Passwords do not match.")
                pwd = p1
            if len(pwd) < 8:
                self.stderr.write(self.style.WARNING("Password is shorter than 8 characters (not recommended)."))

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
            self.stdout.write(self.style.SUCCESS(f"Granted maintenance/inventory/procurement admin permissions to {username}."))
        elif u.is_staff and not options["grant_admin_apps"]:
            self.stdout.write(
                self.style.WARNING(
                    f"User is staff but has no model permissions yet — /admin/ may be empty. Run:\n"
                    f'  python manage.py grant_staff_admin_permissions --usernames={username}'
                )
            )

        self.stdout.write(
            self.style.SUCCESS(
                f"Created user {username!r} role={role} staff={u.is_staff} superuser={u.is_superuser}. "
                f"MMS login: same username/password at site root (not /admin/ unless staff)."
            )
        )
