"""Phase 4: Sync all PM notification stages (upcoming 7d/3d/1d, due today, overdue).

Cron-callable. Run daily (e.g. via systemd timer or cron).
"""
from django.core.management.base import BaseCommand

from maintenance.notifications import sync_pm_notifications


class Command(BaseCommand):
    help = "Sync PM notification cascade: 7d/3d/1d before due, due today, overdue."

    def handle(self, *args, **options):
        counts = sync_pm_notifications()
        for stage, n in counts.items():
            self.stdout.write(f"  {stage}: {n}")
        self.stdout.write(self.style.SUCCESS("PM notifications sync complete."))
