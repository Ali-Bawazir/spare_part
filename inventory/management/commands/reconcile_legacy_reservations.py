"""
Phase 1: cancel all ACTIVE legacy `InventoryReservation` rows whose
`source_line` is NULL.

Legacy reservations were created by `reserve_stock()` calls before
Phase 1 added the `source_line` parameter. They were (a) not linked
to a PartIssueLine so they had no natural lifecycle to release through,
and (b) silently holding stock out of free-stock calculations.

After this command, free-stock calculations stop being polluted.

Idempotent. Safe to re-run.

Usage:
    python manage.py reconcile_legacy_reservations [--dry-run]
    python manage.py reconcile_legacy_reservations --work-order=<pk>
    python manage.py reconcile_legacy_reservations --part=<sku>
"""
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext as _


class Command(BaseCommand):
    help = (
        "Cancel ACTIVE InventoryReservation rows whose source_line is NULL "
        "(legacy reservations from pre-Phase-1 reserve_stock() calls). "
        "Use after deploying Phase 1 to free stock held by orphan reservations."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be cancelled without changing anything.",
        )
        parser.add_argument(
            "--work-order",
            type=int,
            help="Limit to reservations on a specific work order.",
        )
        parser.add_argument(
            "--part",
            type=str,
            help="Limit to reservations on a specific part (SKU).",
        )

    def handle(self, *args, **options):
        from inventory.models import InventoryReservation
        from maintenance.services import log_audit

        dry_run = options["dry_run"]
        qs = (
            InventoryReservation.objects
            .filter(status=InventoryReservation.Status.ACTIVE, source_line__isnull=True)
            .select_related("part", "work_order")
            .order_by("created_at", "pk")
        )
        if options.get("work_order"):
            qs = qs.filter(work_order_id=options["work_order"])
        if options.get("part"):
            qs = qs.filter(part__sku=options["part"])

        # Snapshot for reporting BEFORE any mutation (so dry-run is correct).
        legacy = list(qs)
        total = len(legacy)
        qty_sum = sum((r.quantity for r in legacy), Decimal("0"))
        parts_touched = {r.part.sku for r in legacy}
        wos_touched = {r.work_order_id for r in legacy if r.work_order_id}

        if dry_run:
            self.stdout.write(self.style.WARNING(
                f"[DRY-RUN] {total} legacy reservation(s) would be cancelled, "
                f"freeing {qty_sum} unit(s) across {len(parts_touched)} part(s) "
                f"and {len(wos_touched)} work order(s)."
            ))
            for r in legacy[:25]:
                self.stdout.write(
                    f"  - res #{r.pk} part={r.part.sku} qty={r.quantity} "
                    f"wo={r.work_order.number if r.work_order else '—'} "
                    f"created={r.created_at:%Y-%m-%d %H:%M}"
                )
            if total > 25:
                self.stdout.write(f"  ... and {total - 25} more")
            return

        # Live run: cancel each + audit log
        now = timezone.now()
        reason = _("Auto-released by reconcile_legacy_reservations (Phase 1)")
        cancelled = 0
        with transaction.atomic():
            for res in legacy:
                res.status = InventoryReservation.Status.CANCELLED
                res.released_at = now
                res.release_reason = reason
                res.save(update_fields=["status", "released_at", "release_reason"])
                log_audit(
                    actor=None,
                    action="legacy_reservation_reconciled",
                    entity="InventoryReservation",
                    object_id=str(res.pk),
                    payload={
                        "part": res.part.sku,
                        "qty": str(res.quantity),
                        "work_order": res.work_order.number if res.work_order else None,
                        "reason": "pre-Phase-1 source_line=None",
                    },
                )
                cancelled += 1

        self.stdout.write(self.style.SUCCESS(
            f"[OK] Cancelled {cancelled} legacy reservation(s), freeing {qty_sum} unit(s) "
            f"across {len(parts_touched)} part(s) and {len(wos_touched)} work order(s)."
        ))
