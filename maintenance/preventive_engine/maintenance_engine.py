"""MaintenanceEngine — facade for the Preventive Maintenance workflow.

Single import surface for views. Views should NOT import individual services
directly except where they need a specific low-level method.

Usage:
    from maintenance.services import maintenance_engine as engine
    engine.start_occurrence(pm_execution, technician)
    engine.complete_occurrence(pm_execution, technician, ...)
    engine.approve(pm_execution, manager)
    engine.return_to_technician(pm_execution, manager, reason=...)
    engine.assign(pm_execution, technician, by=manager)
    engine.reassign(pm_execution, new_tech, by=manager, reason=...)
    engine.mark_done(pm_execution, manager)
    engine.generate_today()
    engine.compute_next_due(schedule, after_dt)
"""

from . import occurrence_service
from . import assignment_service
from . import scheduling_service
from . import notification_service


# ──────────── Occurrence ────────────

def get_or_create_today(schedule, due_at=None):
    return occurrence_service.get_or_create_for_today(schedule, due_at)


def start_occurrence(pm_execution, technician, work_order_creator=None):
    return occurrence_service.start(pm_execution, technician, work_order_creator=work_order_creator)


def complete_occurrence(
    pm_execution,
    technician,
    *,
    checklist_results,
    notes="",
    photo_count=0,
    required_photo_count=0,
    root_cause="",
):
    return occurrence_service.complete(
        pm_execution,
        technician,
        checklist_results=checklist_results,
        notes=notes,
        photo_count=photo_count,
        required_photo_count=required_photo_count,
        root_cause=root_cause,
    )


def approve(pm_execution, manager):
    return occurrence_service.approve(pm_execution, manager)


def return_to_technician(pm_execution, manager, *, reason):
    return occurrence_service.return_to_technician(pm_execution, manager, reason=reason)


def mark_done(pm_execution, manager):
    return occurrence_service.mark_done(pm_execution, manager)


# ──────────── Assignment ────────────

def assign(pm_execution, technician, *, by=None):
    return assignment_service.assign(pm_execution, technician, by=by)


def reassign(pm_execution, new_technician, *, by=None, reason=None):
    return assignment_service.reassign(pm_execution, new_technician, by=by, reason=reason)


def validate_one_owner(pm_execution):
    return assignment_service.validate_one_owner(pm_execution)


# ──────────── Scheduling ────────────

def generate_today(today=None, *, force=False):
    return scheduling_service.generate_today(today, force=force)


def compute_next_due(schedule, after):
    return scheduling_service.next_due_at(schedule, after)


def mark_overdue(pm_execution):
    return scheduling_service.mark_overdue(pm_execution)


def is_overdue(pm_execution):
    return scheduling_service.is_overdue(pm_execution)


# ──────────── Notifications ────────────

def notify_new_assignment(pm_execution):
    if pm_execution.assigned_technician:
        notification_service.new_assignment(pm_execution.assigned_technician, pm_execution)


def notify_returned(pm_execution, reason):
    if pm_execution.assigned_technician:
        notification_service.returned(pm_execution.assigned_technician, pm_execution, reason)


def notify_waiting_review(pm_execution):
    notification_service.waiting_review_submitted(pm_execution)


def notify_plan_paused(schedule):
    notification_service.plan_paused(schedule)


# ──────────── Daily batch ────────────

def run_daily_morning_summaries():
    return notification_service.send_all_morning_summaries()


def run_overdue_alerts():
    return notification_service.send_all_overdue_alerts()


def run_unassigned_alerts():
    return notification_service.send_unassigned_alerts()