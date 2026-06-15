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


def pause_other_in_progress(
    technician: User,
    except_pk: int | None = None,
    reason: str = WorkOrder.PauseReason.OPERATIONAL,
    note: str = "",
) -> None:
    """Auto-pause any other IN_PROGRESS work order the technician owns.

    Used when a technician starts a new task (including an emergency) and
    needs to free themselves. The reason is recorded on the paused WO so
    analytics can distinguish emergency overrides from operational pauses.

    P3.5: refuses AWAITING_PARTS / AWAITING_VENDOR reasons. Those are
    WO statuses (WAITING_FOR_PARTS / WAITING_FOR_VENDOR), not pause
    reasons. Callers should transition the WO status instead.
    """
    if reason in ("awaiting_parts", "awaiting_vendor"):
        raise ValueError(
            f"{reason!r} is a work-order status, not a pause reason. "
            f"Transition the WO to WAITING_FOR_PARTS or WAITING_FOR_VENDOR instead."
        )
    qs = WorkOrder.objects.filter(
        assigned_technician=technician,
        status=WorkOrder.Status.IN_PROGRESS,
    ).select_for_update()
    if except_pk:
        qs = qs.exclude(pk=except_pk)
    for other in qs:
        other.labor_stopped_at = timezone.now()
        other.pause_reason = reason
        other.pause_note = note[:500] if note else ""
        other.save(update_fields=[
            "labor_stopped_at", "pause_reason", "pause_note", "updated_at"
        ])
        transition_work_order(
            other,
            WorkOrder.Status.PAUSED,
            actor=technician,
            note=(note or f"Auto-pause: {dict(WorkOrder.PauseReason.choices)[reason]}")[:500],
        )
        prev_assignment = WorkOrderAssignmentHistory.objects.filter(
            work_order=other, technician=technician, unassigned_at__isnull=True
        ).first()
        if prev_assignment:
            prev_assignment.unassigned_at = timezone.now()
            prev_assignment.reason = (
                f"Auto-paused: {dict(WorkOrder.PauseReason.choices)[reason]}"
            )
            prev_assignment.save()


def get_other_active_work_order(technician: User, except_pk: int | None = None) -> WorkOrder | None:
    qs = WorkOrder.objects.filter(
        assigned_technician=technician,
        status=WorkOrder.Status.IN_PROGRESS,
    )
    if except_pk:
        qs = qs.exclude(pk=except_pk)
    return qs.select_related("machine", "issue").order_by("-updated_at").first()


def has_active_emergency(technician: User, except_pk: int | None = None) -> bool:
    """True if this technician has any IN_PROGRESS emergency work order.

    Per SRS UC-06 step 2D (Resume Work): a technician cannot resume a
    paused WO if another Emergency WO is currently IN_PROGRESS — the
    emergency must finish first. Use this guard before allowing a
    transition to IN_PROGRESS for any non-emergency WO.
    """
    qs = WorkOrder.objects.filter(
        assigned_technician=technician,
        status=WorkOrder.Status.IN_PROGRESS,
        is_emergency=True,
    )
    if except_pk:
        qs = qs.exclude(pk=except_pk)
    return qs.exists()


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
    # If the WO being started is an emergency, auto-pausing other WOs
    # uses the EMERGENCY pause reason for clearer reporting. Otherwise
    # we mark them as OPERATIONAL (technician switched task).
    pause_reason = (
        WorkOrder.PauseReason.EMERGENCY
        if wo.is_emergency
        else WorkOrder.PauseReason.OPERATIONAL
    )
    pause_other_in_progress(technician, except_pk=wo.pk, reason=pause_reason)
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
    from .notifications import notify_wo_started
    notify_wo_started(wo)


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
    from .notifications import notify_wo_paused
    notify_wo_paused(wo)


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
    from .notifications import notify_wo_paused
    notify_wo_paused(wo)


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
        from .notifications import notify_wo_closed
        notify_wo_closed(wo)
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
def escalate_issue_to_emergency(issue: MaintenanceIssue, *, actor: User) -> None:
    """P3.3: supervisor / manager / super admin can flip a normal issue
    to emergency. Sets is_emergency=True, priority=CRITICAL, and records
    who escalated and when. Idempotent.
    """
    if issue.is_emergency:
        return  # already emergency, no-op
    issue.is_emergency = True
    issue.priority = MaintenanceIssue.Priority.CRITICAL
    issue.escalated_by = actor
    issue.escalated_at = timezone.now()
    issue.save(update_fields=[
        "is_emergency", "priority", "escalated_by", "escalated_at",
    ])
    log_audit(
        actor=actor, action="issue_escalated_to_emergency",
        entity="MaintenanceIssue", object_id=issue.pk,
        payload={"previous_priority": issue.priority},
    )
    try:
        from .notifications import notify_emergency_issue_reported
        notify_emergency_issue_reported(issue)
    except Exception:
        pass


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


@transaction.atomic
def request_external_repair(
    *,
    work_order: WorkOrder,
    requested_by: User,
    diagnosis_note: str,
    part_description: str,
) -> "ExternalRepairRequest":
    """Technician creates a PENDING external-repair request on their WO.

    Only the assigned technician of the WO may submit a request.
    The request is reviewable by the manager; on approval a DRAFT
    ExternalRepairOrder is created and linked back via FK.
    """
    if work_order.assigned_technician_id != requested_by.id:
        raise ValueError("Only the assigned technician can request external repair.")
    if not diagnosis_note.strip():
        raise ValueError("Diagnosis note is required.")
    if not part_description.strip():
        raise ValueError("Part description is required.")

    from .models import ExternalRepairRequest, ExternalRepairOrder

    err = ExternalRepairRequest.objects.create(
        work_order=work_order,
        requested_by=requested_by,
        diagnosis_note=diagnosis_note.strip(),
        part_description=part_description.strip(),
    )
    log_audit(
        actor=requested_by,
        action="external_repair_requested",
        entity="ExternalRepairRequest",
        object_id=str(err.pk),
        payload={"work_order": work_order.number, "part": part_description.strip()[:80]},
    )
    return err


@transaction.atomic
def approve_external_repair_request(
    *, err: "ExternalRepairRequest", manager: User, manager_note: str = ""
) -> "ExternalRepairOrder":
    """Manager approves a PENDING request → creates DRAFT ERO on the WO.

    Side effects:
      - err.status = APPROVED
      - err.reviewed_by = manager, err.reviewed_at = now
      - ExternalRepairOrder (DRAFT) is created on the same WO
      - err.repair_order FK points at the new ERO
      - Audit log written
    """
    from .models import ExternalRepairRequest, ExternalRepairOrder

    if err.status != ExternalRepairRequest.Status.PENDING:
        raise ValueError("Only PENDING requests can be approved.")

    now = timezone.now()
    note = (manager_note or "").strip()

    ero = ExternalRepairOrder.objects.create(
        work_order=err.work_order,
        title=f"External repair for {err.part_description[:60]}",
        description=err.diagnosis_note,
        created_by=manager,
        handled_by=manager,
        status=ExternalRepairOrder.Status.DRAFT,
    )

    err.status = ExternalRepairRequest.Status.APPROVED
    err.reviewed_by = manager
    err.reviewed_at = now
    err.manager_note = note
    err.repair_order = ero
    err.save(update_fields=["status", "reviewed_by", "reviewed_at", "manager_note", "repair_order"])

    log_audit(
        actor=manager,
        action="external_repair_request_approved",
        entity="ExternalRepairRequest",
        object_id=str(err.pk),
        payload={"ero_pk": str(ero.pk), "work_order": err.work_order.number},
    )

    from .notifications import notify_repair_draft_created
    notify_repair_draft_created(ero)

    return ero


@transaction.atomic
def reject_external_repair_request(
    *, err: "ExternalRepairRequest", manager: User, manager_note: str = ""
) -> "ExternalRepairRequest":
    """Manager rejects a PENDING request. Reason is mandatory."""
    from .models import ExternalRepairRequest

    if err.status != ExternalRepairRequest.Status.PENDING:
        raise ValueError("Only PENDING requests can be rejected.")
    note = (manager_note or "").strip()
    if not note:
        raise ValueError("A rejection reason is required.")

    err.status = ExternalRepairRequest.Status.REJECTED
    err.reviewed_by = manager
    err.reviewed_at = timezone.now()
    err.manager_note = note
    err.save(update_fields=["status", "reviewed_by", "reviewed_at", "manager_note"])
    log_audit(
        actor=manager,
        action="external_repair_request_rejected",
        entity="ExternalRepairRequest",
        object_id=str(err.pk),
        payload={"work_order": err.work_order.number, "reason": note[:120]},
    )
    return err


def technician_stats(technician: User) -> dict:
    """Per-technician KPI roll-up for /reports/technicians/<id>/.

    Returns:
      - completed_count: closed WOs assigned to this tech
      - in_progress_count: currently IN_PROGRESS
      - reopened_count: total rejection count (sum across their WOs)
      - external_repair_count: closed WOs that went through WAITING_FOR_VENDOR
      - avg_repair_minutes: mean labor duration on closed WOs
                           (labor_stopped_at - labor_started_at in minutes)
      - avg_response_minutes: mean time from first assignment to first start
                              (labor_started_at - first assignment.assigned_at)
      - recent: latest 10 closed WOs with status/duration/emergency badge
    """
    from .models import WorkOrder, WorkOrderAssignmentHistory

    closed = WorkOrder.objects.filter(
        assigned_technician=technician, status=WorkOrder.Status.CLOSED
    )
    completed_count = closed.count()
    in_progress_count = WorkOrder.objects.filter(
        assigned_technician=technician, status=WorkOrder.Status.IN_PROGRESS
    ).count()
    reopened_count = sum(
        wo.rejection_count or 0
        for wo in WorkOrder.objects.filter(assigned_technician=technician)
    )
    external_repair_count = closed.filter(
        state_logs__to_status=WorkOrder.Status.WAITING_FOR_VENDOR
    ).distinct().count()

    # Average repair duration (labor minutes) on closed WOs
    durations = []
    for wo in closed.exclude(labor_started_at__isnull=True).exclude(
        labor_stopped_at__isnull=True
    ):
        delta = (wo.labor_stopped_at - wo.labor_started_at).total_seconds() / 60.0
        if delta > 0:
            durations.append(delta)
    avg_repair_minutes = round(sum(durations) / len(durations), 1) if durations else None

    # Average response time: assignment → first start (in minutes)
    response_times = []
    for wo in closed.exclude(labor_started_at__isnull=True):
        first_assignment = (
            WorkOrderAssignmentHistory.objects.filter(
                work_order=wo, technician=technician
            )
            .order_by("assigned_at")
            .first()
        )
        if not first_assignment:
            continue
        delta = (wo.labor_started_at - first_assignment.assigned_at).total_seconds() / 60.0
        if delta >= 0:
            response_times.append(delta)
    avg_response_minutes = (
        round(sum(response_times) / len(response_times), 1) if response_times else None
    )

    recent = list(
        closed.select_related("machine")
        .order_by("-updated_at")[:10]
    )
    return {
        "completed_count": completed_count,
        "in_progress_count": in_progress_count,
        "reopened_count": reopened_count,
        "external_repair_count": external_repair_count,
        "avg_repair_minutes": avg_repair_minutes,
        "avg_response_minutes": avg_response_minutes,
        "recent": recent,
    }
