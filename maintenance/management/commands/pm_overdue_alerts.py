"""Overdue alerts cron entry point — run at 14:00 server time.

    0 14 * * * /usr/bin/python3 manage.py pm_overdue_alerts
"""

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Send 14:00 overdue alerts (PMs not yet completed)."

    def handle(self, *args, **options):
        from maintenance.services import maintenance_engine as engine

        result = engine.run_overdue_alerts()
        self.stdout.write(
            f"Overdue alerts: tech={result.get('tech_notifications', 0)} "
            f"manager={result.get('manager_notifications', 0)}"
        )