"""
WorkOrder operational status computation.

The `lifecycle_status` is explicit (user-driven).
The `operational_status` is DERIVED from open blockers + labor state.
"""
from __future__ import annotations

from typing import Iterable

from .models import (
    WorkOrder,
    WorkOrderBlocker,
)


_PART_SHORTAGE_KINDS = (
    WorkOrderBlocker.Kind.PART,
    WorkOrderBlocker.Kind.SHORTAGE,
)


def _open_blockers(wo: WorkOrder) -> Iterable[WorkOrderBlocker]:
    """Return the OPEN blockers for a WO."""
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

        Phase 1 v1.0.0 hardening — caller is expected to invoke this from
        inside a `transaction.atomic()` block; we re-fetch the WO with
        SELECT FOR UPDATE here so two concurrent blocker changes cannot
        both read the same operational_status and last-writer-wins.

        Saves the WO only if operational_status actually changed.
        Returns the new operational_status.
        """
        from django.db import transaction

        # Refresh with row lock to serialize concurrent recomputes.
        # Caller MUST already be inside transaction.atomic(); on SQLite
        # (test backend) select_for_update is a no-op which is fine.
        if transaction.get_connection().in_atomic_block:
            wo = WorkOrder.objects.select_for_update().get(pk=wo.pk)
        else:
            wo = WorkOrder.objects.get(pk=wo.pk)

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

        # Step 5: lifecycle IN_PROGRESS with no blockers → active.
        if wo.lifecycle_status == WorkOrder.LifecycleStatus.IN_PROGRESS:
            return _set_if_changed(wo, WorkOrder.OperationalStatus.ACTIVE)

        # Step 6: default (assigned/draft with no labor and no blockers).
        return _set_if_changed(wo, WorkOrder.OperationalStatus.PAUSED)


__all__ = ["WorkOrderService"]
