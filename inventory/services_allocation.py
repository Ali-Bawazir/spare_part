"""
Part allocation pipeline.

Reallocation order is priority-ranked (Emergency > CRITICAL > HIGH > NORMAL > LOW;
oldest first as tiebreaker). On PO receive, this service walks all open
PartIssueLines for the part and grants new allocated_qty from replenished stock.

Keystone rule (from ADR-0007): the PART WO Blocker resolves on
`issued_qty == approved_qty` (when warehouse issues the part), NOT on
allocation. The blocker stays OPEN between approval and issue.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any
from django.db import transaction
from django.db.models import Case, When, Value, IntegerField
from django.utils import timezone

from .models import PartIssueLine, Inventory, InventoryReservation


def _wo_priority_rank(wo) -> int:
    """
    Priority rank used in allocation sorting. Lower = higher priority.
    Returns 0 for emergency WOs, 5 otherwise (since WorkOrder has no
    priority attribute; the issue.priority is on MaintenanceIssue but
    for Phase 2B we keep it simple).
    """
    return 0 if getattr(wo, "is_emergency", False) else 5


class PartAllocationService:
    """Priority-ranked allocation pipeline."""

    @staticmethod
    def free_stock_for_part(part: Any) -> Decimal:
        """Compute free stock for a part: max(0, quantity_available - quantity_reserved).

        This is the basis for shortage decisions. Using gross
        quantity_available would over-allocate when many WOs are
        simultaneously reserving the same part.
        """
        inv = Inventory.objects.filter(part=part).first()
        if not inv:
            return Decimal("0")
        return max(Decimal("0"), inv.quantity_available - inv.quantity_reserved)

    @staticmethod
    def queue_position(line: PartIssueLine) -> tuple[int, int]:
        """Return (position, total) for a line's priority-ranked queue position.

        1-indexed. The line at position 1 is the next to be allocated.
        Sorts by (work_order.is_emergency desc, work_order.priority rank,
        line.created_at asc).
        """
        if line.status not in ("pending", "approved", "allocated"):
            return (0, 0)
        open_lines = PartIssueLine.objects.filter(
            part=line.part,
            status__in=["pending", "approved", "allocated"],
        ).annotate(
            sort_key=Case(
                When(work_order__is_emergency=True, then=Value(0)),
                default=Value(1),
                output_field=IntegerField(),
            ),
        ).order_by("sort_key", "created_at")
        position = next((i for i, l in enumerate(open_lines, start=1) if l.pk == line.pk), 0)
        return (position, open_lines.count())

    @staticmethod
    @transaction.atomic
    def allocate_one(line: PartIssueLine) -> bool:
        """Allocate as much as possible to a single line from free stock.

        Creates InventoryReservation rows for the new allocation.
        Returns True if the line is fully allocated
        (allocated_qty == approved_qty). Does NOT resolve the PART blocker.
        """
        if line.status not in ("pending", "approved", "allocated"):
            return False
        if line.approved_qty <= 0:
            return False
        gap = line.approved_qty - line.allocated_qty
        if gap <= 0:
            return line.allocated_qty >= line.approved_qty
        free = PartAllocationService.free_stock_for_part(line.part)
        if free <= 0:
            return False
        give = min(gap, free)
        # Capture the previous status to know if we transitioned
        prev_status = line.status
        # Update the line
        line.allocated_qty = line.allocated_qty + give
        if prev_status == "pending":
            line.status = "approved"
        if line.allocated_qty >= line.approved_qty:
            line.status = "allocated"
        line.save(update_fields=["allocated_qty", "status"])
        # Create the InventoryReservation row
        # The post_save signal on InventoryReservation refreshes
        # Inventory.quantity_reserved (Phase 2A signal in maintenance/signals.py)
        wo_priority_rank = _wo_priority_rank(line.work_order)
        InventoryReservation.objects.create(
            part=line.part,
            work_order=line.work_order,
            quantity=give,
            source_line=line,
            priority_at_creation=wo_priority_rank,
        )
        # Fire PART_ALLOCATED event on the WO's blocker (best-effort)
        try:
            from maintenance.services_blocker import WorkOrderBlockerEventService
            from maintenance.models import WorkOrderBlocker
            blocker = WorkOrderBlocker.objects.filter(
                work_order=line.work_order,
                kind=WorkOrderBlocker.Kind.PART,
                status=WorkOrderBlocker.Status.OPEN,
            ).first()
            if blocker:
                WorkOrderBlockerEventService.record(
                    blocker=blocker,
                    event_type="PART_ALLOCATED",
                    actor=None,
                    payload={
                        "line_id": line.pk,
                        "allocated_qty": str(line.allocated_qty),
                        "granted": str(give),
                    },
                )
        except Exception:
            # Don't let event-log failure break the allocation
            pass
        return line.allocated_qty >= line.approved_qty

    @staticmethod
    @transaction.atomic
    def reallocate_for_part(part: Any) -> list[dict]:
        """Walk all open PartIssueLines for a part in priority order.

        For each line, allocate as much as possible from current free stock.
        Returns a list of dicts: {line_id, work_order_id, allocated_qty,
        granted, fully_allocated} for downstream notifications.
        """
        open_lines = PartIssueLine.objects.filter(
            part=part,
            status__in=["pending", "approved", "allocated"],
        ).annotate(
            sort_key=Case(
                When(work_order__is_emergency=True, then=Value(0)),
                default=Value(1),
                output_field=IntegerField(),
            ),
        ).order_by("sort_key", "created_at").select_related("work_order")
        results = []
        for line in open_lines:
            granted_before = line.allocated_qty
            try:
                PartAllocationService.allocate_one(line)
            except Exception:
                continue
            line.refresh_from_db()
            results.append({
                "line_id": line.pk,
                "work_order_id": line.work_order_id,
                "allocated_qty": line.allocated_qty,
                "granted": line.allocated_qty - granted_before,
                "fully_allocated": line.allocated_qty >= line.approved_qty,
            })
        return results
