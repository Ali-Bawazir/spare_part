"""Workflow-first PM views: 1 technician page + 6 manager pages + 1 detail page.

This module is the public surface of the new PM architecture. Existing pm_*
views remain in views.py for legacy redirects; new code should call these.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Count, Q, Sum
from django.http import HttpResponseRedirect, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods, require_POST

from accounts.permissions import role_required
from accounts.models import User
from .models import (
    Attachment,
    MaintenanceSettings,
    PMChecklistItem,
    PMExecution,
    PMTemplate,
    PMSchedule,
    WorkOrder,
    WorkOrderStateLog,
)
from .preventive_engine import maintenance_engine as engine


# ════════════════════════════════════════════════════════════════════
#  TECHNICIAN  (1 page: My Maintenance)
# ════════════════════════════════════════════════════════════════════


@login_required
@role_required(User.Role.TECHNICIAN, User.Role.SUPER_ADMIN, User.Role.SUPERVISOR, User.Role.MANAGER)
def tech_my(request):
    """Single to-do-list page for technicians."""
    user = request.user
    today = timezone.now().date()
    now = timezone.now()

    # Today's occurrences — split into "Mine" (assigned to me, in progress)
    # vs "Pool" (unassigned, in progress). Pending review / approved items
    # are NOT in Today — they live in the Completed section below.
    today_qs = (
        PMExecution.objects
        .filter(
            scheduled_due_at__date=today,
            pm_schedule__is_active=True,
        )
        .filter(work_order__lifecycle_status__in=["assigned", "in_progress"])
        .select_related("pm_schedule", "pm_schedule__template", "pm_schedule__machine", "assigned_technician")
        .order_by("scheduled_due_at")
    )
    today_mine = today_qs.filter(assigned_technician=user)
    # Pool = unassigned today. Include occurrences WITHOUT a WO yet
    # (lifecycle_status is on WorkOrder, not PMExecution) OR with lifecycle
    # in assigned/in_progress.
    today_pool = (
        PMExecution.objects
        .filter(
            scheduled_due_at__date=today,
            pm_schedule__is_active=True,
            assigned_technician__isnull=True,
        )
        .filter(
            Q(work_order__isnull=True) |
            Q(work_order__lifecycle_status__in=["assigned", "in_progress"])
        )
        .select_related("pm_schedule", "pm_schedule__template", "pm_schedule__machine")
        .order_by("scheduled_due_at")
    )

    # Upcoming — show PMExecutions in the next 14 days. We auto-create
    # placeholder rows for plans whose next_due_at is in the future so the
    # technician can mentally prepare. (Daily cron will fill in their
    # work_order when the day arrives.)
    # _build_due_at is internal; we don't need it
    upcoming_window = []
    for sched in PMSchedule.objects.filter(
        is_active=True,
        next_due_at__date__gt=today,
        next_due_at__date__lte=today + timedelta(days=14),
    ).select_related("template", "machine", "component"):
        # Skip if assigned to a different tech (we'd need to know who)
        # For MVP, all upcoming plans show.
        upcoming_window.append({
            "schedule": sched,
            "due_at": sched.next_due_at,
        })
    upcoming_plans = upcoming_window

    # Completed today (includes Waiting for approval + Approved)
    completed_today = (
        PMExecution.objects
        .filter(
            scheduled_due_at__date=today,
            assigned_technician=user,
        )
        .filter(
            Q(status=PMExecution.Status.APPROVED) | Q(status=PMExecution.Status.SUBMITTED, work_order__lifecycle_status="pending_review")
        )
        .select_related("pm_schedule", "pm_schedule__template", "pm_schedule__machine", "work_order")
        .order_by("-work_order__labor_stopped_at")
    )

    # Rejection banner: any of today's items returned?
    returned = (
        PMExecution.objects
        .filter(
            scheduled_due_at__date=today,
            assigned_technician=user,
            status=PMExecution.Status.REJECTED,
        )
        .select_related("pm_schedule__template")
        .first()
    )

    # Upcoming grouped by day name (Apple Calendar style) — using PLANS not occurrences
    upcoming_by_day = _group_upcoming_plans_by_day(upcoming_plans)

    context = {
        "today_mine": today_mine,
        "today_pool": today_pool,
        "upcoming_by_day": upcoming_by_day,
        "completed_today": completed_today,
        "returned": returned,
        "today_count": today_mine.count() + today_pool.count(),
        "today_mine_count": today_mine.count(),
        "today_pool_count": today_pool.count(),
        "upcoming_count": len(upcoming_plans),
        "completed_count": completed_today.count(),
        "today": today,
    }
    return render(request, "maintenance/preventive/my.html", context)


def _group_upcoming_plans_by_day(plan_dicts):
    """Group upcoming-window dicts into a list of (day_label, [items]) tuples.

    Each input is a dict {"schedule": PMSchedule, "due_at": datetime}.
    Day labels: 'Tomorrow', 'Tue 1 Jul', 'Wed 2 Jul', etc.
    """
    today = timezone.now().date()
    tomorrow = today + timedelta(days=1)
    from collections import OrderedDict
    by_day = OrderedDict()

    for item in plan_dicts:
        d = item["due_at"].date()
        if d == tomorrow:
            key = ("Tomorrow", d)
        else:
            key = (d.strftime("%a %-d %b"), d)
        by_day.setdefault(key, []).append(item)

    result = []
    for (label, date), items in by_day.items():
        result.append({
            "label": label,
            "date": date,
            "is_tomorrow": label == "Tomorrow",
            "items": items,
        })
    return result


@login_required
@role_required(User.Role.TECHNICIAN, User.Role.SUPER_ADMIN, User.Role.SUPERVISOR, User.Role.MANAGER)
def tech_execute(request, occurrence_id):
    """Begin Maintenance screen — checklist + timer + photo + complete."""
    pm_execution = get_object_or_404(
        PMExecution.objects.select_related(
            "pm_schedule", "pm_schedule__template", "pm_schedule__machine",
            "assigned_technician", "work_order",
        ),
        pk=occurrence_id,
    )
    if request.method == "POST":
        # Auto-start if no WO yet
        if not pm_execution.work_order:
            if pm_execution.assigned_technician is None:
                engine.assign(pm_execution, request.user, by=request.user)
            engine.start_occurrence(pm_execution, request.user, work_order_creator=request.user)
            pm_execution.refresh_from_db()
        checklist = _resolve_checklist(pm_execution)
        return _handle_complete(request, pm_execution, pm_execution.work_order, checklist)

    # GET: ensure work order exists
    if not pm_execution.work_order:
        if pm_execution.assigned_technician is None:
            engine.assign(pm_execution, request.user, by=request.user)
        engine.start_occurrence(pm_execution, request.user, work_order_creator=request.user)
        pm_execution.refresh_from_db()
    wo = pm_execution.work_order

    checklist = _resolve_checklist(pm_execution)

    # Photo count for gating
    photo_count = Attachment.objects.filter(
        entity_type="work_order",
        entity_id=wo.pk,
    ).count()
    required_photo_count = pm_execution.pm_schedule.template.requires_photo_min_count

    context = {
        "pm_execution": pm_execution,
        "wo": wo,
        "sched": pm_execution.pm_schedule,
        "template": pm_execution.pm_schedule.template,
        "checklist": checklist,
        "photo_count": photo_count,
        "required_photo_count": required_photo_count,
        "is_returned": pm_execution.status == PMExecution.Status.REJECTED,
    }
    return render(request, "maintenance/preventive/execute.html", context)


@login_required
@require_POST
@role_required(User.Role.TECHNICIAN, User.Role.SUPER_ADMIN, User.Role.SUPERVISOR, User.Role.MANAGER)
def tech_complete(request, occurrence_id):
    """Submit a completed PM for review (POST endpoint)."""
    return tech_execute(request, occurrence_id)


def _resolve_checklist(pm_execution):
    """Build checklist from snapshot or live template. Returns [{text, is_required}]."""
    snapshot = (pm_execution.template_snapshot_json or {}).get("checklist", [])
    if snapshot:
        return [{"text": item.get("text", ""), "is_required": item.get("is_required", True)} for item in snapshot]
    template = pm_execution.pm_schedule.template
    return [
        {"text": item.text, "is_required": item.is_required}
        for item in template.checklist_items.all().order_by("order", "pk")
    ]


def _handle_complete(request, pm_execution, wo, checklist):
    """Process Complete Maintenance POST."""
    checklist_results = []
    notes = request.POST.get("notes", "").strip()
    root_cause = request.POST.get("root_cause", "").strip()

    for i, item in enumerate(checklist):
        checked = request.POST.get(f"checklist_{i}") == "on"
        note = request.POST.get(f"note_{i}", "").strip()
        checklist_results.append({
            "text": item["text"],
            "checked": checked,
            "note": note,
        })

    photo_count = Attachment.objects.filter(
        entity_type="work_order",
        entity_id=wo.pk,
    ).count()
    required_photo_count = pm_execution.pm_schedule.template.requires_photo_min_count

    result = engine.complete_occurrence(
        pm_execution,
        request.user,
        checklist_results=checklist_results,
        notes=notes,
        photo_count=photo_count,
        required_photo_count=required_photo_count,
        root_cause=root_cause,
    )

    if not result.success:
        messages.error(request, result.error)
        return redirect("preventive:execute", occurrence_id=pm_execution.pk)

    # Notify manager of waiting review
    engine.notify_waiting_review(pm_execution)

    messages.success(request, "Maintenance submitted for review.")
    return redirect("preventive:my")


@login_required
@require_POST
@role_required(User.Role.TECHNICIAN, User.Role.SUPER_ADMIN, User.Role.SUPERVISOR, User.Role.MANAGER)
def tech_start(request, occurrence_id):
    """Pick up + start labor on a PM occurrence.

    For unassigned tasks (pool), the technician claims ownership first.
    For already-assigned tasks, just starts the work.
    """
    pm_execution = get_object_or_404(PMExecution, pk=occurrence_id)
    if pm_execution.assigned_technician_id is None:
        engine.assign(pm_execution, request.user, by=request.user)
    engine.start_occurrence(pm_execution, request.user, work_order_creator=request.user)
    return redirect("preventive:execute", occurrence_id=pm_execution.pk)


@login_required
@require_POST
@role_required(User.Role.TECHNICIAN, User.Role.SUPER_ADMIN, User.Role.SUPERVISOR, User.Role.MANAGER)
def tech_add_photo(request, occurrence_id):
    """Upload a photo during execution."""
    pm_execution = get_object_or_404(PMExecution, pk=occurrence_id)
    wo = pm_execution.work_order
    if not wo:
        messages.error(request, "Start the maintenance before adding photos.")
        return redirect("preventive:execute", occurrence_id=pm_execution.pk)

    upload = request.FILES.get("photo")
    if not upload:
        messages.error(request, "No photo uploaded.")
        return redirect("preventive:execute", occurrence_id=pm_execution.pk)

    Attachment.objects.create(
        entity_type="work_order",
        entity_id=wo.pk,
        filename=upload.name,
        file=upload,
        mime_type=upload.content_type,
        uploaded_by=request.user,
    )
    wo.photo_count = Attachment.objects.filter(
        entity_type="work_order",
        entity_id=wo.pk,
    ).count()
    wo.save(update_fields=["photo_count"])

    messages.success(request, "Photo added.")
    return redirect("preventive:execute", occurrence_id=pm_execution.pk)


@login_required
@role_required(User.Role.TECHNICIAN, User.Role.SUPER_ADMIN, User.Role.SUPERVISOR, User.Role.MANAGER)
def tech_resume(request, occurrence_id):
    """Resume after Return — same execution page, status REJECTED."""
    return redirect("preventive:execute", occurrence_id=occurrence_id)


# ════════════════════════════════════════════════════════════════════
#  MANAGER  (6 pages + 1 detail)
# ════════════════════════════════════════════════════════════════════


@login_required
@role_required(User.Role.MANAGER, User.Role.SUPER_ADMIN)
def mgr_dashboard(request):
    """5 counts: Due Today / Overdue / Waiting Review / Unassigned / Completed Today."""
    today = timezone.now().date()
    due_today = PMExecution.objects.filter(
        scheduled_due_at__date=today,
        pm_schedule__is_active=True,
    ).exclude(status=PMExecution.Status.APPROVED).count()

    overdue = PMExecution.objects.filter(
        scheduled_due_at__date__lt=today,
        status=PMExecution.Status.SUBMITTED,
        pm_schedule__is_active=True,
    ).count()

    waiting_review = PMExecution.objects.filter(
        status=PMExecution.Status.SUBMITTED,
        work_order__lifecycle_status="pending_review",
    ).count()

    # Unassigned: today-only (today's occurrences with no technician)
    unassigned = PMExecution.objects.filter(
        scheduled_due_at__date=today,
        assigned_technician__isnull=True,
        pm_schedule__is_active=True,
    ).count()

    completed_today = PMExecution.objects.filter(
        scheduled_due_at__date=today,
        status=PMExecution.Status.APPROVED,
    ).count()

    context = {
        "due_today": due_today,
        "overdue": overdue,
        "waiting_review": waiting_review,
        "unassigned": unassigned,
        "completed_today": completed_today,
        "today": today,
    }
    return render(request, "maintenance/preventive/mgr_dashboard.html", context)


@login_required
@role_required(User.Role.MANAGER, User.Role.SUPER_ADMIN)
def mgr_today(request):
    """Today's Schedule — grouped by time slot."""
    today = timezone.now().date()
    occurrences = (
        PMExecution.objects
        .filter(
            scheduled_due_at__date=today,
            pm_schedule__is_active=True,
        )
        .select_related(
            "pm_schedule", "pm_schedule__template",
            "pm_schedule__machine", "assigned_technician", "work_order",
        )
        .order_by("scheduled_due_at")
    )

    # Group by time slot (hour:minute)
    from collections import OrderedDict
    by_slot = OrderedDict()
    for occ in occurrences:
        slot = occ.scheduled_due_at.strftime("%H:%M")
        by_slot.setdefault(slot, []).append(occ)

    technicians = User.objects.filter(
        role__in=[User.Role.TECHNICIAN, User.Role.SUPERVISOR],
        is_active=True,
    ).order_by("username")

    return render(request, "maintenance/preventive/mgr_today.html", {
        "by_slot": by_slot,
        "technicians": technicians,
        "today": today,
        "occurrences": occurrences,
    })


@login_required
@role_required(User.Role.MANAGER, User.Role.SUPER_ADMIN)
def mgr_reviews(request):
    """Reviews queue — pending PM submissions, oldest first."""
    pending = (
        PMExecution.objects
        .filter(
            status=PMExecution.Status.SUBMITTED,
            work_order__lifecycle_status="pending_review",
        )
        .select_related(
            "pm_schedule", "pm_schedule__template",
            "pm_schedule__machine", "assigned_technician", "work_order",
        )
        .order_by("work_order__labor_stopped_at")  # FIFO: oldest submitted first
    )
    return render(request, "maintenance/preventive/mgr_reviews.html", {
        "pending": pending,
    })


@login_required
@require_POST
@role_required(User.Role.MANAGER, User.Role.SUPER_ADMIN)
def mgr_review_approve(request, occurrence_id):
    pm_execution = get_object_or_404(PMExecution, pk=occurrence_id)
    engine.approve(pm_execution, request.user)
    messages.success(request, "Approved.")
    return redirect("preventive:mgr_reviews")


@login_required
@require_POST
@role_required(User.Role.MANAGER, User.Role.SUPER_ADMIN)
def mgr_review_return(request, occurrence_id):
    pm_execution = get_object_or_404(PMExecution, pk=occurrence_id)
    reason = request.POST.get("reason", "").strip()
    if not reason:
        messages.error(request, "Return reason is required.")
        return redirect("preventive:mgr_reviews")
    engine.return_to_technician(pm_execution, request.user, reason=reason)
    engine.notify_returned(pm_execution, reason)
    messages.info(request, "Returned to technician.")
    return redirect("preventive:mgr_reviews")


@login_required
@role_required(User.Role.MANAGER, User.Role.SUPER_ADMIN)
def mgr_templates(request):
    templates = PMTemplate.objects.all().order_by("code")
    return render(request, "maintenance/preventive/mgr_templates.html", {
        "templates": templates,
    })


@login_required
@role_required(User.Role.MANAGER, User.Role.SUPER_ADMIN)
def mgr_template_create(request):
    return _template_form(request, template=None)


@login_required
@role_required(User.Role.MANAGER, User.Role.SUPER_ADMIN)
def mgr_template_edit(request, pk):
    template = get_object_or_404(PMTemplate, pk=pk)
    return _template_form(request, template=template)


def _template_form(request, template):
    from .forms import PMTemplateForm
    if request.method == "POST":
        form = PMTemplateForm(request.POST, instance=template)
        if form.is_valid():
            tmpl = form.save()
            # Save checklist inline
            tmpl.checklist_items.all().delete()
            texts = request.POST.getlist("checklist_text")
            for i, t in enumerate(texts):
                if t.strip():
                    PMChecklistItem.objects.create(
                        template=tmpl,
                        order=i,
                        text=t.strip(),
                        is_required=True,
                    )
            messages.success(request, "Template saved.")
            return redirect("preventive:mgr_templates")
    else:
        form = PMTemplateForm(instance=template)

    checklist_texts = (
        [c.text for c in template.checklist_items.all().order_by("order", "pk")]
        if template else [""]
    )
    return render(request, "maintenance/preventive/mgr_template_form.html", {
        "form": form,
        "template": template,
        "checklist_texts": checklist_texts,
    })


@login_required
@role_required(User.Role.MANAGER, User.Role.SUPER_ADMIN)
def mgr_plans(request):
    filter_state = request.GET.get("state", "active")
    qs = PMSchedule.objects.select_related(
        "template", "machine", "component",
    ).order_by("next_due_at")
    if filter_state == "active":
        qs = qs.filter(is_active=True)
    elif filter_state == "paused":
        qs = qs.filter(is_active=False, archived_at__isnull=True)
    elif filter_state == "archived":
        qs = qs.filter(archived_at__isnull=False)
    return render(request, "maintenance/preventive/mgr_plans.html", {
        "plans": qs,
        "filter_state": filter_state,
    })


@login_required
@role_required(User.Role.MANAGER, User.Role.SUPER_ADMIN)
def mgr_plan_create(request):
    return _plan_form(request, schedule=None)


@login_required
@role_required(User.Role.MANAGER, User.Role.SUPER_ADMIN)
def mgr_plan_edit(request, pk):
    schedule = get_object_or_404(PMSchedule, pk=pk)
    return _plan_form(request, schedule=schedule)


def _plan_form(request, schedule):
    from .forms import PMScheduleForm
    if request.method == "POST":
        form = PMScheduleForm(request.POST, instance=schedule)
        if form.is_valid():
            form.save()
            messages.success(request, "Plan saved.")
            if schedule:
                return redirect("preventive:mgr_plan_detail", pk=schedule.pk)
            return redirect("preventive:mgr_plans")
    else:
        form = PMScheduleForm(instance=schedule)

    templates = PMTemplate.objects.filter(is_active=True).order_by("code")
    machines = PMSchedule._meta.get_field("machine").related_model.objects.filter(
        is_active=True
    ).order_by("name")
    technicians = User.objects.filter(
        role__in=[User.Role.TECHNICIAN, User.Role.SUPERVISOR],
        is_active=True,
    ).order_by("username")

    return render(request, "maintenance/preventive/mgr_plan_form.html", {
        "form": form,
        "schedule": schedule,
        "templates": templates,
        "machines": machines,
        "technicians": technicians,
    })


@login_required
@role_required(User.Role.MANAGER, User.Role.SUPER_ADMIN)
def mgr_plan_detail(request, pk):
    """Single source of truth for a plan: info, upcoming, recent executions, history link."""
    schedule = get_object_or_404(
        PMSchedule.objects.select_related("template", "machine", "component"),
        pk=pk,
    )
    # Recent executions (last 5)
    recent = (
        PMExecution.objects
        .filter(pm_schedule=schedule)
        .select_related("work_order", "assigned_technician", "completed_by", "approved_by")
        .order_by("-scheduled_due_at")[:5]
    )
    # Next occurrence (today or future)
    next_occ = (
        PMExecution.objects
        .filter(pm_schedule=schedule, scheduled_due_at__gte=timezone.now())
        .order_by("scheduled_due_at")
        .first()
    )
    # Today's occurrence if exists
    today = timezone.now().date()
    today_occ = (
        PMExecution.objects
        .filter(pm_schedule=schedule, scheduled_due_at__date=today)
        .first()
    )
    # Last completed
    last_completed = (
        PMExecution.objects
        .filter(pm_schedule=schedule, status=PMExecution.Status.APPROVED)
        .order_by("-approved_at")
        .first()
    )

    technicians = User.objects.filter(
        role__in=[User.Role.TECHNICIAN, User.Role.SUPERVISOR],
        is_active=True,
    ).order_by("username")

    return render(request, "maintenance/preventive/mgr_plan_detail.html", {
        "schedule": schedule,
        "recent": recent,
        "next_occ": next_occ,
        "today_occ": today_occ,
        "last_completed": last_completed,
        "technicians": technicians,
    })


@login_required
@require_POST
@role_required(User.Role.MANAGER, User.Role.SUPER_ADMIN)
def mgr_plan_assign(request, pk):
    """Assign or reassign technician on a plan's occurrences."""
    schedule = get_object_or_404(PMSchedule, pk=pk)
    technician_id = request.POST.get("technician_id")
    technician = get_object_or_404(User, pk=technician_id) if technician_id else None

    # Apply to today's occurrence + future ones
    today = timezone.now().date()
    target_occurrences = PMExecution.objects.filter(
        pm_schedule=schedule,
        scheduled_due_at__date__gte=today,
    ).exclude(status=PMExecution.Status.APPROVED)

    for occ in target_occurrences:
        if occ.assigned_technician_id:
            engine.reassign(occ, technician, by=request.user, reason="Plan reassignment")
        else:
            engine.assign(occ, technician, by=request.user)

    messages.success(request, "Assigned.")
    return redirect("preventive:mgr_plan_detail", pk=schedule.pk)


@login_required
@require_POST
@role_required(User.Role.MANAGER, User.Role.SUPER_ADMIN)
def mgr_plan_pause(request, pk):
    schedule = get_object_or_404(PMSchedule, pk=pk)
    was_active = schedule.is_active
    schedule.is_active = not schedule.is_active  # toggle pause/resume
    schedule.save(update_fields=["is_active"])

    if was_active:
        engine.notify_plan_paused(schedule)
        messages.info(request, "Plan paused.")
    else:
        messages.success(request, "Plan resumed.")
    return redirect("preventive:mgr_plan_detail", pk=schedule.pk)


@login_required
@require_POST
@role_required(User.Role.MANAGER, User.Role.SUPER_ADMIN)
def mgr_plan_archive(request, pk):
    schedule = get_object_or_404(PMSchedule, pk=pk)
    schedule.archived_at = timezone.now()
    schedule.archived_by = request.user
    schedule.is_active = False
    schedule.save(update_fields=["archived_at", "archived_by", "is_active"])
    messages.info(request, "Plan archived.")
    return redirect("preventive:mgr_plans")


@login_required
@require_POST
@role_required(User.Role.MANAGER, User.Role.SUPER_ADMIN)
def mgr_plan_run_now(request, pk):
    """Create a one-time PM occurrence NOW (Run Now action)."""
    schedule = get_object_or_404(PMSchedule, pk=pk)
    from django.db import transaction
    with transaction.atomic():
        occ = PMExecution.objects.create(
            pm_schedule=schedule,
            scheduled_due_at=timezone.now(),
            status=PMExecution.Status.SUBMITTED,
        )
        if schedule.assigned_technician:
            engine.assign(occ, schedule.assigned_technician, by=request.user)
    messages.success(request, "Maintenance scheduled for now.")
    return redirect("preventive:mgr_today")


@login_required
@role_required(User.Role.MANAGER, User.Role.SUPER_ADMIN)
def mgr_history(request):
    """Searchable cross-machine history. Default: last 30 days."""
    today = timezone.now().date()
    days = int(request.GET.get("days", 30))
    cutoff = today - timedelta(days=days)

    machine_id = request.GET.get("machine", "")
    technician_id = request.GET.get("technician", "")
    status = request.GET.get("status", "")

    qs = (
        PMExecution.objects
        .filter(scheduled_due_at__date__gte=cutoff)
        .filter(status=PMExecution.Status.APPROVED)
        .select_related(
            "pm_schedule", "pm_schedule__template",
            "pm_schedule__machine", "assigned_technician", "completed_by",
            "approved_by", "work_order",
        )
        .order_by("-scheduled_due_at")
    )

    if machine_id:
        qs = qs.filter(pm_schedule__machine_id=machine_id)
    if technician_id:
        qs = qs.filter(
            Q(assigned_technician_id=technician_id) | Q(completed_by_id=technician_id)
        )
    if status == "approved":
        qs = qs.filter(status=PMExecution.Status.APPROVED)
    elif status == "rejected":
        qs = qs.filter(status=PMExecution.Status.REJECTED)

    machines = PMSchedule._meta.get_field("machine").related_model.objects.filter(
        is_active=True
    ).order_by("name")
    technicians = User.objects.filter(
        role__in=[User.Role.TECHNICIAN, User.Role.SUPERVISOR, User.Role.MANAGER],
        is_active=True,
    ).order_by("username")

    executions = qs[:200]

    if not executions:
        message = "No history found."
    else:
        message = None

    return render(request, "maintenance/preventive/mgr_history.html", {
        "executions": executions,
        "machines": machines,
        "technicians": technicians,
        "selected_machine": machine_id,
        "selected_technician": technician_id,
        "selected_status": status,
        "selected_days": days,
        "today": today,
        "empty_message": message,
    })