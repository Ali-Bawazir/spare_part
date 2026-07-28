"""AssignmentService — single-owner enforcement for PM occurrences.

A PM occurrence has exactly one assigned technician at a time. Reassignment
preserves checklist state, photos, and notes; only the active timer resets.
"""

from __future__ import annotations

from typing import Optional

from django.db import transaction
from django.utils import timezone

from ..models import PMExecution, PMSchedule, WorkOrder


@transaction.atomic
def assign(pm_execution: PMExecution, technician, *, by=None) -> PMExecution:
    """Set the owner of a PM occurrence. Creates a WO if none exists yet."""
    wo = getattr(pm_execution, "work_order", None)
    if wo is None:
        wo = WorkOrder.objects.create(
            machine=pm_execution.pm_schedule.machine,
            component=pm_execution.pm_schedule.component,
            category=WorkOrder.Category.PREVENTIVE,
            created_by=by or technician,
            assigned_technician=technician,
            lifecycle_status=WorkOrder.LifecycleStatus.ASSIGNED,
        )
        pm_execution.work_order = wo
    else:
        wo.assigned_technician = technician
        wo.save(update_fields=["assigned_technician", "updated_at"])

    pm_execution.assigned_technician = technician
    pm_execution.assigned_at = timezone.now()
    pm_execution.save(update_fields=["work_order", "assigned_technician", "assigned_at"])
    return pm_execution


@transaction.atomic
def reassign(pm_execution: PMExecution, new_technician, *, by=None, reason: Optional[str] = None) -> PMExecution:
    """Transfer ownership to a new technician.

    Preserves:
      - checklist state (checked items stay checked)
      - photos
      - notes
      - completed_by / completed_at
      - action_taken (if already submitted)

    Resets:
      - PMExecution.work_order.assigned_technician
      - labor_started_at (timer restarts for new owner)
    """
    old_tech = pm_execution.assigned_technician
    if old_tech and old_tech.pk == new_technician.pk:
        return pm_execution  # no-op

    wo = pm_execution.work_order
    if wo is not None:
        wo.assigned_technician = new_technician
        # Reset labor timer for the new owner
        if wo.lifecycle_status == WorkOrder.LifecycleStatus.IN_PROGRESS:
            wo.labor_started_at = timezone.now()
        wo.save(update_fields=["assigned_technician", "labor_started_at", "updated_at"])

    pm_execution.assigned_technician = new_technician
    pm_execution.assigned_at = timezone.now()
    pm_execution.reassignment_count = (pm_execution.reassignment_count or 0) + 1
    pm_execution.last_reassignment_reason = reason or ""
    pm_execution.save(update_fields=[
        "assigned_technician", "assigned_at",
        "reassignment_count", "last_reassignment_reason",
    ])
    return pm_execution


def validate_one_owner(pm_execution: PMExecution) -> None:
    """Raise ValueError if the occurrence has no owner."""
    if pm_execution.assigned_technician_id is None:
        raise ValueError("PM occurrence must have exactly one assigned technician.")