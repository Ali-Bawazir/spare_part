"""
Resolve orphan VENDOR_REPAIR blockers whose linked ERO is already CLOSED.

If the ERO went from SENT -> CLOSED without passing through RETURNED
(pre-fix data, or admin override), the B-2-style blocker would be left
stuck open even though the vendor repair is done. This command finds
those orphan blockers, resolves them with a backfill audit note, and
backfills the cost ledger if missing.

Usage:
    python manage.py reconcile_orphan_vendor_blockers [--dry-run]
"""
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.utils import timezone

from maintenance.models import WorkOrderBlockerEvent


class Command(BaseCommand):
    help = "Resolve orphan VENDOR_REPAIR blockers whose ERO is already CLOSED."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Just list orphans; don't resolve.",
        )

    def handle(self, *args, **options):
        from accounts.models import User
        from maintenance.cost_ledger import CostLedgerService
        from maintenance.models import (
            CostTransaction,
            ExternalRepairOrder,
            WorkOrderBlocker,
            WorkOrderBlockerEvent,
        )
        from maintenance.services_wo_status import WorkOrderService

        orphans = (
            WorkOrderBlocker.objects
            .filter(
                kind=WorkOrderBlocker.Kind.VENDOR_REPAIR,
                status=WorkOrderBlocker.Status.OPEN,
                related_ero__isnull=False,
                related_ero__status=ExternalRepairOrder.Status.CLOSED,
            )
            .select_related("related_ero", "work_order")
        )

        if not orphans.exists():
            self.stdout.write(self.style.SUCCESS(
                "No orphan VENDOR_REPAIR blockers found."
            ))
            return

        self.stdout.write(
            f"Found {orphans.count()} orphan VENDOR_REPAIR blocker(s):"
        )
        for b in orphans:
            ero = b.related_ero
            self.stdout.write(
                f"  B-{b.id} on WO-{b.work_order.number} "
                f"(ERO-{ero.id} {ero.status}, actual_cost={ero.actual_cost})"
            )

        if options["dry_run"]:
            self.stdout.write(self.style.WARNING(
                "--dry-run: not making changes."
            ))
            return

        actor = User.objects.filter(role=User.Role.MANAGER).first()

        resolved_count = 0
        for b in orphans:
            ero = b.related_ero
            wo = b.work_order

            if ero.actual_cost and not CostTransaction.objects.filter(
                source_type="external_repair_order",
                source_id=ero.pk,
            ).exists():
                CostTransaction.objects.create(
                    work_order=wo,
                    machine=wo.machine if wo.machine_id else None,
                    component=wo.component if wo.component_id else None,
                    amount=Decimal(ero.actual_cost).quantize(
                        Decimal("0.01")
                    ),
                    quantity=None,
                    unit_cost=None,
                    category="vendor_repair",
                    source_type="external_repair_order",
                    source_id=ero.pk,
                    actor=actor,
                    memo=(
                        f"Backfill from reconcile_orphan_vendor_blockers: "
                        f"ERO #{ero.pk}"
                    ),
                )
                self.stdout.write(
                    f"  - ERO-{ero.id}: backfilled {ero.actual_cost} "
                    f"SAR to ledger"
                )

            WorkOrderBlockerEvent.objects.create(
                blocker=b,
                event_type=WorkOrderBlockerEvent.EventType.BLOCKER_RESOLVED,
                actor=actor,
                payload={
                    "note": (
                        "Backfilled by reconcile_orphan_vendor_blockers "
                        "(ERO closed but blocker stuck open)"
                    ),
                },
            )
            b.status = WorkOrderBlocker.Status.RESOLVED
            b.resolved_at = timezone.now()
            b.resolved_by = actor
            b.resolution_note = "Backfilled - ERO was already closed"
            b.save(update_fields=[
                "status", "resolved_at", "resolved_by",
                "resolution_note",
            ])
            self.stdout.write(self.style.SUCCESS(f"  - B-{b.id}: resolved"))

            CostLedgerService._refresh_wo_cache(wo.pk)
            WorkOrderService.recompute_operational_status(wo)
            resolved_count += 1

        self.stdout.write(self.style.SUCCESS(
            f"\nResolved {resolved_count} orphan blocker(s)."
        ))