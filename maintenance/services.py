from __future__ import annotations

from datetime import timedelta
from typing import Optional

from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext as _

from .models import AuditEntry, MaintenanceIssue, PMSchedule, WorkOrder, WorkOrderStateLog, Downtime, WorkOrderAssignmentHistory

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
    from_status = wo.lifecycle_status
    if from_status == to_status:
        return
    wo.lifecycle_status = to_status
    wo.save(update_fields=["lifecycle_status", "updated_at"])
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
    # Phase 2B: recompute operational_status after every lifecycle transition.
    # The operational status is derived from open blockers + labor state, so it
    # must refresh whenever the underlying state changes.
    from maintenance.services_wo_status import WorkOrderService
    try:
        WorkOrderService.recompute_operational_status(wo)
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(
            f"Failed to recompute operational_status for WO #{wo.number}: {e}"
        )

    # Phase 12 (Reconciliation sweep): when the WO closes, release any
    # stale ACTIVE inventory reservations on it. Catches:
    #   1. Legacy reservations (no source_line) — created by reserve_stock()
    #      with no PartIssueLine link.
    #   2. Line-linked reservations whose line is already fully issued.
    # These would otherwise hold stock in limbo after the WO is reviewed.
    if to_status == WorkOrder.LifecycleStatus.CLOSED:
        try:
            release_stale_reservations_for_wo(wo, actor)
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning(
                f"Failed to release stale reservations for WO #{wo.number}: {e}"
            )


def release_stale_reservations_for_wo(wo, actor) -> int:
    """Release ACTIVE InventoryReservation rows that are no longer needed.

    Criteria (each row checked independently):
      - Legacy reservation (source_line is NULL) → always release.
      - Line-linked reservation whose source line is already fully issued
        (issued_qty >= approved_qty) → release.

    Returns the count of reservations released. Wired into
    `transition_work_order` so closing a WO cleans up any leftover claims.
    Each release is recorded in the audit log for traceability.
    """
    from inventory.models import InventoryReservation
    from django.utils import timezone as _tz

    active_qs = (
        InventoryReservation.objects
        .filter(work_order=wo, status=InventoryReservation.Status.ACTIVE)
        .select_related("source_line", "part")
        .order_by("created_at", "pk")
    )
    now = _tz.now()
    released = 0
    for res in active_qs:
        if (
            res.source_line_id
            and res.source_line.issued_qty is not None
            and res.source_line.approved_qty is not None
            and res.source_line.issued_qty < res.source_line.approved_qty
        ):
            # Line still has unmet demand — leave the reservation in place.
            continue
        if res.source_line_id:
            res.release_reason = _(
                "Auto-released: source line fully issued (issued_qty >= approved_qty)"
            )
        else:
            res.release_reason = _(
                "Auto-released on WO closure (legacy reserve)"
            )
        res.status = InventoryReservation.Status.RELEASED
        res.released_at = now
        res.save(update_fields=["status", "released_at", "release_reason"])
        log_audit(
            actor=actor,
            action="reservation_auto_released",
            entity="WorkOrder",
            object_id=wo.pk,
            payload={
                "reservation_id": res.pk,
                "part_id": res.part_id,
                "qty": float(res.quantity),
                "reason": res.release_reason,
            },
        )
        released += 1
    return released


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
            _(f"{reason!r} is a work-order status, not a pause reason. "
              f"Transition the WO to WAITING_FOR_PARTS or WAITING_FOR_VENDOR instead.")
        )
    qs = WorkOrder.objects.filter(
        assigned_technician=technician,
        lifecycle_status=WorkOrder.LifecycleStatus.IN_PROGRESS,
    ).select_for_update()
    if except_pk:
        qs = qs.exclude(pk=except_pk)
    for other in qs:
        other.labor_stopped_at = timezone.now()
        other.save(update_fields=[
            "labor_stopped_at", "updated_at"
        ])
        from maintenance.services_wo_status import WorkOrderService
        try:
            WorkOrderService.recompute_operational_status(other)
        except Exception:
            pass
        prev_assignment = WorkOrderAssignmentHistory.objects.filter(
            work_order=other, technician=technician, unassigned_at__isnull=True
        ).first()
        if prev_assignment:
            prev_assignment.unassigned_at = timezone.now()
            prev_assignment.reason = (
                _(f"Auto-paused: {dict(WorkOrder.PauseReason.choices)[reason]}")
            )
            prev_assignment.save()
        # Phase 2B: open OPERATIONAL blocker on each auto-paused WO, with the
        # emergency WO as the source (enables the "Why is this paused?" answer
        # to walk the chain back to the root emergency). The source WO is
        # the one being started (except_pk) — i.e. the emergency WO.
        from maintenance.services_blocker import WorkOrderBlockerService
        try:
            source_wo = (
                WorkOrder.objects.filter(pk=except_pk).first() if except_pk else None
            )
            WorkOrderBlockerService.open_operational_blocker(
                work_order=other,
                opened_by=None,  # system-initiated
                note=_(f"Auto-paused (reason={reason})"),
                pause_reason=WorkOrder.PauseReason.EMERGENCY,
                source_work_order=source_wo,
            )
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning(
                f"Failed to open auto-pause blocker: {e}"
            )


@transaction.atomic
def work_order_pause(
    wo: WorkOrder,
    pause_reason: str,
    pause_note: str = "",
    actor: Optional[User] = None,
) -> None:
    """Pause an in-progress WO and (per ADR-0007 sub-decision 6) open an
    OPERATIONAL WO Blocker only when the pause is "significant":
        - reason is 'other', OR
        - pause note is non-empty, OR
        - reason is 'emergency' (auto-triggered by pause_other_in_progress).

    For micro-pauses (e.g. grabbing a coffee) the transition + state log
    happen but no blocker row is created — the dual-read fallback in
    WorkOrderService.recompute_operational_status still derives 'paused'
    from the populated pause_reason field during the migration window.
    """
    wo = WorkOrder.objects.select_for_update().get(pk=wo.pk)
    now = timezone.now()
    wo.labor_stopped_at = now
    wo.save(update_fields=[
        "labor_stopped_at", "updated_at"
    ])
    state_log_note = (
        (pause_note or "")[:500] if (pause_note or "").strip()
        else _(f"Paused: {dict(WorkOrder.PauseReason.choices)[pause_reason]}")
    )
    # Phase 2B: open OPERATIONAL blocker if the pause is "significant".
    # Content-based rule (ADR-0007 sub-decision 6).
    from maintenance.services_blocker import WorkOrderBlockerService
    should_open = (
        pause_reason == WorkOrder.PauseReason.OTHER
        or (pause_note or "").strip()
        or pause_reason == WorkOrder.PauseReason.EMERGENCY
    )
    if should_open:
        try:
            WorkOrderBlockerService.open_operational_blocker(
                work_order=wo,
                opened_by=actor,
                note=pause_note or "",
                pause_reason=pause_reason,
                # no source_work_order — tech-initiated pause has no source
            )
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning(
                f"Failed to open OPERATIONAL blocker: {e}"
            )
    # Always recompute WO status (recompute_operational_status is a no-op
    # for terminal lifecycle states).
    from maintenance.services_wo_status import WorkOrderService
    try:
        WorkOrderService.recompute_operational_status(wo)
    except Exception:
        pass


def get_other_active_work_order(technician: User, except_pk: int | None = None) -> WorkOrder | None:
    qs = WorkOrder.objects.filter(
        assigned_technician=technician,
        lifecycle_status=WorkOrder.LifecycleStatus.IN_PROGRESS,
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
        lifecycle_status=WorkOrder.LifecycleStatus.IN_PROGRESS,
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
            reason=_(f"WO started — {'Emergency' if wo.is_emergency else 'Breakdown'}"),
        )
    if wo.lifecycle_status == WorkOrder.LifecycleStatus.IN_PROGRESS:
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
    if wo.lifecycle_status == WorkOrder.LifecycleStatus.IN_PROGRESS:
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
    transition_work_order(wo, WorkOrder.LifecycleStatus.IN_PROGRESS, actor=technician, note=_("Start work"))
    from .notifications import notify_wo_started
    notify_wo_started(wo)
    # Phase 2B: resolve all open OPERATIONAL blockers on this WO. This
    # naturally covers the resume path (technician moving the WO from
    # PAUSED back to IN_PROGRESS) without distinguishing it from the
    # initial start: the query filter on OPEN OPERATIONAL blockers is
    # empty for fresh starts, so the loop is a no-op there.
    from maintenance.services_blocker import WorkOrderBlockerService
    from maintenance.models import WorkOrderBlocker
    try:
        open_operational = WorkOrderBlocker.objects.filter(
            work_order=wo,
            kind=WorkOrderBlocker.Kind.OPERATIONAL,
            status=WorkOrderBlocker.Status.OPEN,
        )
        if open_operational.exists():
            for blocker in open_operational:
                WorkOrderBlockerService.resolve_blocker(
                    blocker=blocker,
                    resolution_note=(
                        _(f"Resumed at {wo.labor_started_at.isoformat() if wo.labor_started_at else _('now')}")
                    ),
                    resolved_by=technician,
                )
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(
            f"Failed to resolve OPERATIONAL blockers on resume: {e}"
        )


@transaction.atomic
def technician_submit_for_review(wo: WorkOrder, technician: User) -> None:
    now = timezone.now()
    wo.labor_stopped_at = now
    wo.save(update_fields=["labor_stopped_at", "updated_at"])
    transition_work_order(wo, WorkOrder.LifecycleStatus.PENDING_REVIEW, actor=technician, note=_("Submitted for review"))
    from .notifications import notify_wo_pending_review

    notify_wo_pending_review(wo)


@transaction.atomic
def technician_mark_pending_parts(wo: WorkOrder, technician: User, note: str = "") -> None:
    if wo.lifecycle_status != WorkOrder.LifecycleStatus.IN_PROGRESS:
        raise ValueError(_("Work order must be in progress."))
    wo.labor_stopped_at = timezone.now()
    wo.save(update_fields=["labor_stopped_at", "updated_at"])
    from maintenance.services_wo_status import WorkOrderService
    try:
        WorkOrderService.recompute_operational_status(wo)
    except Exception:
        pass
    from .notifications import notify_wo_paused
    notify_wo_paused(wo)


@transaction.atomic
def technician_mark_waiting_vendor(wo: WorkOrder, technician: User, note: str = "") -> None:
    if wo.lifecycle_status != WorkOrder.LifecycleStatus.IN_PROGRESS:
        raise ValueError(_("Work order must be in progress."))
    wo.labor_stopped_at = timezone.now()
    wo.save(update_fields=["labor_stopped_at", "updated_at"])
    from maintenance.services_wo_status import WorkOrderService
    try:
        WorkOrderService.recompute_operational_status(wo)
    except Exception:
        pass
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
        transition_work_order(wo, WorkOrder.LifecycleStatus.CLOSED, actor=manager, note=_("Approved & closed"))
        from .notifications import notify_wo_closed
        notify_wo_closed(wo)
    else:
        if not rejection_reason or not rejection_reason.strip():
            raise ValueError(_("Rejection reason is required."))
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
            wo, WorkOrder.LifecycleStatus.IN_PROGRESS,
            actor=manager,
            note=_(f"Rejected: {rejection_reason.strip()[:200]}")
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
        raise ValueError(_("Work order is already archived."))
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
        raise ValueError(_("Work order is not archived."))
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
        raise ValueError(_("Issue is already archived."))
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
        raise ValueError(_("Only the assigned technician can request external repair."))
    if not diagnosis_note.strip():
        raise ValueError(_("Diagnosis note is required."))
    if not part_description.strip():
        raise ValueError(_("Part description is required."))

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
    # Phase 2B-6: open VENDOR_REPAIR blocker keyed to the ExternalRepairRequest.
    try:
        from maintenance.services_blocker import WorkOrderBlockerService
        from maintenance.models import WorkOrderBlocker
        _label = _(f"Vendor repair — {part_description.strip()[:80]}") if part_description else _("Vendor repair")
        WorkOrderBlockerService.open_blocker(
            work_order=work_order,
            kind=WorkOrderBlocker.Kind.VENDOR_REPAIR,
            external_obj=err,
            opened_by=requested_by,
            note=diagnosis_note or "",
            external_label=_label,
        )
    except Exception as _e:
        import logging
        logging.getLogger(__name__).warning(f"Failed to open VENDOR_REPAIR blocker: {_e}")

    from maintenance.services_wo_status import WorkOrderService
    try:
        WorkOrderService.recompute_operational_status(work_order)
    except Exception:
        pass
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
        raise ValueError(_("Only PENDING requests can be approved."))

    now = timezone.now()
    note = (manager_note or "").strip()

    ero = ExternalRepairOrder.objects.create(
        work_order=err.work_order,
        title=_(f"External repair for {err.part_description[:60]}"),
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

    # Phase 2B-6: attach the newly-created ERO to the VENDOR_REPAIR blocker and
    # fire the ERO_CREATED event. Idempotent: if no OPEN blocker exists (e.g.
    # the request was opened under the legacy model), we skip silently.
    try:
        from maintenance.models import WorkOrderBlocker, WorkOrderBlockerEvent
        from maintenance.services_blocker import WorkOrderBlockerEventService
        _blocker = WorkOrderBlocker.objects.filter(
            work_order=err.work_order,
            kind=WorkOrderBlocker.Kind.VENDOR_REPAIR,
            status=WorkOrderBlocker.Status.OPEN,
        ).first()
        if _blocker:
            _blocker.related_ero = ero
            _blocker.save(update_fields=["related_ero"])
            WorkOrderBlockerEventService.record(
                blocker=_blocker,
                event_type=WorkOrderBlockerEvent.EventType.ERO_CREATED,
                actor=manager,
                payload={"ero_id": ero.pk, "ero_title": ero.title},
            )
    except Exception as _e:
        import logging
        logging.getLogger(__name__).warning(f"Failed to set related_ero on VENDOR_REPAIR blocker: {_e}")

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
        raise ValueError(_("Only PENDING requests can be rejected."))
    note = (manager_note or "").strip()
    if not note:
        raise ValueError(_("A rejection reason is required."))

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
        assigned_technician=technician, lifecycle_status=WorkOrder.LifecycleStatus.CLOSED
    )
    completed_count = closed.count()
    in_progress_count = WorkOrder.objects.filter(
        assigned_technician=technician, lifecycle_status=WorkOrder.LifecycleStatus.IN_PROGRESS
    ).count()
    reopened_count = sum(
        wo.rejection_count or 0
        for wo in WorkOrder.objects.filter(assigned_technician=technician)
    )
    external_repair_count = (
        closed.filter(external_repairs__isnull=False).distinct().count()
    )

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


def capture_template_snapshot(template: "PMTemplate") -> dict:
    """Capture template state for historical record on PMExecution."""
    from .models import PMChecklistItem, PMTemplate as _PMT
    if not isinstance(template, _PMT):
        template = _PMT.objects.get(pk=template.pk)
    items = list(
        PMChecklistItem.objects.filter(template=template)
        .order_by("order", "pk")
        .values("order", "text", "is_required")
    )
    return {
        "template_code": template.code,
        "template_title": template.title,
        "template_priority": template.priority,
        "template_duration_minutes": template.estimated_duration_minutes,
        "checklist": items,
        "captured_at": timezone.now().isoformat(),
    }


def next_pm_execution_sequence(schedule: "PMSchedule") -> int:
    """Return next execution_sequence for this schedule (max + 1)."""
    from .models import PMExecution
    last = (
        PMExecution.objects.filter(pm_schedule=schedule)
        .order_by("-execution_sequence")
        .values_list("execution_sequence", flat=True)
        .first()
    )
    return (last or 0) + 1


def create_pm_execution_for_wo(schedule: "PMSchedule", work_order, actor=None) -> "PMExecution":
    """Create a PMExecution in SUBMITTED status tied to a WO and template snapshot."""
    from .models import PMExecution
    snapshot = capture_template_snapshot(schedule.template)
    execution = PMExecution.objects.create(
        pm_schedule=schedule,
        work_order=work_order,
        scheduled_due_at=schedule.next_due_at,
        execution_sequence=next_pm_execution_sequence(schedule),
        status=PMExecution.Status.SUBMITTED,
        template_snapshot_json=snapshot,
        completed_by=actor,
        completed_at=timezone.now(),
    )
    return execution


def compute_next_due_at(schedule: "PMSchedule", after):
    """Compute next due datetime after `after` using frequency_type + interval."""
    from datetime import datetime, timedelta
    if isinstance(after, datetime):
        base = after
    else:
        from django.utils import timezone as _tz
        base = _tz.now()
    if schedule.frequency_type == PMSchedule.FrequencyType.DAILY:
        return base + timedelta(days=schedule.interval)
    if schedule.frequency_type == PMSchedule.FrequencyType.WEEKLY:
        return base + timedelta(weeks=schedule.interval)
    if schedule.frequency_type == PMSchedule.FrequencyType.MONTHLY:
        months = schedule.interval
        year = base.year + (base.month - 1 + months) // 12
        month = (base.month - 1 + months) % 12 + 1
        from calendar import monthrange
        day = min(base.day, monthrange(year, month)[1])
        return base.replace(year=year, month=month, day=day)
    if schedule.frequency_type == PMSchedule.FrequencyType.YEARLY:
        from calendar import monthrange
        year = base.year + schedule.interval
        day = min(base.day, monthrange(year, base.month)[1])
        return base.replace(year=year, day=day)
    return base + timedelta(days=30 * schedule.interval)


@transaction.atomic
def manager_approve_pm_execution(execution, *, manager: User) -> None:
    if not execution.work_order_id:
        raise ValueError(_("PM execution has no associated work order."))
    if execution.work_order.lifecycle_status != WorkOrder.LifecycleStatus.PENDING_REVIEW:
        raise ValueError(_("Work order is not pending review."))
    if execution.status not in (execution.Status.SUBMITTED, execution.Status.REJECTED):
        raise ValueError(_(f"Cannot approve execution in status {execution.status}."))

    now = timezone.now()
    schedule = execution.pm_schedule

    execution.status = execution.Status.APPROVED
    execution.approved_by = manager
    execution.approved_at = now
    execution.save(update_fields=["status", "approved_by", "approved_at"])

    manager_close_work_order(execution.work_order, manager, approve=True)

    schedule.last_completed_at = now
    schedule.next_due_at = compute_next_due_at(schedule, schedule.next_due_at)
    schedule.save(update_fields=["last_completed_at", "next_due_at"])


@transaction.atomic
def manager_reject_pm_execution(execution, *, manager: User, reason: str) -> None:
    if not execution.work_order_id:
        raise ValueError(_("PM execution has no associated work order."))
    if execution.work_order.lifecycle_status != WorkOrder.LifecycleStatus.PENDING_REVIEW:
        raise ValueError(_("Work order is not pending review."))
    if execution.status not in (execution.Status.SUBMITTED,):
        raise ValueError(_(f"Cannot reject execution in status {execution.status}."))
    if not reason or not reason.strip():
        raise ValueError(_("Rejection reason is required."))

    now = timezone.now()
    execution.status = execution.Status.REJECTED
    execution.approved_by = manager
    execution.approved_at = now
    execution.notes = (execution.notes or "").strip()
    if execution.notes:
        execution.notes += "\n\n"
    execution.notes += _(f"[Rejected {now.strftime('%Y-%m-%d %H:%M')}] {reason.strip()[:500]}")
    execution.save(update_fields=["status", "approved_by", "approved_at", "notes"])

    manager_close_work_order(execution.work_order, manager, approve=False, rejection_reason=reason)


def compute_compliance(*, window_days: int = 90, grace_days: int = 7, machine=None) -> dict:
    from django.db.models import F

    from .models import PMExecution

    now = timezone.now()
    window_start = now - timedelta(days=window_days)

    qs = PMExecution.objects.filter(scheduled_due_at__gte=window_start)
    if machine is not None:
        qs = qs.filter(pm_schedule__machine=machine)

    scheduled = qs.count()
    on_time = qs.filter(
        status=PMExecution.Status.APPROVED,
        approved_at__isnull=False,
        approved_at__lte=F("scheduled_due_at") + timedelta(days=grace_days),
    ).count()
    approved_total = qs.filter(status=PMExecution.Status.APPROVED).count()
    missed = qs.filter(status=PMExecution.Status.MISSED).count()
    pending = qs.filter(
        status__in=[PMExecution.Status.SUBMITTED, PMExecution.Status.REJECTED]
    ).count()

    pct = None
    if scheduled > 0:
        pct = int(round((on_time / scheduled) * 100))

    return {
        "scheduled": scheduled,
        "on_time": on_time,
        "approved_total": approved_total,
        "missed": missed,
        "pending": pending,
        "pct": pct,
        "window_days": window_days,
        "grace_days": grace_days,
    }


class ToolAvailabilityService:
    """Centralized availability / reuse check for `Tool`.

    Used by:
      - `tool_assign` view (block bad assignments with explicit reason)
      - `tools.html` template (red banner showing why a tool is unavailable)
      - QR scan handler (preview before showing the assign form)

    Returns a tuple `(ok: bool, reason: str)`.
    """

    @staticmethod
    def can_assign(tool) -> "tuple[bool, str]":
        """Return (ok, reason) for whether `tool` can be assigned right now.

        ok=True:  tool.status == AVAILABLE. reason = "".
        ok=False: tool.status in {IN_USE, OUT_OF_SERVICE}; reason is a translated
                  human-readable string for UI display.
        """
        # Localised imports keep this module decoupled at import time.
        from django.utils.translation import gettext_lazy as _

        if tool.status == tool.Status.AVAILABLE:
            return True, ""

        if tool.status == tool.Status.IN_USE:
            # Try to surface the current holder's name + since date.
            from maintenance.models import ToolAssignment
            ta = (
                ToolAssignment.objects.filter(tool=tool, returned_at__isnull=True)
                .select_related("user")
                .order_by("-assigned_at")
                .first()
            )
            holder = ta.user.get_full_name() or ta.user.username if ta else "?"
            since = ta.assigned_at.strftime("%Y-%m-%d") if ta else "?"
            return False, _(
                "Currently assigned to %(holder)s since %(date)s."
            ) % {"holder": holder, "date": since}

        if tool.status == tool.Status.OUT_OF_SERVICE:
            from maintenance.models import ToolDamageRecord
            rec = (
                ToolDamageRecord.objects.filter(tool=tool)
                .order_by("-created_at")
                .first()
            )
            if rec is not None:
                supplier_label = rec.supplier.name if rec.supplier_id else _("unknown supplier")
                return False, _(
                    "Out of service — damaged %(date)s (supplier: %(supplier)s). "
                    "Replacement status: %(action)s."
                ) % {
                    "date": rec.created_at.strftime("%Y-%m-%d"),
                    "supplier": supplier_label,
                    "action": rec.get_replacement_action_display(),
                }
            return False, _("Out of service.")

        return False, _("Unknown status — cannot assign.")
