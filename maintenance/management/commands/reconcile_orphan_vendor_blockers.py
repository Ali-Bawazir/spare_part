"""
Resolve orphan VENDOR_REPAIR blockers whose underlying ERO is already
RETURNED or CLOSED.

Two failure modes this catches:

1. The ERO went from SENT -> CLOSED without passing through RETURNED
   (pre-fix data, or admin override), so the existing
   sync_from_external_event(ERO_RETURNED) call never fired. The
   blocker is left stuck open even though the vendor repair is done.

2. **The bug fixed in this branch**: the
   sync_from_external_event(ERO_RETURNED) fallback expected
   ExternalRepairOrder.origin_request which doesn't exist, so the
   VENDOR_REPAIR blocker — keyed to the ExternalRepairRequest, not the
   ERO — never resolved even when the ERO went SENT -> RETURNED or
   SENT -> RETURNED -> CLOSED. WOs stayed stuck at operational=
   waiting_vendor indefinitely.

This command finds those orphan blockers, resolves them with a
backfill audit note, and backfills the cost ledger if missing.

Usage:
    python manage.py reconcile_orphan_vendor_blockers [--dry-run]
"""
from decimal import Decimal

from django.contrib.contenttypes.models import ContentType
from django.core.management.base import BaseCommand
from django.utils import timezone

from maintenance.models import WorkOrderBlockerEvent


class Command(BaseCommand):
    help = (
        "Resolve orphan VENDOR_REPAIR blockers whose ERO is already "
        "RETURNED or CLOSED. Use after deploying the ERO_RETURNED "
        "fallback fix to unstick affected WOs."
    )

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
            ExternalRepairRequest,
            WorkOrderBlocker,
            WorkOrderBlockerEvent,
        )
        from maintenance.services_wo_status import WorkOrderService

        TERMINAL_STATUSES = (
            ExternalRepairOrder.Status.RETURNED,
            ExternalRepairOrder.Status.CLOSED,
        )

        # Build candidate set: all OPEN VENDOR_REPAIR blockers whose related
        # ERO (or ERR-keyed lookup) is in a terminal state.
        candidates = list(
            WorkOrderBlocker.objects
            .filter(
                kind=WorkOrderBlocker.Kind.VENDOR_REPAIR,
                status=WorkOrderBlocker.Status.OPEN,
            )
            .select_related("related_ero", "work_order", "content_type")
        )

        err_ct = ContentType.objects.get_for_model(ExternalRepairRequest)
        ero_ct = ContentType.objects.get_for_model(ExternalRepairOrder)

        orphans = []
        for b in candidates:
            ero = None
            # 1. ERO linked via related_ero FK
            if b.related_ero_id:
                ero = b.related_ero
            # 2. ERO linked via generic FK
            elif b.content_type_id == ero_ct.id:
                try:
                    ero = ExternalRepairOrder.objects.get(pk=b.object_id)
                except ExternalRepairOrder.DoesNotExist:
                    pass
            # 3. ERR linked via generic FK — find the ERO via FK
            elif b.content_type_id == err_ct.id:
                err = (
                    ExternalRepairRequest.objects
                    .filter(pk=b.object_id)
                    .select_related("repair_order")
                    .first()
                )
                if err and err.repair_order_id:
                    ero = err.repair_order

            if ero and ero.status in TERMINAL_STATUSES:
                orphans.append((b, ero))

        if not orphans:
            self.stdout.write(self.style.SUCCESS(
                "No orphan VENDOR_REPAIR blockers found."
            ))
            return

        self.stdout.write(
            f"Found {len(orphans)} orphan VENDOR_REPAIR blocker(s):"
        )
        for b, ero in orphans:
            self.stdout.write(
                f"  B-{b.id} on WO-{b.work_order.number} "
                f"(ERO-{ero.id} {ero.status}, "
                f"actual_cost={ero.actual_cost})"
            )

        if options["dry_run"]:
            self.stdout.write(self.style.WARNING(
                "--dry-run: not making changes."
            ))
            return

        actor = User.objects.filter(role=User.Role.MANAGER).first()

        resolved_count = 0
        for b, ero in orphans:
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

            # Resolve note reflects whether ERO was RETURNED or CLOSED.
            resolution_label = (
                "ERO already returned"
                if ero.status == ExternalRepairOrder.Status.RETURNED
                else "ERO already closed"
            )

            WorkOrderBlockerEvent.objects.create(
                blocker=b,
                event_type=WorkOrderBlockerEvent.EventType.BLOCKER_RESOLVED,
                actor=actor,
                payload={
                    "note": (
                        f"Backfilled by reconcile_orphan_vendor_blockers "
                        f"({resolution_label} but blocker stuck open)"
                    ),
                },
            )
            b.status = WorkOrderBlocker.Status.RESOLVED
            b.resolved_at = timezone.now()
            b.resolved_by = actor
            b.resolution_note = f"Backfilled — {resolution_label}"
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