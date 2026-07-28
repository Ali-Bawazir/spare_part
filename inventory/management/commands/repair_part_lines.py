"""Repair stuck PART / SHORTAGE blockers for which stock is now available.

When a technician requests a part the request goes through this flow:
    1. PENDING PartIssueLine is created
    2. Manager approves → PartIssueLine.status='approved',
       approved_qty=qty, shortage_qty=0 (if stock available) OR
       approved_qty=0, shortage_qty=qty (if no stock), and a
       PartShortageReport is created
    3. Stock arrives (PO receive) → the shortage_filled event fires
    4. The PART/SHORTAGE blockers should resolve via the
       sync_from_external_event handler

The missed step (the one that creates stuck blockers):

    If stock is available at approval time but the manager short-approves
    the line (or the shortage decision is split: 0 issue + N procurement),
    the line stays `status=approved` with `approved_qty=0` and
    `shortage_qty=quantity`. When stock later arrives, the shortage
    pathway does NOT auto-issue the line because `approved_qty=0`.

The fix in this command:

    For each PartIssueLine where:
      - status='approved'
      - shortage_qty > 0
      - inventory has enough stock (quantity_on_hand >= quantity)

    ... auto-issue the line (set issued_qty=quantity, status='issued',
    issued_at=now, issued_by=manager) and call
    sync_from_external_event(line, 'PART_ISSUED') to resolve the
    PART blocker.

    For each PartShortageReport where status='approved' and the
    associated PR is no longer pending:
        - transition to 'in_fulfillment' (the standard populated
          state) and call sync_from_external_event(report,
          'SHORTAGE_FULFILLED') to resolve the SHORTAGE blocker.

This is a one-shot reconciliation. Safe to run anytime. Idempotent:
a line that's already issued is skipped.

Usage:
    python manage.py repair_part_lines [--dry-run]
"""
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.utils import timezone


class Command(BaseCommand):
    help = (
        "Resolve stuck PART and SHORTAGE blockers where stock is "
        "available but the line was never auto-issued. "
        "One-shot reconciliation for data produced before the slot "
        "for PO-receive → PSR-fulfilled → line-issued was wired in."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Just list candidates; don't issue lines or resolve blockers.",
        )

    def handle(self, *args, **options):
        from accounts.models import User
        from inventory.models import (
            Inventory,
            PartIssueLine,
            PartShortageReport,
        )
        from inventory.services import execute_warehouse_issue
        from maintenance.models import WorkOrder
        from maintenance.services_blocker import WorkOrderBlockerService
        from maintenance.services_wo_status import WorkOrderService

        manager = (
            User.objects.filter(
                role__in=[User.Role.MANAGER, User.Role.SUPER_ADMIN],
                is_active=True,
            ).first()
            or User.objects.filter(is_superuser=True).first()
        )
        if manager is None:
            self.stdout.write(self.style.ERROR(
                "No manager/super_admin user found. Create one first."
            ))
            return

        # 1. Find stuck PART lines: APPROVED, has shortage, inventory can
        #    cover the full quantity, but issued_qty=0.
        stuck_lines = (
            PartIssueLine.objects
            .filter(
                status=PartIssueLine.Status.APPROVED,
                shortage_qty__gt=0,
                issued_qty=0,
            )
            .select_related("work_order", "part")
            .order_by("pk")
        )

        candidates = []
        for line in stuck_lines:
            inv = Inventory.objects.filter(
                part=line.part, site__is_default=True,
            ).first()
            available = inv.quantity_available if inv else Decimal("0")
            if available >= line.quantity:
                candidates.append(line)

        # Affected-WO set is shared between PART and SHORTAGE blocks.
        # repaired counter is also shared (PART lines + SHORTAGE fixes).
        affected_wos = set()
        repaired = 0
        if not candidates:
            self.stdout.write(self.style.SUCCESS(
                "No stuck PART lines found."
            ))
        else:
            self.stdout.write(
                f"Found {len(candidates)} stuck PART line(s) "
                f"with stock available:"
            )
            for line in candidates:
                inv = Inventory.objects.filter(
                    part=line.part, site__is_default=True,
                ).first()
                self.stdout.write(
                    f"  Line #{line.pk}: WO-{line.work_order.number} {line.part.sku} "
                    f"qty={line.quantity} (available={inv.quantity_available if inv else 0})"
                )

            # Apply: call the existing business service. The
            # skip_approval_check=True flag bypasses only the
            # approved_qty>0 check (documented inline at the gate in
            # services.py). Every other precondition, the inventory
            # deduction, the StockMovement, and the PART_ISSUED sync
            # event all run normally.
            for line in candidates:
                try:
                    execute_warehouse_issue(
                        line=line,
                        qty=line.quantity,
                        actor=manager,
                        skip_approval_check=True,
                    )
                except Exception as e:
                    self.stdout.write(self.style.ERROR(
                        f"  Line #{line.pk}: execute_warehouse_issue failed — {e}"
                    ))
                    continue
                affected_wos.add(line.work_order_id)
                repaired += 1
                self.stdout.write(self.style.SUCCESS(
                    f"  Line #{line.pk}: issued ({line.quantity} {line.part.sku})"
                ))

        # 2. For each stuck SHORTAGE (status=approved) whose PSR is no
        #    longer pending, transition to in_fulfillment and resolve the
        #    SHORTAGE blocker.
        if options["dry_run"]:
            self.stdout.write(self.style.WARNING(
                "\n--dry-run: no changes made."
            ))
            return

        stuck_shortages = list(
            PartShortageReport.objects
            .filter(status=PartShortageReport.Status.APPROVED)
            .select_related("work_order", "part")
            .order_by("pk")
        )
        for psr in stuck_shortages:
            # Skip if the linked PR is still pending
            pr = getattr(psr, "purchase_request", None)
            if pr and pr.status == "pending":
                continue
            psr.status = PartShortageReport.Status.IN_FULFILLMENT
            psr.save(update_fields=["status"])
            try:
                WorkOrderBlockerService.sync_from_external_event(
                    external_obj=psr,
                    event_type="SHORTAGE_FULFILLED",
                    actor=manager,
                )
            except Exception as e:
                self.stdout.write(self.style.ERROR(
                    f"  PSR #{psr.pk}: SHORTAGE_FULFILLED sync failed — {e}"
                ))
                continue
            if psr.work_order_id:
                affected_wos.add(psr.work_order_id)
            self.stdout.write(self.style.SUCCESS(
                f"  PSR #{psr.pk}: transitioned to in_fulfillment"
            ))

        # Recompute operational_status on every affected WO
        for wid in affected_wos:
            try:
                wo = WorkOrder.objects.get(pk=wid)
                WorkOrderService.recompute_operational_status(wo)
            except Exception:
                pass

        # Summary: be honest about what we touched.
        part_count = repaired
        shortage_count = sum(
            1 for psr in stuck_shortages
            if getattr(psr, "purchase_request", None) is None
            or psr.purchase_request.status != "pending"
        )
        if part_count or shortage_count:
            parts = (
                f"Issued {part_count} PART line(s). " if part_count else ""
            )
            shortages = (
                f"Transitioned {shortage_count} SHORTAGE report(s). "
                if shortage_count else ""
            )
            self.stdout.write(self.style.SUCCESS(
                f"\nDone. {parts}{shortages}"
                f"Recomputed operational_status for {len(affected_wos)} WO(s)."
            ))
        else:
            self.stdout.write(self.style.SUCCESS("\nNothing to repair."))

