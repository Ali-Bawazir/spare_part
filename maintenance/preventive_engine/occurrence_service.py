"""OccurrenceService — manages PMExecution lifecycle and WorkOrder transitions.

One occurrence = one PMExecution row, optionally linked to a WorkOrder.
This service is the single entry point for starting, completing, returning,
and approving PM work. Views call these methods; they never mutate state
directly.

State machine:
    (no row yet)
        ↓  generate_today() / start()
    SUBMITTED + WorkOrder IN_PROGRESS  (technician actively working)
        ↓  complete()  (gated: ≥1 checked OR notes, + photos if required)
    SUBMITTED + WorkOrder PENDING_REVIEW  (waiting for manager)
        ↓  approve()
    APPROVED + WorkOrder CLOSED + schedule.next_due_at advanced
        ↓  return_to_technician()
    REJECTED + WorkOrder IN_PROGRESS  (yellow banner on tech page)
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Optional

from django.db import transaction
from django.utils import timezone

from ..models import (
    PMExecution,
    PMSchedule,
    WorkOrder,
    WorkOrderStateLog,
)


@dataclass
class CompleteResult:
    success: bool
    error: str = ""
    pm_execution: Optional[PMExecution] = None
    work_order: Optional[WorkOrder] = None


@transaction.atomic
def get_or_create_for_today(schedule: PMSchedule, due_at) -> PMExecution:
    """Return the (today's) PMExecution for a schedule+due_at tuple.

    Idempotent: same (schedule, scheduled_due_at) returns the same row.
    Creates a new row in SUBMITTED status if none exists yet.
    """
    scheduled = timezone.now().replace(
        hour=schedule.due_time.hour if hasattr(schedule.due_time, "hour") else 8,
        minute=schedule.due_time.minute if hasattr(schedule.due_time, "minute") else 0,
        second=0, microsecond=0,
    )
    obj, _ = PMExecution.objects.get_or_create(
        pm_schedule=schedule,
        scheduled_due_at=scheduled,
        defaults={"status": PMExecution.Status.SUBMITTED},
    )
    return obj


@transaction.atomic
def start(pm_execution: PMExecution, technician, work_order_creator=None) -> WorkOrder:
    """Start labor on a PM occurrence.

    Creates a WorkOrder if none exists, transitions lifecycle to IN_PROGRESS,
    and stamps labor_started_at. The PMExecution remains in SUBMITTED until
    complete() is called.
    """
    wo = getattr(pm_execution, "work_order", None)
    if wo is None:
        wo = WorkOrder.objects.create(
            machine=pm_execution.pm_schedule.machine,
            component=pm_execution.pm_schedule.component,
            category=WorkOrder.Category.PREVENTIVE,
            created_by=work_order_creator or technician,
            assigned_technician=technician,
            lifecycle_status=WorkOrder.LifecycleStatus.IN_PROGRESS,
            labor_started_at=timezone.now(),
        )
        pm_execution.work_order = wo
        pm_execution.completed_by = technician
        pm_execution.completed_at = timezone.now()
        pm_execution.save(update_fields=["work_order", "completed_by", "completed_at"])
        WorkOrderStateLog.objects.create(
            work_order=wo,
            from_status="",
            to_status=WorkOrder.LifecycleStatus.IN_PROGRESS,
            actor=technician,
            note=f"PM started ({pm_execution.pm_schedule.template.title})",
        )
    elif wo.lifecycle_status == WorkOrder.LifecycleStatus.ASSIGNED:
        wo.lifecycle_status = WorkOrder.LifecycleStatus.IN_PROGRESS
        wo.labor_started_at = timezone.now()
        wo.assigned_technician = technician
        wo.save(update_fields=["lifecycle_status", "labor_started_at", "assigned_technician"])
        WorkOrderStateLog.objects.create(
            work_order=wo,
            from_status=WorkOrder.LifecycleStatus.ASSIGNED,
            to_status=WorkOrder.LifecycleStatus.IN_PROGRESS,
            actor=technician,
            note="PM work started",
        )
    return wo


@transaction.atomic
def complete(
    pm_execution: PMExecution,
    technician,
    *,
    checklist_results: list,
    notes: str = "",
    photo_count: int = 0,
    required_photo_count: int = 0,
    root_cause: str = "",
) -> CompleteResult:
    """Submit a completed PM for review.

    Gating:
      - ≥1 checklist item checked OR notes non-empty
      - photos ≥ required_photo_count (if required_photo_count > 0)

    On success: WO → PENDING_REVIEW, action_taken written as structured
    checklist summary, PMExecution stays in SUBMITTED (waiting review).
    """
    has_check = any(r.get("checked") for r in checklist_results)
    notes_ok = bool(notes and notes.strip())
    if not (has_check or notes_ok):
        return CompleteResult(False, "Please check at least one item or add notes before completing.")
    if required_photo_count > 0 and photo_count < required_photo_count:
        return CompleteResult(False, f"At least {required_photo_count} photo required.")

    wo = pm_execution.work_order
    if wo is None:
        return CompleteResult(False, "Work order not started yet.")

    # Build structured action_taken. On re-submission (PM is currently
    # REJECTED), APPEND instead of overwriting so the audit trail of
    # earlier attempts is preserved.
    lines = []
    for r in checklist_results:
        marker = "✓" if r.get("checked") else "✗"
        lines.append(f"[{marker}] {r.get('text','')}")
        if r.get("note"):
            lines.append(f"  Note: {r['note']}")
    if notes:
        lines.append("")
        lines.append(f"Notes: {notes}")
    attempt_block = "\n".join(lines)

    is_resubmit = pm_execution.status == PMExecution.Status.REJECTED
    if is_resubmit:
        # This is a re-submission of a previously returned PM.
        # Preserve the prior action_taken and append the new attempt,
        # labelled with attempt number + who submitted.
        # The "attempt number" is the new count after bumping.
        wo.rejection_count = (wo.rejection_count or 0) + 1
        attempt_n = wo.rejection_count + 1  # the attempt we're saving now
        submitted_at = timezone.now().strftime("%Y-%m-%d %H:%M")
        prefix = (
            f"--- Attempt {attempt_n} "
            f"(re-submitted by {technician} at {submitted_at}) ---\n"
        )
        if wo.action_taken:
            wo.action_taken = wo.action_taken + "\n\n" + prefix + attempt_block
        else:
            wo.action_taken = prefix + attempt_block
        # Flip back to SUBMITTED so the manager's reviews queue
        # (which filters status=SUBMITTED) sees this re-submission.
        # The original rejection metadata is kept on the WO so the
        # history remains intact; only the PM status moves.
        pm_execution.status = PMExecution.Status.SUBMITTED
        pm_execution.rejected_at = None
        pm_execution.rejected_by = None
        # rejection_reason is preserved on WO but cleared on PM so the
        # manager isn't confused — the next review pass starts fresh.
        pm_execution.rejection_reason = ""
        pm_execution.save(update_fields=["status", "rejected_at", "rejected_by", "rejection_reason"])
    else:
        wo.action_taken = attempt_block
    if root_cause:
        wo.root_cause = root_cause
    wo.photo_count = photo_count
    if wo.lifecycle_status != WorkOrder.LifecycleStatus.PENDING_REVIEW:
        wo.lifecycle_status = WorkOrder.LifecycleStatus.PENDING_REVIEW
        wo.labor_stopped_at = timezone.now()
    wo.save(update_fields=["action_taken", "root_cause", "photo_count", "lifecycle_status", "labor_stopped_at", "rejection_count", "updated_at"])

    if wo.lifecycle_status == WorkOrder.LifecycleStatus.PENDING_REVIEW:
        WorkOrderStateLog.objects.create(
            work_order=wo,
            from_status=WorkOrder.LifecycleStatus.IN_PROGRESS,
            to_status=WorkOrder.LifecycleStatus.PENDING_REVIEW,
            actor=technician,
            note="PM submitted for manager review",
        )

    return CompleteResult(True, pm_execution=pm_execution, work_order=wo)


@transaction.atomic
def return_to_technician(
    pm_execution: PMExecution,
    manager,
    *,
    reason: str,
) -> WorkOrder:
    """Manager returns the submission. WO back to IN_PROGRESS, REJECTED status."""
    if not reason or not reason.strip():
        raise ValueError("Return reason is required.")

    wo = pm_execution.work_order
    pm_execution.status = PMExecution.Status.REJECTED
    pm_execution.rejected_by = manager
    pm_execution.rejected_at = timezone.now()
    pm_execution.rejection_reason = reason
    pm_execution.save(update_fields=["status", "rejected_by", "rejected_at", "rejection_reason"])

    wo.lifecycle_status = WorkOrder.LifecycleStatus.IN_PROGRESS
    wo.rejected_at = timezone.now()
    wo.rejected_by = manager
    wo.rejection_reason = reason
    wo.rejection_count = (wo.rejection_count or 0) + 1
    wo.save(update_fields=[
        "lifecycle_status", "rejected_at", "rejected_by", "rejection_reason", "rejection_count", "updated_at",
    ])

    WorkOrderStateLog.objects.create(
        work_order=wo,
        from_status=WorkOrder.LifecycleStatus.PENDING_REVIEW,
        to_status=WorkOrder.LifecycleStatus.IN_PROGRESS,
        actor=manager,
        note=f"Returned to technician: {reason[:200]}",
    )
    return wo


@transaction.atomic
def approve(pm_execution: PMExecution, manager) -> WorkOrder:
    """Manager approves. WO → CLOSED, PMExecution → APPROVED, schedule advances."""
    wo = pm_execution.work_order

    pm_execution.status = PMExecution.Status.APPROVED
    pm_execution.approved_by = manager
    pm_execution.approved_at = timezone.now()
    pm_execution.save(update_fields=["status", "approved_by", "approved_at"])

    # Backfill completed_by from assigned_technician if a technician
    # did the work but the submit flow didn't record it on the
    # PMExecution row. Keeps the History table clean (no "— technician"
    # with em-dash when the work actually was completed).
    if pm_execution.completed_by_id is None and pm_execution.assigned_technician_id is not None:
        pm_execution.completed_by = pm_execution.assigned_technician
        pm_execution.save(update_fields=["completed_by"])

    if wo is not None:
        wo.lifecycle_status = WorkOrder.LifecycleStatus.CLOSED
        wo.save(update_fields=["lifecycle_status", "updated_at"])
        WorkOrderStateLog.objects.create(
            work_order=wo,
            from_status=WorkOrder.LifecycleStatus.PENDING_REVIEW,
            to_status=WorkOrder.LifecycleStatus.CLOSED,
            actor=manager,
            note="PM approved",
        )

    # Advance schedule to next due
    from . import scheduling_service
    sched = pm_execution.pm_schedule
    sched.next_due_at = scheduling_service.next_due_at(sched, timezone.now())
    sched.last_completed_at = timezone.now()
    sched.save(update_fields=["next_due_at", "last_completed_at"])

    return wo


@transaction.atomic
def mark_done(pm_execution: PMExecution, manager) -> WorkOrder:
    """Manager manually completes a PM (emergency path). Auto-approves."""
    wo = pm_execution.work_order
    if wo is None:
        # Synthesize a completed-on-behalf WO
        wo = WorkOrder.objects.create(
            machine=pm_execution.pm_schedule.machine,
            component=pm_execution.pm_schedule.component,
            category=WorkOrder.Category.PREVENTIVE,
            created_by=manager,
            assigned_technician=manager,
            lifecycle_status=WorkOrder.LifecycleStatus.CLOSED,
            labor_started_at=timezone.now(),
            labor_stopped_at=timezone.now(),
            action_taken="[Manager-completed] Manual mark done by manager.",
        )
        pm_execution.work_order = wo
    pm_execution.completed_by = manager
    pm_execution.completed_at = timezone.now()
    pm_execution.save(update_fields=["work_order", "completed_by", "completed_at"])
    return approve(pm_execution, manager)