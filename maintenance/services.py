from __future__ import annotations

from typing import Optional

from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone

from .models import AuditEntry, MaintenanceIssue, WorkOrder, WorkOrderStateLog, Downtime, WorkOrderAssignmentHistory

User = get_user_model()


def log_audit(*, actor, action: str, entity: str = "", object_id: str = "", payload=None):
    AuditEntry.objects.create(
        actor=actor,
        action=action,
        entity=entity,
        object_id=str(object_id) if object_id is not None else "",
        payload=payload or {},
    )


def transition_work_order(
    wo: WorkOrder,
    to_status: str,
    *,
    actor: Optional[User],
    note: str = "",
) -> None:
    from_status = wo.status
    if from_status == to_status:
        return
    wo.status = to_status
    wo.save(update_fields=["status", "updated_at"])
    WorkOrderStateLog.objects.create(
        work_order=wo,
        from_status=from_status,
        to_status=to_status,
        actor=actor,
        note=note[:500],
    )
    log_audit(
        actor=actor,
        action="work_order_status",
        entity="WorkOrder",
        object_id=wo.pk,
        payload={"from": from_status, "to": to_status, "note": note},
    )


def pause_other_in_progress(technician: User, except_pk: int | None = None) -> None:
    qs = WorkOrder.objects.filter(
        assigned_technician=technician,
        status=WorkOrder.Status.IN_PROGRESS,
    ).select_for_update()
    if except_pk:
        qs = qs.exclude(pk=except_pk)
    for other in qs:
        other.labor_stopped_at = timezone.now()
        other.save(update_fields=["labor_stopped_at", "updated_at"])
        transition_work_order(other, WorkOrder.Status.PAUSED, actor=technician, note="Auto-pause: another task started")
        prev_assignment = WorkOrderAssignmentHistory.objects.filter(
            work_order=other, technician=technician, unassigned_at__isnull=True
        ).first()
        if prev_assignment:
            prev_assignment.unassigned_at = timezone.now()
            prev_assignment.reason = "Auto-paused: started another work order"
            prev_assignment.save()


def get_other_active_work_order(technician: User, except_pk: int | None = None) -> WorkOrder | None:
    qs = WorkOrder.objects.filter(
        assigned_technician=technician,
        status=WorkOrder.Status.IN_PROGRESS,
    )
    if except_pk:
        qs = qs.exclude(pk=except_pk)
    return qs.select_related("machine", "issue").order_by("-updated_at").first()


@transaction.atomic
def technician_start_work(wo: WorkOrder, technician: User) -> None:
    wo = WorkOrder.objects.select_for_update().get(pk=wo.pk)
    # Create a downtime record on first start (not on resume from pause)
    if not wo.downtime_started_at:
        wo.downtime_started_at = timezone.now()
        wo.save(update_fields=["downtime_started_at", "updated_at"])
        Downtime.objects.create(
            work_order=wo,
            downtime_type=Downtime.DowntimeType.EMERGENCY if wo.is_emergency else Downtime.DowntimeType.BREAKDOWN,
            start_time=timezone.now(),
            reason=f"WO started — {'Emergency' if wo.is_emergency else 'Breakdown'}",
        )
    if wo.status == WorkOrder.Status.IN_PROGRESS:
        return
    pause_other_in_progress(technician, except_pk=wo.pk)
    now = timezone.now()
    if wo.status == WorkOrder.Status.IN_PROGRESS:
        wo.labor_started_at = now
        wo.labor_stopped_at = None
        wo.save(update_fields=["labor_started_at", "labor_stopped_at", "updated_at"])
        log_audit(
            actor=technician,
            action="work_order_resume_labor",
            entity="WorkOrder",
            object_id=wo.pk,
            payload={},
        )
        return
    if wo.downtime_started_at is None:
        wo.downtime_started_at = now
    wo.labor_started_at = now
    wo.labor_stopped_at = None
    wo.save(update_fields=["downtime_started_at", "labor_started_at", "labor_stopped_at", "updated_at"])
    transition_work_order(wo, WorkOrder.Status.IN_PROGRESS, actor=technician, note="Start work")


@transaction.atomic
def technician_submit_for_review(wo: WorkOrder, technician: User) -> None:
    now = timezone.now()
    wo.labor_stopped_at = now
    wo.save(update_fields=["labor_stopped_at", "updated_at"])
    transition_work_order(wo, WorkOrder.Status.PENDING_REVIEW, actor=technician, note="Submitted for review")
    from .notifications import notify_wo_pending_review

    notify_wo_pending_review(wo)


@transaction.atomic
def technician_mark_pending_parts(wo: WorkOrder, technician: User, note: str = "") -> None:
    if wo.status != WorkOrder.Status.IN_PROGRESS:
        raise ValueError("Work order must be in progress.")
    wo.labor_stopped_at = timezone.now()
    wo.save(update_fields=["labor_stopped_at", "updated_at"])
    transition_work_order(
        wo,
        WorkOrder.Status.PENDING_PARTS,
        actor=technician,
        note=(note or "Waiting for spare parts")[:500],
    )


@transaction.atomic
def technician_mark_waiting_vendor(wo: WorkOrder, technician: User, note: str = "") -> None:
    if wo.status != WorkOrder.Status.IN_PROGRESS:
        raise ValueError("Work order must be in progress.")
    wo.labor_stopped_at = timezone.now()
    wo.save(update_fields=["labor_stopped_at", "updated_at"])
    transition_work_order(
        wo,
        WorkOrder.Status.WAITING_FOR_VENDOR,
        actor=technician,
        note=(note or "Waiting for external vendor")[:500],
    )


@transaction.atomic
def manager_close_work_order(wo: WorkOrder, manager: User, approve: bool, rejection_reason: str = "") -> None:
    """
    Manager approves or rejects a work order pending review.
    
    If approve=False, rejection_reason is REQUIRED (raises ValueError if empty).
    """
    now = timezone.now()
    if approve:
        open_dt = wo.downtime_records.filter(end_time__isnull=True).first()
        if open_dt:
            open_dt.end_time = timezone.now()
            open_dt.save()
            wo.downtime_ended_at = timezone.now()
            wo.save(update_fields=["downtime_ended_at", "updated_at"])
        wo.rejected_at = None
        wo.rejected_by = None
        wo.rejection_reason = ""
        wo.save(update_fields=["rejected_at", "rejected_by", "rejection_reason", "updated_at"])
        transition_work_order(wo, WorkOrder.Status.CLOSED, actor=manager, note="Approved & closed")
    else:
        if not rejection_reason or not rejection_reason.strip():
            raise ValueError("Rejection reason is required.")
        wo.rejection_count = (wo.rejection_count or 0) + 1
        wo.rejected_at = now
        wo.rejected_by = manager
        wo.rejection_reason = rejection_reason.strip()[:500]
        wo.labor_started_at = now
        wo.labor_stopped_at = None
        wo.save(update_fields=[
            "rejection_count", "rejected_at", "rejected_by", "rejection_reason",
            "labor_started_at", "labor_stopped_at", "updated_at"
        ])
        transition_work_order(
            wo, WorkOrder.Status.IN_PROGRESS, 
            actor=manager, 
            note=f"Rejected: {rejection_reason.strip()[:200]}"
        )


def validate_issue(issue: MaintenanceIssue, *, actor: User, priority: str) -> None:
    issue.status = MaintenanceIssue.Status.VALIDATED
    issue.priority = priority
    issue.validated_by = actor
    issue.validated_at = timezone.now()
    issue.save()
    log_audit(actor=actor, action="issue_validated", entity="MaintenanceIssue", object_id=issue.pk, payload={"priority": priority})
    from .notifications import notify_issue_validated

    notify_issue_validated(issue)


@transaction.atomic
def archive_work_order(wo, actor, reason=""):
    """Archive a work order. Archived WOs are hidden from normal queries."""
    if wo.is_archived:
        raise ValueError("Work order is already archived.")
    wo.is_archived = True
    wo.archived_at = timezone.now()
    wo.archived_by = actor
    wo.save(update_fields=["is_archived", "archived_at", "archived_by", "updated_at"])
    log_audit(
        actor=actor, action="work_order_archived",
        entity="WorkOrder", object_id=str(wo.pk),
        payload={"reason": reason}
    )


def restore_work_order(wo, actor):
    """Restore an archived work order."""
    if not wo.is_archived:
        raise ValueError("Work order is not archived.")
    wo.is_archived = False
    wo.archived_at = None
    wo.archived_by = None
    wo.save(update_fields=["is_archived", "archived_at", "archived_by", "updated_at"])
    log_audit(
        actor=actor, action="work_order_restored",
        entity="WorkOrder", object_id=str(wo.pk)
    )


@transaction.atomic
def archive_maintenance_issue(issue, actor, reason=""):
    if issue.is_archived:
        raise ValueError("Issue is already archived.")
    issue.is_archived = True
    issue.archived_at = timezone.now()
    issue.archived_by = actor
    issue.save(update_fields=["is_archived", "archived_at", "archived_by"])
    log_audit(actor=actor, action="issue_archived", entity="MaintenanceIssue",
               object_id=str(issue.pk), payload={"reason": reason})
