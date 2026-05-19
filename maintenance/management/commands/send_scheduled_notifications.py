from django.core.management.base import BaseCommand

from maintenance.notifications import sync_pm_overdue_notifications


class Command(BaseCommand):
    help = "Emit in-app notifications for scheduled alerts (e.g. overdue preventive maintenance). Run daily via Task Scheduler / cron."

    def handle(self, *args, **options):
        n = sync_pm_overdue_notifications()
        self.stdout.write(self.style.SUCCESS(f"PM overdue notifications created: {n}"))
