"""
WorkOrder operational status computation.

The `lifecycle_status` is explicit (user-driven).
The `operational_status` is DERIVED from open blockers + labor state.

During the migration window (Phase 1-4), the derivation has a dual-read
fallback: if a WO has no blocker rows, query the external entities
directly. After 2 release cycles (Phase 5), the fallback is removed.
"""
from __future__ import annotations

from typing import Iterable

from .models import (
    ExternalRepairOrder,
    ExternalRepairRequest,
    WorkOrder,
    WorkOrderBlocker,
)
from inventory.models import PartIssueLine, PartShortageReport


_PART_SHORTAGE_KINDS = (
    WorkOrderBlocker.Kind.PART,
    WorkOrderBlocker.Kind.SHORTAGE,
)


def _open_blockers(wo: WorkOrder) -> Iterable[WorkOrderBlocker]:
    """Return the OPEN blockers for a WO (uses the new table directly)."""
    return wo.blockers.filter(status=WorkOrderBlocker.Status.OPEN)


def _set_if_changed(wo: WorkOrder, new_status: str) -> str:
    """Update operational_status only if it actually changed (avoid log
    noise from save() during recompute). Returns the new value either way.
    """
    if wo.operational_status != new_status:
        wo.operational_status = new_status
        wo.save(update_fields=["operational_status", "updated_at"])
    return new_status


class WorkOrderService:
    @staticmethod
    def recompute_operational_status(wo: WorkOrder) -> str:
        """
        Derive operational_status from current state. Called after every
        blocker change, every labor start/stop, every assignment change.

        Order of precedence:
        1. If any open PART or SHORTAGE blocker -> "pending_parts"
        2. If any open VENDOR_REPAIR blocker   -> "waiting_vendor"
        3. If any open OPERATIONAL blocker     -> "paused"
        4. If lifecycle is closed/cancelled    -> terminal (don't change)
        5. If labor is actively running        -> "active"
        6. Default                             -> "paused"

        Saves the WO only if operational_status actually changed
        (avoid log noise). Returns the new operational_status.

        During Phase 1-4, also has a dual-read fallback for legacy WOs
        (no blockers) — query external entities directly.
        """
        # Step 4: terminal states — never auto-modify.
        if wo.lifecycle_status in (
            WorkOrder.LifecycleStatus.CLOSED,
            WorkOrder.LifecycleStatus.CANCELLED,
        ):
            return wo.operational_status

        open_blockers = list(_open_blockers(wo))

        # Steps 1-3: blocker-based precedence (highest priority first).
        if any(b.kind in _PART_SHORTAGE_KINDS for b in open_blockers):
            return _set_if_changed(wo, WorkOrder.OperationalStatus.PENDING_PARTS)
        if any(b.kind == WorkOrderBlocker.Kind.VENDOR_REPAIR for b in open_blockers):
            return _set_if_changed(wo, WorkOrder.OperationalStatus.WAITING_VENDOR)
        if any(b.kind == WorkOrderBlocker.Kind.OPERATIONAL for b in open_blockers):
            return _set_if_changed(wo, WorkOrder.OperationalStatus.PAUSED)

        # Dual-read fallback (Phase 1-4): if the WO has no blocker rows
        # at all, query the authoritative external entities directly.
        # See ADR-0007 sub-decision 5.
        if not open_blockers:
            if PartIssueLine.objects.filter(
                work_order=wo,
                status__in=[
                    PartIssueLine.Status.PENDING,
                    PartIssueLine.Status.APPROVED,
                    PartIssueLine.Status.ALLOCATED,
                ],
            ).exists() or PartShortageReport.objects.filter(
                work_order=wo,
                status__in=[
                    PartShortageReport.Status.PENDING_REVIEW,
                    PartShortageReport.Status.APPROVED,
                    PartShortageReport.Status.IN_FULFILLMENT,
                    PartShortageReport.Status.BLOCKED,
                ],
            ).exists():
                return _set_if_changed(wo, WorkOrder.OperationalStatus.PENDING_PARTS)
            if ExternalRepairRequest.objects.filter(
                work_order=wo,
                status=ExternalRepairRequest.Status.PENDING,
            ).exists() or ExternalRepairOrder.objects.filter(
                work_order=wo,
                status__in=[
                    ExternalRepairOrder.Status.DRAFT,
                    ExternalRepairOrder.Status.SENT_TO_VENDOR,
                    ExternalRepairOrder.Status.RETURNED,
                ],
            ).exists():
                return _set_if_changed(wo, WorkOrder.OperationalStatus.WAITING_VENDOR)
            if getattr(wo, "pause_reason", ""):
                return _set_if_changed(wo, WorkOrder.OperationalStatus.PAUSED)

        # Step 5: labor is actively running.
        if wo.labor_started_at and not wo.labor_stopped_at:
            return _set_if_changed(wo, WorkOrder.OperationalStatus.ACTIVE)

        # Step 6: default.
        return _set_if_changed(wo, WorkOrder.OperationalStatus.PAUSED)


__all__ = ["WorkOrderService"]
