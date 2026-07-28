"""Resolve OPERATIONAL blockers whose source WO is no longer blocking.

A dependent WO is paused because of its source WO only while that
source is actively being worked on (lifecycle IN_PROGRESS and
operational ACTIVE). When the source stops blocking — terminal,
paused by something else, or stuck waiting for parts/vendor — the
dependent's OPERATIONAL blocker should be resolved so the technician
can resume.

One invariant. One helper. Used by both runtime and this command.

Usage:
    python manage.py repair_paused_blockers [--verbose]
"""
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = (
        "Resolve open OPERATIONAL blockers whose source WO no longer "
        "blocks (terminal, paused, or pending parts/vendor). One-shot "
        "backfill for data stuck before the auto-release fix was deployed."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--verbose",
            action="store_true",
            help="Print per-WO reasoning.",
        )

    def handle(self, *args, **options):
        from accounts.models import User
        from maintenance.models import WorkOrder
        from maintenance.services import (
            release_dependent_blockers,
            source_still_blocks,
        )

        # Iterate over every WO that is the source of an open OPERATIONAL
        # blocker. `interruptions_caused` is the related_name on the
        # source_work_order FK in WorkOrderBlocker.
        source_wos = (
            WorkOrder.objects
            .filter(
                interruptions_caused__status="open",
                interruptions_caused__kind="operational",
            )
            .distinct()
            .order_by("number")
        )

        actor = (
            User.objects.filter(
                role__in=[User.Role.MANAGER, User.Role.SUPER_ADMIN],
                is_active=True,
            ).first()
            or User.objects.filter(is_superuser=True).first()
        )
        if actor is None:
            self.stdout.write(self.style.ERROR(
                "No manager/super_admin user found to record resolution. "
                "Create one first."
            ))
            return

        released_numbers = []
        for wo in source_wos:
            if source_still_blocks(wo):
                if options["verbose"]:
                    self.stdout.write(
                        f"  WO-{wo.number}: ACTIVE+IN_PROGRESS — skip"
                    )
                continue
            count = release_dependent_blockers(wo, actor)
            if count:
                released_numbers.append((wo.number, count))

        if not released_numbers:
            self.stdout.write(self.style.SUCCESS(
                "No stuck operational blockers found."
            ))
            return

        for n, c in released_numbers:
            self.stdout.write(f"  WO-{n} released ({c} blocker(s))")
        self.stdout.write(self.style.SUCCESS(
            f"\nResolved {sum(c for _, c in released_numbers)} operational blocker(s) "
            f"across {len(released_numbers)} source WO(s)."
        ))
