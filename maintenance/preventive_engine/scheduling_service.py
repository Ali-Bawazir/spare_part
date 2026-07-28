"""SchedulingService — PM cycle arithmetic and daily occurrence generation.

The single source of truth for when the next PM is due. Idempotent daily
generation so the cron can re-run safely.
"""

from __future__ import annotations

import calendar
import logging
from datetime import date, datetime, time, timedelta

from django.db import transaction
from django.utils import timezone

from ..models import MaintenanceSettings, PMExecution, PMSchedule

logger = logging.getLogger(__name__)


def next_due_at(schedule: PMSchedule, after: datetime) -> datetime:
    """Compute the next due datetime based on (after) — no drift.

    The schedule advances from the previous due date, not from `now()`.
    This prevents accumulation drift if approvals are late.
    """
    ft = schedule.frequency_type
    interval = max(1, schedule.interval)
    base = schedule.next_due_at
    if base is None:
        base = after

    if ft == PMSchedule.FrequencyType.DAILY:
        nxt = base + timedelta(days=interval)
    elif ft == PMSchedule.FrequencyType.WEEKLY:
        nxt = base + timedelta(weeks=interval)
    elif ft == PMSchedule.FrequencyType.MONTHLY:
        nxt = _add_months(base, interval)
    elif ft == PMSchedule.FrequencyType.YEARLY:
        nxt = _add_months(base, 12 * interval)
    else:
        nxt = base + timedelta(days=30 * interval)

    # Skip occurrences past the ends_at date
    if schedule.ends_at and nxt.date() > schedule.ends_at:
        return nxt  # Caller should detect ends_at exceeded and deactivate

    return nxt


def _add_months(dt: datetime, months: int) -> datetime:
    month = dt.month - 1 + months
    year = dt.year + month // 12
    month = month % 12 + 1
    day = min(dt.day, calendar.monthrange(year, month)[1])
    return dt.replace(year=year, month=month, day=day)


@transaction.atomic
def generate_today(today: date = None, *, force: bool = False) -> dict:
    """Generate today's PM occurrences.

    Idempotent: skips if already run today (unless force=True).
    Returns a summary dict with counts.
    """
    settings = MaintenanceSettings.get_solo()
    today = today or timezone.now().date()

    if not force and settings.last_generate_run:
        if settings.last_generate_run.date() == today:
            return {"skipped": True, "reason": "already_generated_today", "count": 0}

    # Find schedules whose next_due_at falls on today (date comparison)
    from datetime import datetime, time as dtime
    day_start = timezone.make_aware(datetime.combine(today, dtime.min)) if timezone.is_naive(datetime.combine(today, dtime.min)) else datetime.combine(today, dtime.min)
    day_start = day_start.replace(hour=0, minute=0, second=0, microsecond=0) if day_start.tzinfo else day_start
    day_end = day_start + timedelta(days=1)

    # Use schedule.due_time combined with today
    eligible = PMSchedule.objects.filter(
        is_active=True,
        next_due_at__gte=day_start,
        next_due_at__lt=day_end,
    )
    # Also include schedules that may have been skipped past (back-dated)
    eligible = eligible | PMSchedule.objects.filter(
        is_active=True,
        next_due_at__lt=day_start,
    ).exclude(ends_at__lt=today)

    created = 0
    for sched in eligible.distinct():
        # Respect ends_at
        if sched.ends_at and sched.ends_at < today:
            continue
        # Build due_at as today + schedule.due_time (not sched.next_due_at which may be old)
        from datetime import datetime, time as dtime
        due_time_obj = sched.due_time if hasattr(sched.due_time, "hour") else dtime(8, 0)
        due_at_naive = datetime.combine(today, due_time_obj)
        due_at = (
            timezone.make_aware(due_at_naive)
            if timezone.is_naive(due_at_naive)
            else due_at_naive
        )
        # Idempotent: same (schedule, due_at) returns the same row
        _, was_created = PMExecution.objects.get_or_create(
            pm_schedule=sched,
            scheduled_due_at=due_at,
            defaults={"status": PMExecution.Status.SUBMITTED},
        )
        if was_created:
            created += 1

    settings.last_generate_run = timezone.now()
    settings.save(update_fields=["last_generate_run"])
    return {"skipped": False, "count": created, "date": str(today)}


def mark_overdue(pm_execution: PMExecution) -> None:
    """Flag an execution as MISSED if it hasn't started within grace period."""
    if pm_execution.status != PMExecution.Status.SUBMITTED:
        return
    grace = pm_execution.pm_schedule.grace_days or 7
    if pm_execution.scheduled_due_at + timedelta(days=grace) < timezone.now():
        pm_execution.status = PMExecution.Status.MISSED
        pm_execution.save(update_fields=["status"])


def is_overdue(pm_execution: PMExecution) -> bool:
    if pm_execution.status != PMExecution.Status.SUBMITTED:
        return False
    grace = pm_execution.pm_schedule.grace_days or 7
    return pm_execution.scheduled_due_at + timedelta(days=grace) < timezone.now()