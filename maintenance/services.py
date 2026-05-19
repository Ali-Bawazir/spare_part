from __future__ import annotations

from typing import Optional

from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone

from .models import AuditEntry, MaintenanceIssue, WorkOrder, WorkOrderStateLog

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
    )
    if except_pk:
        qs = qs.exclude(pk=except_pk)
    for other in qs:
        other.labor_stopped_at = timezone.now()
        other.save(update_fields=["labor_stopped_at", "updated_at"])
        transition_work_order(other, WorkOrder.Status.PAUSED, actor=technician, note="Auto-pause: another task started")


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
def manager_close_work_order(wo: WorkOrder, manager: User, approve: bool) -> None:
    now = timezone.now()
    if approve:
        wo.downtime_ended_at = now
        wo.save(update_fields=["downtime_ended_at", "updated_at"])
        transition_work_order(wo, WorkOrder.Status.CLOSED, actor=manager, note="Approved & closed")
    else:
        wo.labor_started_at = timezone.now()
        wo.labor_stopped_at = None
        wo.save(update_fields=["labor_started_at", "labor_stopped_at", "updated_at"])
        transition_work_order(wo, WorkOrder.Status.IN_PROGRESS, actor=manager, note="Rejected — resume work")


def validate_issue(issue: MaintenanceIssue, *, actor: User, priority: str) -> None:
    issue.status = MaintenanceIssue.Status.VALIDATED
    issue.priority = priority
    issue.validated_by = actor
    issue.validated_at = timezone.now()
    issue.save()
    log_audit(actor=actor, action="issue_validated", entity="MaintenanceIssue", object_id=issue.pk, payload={"priority": priority})
    from .notifications import notify_issue_validated

    notify_issue_validated(issue)
