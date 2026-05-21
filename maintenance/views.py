from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Count, F, Q, Sum
from django.db.models import Case, IntegerField, Value, When
from django.http import Http404
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils import timezone
from django.views.decorators.http import require_POST
from datetime import timedelta
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from datetime import timedelta as td

STALE_THRESHOLDS = {
    "critical": td(hours=1),
    "high": td(hours=4),
    "medium": td(hours=8),
    "low": td(hours=24),
}

from accounts.capabilities import get_mms_capabilities
from accounts.models import User
from accounts.permissions import role_required
from inventory.forms import ConsumableUseForm, IssuePartForm, StockInForm
from inventory.models import PartIssueLine, SparePart
from inventory.qr_utils import qr_scan_decode as decode_qr
from inventory.services import consumable_use, issue_part_to_work_order, stock_in
from procurement.models import PurchaseRequest

from .forms import (
    AssignTechnicianForm,
    EmergencyWOForm,
    ExternalRepairForm,
    ExternalRepairOfficerForm,
    IssueReportForm,
    MachineForm,
    PMScheduleForm,
    QuickLogForm,
    TechVendorNoteForm,
    ToolAssignForm,
    ToolForm,
    ToolReturnForm,
    ValidateIssueForm,
    WorkOrderCompleteForm,
)
from .models import (
    AuditEntry,
    ExternalRepairOrder,
    Machine,
    MaintenanceIssue,
    Notification,
    PMSchedule,
    QuickMaintenanceLog,
    Tool,
    ToolAssignment,
    WorkOrder,
    WorkOrderAssignmentHistory,
)
from .services import (
    archive_maintenance_issue,
    archive_work_order,
    get_other_active_work_order,
    log_audit,
    manager_close_work_order,
    technician_mark_pending_parts,
    technician_mark_waiting_vendor,
    technician_start_work,
    technician_submit_for_review,
    transition_work_order,
    validate_issue,
)


def _queue_priority_and_status_rank():
    """
    Orders technician's WO queue by:
    1. IN_PROGRESS first (active work)
    2. Emergency WOs before non-emergency
    3. Issue priority (CRITICAL > HIGH > MEDIUM > LOW from validated issue)
    4. Status rank as tiebreaker
    """
    return Case(
        When(status=WorkOrder.Status.IN_PROGRESS, then=Value(0)),
        When(is_emergency=True, then=Value(1)),
        When(issue__priority=MaintenanceIssue.Priority.CRITICAL, then=Value(2)),
        When(issue__priority=MaintenanceIssue.Priority.HIGH, then=Value(3)),
        When(issue__priority=MaintenanceIssue.Priority.MEDIUM, then=Value(4)),
        When(issue__priority=MaintenanceIssue.Priority.LOW, then=Value(5)),
        When(status=WorkOrder.Status.ASSIGNED, then=Value(6)),
        When(status=WorkOrder.Status.PAUSED, then=Value(7)),
        When(status=WorkOrder.Status.PENDING_PARTS, then=Value(8)),
        When(status=WorkOrder.Status.WAITING_FOR_VENDOR, then=Value(9)),
        When(status=WorkOrder.Status.PENDING_REVIEW, then=Value(10)),
        When(status=WorkOrder.Status.APPROVED, then=Value(11)),
        default=Value(12),
        output_field=IntegerField(),
    )


def _priority_rank():
    return Case(
        When(issue__priority=MaintenanceIssue.Priority.CRITICAL, then=Value(0)),
        When(issue__priority=MaintenanceIssue.Priority.HIGH, then=Value(1)),
        When(issue__priority=MaintenanceIssue.Priority.MEDIUM, then=Value(2)),
        When(issue__priority=MaintenanceIssue.Priority.LOW, then=Value(3)),
        default=Value(4),
        output_field=IntegerField(),
    )


def _queue_status_rank():
    return Case(
        When(status=WorkOrder.Status.IN_PROGRESS, then=Value(0)),
        When(status=WorkOrder.Status.ASSIGNED, then=Value(1)),
        When(status=WorkOrder.Status.PAUSED, then=Value(2)),
        When(status=WorkOrder.Status.PENDING_PARTS, then=Value(3)),
        When(status=WorkOrder.Status.WAITING_FOR_VENDOR, then=Value(4)),
        When(status=WorkOrder.Status.PENDING_REVIEW, then=Value(5)),
        When(status=WorkOrder.Status.APPROVED, then=Value(6)),
        default=Value(7),
        output_field=IntegerField(),
    )


def _safe_next_path(request, default_name="dashboard"):
    next_path = request.GET.get("next") or request.POST.get("next") or ""
    if next_path and url_has_allowed_host_and_scheme(
        next_path,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return next_path
    if next_path.startswith("/"):
        return next_path
    return reverse(default_name)


def _append_query_value(path: str, key: str, value: str) -> str:
    parts = urlsplit(path)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query[key] = value
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def _decode_uploaded_qr(uploaded_file) -> str:
    import cv2
    import numpy as np

    data = uploaded_file.read()
    if not data:
        return ""
    image = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        return ""
    detector = cv2.QRCodeDetector()
    value, _points, _straight = detector.detectAndDecode(image)
    return (value or "").strip()


@login_required
def dashboard(request):
    role = getattr(request.user, "role", "")
    stale_before = timezone.now() - timedelta(hours=24)
    caps = get_mms_capabilities(request.user)
    if caps.get("pm_schedule_manage"):
        from .notifications import sync_pm_overdue_notifications

        sync_pm_overdue_notifications()
    ctx = {
        "role": role,
        "open_issues": None,
        "open_wos": None,
        "pending_review": None,
        "emergency_open": None,
        "pending_procurement": None,
        "stale_new_issues": 0,
        "overdue_pm": [],
        "notif_feed": Notification.objects.filter(recipient=request.user).order_by("-created_at")[:8],
    }
    if caps.get("view_issues"):
        ctx["open_issues"] = MaintenanceIssue.objects.filter(status=MaintenanceIssue.Status.NEW).count()
    if caps.get("view_work_orders"):
        ctx["open_wos"] = WorkOrder.objects.exclude(status=WorkOrder.Status.CLOSED).count()
        ctx["emergency_open"] = WorkOrder.objects.filter(is_emergency=True).exclude(
            status=WorkOrder.Status.CLOSED
        ).count()
    if role in (User.Role.MANAGER, User.Role.SUPERVISOR, User.Role.SUPER_ADMIN) or request.user.is_superuser:
        ctx["stale_new_issues"] = MaintenanceIssue.objects.filter(
            status=MaintenanceIssue.Status.NEW,
            created_at__lt=stale_before,
        ).count()
    if caps.get("pm_schedule_manage"):
        ctx["overdue_pm"] = PMSchedule.objects.filter(is_active=True, next_due_at__lt=timezone.now()).select_related(
            "machine"
        )[:12]

    now = timezone.now()
    stale_issues_by_priority = {}
    for priority, threshold in STALE_THRESHOLDS.items():
        stale_qs = MaintenanceIssue.objects.filter(
            status=MaintenanceIssue.Status.NEW,
            priority=priority.upper(),
            created_at__lt=now - threshold,
            is_archived=False,
        )
        if stale_qs.exists():
            stale_issues_by_priority[priority] = stale_qs.count()

    stale_red_count = stale_issues_by_priority.get("critical", 0) + stale_issues_by_priority.get("high", 0)
    stale_yellow_count = stale_issues_by_priority.get("medium", 0) + stale_issues_by_priority.get("low", 0)

    ctx.update({
        "stale_issues_by_priority": stale_issues_by_priority,
        "stale_red_count": stale_red_count,
        "stale_yellow_count": stale_yellow_count,
    })
    if role == User.Role.OPERATOR:
        ctx["my_issues"] = MaintenanceIssue.objects.filter(reported_by=request.user)[:10]
    if role in (User.Role.TECHNICIAN,):
        ctx["my_queue"] = (
            WorkOrder.objects.filter(assigned_technician=request.user)
            .exclude(status=WorkOrder.Status.CLOSED)
            .annotate(queue_rank=_queue_priority_and_status_rank())
            .order_by("queue_rank", "created_at")[:20]
        )
    if caps.get("close_or_review_wo"):
        ctx["pending_review"] = WorkOrder.objects.filter(status=WorkOrder.Status.PENDING_REVIEW).count()
    if caps.get("view_procurement_requests"):
        ctx["pending_procurement"] = PurchaseRequest.objects.filter(status=PurchaseRequest.Status.PENDING).count()
    return render(request, "maintenance/dashboard.html", ctx)


@login_required
def qr_scan(request):
    next_path = _safe_next_path(request)
    param = (request.GET.get("param") or "qr").strip() or "qr"
    label = (request.GET.get("label") or "code").strip() or "code"
    if request.method == "POST":
        param = (request.POST.get("param") or param).strip() or "qr"
        label = (request.POST.get("label") or label).strip() or "code"
        upload = request.FILES.get("qr_image")
        if not upload:
            messages.error(request, "Choose a QR image to upload, or use manual entry.")
        else:
            decoded_value = _decode_uploaded_qr(upload)
            if decoded_value:
                return redirect(_append_query_value(next_path, param, decoded_value))
            messages.error(
                request,
                f"We could not read a QR code from that image. Try a clearer {label} photo or enter the code manually.",
            )
    return render(
        request,
        "maintenance/qr_scan.html",
        {
            "next_path": next_path,
            "param_name": param,
            "scan_label": label,
        },
    )


@login_required
@require_POST
def qr_scan_decode(request):
    upload = request.FILES.get("qr_image")
    if not upload:
        return JsonResponse({"decoded_value": "", "error": "missing_file"}, status=400)
    decoded_value = _decode_uploaded_qr(upload)

    decoded = decode_qr(decoded_value)

    if decoded["type"] == "part":
        from maintenance.models import Site
        part = get_object_or_404(SparePart, sku=decoded["sku"])
        site = Site.objects.filter(is_default=True).first()
        inv = part.inventory_items.filter(site=site).first() if site else None
        return JsonResponse({
            "type": "part",
            "sku": part.sku,
            "name": part.name,
            "category": part.category or "",
            "is_consumable": part.is_consumable,
            "quantity_available": str(inv.quantity_available) if inv else "0",
            "rack_location": inv.rack_location if inv else "",
            "unit": part.unit or "",
            "site": site.name if site else "",
        })

    return JsonResponse({"decoded_value": decoded_value, "type": "machine"})


@login_required
@role_required(User.Role.MANAGER, User.Role.SUPER_ADMIN)
def machine_list(request):
    machines = (
        Machine.objects.annotate(
            issue_count=Count("issues", distinct=True),
            open_work_orders=Count(
                "work_orders",
                filter=~Q(work_orders__status=WorkOrder.Status.CLOSED),
                distinct=True,
            ),
        )
        .order_by("name")
    )
    return render(request, "maintenance/machine_list.html", {"machines": machines})


@login_required
@role_required(User.Role.MANAGER, User.Role.SUPER_ADMIN)
def machine_create(request):
    if request.method == "POST":
        form = MachineForm(request.POST)
        if form.is_valid():
            machine = form.save()
            log_audit(actor=request.user, action="machine_created", entity="Machine", object_id=machine.pk)
            messages.success(request, "Machine saved.")
            return redirect("machine_list")
    else:
        form = MachineForm()
    return render(request, "maintenance/machine_form.html", {"form": form, "page_title": "Add machine"})


@login_required
@role_required(User.Role.MANAGER, User.Role.SUPER_ADMIN)
def machine_edit(request, pk):
    machine = get_object_or_404(Machine, pk=pk)
    if request.method == "POST":
        form = MachineForm(request.POST, instance=machine)
        if form.is_valid():
            machine = form.save()
            log_audit(actor=request.user, action="machine_updated", entity="Machine", object_id=machine.pk)
            messages.success(request, "Machine updated.")
            return redirect("machine_list")
    else:
        form = MachineForm(instance=machine)
    return render(request, "maintenance/machine_form.html", {"form": form, "page_title": "Edit machine", "machine": machine})


@login_required
@role_required(User.Role.OPERATOR, User.Role.MANAGER, User.Role.SUPERVISOR, User.Role.SUPER_ADMIN)
def issue_list(request):
    qs = MaintenanceIssue.objects.select_related("machine", "reported_by")
    if request.user.role == User.Role.OPERATOR and not request.user.is_super_admin_role():
        qs = qs.filter(reported_by=request.user)
    return render(request, "maintenance/issue_list.html", {"issues": qs[:200]})


@login_required
@role_required(User.Role.OPERATOR, User.Role.SUPERVISOR, User.Role.MANAGER, User.Role.SUPER_ADMIN)
def issue_create(request):
    qr = request.GET.get("qr", "").strip()
    matched_machine = Machine.objects.filter(qr_code=qr).first() if qr else None
    if request.method == "POST":
        form = IssueReportForm(request.POST)
        if form.is_valid():
            issue = form.save(commit=False)
            issue.reported_by = request.user
            issue.save()
            log_audit(actor=request.user, action="issue_created", entity="MaintenanceIssue", object_id=issue.pk)
            from .notifications import notify_new_issue

            notify_new_issue(issue)
            messages.success(request, "Issue reported.")
            return redirect("issue_list")
    else:
        initial = {}
        if matched_machine:
            initial["machine"] = matched_machine.pk
        form = IssueReportForm(initial=initial)
    return render(
        request,
        "maintenance/issue_form.html",
        {
            "form": form,
            "qr_value": qr,
            "matched_machine": matched_machine,
        },
    )


@login_required
@role_required(User.Role.SUPERVISOR, User.Role.MANAGER, User.Role.SUPER_ADMIN)
def issue_validate(request, pk):
    issue = get_object_or_404(MaintenanceIssue, pk=pk)
    if issue.status != MaintenanceIssue.Status.NEW:
        messages.warning(request, "Issue is not in NEW state.")
        return redirect("issue_list")
    if request.method == "POST":
        form = ValidateIssueForm(request.POST)
        if form.is_valid():
            validate_issue(issue, actor=request.user, priority=form.cleaned_data["priority"])
            messages.success(request, "Issue validated.")
            return redirect("issue_list")
    else:
        form = ValidateIssueForm()
    return render(request, "maintenance/issue_validate.html", {"issue": issue, "form": form})


@login_required
@role_required(User.Role.MANAGER, User.Role.TECHNICIAN, User.Role.SUPER_ADMIN)
def work_order_list(request):
    wos = WorkOrder.objects.select_related("machine", "assigned_technician", "issue").annotate(
        queue_rank=_queue_priority_and_status_rank(),
    )
    if request.user.role == User.Role.TECHNICIAN and not request.user.is_super_admin_role():
        wos = wos.filter(assigned_technician=request.user).exclude(status=WorkOrder.Status.CLOSED)
    st = request.GET.get("status")
    if st in dict(WorkOrder.Status.choices):
        wos = wos.filter(status=st)
    wos = wos.order_by("queue_rank", "created_at")[:400]
    return render(
        request,
        "maintenance/workorder_list.html",
        {"work_orders": wos, "status_filter": st or "", "status_choices": WorkOrder.Status.choices},
    )


@login_required
@role_required(User.Role.MANAGER, User.Role.TECHNICIAN, User.Role.SUPER_ADMIN)
def work_order_detail(request, pk):
    wo = get_object_or_404(
        WorkOrder.objects.select_related("machine", "assigned_technician", "issue"),
        pk=pk,
    )
    if request.user.role == User.Role.TECHNICIAN and wo.assigned_technician_id != request.user.id:
        raise Http404()
    part_issues = wo.part_issues.select_related("part", "issued_by")
    logs = wo.state_logs.select_related("actor")[:50]
    assign_form = AssignTechnicianForm()
    complete_form = WorkOrderCompleteForm(instance=wo)
    issue_part_form = IssuePartForm()
    linked_prs = wo.purchase_requests.select_related("part", "supplier")[:25]
    tech_parts_note = TechVendorNoteForm(prefix="parts")
    tech_vendor_note = TechVendorNoteForm(prefix="vendor")
    active_conflict = None
    if request.user.role == User.Role.TECHNICIAN and wo.assigned_technician_id == request.user.id:
        active_conflict = get_other_active_work_order(request.user, except_pk=wo.pk)
    return render(
        request,
        "maintenance/workorder_detail.html",
        {
            "wo": wo,
            "logs": logs,
            "part_issues": part_issues,
            "assign_form": assign_form,
            "complete_form": complete_form,
            "issue_part_form": issue_part_form,
            "linked_prs": linked_prs,
            "tech_parts_note": tech_parts_note,
            "tech_vendor_note": tech_vendor_note,
            "active_conflict": active_conflict,
        },
    )


@login_required
@role_required(User.Role.MANAGER, User.Role.SUPER_ADMIN)
def work_order_create_from_issue(request, issue_pk):
    issue = get_object_or_404(
        MaintenanceIssue.objects.select_related("machine", "reported_by", "validated_by"),
        pk=issue_pk,
    )
    if issue.status != MaintenanceIssue.Status.VALIDATED:
        messages.error(request, "Issue must be validated first.")
        return redirect("issue_list")
    if hasattr(issue, "work_order") and issue.work_order_id:
        messages.info(request, "Work order already exists for this issue.")
        return redirect("work_order_detail", pk=issue.work_order_id)
    if request.method != "POST":
        return render(request, "maintenance/workorder_create_confirm.html", {"issue": issue})
    wo = WorkOrder.objects.create(
        category=WorkOrder.Category.BREAKDOWN,
        issue=issue,
        machine=issue.machine,
        status=WorkOrder.Status.APPROVED,
        created_by=request.user,
    )
    issue.status = MaintenanceIssue.Status.CONVERTED
    issue.save(update_fields=["status"])
    transition_work_order(
        wo,
        WorkOrder.Status.APPROVED,
        actor=request.user,
        note=f"Created from validated issue #{issue.pk}",
    )
    log_audit(actor=request.user, action="wo_created", entity="WorkOrder", object_id=wo.pk)
    messages.success(request, f"Work order WO-{wo.number} created.")
    return redirect("work_order_detail", pk=wo.pk)


@login_required
@role_required(User.Role.MANAGER, User.Role.SUPER_ADMIN)
def work_order_assign(request, pk):
    wo = get_object_or_404(WorkOrder, pk=pk)
    previous_technician_id = wo.assigned_technician_id
    if wo.status not in (
        WorkOrder.Status.APPROVED,
        WorkOrder.Status.ASSIGNED,
        WorkOrder.Status.PAUSED,
    ):
        messages.error(request, "Work order cannot be assigned in current state.")
        return redirect("work_order_detail", pk=pk)
    if request.method != "POST":
        return redirect("work_order_detail", pk=pk)
    form = AssignTechnicianForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Invalid assignment.")
        return redirect("work_order_detail", pk=pk)
    new_technician = form.cleaned_data["technician"]
    wo.assigned_technician = new_technician
    wo.save(update_fields=["assigned_technician", "updated_at"])
    transition_work_order(wo, WorkOrder.Status.ASSIGNED, actor=request.user, note="Technician assigned")
    WorkOrderAssignmentHistory.objects.create(
        work_order=wo,
        technician=new_technician,
        action=WorkOrderAssignmentHistory.Action.ASSIGNED,
        assigned_by=request.user,
        reason=f"Assigned by {request.user.get_full_name() or request.user.username}",
    )
    if previous_technician_id and previous_technician_id != new_technician.id:
        old_technician = User.objects.get(pk=previous_technician_id)
        prev = WorkOrderAssignmentHistory.objects.filter(
            work_order=wo, technician=old_technician, unassigned_at__isnull=True
        ).first()
        if prev:
            prev.unassigned_at = timezone.now()
            prev.reason = f"Reassigned to {new_technician.get_full_name() or new_technician.username}"
            prev.save()
    from .notifications import notify_wo_assigned

    notify_wo_assigned(wo)
    messages.success(request, "Technician assigned.")
    return redirect("work_order_detail", pk=pk)


@login_required
@require_POST
@role_required(User.Role.TECHNICIAN, User.Role.SUPER_ADMIN)
def work_order_start(request, pk):
    wo = get_object_or_404(WorkOrder, pk=pk)
    if wo.assigned_technician != request.user:
        messages.error(request, "You can only start work orders assigned to you.")
        return redirect("work_order_detail", pk=wo.pk)
    if wo.assigned_technician_id != request.user.id and not request.user.is_super_admin_role():
        raise Http404()
    if wo.status not in (
        WorkOrder.Status.ASSIGNED,
        WorkOrder.Status.PAUSED,
        WorkOrder.Status.PENDING_PARTS,
        WorkOrder.Status.WAITING_FOR_VENDOR,
        WorkOrder.Status.IN_PROGRESS,
    ):
        messages.error(request, "Cannot start work in this state.")
        return redirect("work_order_detail", pk=pk)
    conflicting_wo = get_other_active_work_order(request.user, except_pk=wo.pk)
    if conflicting_wo and wo.status != WorkOrder.Status.IN_PROGRESS and request.POST.get("confirm_switch") != "1":
        return render(
            request,
            "maintenance/workorder_switch_confirm.html",
            {"wo": wo, "conflicting_wo": conflicting_wo},
        )
    technician_start_work(wo, request.user)
    messages.success(request, "Work started.")
    return redirect("work_order_detail", pk=pk)


@login_required
@require_POST
@role_required(User.Role.TECHNICIAN, User.Role.SUPER_ADMIN)
def work_order_pause(request, pk):
    wo = get_object_or_404(WorkOrder, pk=pk)
    if wo.assigned_technician != request.user:
        messages.error(request, "You can only pause work orders assigned to you.")
        return redirect("work_order_detail", pk=wo.pk)
    if wo.assigned_technician_id != request.user.id and not request.user.is_super_admin_role():
        raise Http404()
    if wo.status != WorkOrder.Status.IN_PROGRESS:
        messages.error(request, "Not in progress.")
        return redirect("work_order_detail", pk=pk)
    wo.labor_stopped_at = timezone.now()
    wo.save(update_fields=["labor_stopped_at", "updated_at"])
    transition_work_order(wo, WorkOrder.Status.PAUSED, actor=request.user, note="Paused")
    messages.info(request, "Paused.")
    return redirect("work_order_detail", pk=pk)


@login_required
@require_POST
@role_required(User.Role.TECHNICIAN, User.Role.SUPER_ADMIN)
def work_order_submit(request, pk):
    wo = get_object_or_404(WorkOrder, pk=pk)
    if wo.assigned_technician != request.user:
        messages.error(request, "You can only submit work orders assigned to you.")
        return redirect("work_order_detail", pk=wo.pk)
    if wo.assigned_technician_id != request.user.id and not request.user.is_super_admin_role():
        raise Http404()
    if wo.status != WorkOrder.Status.IN_PROGRESS:
        messages.error(request, "Submit for review is only available while work is in progress.")
        return redirect("work_order_detail", pk=pk)
    form = WorkOrderCompleteForm(request.POST, instance=wo)
    if not form.is_valid():
        messages.error(request, "Check completion fields.")
        return redirect("work_order_detail", pk=pk)
    form.save()
    technician_submit_for_review(wo, request.user)
    messages.success(request, "Submitted for manager review.")
    return redirect("work_order_detail", pk=pk)


@login_required
@role_required(User.Role.MANAGER, User.Role.SUPER_ADMIN)
def work_order_close(request, pk):
    wo = get_object_or_404(WorkOrder, pk=pk)
    if wo.status != WorkOrder.Status.PENDING_REVIEW:
        messages.error(request, "Work order is not pending review.")
        return redirect("work_order_detail", pk=pk)
    if request.method != "POST":
        return redirect("work_order_detail", pk=pk)
    action = request.POST.get("action")
    manager_close_work_order(wo, request.user, approve=(action == "approve"))
    messages.success(request, "Closed." if action == "approve" else "Returned to technician.")
    return redirect("work_order_detail", pk=pk)


@login_required
@role_required(User.Role.MANAGER, User.Role.SUPER_ADMIN)
def work_order_issue_part(request, pk):
    wo = get_object_or_404(WorkOrder, pk=pk)
    if request.method != "POST":
        return redirect("work_order_detail", pk=pk)
    form = IssuePartForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Invalid part issue.")
        return redirect("work_order_detail", pk=pk)
    ok, msg = issue_part_to_work_order(
        wo=wo,
        part=form.cleaned_data["part"],
        quantity=form.cleaned_data["quantity"],
        unit_cost=form.cleaned_data["unit_cost"],
        invoice_ref=form.cleaned_data["invoice_ref"],
        supplier_name=form.cleaned_data.get("supplier_name") or "",
        issued_by=request.user,
    )
    (messages.success if ok else messages.error)(request, msg)
    return redirect("work_order_detail", pk=pk)


@login_required
@role_required(User.Role.MANAGER, User.Role.SUPER_ADMIN)
def emergency_work_order_create(request):
    if request.method == "POST":
        form = EmergencyWOForm(request.POST)
        if form.is_valid():
            wo = WorkOrder.objects.create(
                category=WorkOrder.Category.EMERGENCY,
                is_emergency=True,
                machine=form.cleaned_data["machine"],
                status=WorkOrder.Status.APPROVED,
                created_by=request.user,
                notes=f"{form.cleaned_data['title']}\n\n{form.cleaned_data['detail']}",
            )
            transition_work_order(wo, WorkOrder.Status.APPROVED, actor=request.user, note="Emergency WO created")
            log_audit(actor=request.user, action="emergency_wo", entity="WorkOrder", object_id=wo.pk)
            from .notifications import notify_emergency_work_order

            notify_emergency_work_order(wo)
            messages.success(request, f"Emergency work order WO-{wo.number} created.")
            return redirect("work_order_detail", pk=wo.pk)
    else:
        form = EmergencyWOForm()
    return render(request, "maintenance/emergency_wo.html", {"form": form})


@login_required
@require_POST
@role_required(User.Role.TECHNICIAN, User.Role.SUPER_ADMIN)
def work_order_mark_parts(request, pk):
    wo = get_object_or_404(WorkOrder, pk=pk)
    if wo.assigned_technician != request.user:
        messages.error(request, "You can only mark parts for work orders assigned to you.")
        return redirect("work_order_detail", pk=wo.pk)
    if wo.assigned_technician_id != request.user.id and not request.user.is_super_admin_role():
        raise Http404()
    form = TechVendorNoteForm(request.POST, prefix="parts")
    if not form.is_valid():
        messages.error(request, "Invalid form.")
        return redirect("work_order_detail", pk=pk)
    try:
        technician_mark_pending_parts(wo, request.user, note=form.cleaned_data.get("note") or "")
        messages.warning(request, "Work order set to waiting for parts (labor timer stopped).")
    except ValueError as e:
        messages.error(request, str(e))
    return redirect("work_order_detail", pk=pk)


@login_required
@require_POST
@role_required(User.Role.TECHNICIAN, User.Role.SUPER_ADMIN)
def work_order_mark_vendor(request, pk):
    wo = get_object_or_404(WorkOrder, pk=pk)
    if wo.assigned_technician != request.user:
        messages.error(request, "You can only mark vendor status for work orders assigned to you.")
        return redirect("work_order_detail", pk=wo.pk)
    if wo.assigned_technician_id != request.user.id and not request.user.is_super_admin_role():
        raise Http404()
    form = TechVendorNoteForm(request.POST, prefix="vendor")
    if not form.is_valid():
        messages.error(request, "Invalid form.")
        return redirect("work_order_detail", pk=pk)
    try:
        technician_mark_waiting_vendor(wo, request.user, note=form.cleaned_data.get("note") or "")
        messages.warning(request, "Work order set to waiting for vendor (labor timer stopped).")
    except ValueError as e:
        messages.error(request, str(e))
    return redirect("work_order_detail", pk=pk)


@login_required
@role_required(User.Role.MANAGER, User.Role.PROCUREMENT, User.Role.SUPER_ADMIN)
def stock_dashboard(request):
    parts = SparePart.objects.all()[:500]
    return render(request, "maintenance/stock_dashboard.html", {"parts": parts})


@login_required
@role_required(User.Role.MANAGER, User.Role.PROCUREMENT, User.Role.SUPER_ADMIN)
def stock_in_view(request):
    if request.method == "POST":
        form = StockInForm(request.POST)
        if form.is_valid():
            stock_in(
                part=form.cleaned_data["part"],
                quantity=form.cleaned_data["quantity"],
                performed_by=request.user,
                supplier_name=form.cleaned_data["supplier_name"],
                unit_cost=form.cleaned_data["unit_cost"],
                invoice_ref=form.cleaned_data["invoice_ref"],
                note=form.cleaned_data.get("note") or "",
            )
            messages.success(request, "Stock-in recorded.")
            return redirect("stock_dashboard")
    else:
        form = StockInForm()
    return render(request, "maintenance/stock_in.html", {"form": form})


@login_required
@role_required(User.Role.OPERATOR, User.Role.SUPERVISOR, User.Role.TECHNICIAN, User.Role.MANAGER, User.Role.SUPER_ADMIN)
def consumables_view(request):
    if request.method == "POST":
        form = ConsumableUseForm(request.POST)
        if form.is_valid():
            ok, msg = consumable_use(
                part=form.cleaned_data["part"],
                quantity=form.cleaned_data["quantity"],
                user=request.user,
                machine_id=form.cleaned_data.get("machine_id"),
            )
            (messages.success if ok else messages.error)(request, msg)
            return redirect("consumables")
    else:
        form = ConsumableUseForm()
    return render(request, "maintenance/consumables.html", {"form": form})


@login_required
@role_required(User.Role.MANAGER, User.Role.SUPER_ADMIN)
def pm_list(request):
    from .notifications import sync_pm_overdue_notifications

    sync_pm_overdue_notifications()
    schedules = PMSchedule.objects.select_related("machine")
    return render(request, "maintenance/pm_list.html", {"schedules": schedules})


@login_required
@role_required(User.Role.MANAGER, User.Role.SUPER_ADMIN)
def pm_create(request):
    if request.method == "POST":
        form = PMScheduleForm(request.POST)
        if form.is_valid():
            form.save()
            messages.success(request, "PM schedule saved.")
            return redirect("pm_list")
    else:
        form = PMScheduleForm(initial={"next_due_at": timezone.now()})
    return render(request, "maintenance/pm_form.html", {"form": form})


@login_required
@role_required(User.Role.MANAGER, User.Role.SUPER_ADMIN)
def pm_spawn_wo(request, pk):
    sched = get_object_or_404(PMSchedule, pk=pk)
    if request.method == "POST":
        form = PMScheduleForm(request.POST)
        if form.is_valid():
            wo = WorkOrder.objects.create(
                category=WorkOrder.Category.PREVENTIVE,
                machine=sched.machine,
                status=WorkOrder.Status.APPROVED,
                created_by=request.user,
                notes=f"PM: {sched.title}",
            )
            transition_work_order(wo, WorkOrder.Status.APPROVED, actor=request.user, note="PM work order")
            if form.cleaned_data.get("propagate_to_children") and sched.machine.children.exists():
                child_count = 0
                for child_machine in sched.machine.children.all():
                    child_wo = WorkOrder.objects.create(
                        category=WorkOrder.Category.PREVENTIVE,
                        machine=child_machine,
                        status=WorkOrder.Status.APPROVED,
                        created_by=request.user,
                        notes=f"PM spawned from {sched.machine.name} → {child_machine.name}",
                    )
                    transition_work_order(child_wo, WorkOrder.Status.APPROVED, actor=request.user,
                                         note=f"PM for {child_machine.name} (child of {sched.machine.name})")
                    child_count += 1
                if child_count:
                    messages.success(request, f"Created {child_count} child PM work orders.")
            messages.success(request, f"PM work order WO-{wo.number} created.")
            return redirect("work_order_detail", pk=wo.pk)
    else:
        form = PMScheduleForm()
    return render(request, "maintenance/pm_spawn_wo.html", {"form": form, "schedule": sched})


@login_required
@role_required(User.Role.MANAGER, User.Role.TECHNICIAN, User.Role.OPERATOR, User.Role.SUPER_ADMIN)
def tool_list(request):
    tools = Tool.objects.all()
    available_tools = tools.filter(status=Tool.Status.AVAILABLE)
    open_assignments = ToolAssignment.objects.filter(returned_at__isnull=True).select_related("tool", "user")
    if request.user.role == User.Role.TECHNICIAN and not request.user.is_super_admin_role():
        open_assignments = open_assignments.filter(user=request.user)
    if request.user.role == User.Role.OPERATOR and not request.user.is_super_admin_role():
        open_assignments = open_assignments.filter(user=request.user)
    scanned_code = (request.GET.get("tool") or "").strip()
    matched_tool = Tool.objects.filter(code__iexact=scanned_code).first() if scanned_code else None
    matched_assignment = (
        open_assignments.filter(tool__code__iexact=scanned_code).select_related("tool", "user").first()
        if scanned_code
        else None
    )
    assign_initial = {"tool": matched_tool.pk} if matched_tool and matched_tool.status == Tool.Status.AVAILABLE else {}
    assign_form = ToolAssignForm(initial=assign_initial)
    return render(
        request,
        "maintenance/tools.html",
        {
            "tools": tools,
            "open_assignments": open_assignments,
            "assign_form": assign_form,
            "scanned_code": scanned_code,
            "matched_tool": matched_tool,
            "matched_assignment": matched_assignment,
            "available_tools_count": available_tools.count(),
        },
    )


@login_required
@role_required(User.Role.MANAGER, User.Role.SUPER_ADMIN)
def tool_create(request):
    if request.method == "POST":
        form = ToolForm(request.POST)
        if form.is_valid():
            tool = form.save()
            log_audit(actor=request.user, action="tool_created", entity="Tool", object_id=tool.pk)
            messages.success(request, "Tool saved.")
            return redirect("tool_list")
    else:
        form = ToolForm(initial={"status": Tool.Status.AVAILABLE})
    return render(request, "maintenance/tool_form.html", {"form": form, "page_title": "Add tool"})


@login_required
@role_required(User.Role.MANAGER, User.Role.SUPER_ADMIN)
def tool_edit(request, pk):
    tool = get_object_or_404(Tool, pk=pk)
    if request.method == "POST":
        form = ToolForm(request.POST, instance=tool)
        if form.is_valid():
            tool = form.save()
            log_audit(actor=request.user, action="tool_updated", entity="Tool", object_id=tool.pk)
            messages.success(request, "Tool updated.")
            return redirect("tool_list")
    else:
        form = ToolForm(instance=tool)
    return render(request, "maintenance/tool_form.html", {"form": form, "page_title": "Edit tool", "tool": tool})


@login_required
@role_required(User.Role.MANAGER, User.Role.SUPER_ADMIN)
def tool_assign(request):
    if request.method != "POST":
        return redirect("tool_list")
    form = ToolAssignForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Invalid tool assignment.")
        return redirect("tool_list")
    tool = form.cleaned_data["tool"]
    assignee = form.cleaned_data["assignee"]
    if tool.status != Tool.Status.AVAILABLE:
        messages.error(request, "Tool not available.")
        return redirect("tool_list")
    tool.status = Tool.Status.IN_USE
    tool.save(update_fields=["status"])
    ToolAssignment.objects.create(tool=tool, user=assignee, assigned_by=request.user)
    log_audit(actor=request.user, action="tool_assigned", entity="Tool", object_id=tool.pk, payload={"to": assignee.username})
    messages.success(request, "Tool assigned.")
    return redirect("tool_list")


@login_required
@role_required(User.Role.OPERATOR, User.Role.TECHNICIAN, User.Role.MANAGER, User.Role.SUPER_ADMIN)
def tool_return(request, assignment_pk):
    ta = get_object_or_404(ToolAssignment.objects.select_related("tool"), pk=assignment_pk)
    if ta.user_id != request.user.id and not request.user.is_super_admin_role():
        if request.user.role != User.Role.MANAGER:
            raise Http404()
    if ta.returned_at:
        messages.info(request, "Already returned.")
        return redirect("tool_list")
    if request.method == "POST":
        form = ToolReturnForm(request.POST)
        if form.is_valid():
            cond = form.cleaned_data["condition"]
            ta.return_condition = cond
            ta.returned_at = timezone.now()
            ta.save()
            if cond == ToolAssignment.ReturnCondition.GOOD:
                ta.tool.status = Tool.Status.AVAILABLE
            elif cond == ToolAssignment.ReturnCondition.DAMAGED:
                ta.tool.status = Tool.Status.OUT_OF_SERVICE
            else:
                ta.tool.status = Tool.Status.OUT_OF_SERVICE
            ta.tool.save(update_fields=["status"])
            log_audit(actor=request.user, action="tool_returned", entity="Tool", object_id=ta.tool.pk, payload={"condition": cond})
            messages.success(request, "Return recorded.")
            return redirect("tool_list")
    else:
        form = ToolReturnForm()
    return render(request, "maintenance/tool_return.html", {"assignment": ta, "form": form})


@login_required
@role_required(User.Role.MANAGER, User.Role.SUPER_ADMIN)
def repair_create(request):
    if request.method == "POST":
        form = ExternalRepairForm(request.POST)
        if form.is_valid():
            r = form.save(commit=False)
            r.created_by = request.user
            r.save()
            messages.success(request, "Repair request created.")
            return redirect("repair_list")
    else:
        form = ExternalRepairForm()
    return render(request, "maintenance/repair_form.html", {"form": form})


@login_required
@role_required(User.Role.PROCUREMENT, User.Role.MANAGER, User.Role.SUPER_ADMIN)
def repair_list(request):
    rows = ExternalRepairOrder.objects.order_by("-created_at")[:200]
    return render(request, "maintenance/repair_list.html", {"repairs": rows})


@login_required
@role_required(User.Role.PROCUREMENT, User.Role.SUPER_ADMIN)
def repair_officer(request, pk):
    rwo = get_object_or_404(ExternalRepairOrder, pk=pk)
    old_status = rwo.status
    if request.method == "POST":
        form = ExternalRepairOfficerForm(request.POST, instance=rwo)
        if form.is_valid():
            inst = form.save(commit=False)
            inst.handled_by = request.user
            if inst.status == ExternalRepairOrder.Status.SENT_TO_VENDOR and not inst.sent_at:
                inst.sent_at = timezone.now()
            if inst.status == ExternalRepairOrder.Status.CLOSED and not inst.closed_at:
                inst.closed_at = timezone.now()
            inst.save()
            if (
                inst.status == ExternalRepairOrder.Status.RETURNED
                and old_status != ExternalRepairOrder.Status.RETURNED
            ):
                from maintenance.notifications import notify_repair_returned

                notify_repair_returned(inst)
            messages.success(request, "Repair order updated.")
            return redirect("repair_list")
    else:
        form = ExternalRepairOfficerForm(instance=rwo)
    return render(request, "maintenance/repair_officer.html", {"rwo": rwo, "form": form})


@login_required
@role_required(User.Role.OPERATOR, User.Role.SUPERVISOR, User.Role.TECHNICIAN, User.Role.MANAGER, User.Role.SUPER_ADMIN)
def quick_log(request):
    if request.method == "POST":
        form = QuickLogForm(request.POST)
        if form.is_valid():
            log = form.save(commit=False)
            log.author = request.user
            log.save()
            messages.success(request, "Quick log saved.")
            return redirect("dashboard")
    else:
        form = QuickLogForm()
    return render(request, "maintenance/quick_log.html", {"form": form})


@login_required
@role_required(
    User.Role.SUPERVISOR,
    User.Role.TECHNICIAN,
    User.Role.MANAGER,
    User.Role.PROCUREMENT,
    User.Role.SUPER_ADMIN,
)
def reports_view(request):
    most_issues = Machine.objects.annotate(ic=Count("issues")).order_by("-ic")[:10]
    low_stock = SparePart.objects.filter(quantity_on_hand__lte=F("min_stock_level")).order_by("sku")[:50]
    top_parts = (
        PartIssueLine.objects.values("part__sku", "part__name")
        .annotate(total_qty=Sum("quantity"))
        .order_by("-total_qty")[:12]
    )
    labels = dict(WorkOrder.Status.choices)
    status_counts = [
        {"code": row["status"], "label": labels.get(row["status"], row["status"]), "count": row["c"]}
        for row in WorkOrder.objects.values("status").annotate(c=Count("id")).order_by("status")
    ]
    tech_done = (
        User.objects.filter(role=User.Role.TECHNICIAN)
        .annotate(
            closed_wos=Count("assigned_work_orders", filter=Q(assigned_work_orders__status=WorkOrder.Status.CLOSED)),
        )
        .order_by("-closed_wos")[:10]
    )
    return render(
        request,
        "maintenance/reports.html",
        {
            "most_issues": most_issues,
            "low_stock": low_stock,
            "top_parts": top_parts,
            "status_counts": status_counts,
            "tech_done": tech_done,
        },
    )


@login_required
def notification_list(request):
    rows = Notification.objects.filter(recipient=request.user).order_by("-created_at")[:200]
    return render(request, "maintenance/notifications.html", {"notifications": rows})


@login_required
@require_POST
def notification_mark_read(request, pk):
    n = get_object_or_404(Notification, pk=pk, recipient=request.user)
    n.read_at = timezone.now()
    n.save(update_fields=["read_at"])
    return redirect("notification_list")


@login_required
@require_POST
def notification_mark_all_read(request):
    Notification.objects.filter(recipient=request.user, read_at__isnull=True).update(read_at=timezone.now())
    return redirect("notification_list")


@login_required
@role_required(User.Role.SUPER_ADMIN)
def audit_log_list(request):
    qs = AuditEntry.objects.select_related("actor").order_by("-created_at")
    paginator = Paginator(qs, 50)
    page = request.GET.get("page")
    return render(request, "maintenance/audit_log.html", {"page_obj": paginator.get_page(page)})


@login_required
@role_required(
    User.Role.SUPERVISOR,
    User.Role.TECHNICIAN,
    User.Role.MANAGER,
    User.Role.PROCUREMENT,
    User.Role.SUPER_ADMIN,
)
def kpi_dashboard(request):
    now = timezone.now()
    td90 = now - timedelta(days=90)
    td180 = now - timedelta(days=180)
    td365 = now - timedelta(days=365)

    closed = list(
        WorkOrder.objects.filter(
            status=WorkOrder.Status.CLOSED,
            downtime_started_at__isnull=False,
            downtime_ended_at__isnull=False,
            downtime_ended_at__gte=td90,
        )
        .select_related("machine", "assigned_technician", "issue")
        .order_by("downtime_ended_at")[:500]
    )
    downtime_hours = []
    repair_hours = []
    downtime_by_month = {}
    for w in closed:
        down_h = (w.downtime_ended_at - w.downtime_started_at).total_seconds() / 3600
        if down_h >= 0:
            downtime_hours.append(down_h)
            month_key = w.downtime_ended_at.strftime("%Y-%m")
            downtime_by_month[month_key] = downtime_by_month.get(month_key, 0) + down_h
        if w.labor_started_at and w.labor_stopped_at:
            labor_h = (w.labor_stopped_at - w.labor_started_at).total_seconds() / 3600
            if labor_h >= 0:
                repair_hours.append(labor_h)
    mttr_hours = sum(repair_hours) / len(repair_hours) if repair_hours else None
    avg_downtime_hours = sum(downtime_hours) / len(downtime_hours) if downtime_hours else None

    wo_started = list(
        WorkOrder.objects.filter(
            issue__isnull=False,
            labor_started_at__isnull=False,
            labor_started_at__gte=td90,
        )
        .select_related("issue")
        .order_by("-labor_started_at")[:500]
    )
    wait_hours = []
    for w in wo_started:
        if w.issue and w.issue.created_at:
            delta_h = (w.labor_started_at - w.issue.created_at).total_seconds() / 3600
            if delta_h >= 0:
                wait_hours.append(delta_h)
    mttw_hours = sum(wait_hours) / len(wait_hours) if wait_hours else None

    issue_rows = list(
        MaintenanceIssue.objects.filter(created_at__gte=td365)
        .select_related("machine")
        .order_by("machine_id", "created_at")
    )
    gaps = []
    machine_failure_rows = {}
    prev_by_machine = {}
    for issue in issue_rows:
        machine_failure_rows.setdefault(issue.machine_id, {"machine": issue.machine, "count": 0})
        machine_failure_rows[issue.machine_id]["count"] += 1
        prev_issue = prev_by_machine.get(issue.machine_id)
        if prev_issue:
            gap_h = (issue.created_at - prev_issue.created_at).total_seconds() / 3600
            if gap_h >= 0:
                gaps.append(gap_h)
        prev_by_machine[issue.machine_id] = issue
    mtbf_hours = sum(gaps) / len(gaps) if gaps else None

    pm_closed = WorkOrder.objects.filter(
        category=WorkOrder.Category.PREVENTIVE,
        status=WorkOrder.Status.CLOSED,
        updated_at__gte=td90,
    ).count()
    pm_due = PMSchedule.objects.filter(is_active=True, next_due_at__lt=now).count()
    pm_active = PMSchedule.objects.filter(is_active=True).count()
    pm_compliance_pct = int((pm_closed / max(pm_closed + pm_due, 1)) * 100) if (pm_closed or pm_due) else None

    repeat_failures = (
        Machine.objects.annotate(
            recent_failures=Count("issues", filter=Q(issues__created_at__gte=td180))
        )
        .filter(recent_failures__gte=2)
        .order_by("-recent_failures")[:15]
    )

    most_used_parts = (
        PartIssueLine.objects.values("part__name", "part__sku")
        .annotate(total_qty=Sum("quantity"))
        .order_by("-total_qty")[:10]
    )

    machine_failure_rate = sorted(
        (
            {
                "machine": row["machine"],
                "failure_count": row["count"],
                "monthly_rate": round(row["count"] / 12, 2),
            }
            for row in machine_failure_rows.values()
        ),
        key=lambda row: row["failure_count"],
        reverse=True,
    )[:10]

    tech_efficiency = []
    tech_rows = (
        User.objects.filter(role=User.Role.TECHNICIAN)
        .prefetch_related("assigned_work_orders")
        .order_by("username")
    )
    for tech in tech_rows:
        tech_closed = [
            wo
            for wo in tech.assigned_work_orders.all()
            if wo.status == WorkOrder.Status.CLOSED and wo.updated_at >= td90
        ]
        avg_hours = None
        labor_values = []
        for wo in tech_closed:
            if wo.labor_started_at and wo.labor_stopped_at:
                labor_h = (wo.labor_stopped_at - wo.labor_started_at).total_seconds() / 3600
                if labor_h >= 0:
                    labor_values.append(labor_h)
        if labor_values:
            avg_hours = sum(labor_values) / len(labor_values)
        tech_efficiency.append(
            {
                "username": tech.username,
                "closed_count": len(tech_closed),
                "avg_repair_hours": avg_hours,
            }
        )
    tech_efficiency.sort(key=lambda row: row["closed_count"], reverse=True)
    tech_efficiency = tech_efficiency[:10]

    tool_returns = ToolAssignment.objects.exclude(returned_at__isnull=True)
    tool_lost_count = tool_returns.filter(return_condition=ToolAssignment.ReturnCondition.LOST).count()
    tool_returned_count = tool_returns.count()
    tool_loss_rate_pct = (
        round((tool_lost_count / tool_returned_count) * 100, 1) if tool_returned_count else None
    )

    supplier_costs = {}
    for pr in PurchaseRequest.objects.filter(updated_at__gte=td365).select_related("supplier", "part"):
        if not pr.supplier_id or pr.unit_price is None:
            continue
        supplier_costs.setdefault(
            pr.supplier_id,
            {"supplier": pr.supplier.name, "total_cost": 0, "requests": 0},
        )
        supplier_costs[pr.supplier_id]["total_cost"] += float(pr.quantity) * float(pr.unit_price)
        supplier_costs[pr.supplier_id]["requests"] += 1
    supplier_cost_ranking = sorted(
        supplier_costs.values(),
        key=lambda row: row["total_cost"],
        reverse=True,
    )[:10]

    downtime_trends = [
        {"month": month, "hours": round(hours, 1)}
        for month, hours in sorted(downtime_by_month.items())
    ]

    return render(
        request,
        "maintenance/kpi.html",
        {
            "mttr_hours": mttr_hours,
            "mttw_hours": mttw_hours,
            "mtbf_hours": mtbf_hours,
            "avg_downtime_hours": avg_downtime_hours,
            "pm_compliance_pct": pm_compliance_pct,
            "pm_closed_90d": pm_closed,
            "pm_active_schedules": pm_active,
            "pm_due_count": pm_due,
            "repeat_failures": repeat_failures,
            "open_emergency_wos": WorkOrder.objects.filter(is_emergency=True)
            .exclude(status=WorkOrder.Status.CLOSED)
            .count(),
            "most_used_parts": most_used_parts,
            "machine_failure_rate": machine_failure_rate,
            "tech_efficiency": tech_efficiency,
            "tool_lost_count": tool_lost_count,
            "tool_loss_rate_pct": tool_loss_rate_pct,
            "supplier_cost_ranking": supplier_cost_ranking,
            "downtime_trends": downtime_trends,
        },
    )


@login_required
@role_required(User.Role.MANAGER, User.Role.SUPER_ADMIN)
def repair_manager_accept(request, pk):
    rwo = get_object_or_404(ExternalRepairOrder, pk=pk)
    if rwo.status != ExternalRepairOrder.Status.RETURNED:
        messages.error(request, "Repair must be in Returned status before manager acceptance (UC-20).")
        return redirect("repair_list")
    if request.method == "POST":
        rwo.status = ExternalRepairOrder.Status.CLOSED
        rwo.closed_at = timezone.now()
        rwo.save(update_fields=["status", "closed_at"])
        log_audit(actor=request.user, action="repair_manager_accept", entity="ExternalRepairOrder", object_id=rwo.pk)
        messages.success(request, "Repair verified and closed.")
        return redirect("repair_list")
    return render(request, "maintenance/repair_accept.html", {"rwo": rwo})


@login_required
@require_POST
def attachment_upload(request):
    """Handle file upload for any entity."""
    from .models import Attachment
    entity_type = request.POST.get("entity_type")
    entity_id = request.POST.get("entity_id")
    file = request.FILES.get("file")
    note = request.POST.get("note", "")

    if not entity_type or not entity_id or not file:
        return JsonResponse({"error": "Missing entity_type, entity_id, or file."}, status=400)

    att = Attachment.objects.create(
        entity_type=entity_type,
        entity_id=int(entity_id),
        file=file,
        filename=file.name,
        size_bytes=file.size or 0,
        uploaded_by=request.user,
        note=note,
    )
    return JsonResponse({
        "id": att.pk,
        "filename": att.filename,
        "size": att.size_bytes,
        "uploaded_at": att.uploaded_at.isoformat(),
        "uploaded_by": att.uploaded_by.username if att.uploaded_by else "",
    })


@login_required
def attachment_list(request, entity_type, entity_id):
    """Return JSON list of attachments for an entity."""
    from .models import Attachment
    attachments = Attachment.objects.filter(
        entity_type=entity_type, entity_id=int(entity_id)
    ).select_related("uploaded_by").order_by("-uploaded_at")
    data = [{
        "id": a.pk,
        "filename": a.filename,
        "size": a.size_bytes,
        "mime_type": a.mime_type,
        "uploaded_at": a.uploaded_at.isoformat(),
        "uploaded_by": a.uploaded_by.username if a.uploaded_by else "",
        "note": a.note,
        "url": a.file.url if a.file else "",
    } for a in attachments]
    return JsonResponse({"attachments": data})


@login_required
@require_POST
def attachment_delete(request, pk):
    """Delete an attachment (owner or admin only)."""
    from .models import Attachment
    att = get_object_or_404(Attachment, pk=pk)
    if att.uploaded_by != request.user and not request.user.is_super_admin_role():
        return JsonResponse({"error": "Not authorized."}, status=403)
    att.delete()
    return JsonResponse({"status": "deleted"})


@login_required
@require_POST
def issue_archive(request, pk):
    issue = get_object_or_404(MaintenanceIssue, pk=pk)
    if issue.is_archived:
        messages.error(request, "Issue is already archived.")
        return redirect("issue_list")
    if issue.status == MaintenanceIssue.Status.CONVERTED:
        messages.error(request, "Cannot archive a converted issue. Archive the linked work order instead.")
        return redirect("issue_list")
    archive_maintenance_issue(issue, request.user)
    messages.success(request, f"Issue #{issue.pk} has been archived.")
    return redirect("issue_list")


@login_required
@require_POST
def work_order_archive(request, pk):
    wo = get_object_or_404(WorkOrder, pk=pk)
    if wo.is_archived:
        messages.error(request, "Work order is already archived.")
        return redirect("work_order_list")
    archive_work_order(wo, request.user)
    messages.success(request, f"Work order WO-{wo.number} has been archived.")
    return redirect("work_order_list")
