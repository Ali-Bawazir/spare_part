"""NotificationService — 9 events for the Preventive Maintenance workflow.

4 technician events:
  1. Morning summary (07:00 daily)
  2. New assignment (instant)
  3. Returned (instant)
  4. Overdue (14:00 daily, if not yet done)

5 manager events:
  1. Morning summary (07:00 daily)
  2. Waiting Review submitted (instant on tech submit)
  3. Overdue (14:00 daily)
  4. Unassigned (09:00 daily)
  5. Plan paused (instant)

Old 7d/3d/1d PM triggers are removed. Manager dashboard + Today's Schedule
surface everything else.
"""

from __future__ import annotations

from datetime import datetime, time as dtime, timedelta
from typing import Iterable

from django.contrib.auth import get_user_model
from django.utils import timezone

from ..models import (
    MaintenanceSettings,
    PMExecution,
    PMSchedule,
    WorkOrder,
)
from ..models import Notification

User = get_user_model()


def _ensure_aware(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return timezone.make_aware(dt)
    return dt


def send(user, kind: str, title: str, body: str = "", url: str = "") -> Notification:
    """Internal helper — create a notification for one user."""
    return Notification.objects.create(
        recipient=user,
        kind=kind,
        title=title[:200],
        body=body[:1000],
        link=url[:300],
    )


def send_bulk(users: Iterable, kind: str, title: str, body: str = "", url: str = "") -> int:
    """Create one notification per user. Returns count created."""
    n = 0
    for u in users:
        if u is None:
            continue
        send(u, kind, title, body, url)
        n += 1
    return n


# ──────────── Technician events ────────────


def morning_summary_to_tech(technician) -> int:
    """Sent at 07:00 to each technician who has work today."""
    today = timezone.now().date()
    todays = PMExecution.objects.filter(
        scheduled_due_at__date=today,
        pm_schedule__is_active=True,
    ).filter(
        # Either assigned to this tech OR unassigned (catch-all)
        # We'll resolve per-tech in the loop
    )
    # Per-technician query
    my_today = WorkOrder.objects.filter(
        assigned_technician=technician,
        category=WorkOrder.Category.PREVENTIVE,
        lifecycle_status__in=[
            WorkOrder.LifecycleStatus.ASSIGNED,
            WorkOrder.LifecycleStatus.IN_PROGRESS,
            WorkOrder.LifecycleStatus.PENDING_REVIEW,
        ],
    ).filter(
        pm_execution__scheduled_due_at__date=today,
    ).count()

    if my_today == 0:
        return 0

    first_wo = (
        WorkOrder.objects.filter(
            assigned_technician=technician,
            category=WorkOrder.Category.PREVENTIVE,
            lifecycle_status__in=[
                WorkOrder.LifecycleStatus.ASSIGNED,
                WorkOrder.LifecycleStatus.IN_PROGRESS,
            ],
        )
        .filter(pm_execution__scheduled_due_at__date=today)
        .order_by("pm_execution__scheduled_due_at")
        .select_related("pm_execution__pm_schedule")
        .first()
    )

    first_text = ""
    if first_wo:
        first_time = first_wo.pm_execution.scheduled_due_at.strftime("%H:%M")
        first_text = f" First task {first_time}."

    send(
        technician,
        kind="pm_morning_summary",
        title=f"Good Morning {technician.username}",
        body=f"Today: {my_today} maintenance tasks.{first_text}",
        url="/preventive/my/",
    )
    return 1


def morning_summary_to_manager(manager) -> int:
    """Sent at 07:00 to each manager."""
    today = timezone.now().date()
    scheduled = PMExecution.objects.filter(
        scheduled_due_at__date=today,
        pm_schedule__is_active=True,
    ).count()
    overdue = PMExecution.objects.filter(
        scheduled_due_at__date__lt=today,
        status=PMExecution.Status.SUBMITTED,
        pm_schedule__is_active=True,
    ).count()
    unassigned = PMExecution.objects.filter(
        scheduled_due_at__date=today,
        assigned_technician__isnull=True,
        pm_schedule__is_active=True,
    ).count()

    send(
        manager,
        kind="pm_manager_morning",
        title="Today's Maintenance",
        body=f"{scheduled} scheduled, {overdue} overdue, {unassigned} unassigned.",
        url="/preventive/manage/today/",
    )
    return 1


def new_assignment(technician, pm_execution: PMExecution) -> None:
    sched = pm_execution.pm_schedule
    send(
        technician,
        kind="pm_new_assignment",
        title="You have been assigned",
        body=f"{sched.template.title}. Today {pm_execution.scheduled_due_at.strftime('%H:%M')}.",
        url=f"/preventive/my/{pm_execution.pk}/",
    )


def returned(technician, pm_execution: PMExecution, reason: str) -> None:
    sched = pm_execution.pm_schedule
    send(
        technician,
        kind="pm_returned",
        title=f"{sched.template.title} was returned",
        body=f"Reason: {reason}",
        url=f"/preventive/my/{pm_execution.pk}/return/",
    )


def overdue_tech(technician, pm_execution: PMExecution) -> None:
    sched = pm_execution.pm_schedule
    send(
        technician,
        kind="pm_overdue_tech",
        title=f"{sched.template.title} is overdue",
        body="Please complete.",
        url=f"/preventive/my/{pm_execution.pk}/",
    )


def overdue_manager(pm_execution: PMExecution) -> None:
    """Find managers and notify about overdue PMs at 14:00."""
    sched = pm_execution.pm_schedule
    managers = User.objects.filter(role__in=["manager", "super_admin"], is_active=True)
    send_bulk(
        managers,
        kind="pm_overdue_manager",
        title=f"{sched.template.title} is overdue",
        body=f"{sched.machine.name} — please follow up.",
        url="/preventive/manage/today/",
    )


def unassigned_alert(pm_execution: PMExecution) -> None:
    sched = pm_execution.pm_schedule
    managers = User.objects.filter(role__in=["manager", "super_admin"], is_active=True)
    send_bulk(
        managers,
        kind="pm_unassigned",
        title=f"{sched.template.title} has no technician",
        body=f"{sched.machine.name}",
        url=f"/preventive/manage/plans/{sched.pk}/",
    )


def waiting_review_submitted(pm_execution: PMExecution) -> None:
    sched = pm_execution.pm_schedule
    tech = pm_execution.assigned_technician
    tech_name = tech.username if tech else "Technician"
    managers = User.objects.filter(role__in=["manager", "super_admin"], is_active=True)
    send_bulk(
        managers,
        kind="pm_waiting_review",
        title=f"{tech_name} submitted {sched.template.title}",
        body="Waiting for your review.",
        url="/preventive/manage/reviews/",
    )


def plan_paused(schedule: PMSchedule) -> None:
    managers = User.objects.filter(role__in=["manager", "super_admin"], is_active=True)
    send_bulk(
        managers,
        kind="pm_plan_paused",
        title=f"{schedule.template.title} has been paused",
        body=f"{schedule.machine.name}",
        url=f"/preventive/manage/plans/{schedule.pk}/",
    )


# ──────────── Daily batch entry points (called from cron) ────────────


def send_all_morning_summaries() -> dict:
    """07:00 cron: send morning summaries to all techs + managers.
    Idempotent: once per day via MaintenanceSettings.morning_summary_sent_date.
    """
    settings = MaintenanceSettings.get_solo()
    today = timezone.now().date()
    if settings.morning_summary_sent_date == today:
        return {"skipped": True, "reason": "already_sent_today"}

    tech_count = 0
    techs_with_work_today = WorkOrder.objects.filter(
        category=WorkOrder.Category.PREVENTIVE,
        lifecycle_status__in=[
            WorkOrder.LifecycleStatus.ASSIGNED,
            WorkOrder.LifecycleStatus.IN_PROGRESS,
        ],
    ).filter(
        pm_execution__scheduled_due_at__date=today,
    ).values_list("assigned_technician_id", flat=True).distinct()

    for tech_id in techs_with_work_today:
        if tech_id is None:
            continue
        tech = User.objects.filter(pk=tech_id, is_active=True).first()
        if tech:
            tech_count += morning_summary_to_tech(tech)

    manager_count = 0
    for mgr in User.objects.filter(role__in=["manager", "super_admin"], is_active=True):
        manager_count += morning_summary_to_manager(mgr)

    settings.morning_summary_sent_date = today
    settings.save(update_fields=["morning_summary_sent_date"])
    return {"skipped": False, "tech_notifications": tech_count, "manager_notifications": manager_count}


def send_all_overdue_alerts() -> dict:
    """14:00 cron: notify techs about their overdue items + managers about any overdue."""
    today = timezone.now().date()
    overdue_executions = PMExecution.objects.filter(
        status=PMExecution.Status.SUBMITTED,
        scheduled_due_at__date__lt=today,
        pm_schedule__is_active=True,
    ).select_related("pm_schedule", "assigned_technician")

    tech_notifs = 0
    mgr_notifs = 0
    for pe in overdue_executions:
        if pe.assigned_technician:
            overdue_tech(pe.assigned_technician, pe)
            tech_notifs += 1
        overdue_manager(pe)
        mgr_notifs += 1

    return {"tech_notifications": tech_notifs, "manager_notifications": mgr_notifs}


def send_unassigned_alerts() -> dict:
    """09:00 cron: notify managers about today's unassigned PMs."""
    today = timezone.now().date()
    unassigned = PMExecution.objects.filter(
        scheduled_due_at__date=today,
        assigned_technician__isnull=True,
        pm_schedule__is_active=True,
    )
    n = 0
    for pe in unassigned:
        unassigned_alert(pe)
        n += 1
    return {"manager_notifications": n}