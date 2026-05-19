from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand

from accounts.staff_admin import assign_staff_model_permissions


class Command(BaseCommand):
    help = (
        "Grant Django model permissions for maintenance, inventory, and procurement "
        "so staff users see those apps in /admin/. "
        "Default usernames: manager, procurement. Use --usernames for others."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--usernames",
            default="",
            help="Comma-separated Django usernames (e.g. jdoe,asmith). "
            "If omitted, updates manager and procurement only.",
        )

    def handle(self, *args, **options):
        raw = (options.get("usernames") or "").strip()
        names = [s.strip() for s in raw.split(",") if s.strip()] or None
        assign_staff_model_permissions(get_user_model(), usernames=names)
        who = ", ".join(names) if names else "manager, procurement"
        self.stdout.write(self.style.SUCCESS(f"Staff admin permissions updated for: {who}"))
