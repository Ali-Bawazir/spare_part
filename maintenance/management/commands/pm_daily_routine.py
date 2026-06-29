"""Cron entry point: generate today's PM occurrences + send morning summaries.

Run at 07:00 server time daily:
    0 7 * * * /usr/bin/python3 manage.py pm_daily_routine
"""

from django.core.management.base import BaseCommand
from django.utils import timezone


class Command(BaseCommand):
    help = "Generate today's PM occurrences, send morning summaries, and alert overdue."

    def add_arguments(self, parser):
        parser.add_argument(
            "--force-generate",
            action="store_true",
            help="Regenerate today's occurrences even if already generated",
        )
        parser.add_argument(
            "--skip-morning",
            action="store_true",
            help="Skip morning summary notifications",
        )
        parser.add_argument(
            "--skip-overdue",
            action="store_true",
            help="Skip overdue alerts",
        )

    def handle(self, *args, **options):
        from maintenance.services import maintenance_engine as engine

        self.stdout.write(f"[{timezone.now():%H:%M}] Starting PM daily routine")

        if not options["skip_morning"]:
            gen_result = engine.generate_today(force=options["force_generate"])
            self.stdout.write(
                f"  Generated: {gen_result.get('count', 0)} occurrences"
                + (" (forced)" if options["force_generate"] else "")
            )

            morning = engine.run_daily_morning_summaries()
            self.stdout.write(
                f"  Morning summaries: tech={morning.get('tech_notifications', 0)} "
                f"manager={morning.get('manager_notifications', 0)}"
            )

        if not options["skip_overdue"]:
            unassigned = engine.run_unassigned_alerts()
            self.stdout.write(f"  Unassigned alerts: {unassigned.get('manager_notifications', 0)}")

        self.stdout.write(f"[{timezone.now():%H:%M}] PM daily routine complete")


class OverdueCommand(BaseCommand):
    help = "Send 14:00 overdue alerts (PMs not yet done)."

    def handle(self, *args, **options):
        from maintenance.services import maintenance_engine as engine

        result = engine.run_overdue_alerts()
        self.stdout.write(
            f"Overdue alerts: tech={result.get('tech_notifications', 0)} "
            f"manager={result.get('manager_notifications', 0)}"
        )