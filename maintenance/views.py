from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Case, Count, F, IntegerField, Max, OuterRef, Q, Subquery, Sum, Value, When
from django.db import transaction
from django.db.models.functions import Coalesce
from django.http import Http404
from django.http import HttpResponse, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST
from datetime import timedelta
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from datetime import timedelta as td
import json
from decimal import Decimal

STALE_THRESHOLDS = {
    "critical": td(hours=1),
    "high": td(hours=4),
    "medium": td(hours=8),
    "low": td(hours=24),
}

from accounts.capabilities import get_mms_capabilities
from accounts.models import User
from accounts.permissions import role_required
from inventory.forms import (
    ConsumableUseForm,
    IssuePartForm,
    PartRequestDecisionForm,
    PartRequestForm,
    SparePartCreateForm,
    StockInForm,
)
from inventory.models import Inventory, PartIssueLine, PartShortageReport, SparePart, StockMovement

from inventory.qr_utils import qr_scan_decode as decode_qr
from inventory.services import (
    approve_part_request,
    consumable_use,
    edit_part_request_qty,
    issue_part_to_work_order,
    reject_part_request,
    request_part_on_wo,
    stock_in,
)
from procurement.models import PurchaseRequest, Supplier
from procurement.forms import SupplierForm

from .forms import (
    AssignTechnicianForm,
    EmergencyWOForm,
    ExternalRepairForm,
    ExternalRepairOfficerForm,
    ExternalRepairRequestDecisionForm,
    ExternalRepairRequestForm,
    IssueReportForm,
    MachineForm,
    PMScheduleForm,
    QuickLogForm,
    RepairManagerAcceptForm,
    TechVendorNoteForm,
    ToolAssignForm,
    ToolForm,
    ToolReturnForm,
    ValidateIssueForm,
    WorkOrderCompleteForm,
    WorkOrderPauseForm,
)
from .models import (
    Attachment,
    Incident,
    AuditEntry,
    ExternalRepairOrder,
    ExternalRepairRequest,
    Machine,
    MaintenanceIssue,
    Notification,
    PMSchedule,
    QuickMaintenanceLog,
    Site,
    Tool,
    ToolAssignment,
    WorkOrder,
    WorkOrderAssignmentHistory,
    WorkOrderCost,
)
from .services import (
    approve_external_repair_request,
    archive_maintenance_issue,
    archive_work_order,
    escalate_issue_to_emergency,
    get_other_active_work_order,
    has_active_emergency,
    log_audit,
    manager_close_work_order,
    reject_external_repair_request,
    request_external_repair,
    technician_mark_pending_parts,
    technician_mark_waiting_vendor,
    technician_start_work,
    technician_stats,
    technician_submit_for_review,
    transition_work_order,
    validate_issue,
    work_order_pause as wo_pause_service,
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
        When(lifecycle_status=WorkOrder.LifecycleStatus.IN_PROGRESS, then=Value(0)),
        When(is_emergency=True, then=Value(1)),
        When(issue__priority=MaintenanceIssue.Priority.CRITICAL, then=Value(2)),
        When(issue__priority=MaintenanceIssue.Priority.HIGH, then=Value(3)),
        When(issue__priority=MaintenanceIssue.Priority.MEDIUM, then=Value(4)),
        When(issue__priority=MaintenanceIssue.Priority.LOW, then=Value(5)),
        When(lifecycle_status=WorkOrder.LifecycleStatus.ASSIGNED, then=Value(6)),
        When(operational_status=WorkOrder.OperationalStatus.PAUSED, then=Value(7)),
        When(operational_status=WorkOrder.OperationalStatus.PENDING_PARTS, then=Value(8)),
        When(operational_status=WorkOrder.OperationalStatus.WAITING_VENDOR, then=Value(9)),
        When(lifecycle_status=WorkOrder.LifecycleStatus.PENDING_REVIEW, then=Value(10)),
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
        When(lifecycle_status=WorkOrder.LifecycleStatus.IN_PROGRESS, then=Value(0)),
        When(lifecycle_status=WorkOrder.LifecycleStatus.ASSIGNED, then=Value(1)),
        When(operational_status=WorkOrder.OperationalStatus.PAUSED, then=Value(2)),
        When(operational_status=WorkOrder.OperationalStatus.PENDING_PARTS, then=Value(3)),
        When(operational_status=WorkOrder.OperationalStatus.WAITING_VENDOR, then=Value(4)),
        When(lifecycle_status=WorkOrder.LifecycleStatus.PENDING_REVIEW, then=Value(5)),
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
@role_required(User.Role.MANAGER, User.Role.PROCUREMENT, User.Role.SUPER_ADMIN)
def reorder_suggestions(request):
    """Parts below minimum stock with suggested reorder quantities."""
    from decimal import Decimal
    from maintenance.models import Site
    site = Site.objects.filter(is_default=True).first()
    low = SparePart.objects.filter(status="active", min_stock_level__gt=0).annotate(
        effective_qty=Coalesce(
            Subquery(
                Inventory.objects.filter(part=OuterRef("pk"), site=site)
                .values("quantity_available")[:1]
            ),
            Value(Decimal("0")),
        ),
    ).filter(Q(effective_qty=0) | Q(effective_qty__lt=F("min_stock_level"))).order_by("sku")

    for p in low:
        p.suggested_qty = max(p.min_stock_level * 2 - p.effective_qty, 1)
        if p.max_stock_level:
            p.suggested_qty = min(p.suggested_qty, p.max_stock_level - p.effective_qty)
            if p.suggested_qty < 1:
                p.suggested_qty = 1

    return render(request, "maintenance/reorder_suggestions.html", {
        "parts": low,
        "count": low.count(),
    })


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
        "supply_pending_prs": None,
        "supply_low_stock": None,
        "supply_draft_eros": None,
        "supply_returned_eros": None,
        "supply_open_vendor_orders": None,
        "supply_monthly_cost": None,
        "notif_feed": Notification.objects.filter(recipient=request.user).order_by("-created_at")[:8],
    }
    if caps.get("view_issues"):
        ctx["open_issues"] = MaintenanceIssue.objects.filter(status=MaintenanceIssue.Status.NEW).count()
    if caps.get("view_work_orders"):
        ctx["open_wos"] = WorkOrder.objects.exclude(lifecycle_status=WorkOrder.LifecycleStatus.CLOSED).count()
        ctx["emergency_open"] = WorkOrder.objects.filter(is_emergency=True).exclude(
            lifecycle_status=WorkOrder.LifecycleStatus.CLOSED
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
    if role == User.Role.SUPERVISOR:
        ctx["issues_pending_validation"] = MaintenanceIssue.objects.filter(
            status=MaintenanceIssue.Status.NEW,
        ).order_by("created_at")[:20]
    if role in (User.Role.TECHNICIAN,):
        ctx["my_queue"] = (
            WorkOrder.objects.filter(assigned_technician=request.user)
            .exclude(lifecycle_status=WorkOrder.LifecycleStatus.CLOSED)
            .annotate(queue_rank=_queue_priority_and_status_rank())
            .order_by("queue_rank", "created_at")[:20]
        )
    if caps.get("close_or_review_wo"):
        ctx["pending_review"] = WorkOrder.objects.filter(lifecycle_status=WorkOrder.LifecycleStatus.PENDING_REVIEW).count()
    if caps.get("view_procurement_requests"):
        ctx["pending_procurement"] = PurchaseRequest.objects.filter(status=PurchaseRequest.Status.PENDING).count()

    # Supply Officer KPIs and queue.
    if role == User.Role.PROCUREMENT:
        from datetime import datetime
        from maintenance.models import Site
        ctx["supply_pending_prs"] = PurchaseRequest.objects.filter(status=PurchaseRequest.Status.PENDING).count()
        ctx["supply_draft_eros"] = ExternalRepairOrder.objects.filter(status=ExternalRepairOrder.Status.DRAFT).count()
        ctx["supply_returned_eros"] = ExternalRepairOrder.objects.filter(status=ExternalRepairOrder.Status.RETURNED).count()
        ctx["supply_open_vendor_orders"] = ExternalRepairOrder.objects.filter(
            status=ExternalRepairOrder.Status.SENT_TO_VENDOR
        ).count()
        ctx["supply_monthly_cost"] = StockMovement.objects.filter(
            movement_type=StockMovement.MovementType.STOCK_IN,
            created_at__month=datetime.now().month,
            created_at__year=datetime.now().year,
        ).aggregate(total=Sum(F("unit_cost") * F("quantity")))["total"] or 0
        # Low-stock count using same logic as is_low_stock()
        from decimal import Decimal
        from maintenance.models import Site
        default_site = Site.objects.filter(is_default=True).first()
        if default_site:
            low_stock_count = SparePart.objects.filter(status="active", min_stock_level__gt=0).annotate(
                effective_qty=Coalesce(
                    Subquery(
                        Inventory.objects.filter(part=OuterRef("pk"), site=default_site)
                        .values("quantity_available")[:1]
                    ),
                    Value(Decimal("0")),
                ),
            ).filter(
                Q(effective_qty=0) | Q(effective_qty__lte=F("min_stock_level"))
            ).count()
        else:
            low_stock_count = 0
        ctx["supply_low_stock"] = low_stock_count

        # Queue items
        ctx["supply_queue_pr"] = PurchaseRequest.objects.filter(status=PurchaseRequest.Status.PENDING).order_by("-created_at")[:10]
        ctx["supply_queue_ero_draft"] = ExternalRepairOrder.objects.filter(status=ExternalRepairOrder.Status.DRAFT).order_by("-created_at")[:10]
        ctx["supply_queue_ero_sent"] = ExternalRepairOrder.objects.filter(status=ExternalRepairOrder.Status.SENT_TO_VENDOR).order_by("-created_at")[:10]
        ctx["supply_queue_ero_returned"] = ExternalRepairOrder.objects.filter(status=ExternalRepairOrder.Status.RETURNED).order_by("-created_at")[:10]

    # P3.6 — manager/supervisor action counters.
    if caps.get("approve_part_request"):
        from inventory.models import PartIssueLine
        ctx["pending_part_requests"] = PartIssueLine.objects.filter(
            status=PartIssueLine.Status.PENDING
        ).count()
    if caps.get("approve_external_repair_request"):
        ctx["pending_external_repair_requests"] = ExternalRepairRequest.objects.filter(
            status=ExternalRepairRequest.Status.PENDING
        ).count()
    if caps.get("view_work_orders"):
        ctx["paused_wos"] = WorkOrder.objects.filter(
            operational_status=WorkOrder.OperationalStatus.PAUSED
        ).count()
        ctx["overdue_wos"] = WorkOrder.objects.filter(
            lifecycle_status__in=[
                WorkOrder.LifecycleStatus.ASSIGNED, WorkOrder.LifecycleStatus.IN_PROGRESS,
            ],
            created_at__lt=now - timedelta(days=7),
        ).exclude(
            lifecycle_status=WorkOrder.LifecycleStatus.CLOSED,
        ).count()
    if caps.get("view_stock"):
        parts_with_primary = Attachment.objects.filter(
            entity_type="spare_part", is_primary=True
        ).values_list("entity_id", flat=True)
        ctx["missing_image_count"] = SparePart.objects.exclude(
            pk__in=parts_with_primary
        ).filter(status="active").count()
    # P3.6 — technician counters.
    if role == User.Role.TECHNICIAN:
        from inventory.models import PartIssueLine
        ctx["my_pending_requests"] = PartIssueLine.objects.filter(
            work_order__assigned_technician=request.user,
            status=PartIssueLine.Status.PENDING,
        ).count()
        ctx["my_in_progress_wos"] = WorkOrder.objects.filter(
            assigned_technician=request.user,
            lifecycle_status=WorkOrder.LifecycleStatus.IN_PROGRESS,
        ).count()
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
    view_mode = request.GET.get("view", "list")
    context = {"view_mode": view_mode}

    if view_mode == "tree":
        # Fetch all root-level machines (no parent), then the tree is rendered
        # recursively in the template via _asset_tree_browser_node.html
        roots = Machine.objects.filter(parent__isnull=True).order_by("name")
        context["asset_tree"] = roots
    else:
        machines = (
            Machine.objects.annotate(
                issue_count=Count("issues", distinct=True),
                open_work_orders=Count(
                    "work_orders",
                    filter=~Q(work_orders__lifecycle_status=WorkOrder.LifecycleStatus.CLOSED),
                    distinct=True,
                ),
            )
            .order_by("name")
        )
        context["machines"] = machines

    return render(request, "maintenance/machine_list.html", context)


@login_required
@role_required(User.Role.MANAGER, User.Role.SUPER_ADMIN)
def machine_create(request):
    # Read pre-fill hints from GET params (when first loading the form)
    initial = {}
    preselected_parent = None
    page_title = "Add machine"

    if request.method == "GET":
        parent_id = request.GET.get("parent")
        asset_level = request.GET.get("asset_level")
        if parent_id:
            try:
                preselected_parent = Machine.objects.get(pk=int(parent_id))
                initial["parent"] = preselected_parent.pk
                if asset_level:
                    try:
                        initial["asset_level"] = int(asset_level)
                    except (ValueError, TypeError):
                        pass
                # Default asset_level based on parent's level if not specified
                if not asset_level:
                    if preselected_parent.asset_level == 3:
                        initial["asset_level"] = 4  # default to Subassembly under Machine
                    elif preselected_parent.asset_level == 4:
                        initial["asset_level"] = 5  # default to Component under Subassembly
                    else:
                        initial["asset_level"] = 3  # default to Machine
                # Set page title based on context
                level_label = {1: "Area", 2: "Production Line", 3: "Machine", 4: "Subassembly", 5: "Component"}.get(initial["asset_level"], "Asset")
                page_title = f"Add {level_label.lower()} under {preselected_parent.name}"
            except (Machine.DoesNotExist, ValueError):
                pass
        elif asset_level:
            try:
                initial["asset_level"] = int(asset_level)
                level_label = {1: "Area", 2: "Production Line", 3: "Machine", 4: "Subassembly", 5: "Component"}.get(initial["asset_level"], "Asset")
                page_title = f"Add {level_label.lower()}"
            except (ValueError, TypeError):
                pass

    if request.method == "POST":
        form = MachineForm(request.POST)
        if form.is_valid():
            machine = form.save()
            log_audit(actor=request.user, action="machine_created", entity="Machine", object_id=machine.pk)
            messages.success(request, f"{machine.name} saved.")
            return redirect("machine_detail", pk=machine.pk)
    else:
        form = MachineForm(initial=initial)

    return render(request, "maintenance/machine_form.html", {
        "form": form,
        "page_title": page_title,
        "preselected_parent": preselected_parent,
    })


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
@role_required(User.Role.OPERATOR, User.Role.SUPERVISOR, User.Role.MANAGER, User.Role.PROCUREMENT, User.Role.SUPER_ADMIN)
def machine_detail(request, pk):
    """Asset detail page — handles Machine (level 3), Subassembly (level 4), and Component (level 5).

    The page shows the asset's full info plus:
    - Tree sidebar (rendered by template tag)
    - Big action buttons to create PM, ERO, PR, Issue, WO
    - 'Related records' panel listing the records attached to this asset
    """
    machine = get_object_or_404(
        Machine.objects.select_related("parent", "site", "failure_category"),
        pk=pk,
    )

    # Compute the vertical chain (ancestors) for breadcrumbs
    ancestors = []
    current = machine.parent
    while current is not None:
        ancestors.insert(0, current)
        current = current.parent

    # Related records attached to this asset (or its root machine, for subassembly/component)
    # Per CONTEXT.md, all 5 record types (Issue, WO, PM, ERO, PR) can be filed at any level
    from maintenance.models import (
        MaintenanceIssue, WorkOrder, PMSchedule, ExternalRepairOrder,
    )
    from procurement.models import PurchaseRequest

    # For subassembly/component, "this asset" = this asset
    # For machine, "this asset" = this machine (records can be filed directly against the machine)
    # In all cases, both machine=this and component=this records are relevant
    related_issues = MaintenanceIssue.objects.filter(
        Q(machine=machine) | Q(component=machine)
    ).select_related("reported_by", "machine", "component").order_by("-created_at")[:20]

    related_wos = WorkOrder.objects.filter(
        Q(machine=machine) | Q(component=machine)
    ).select_related("machine", "component", "assigned_technician").order_by("-created_at")[:20]

    related_pms = PMSchedule.objects.filter(
        Q(machine=machine) | Q(component=machine)
    ).select_related("machine", "component").order_by("-created_at")[:20]

    related_eros = ExternalRepairOrder.objects.filter(
        Q(machine=machine) | Q(component=machine)
    ).select_related("machine", "component").order_by("-created_at")[:20]

    related_prs = PurchaseRequest.objects.filter(
        Q(machine=machine) | Q(component=machine)
    ).select_related("machine", "component", "part", "supplier").order_by("-created_at")[:20]

    context = {
        "machine": machine,
        "ancestors": ancestors,
        "related_issues": related_issues,
        "related_wos": related_wos,
        "related_pms": related_pms,
        "related_eros": related_eros,
        "related_prs": related_prs,
        "is_machine_level": machine.asset_level == 3,
        "is_subassembly_level": machine.asset_level == 4,
        "is_component_level": machine.asset_level == 5,
    }
    if machine is not None:
        context["attachments"] = Attachment.objects.filter(
            entity_type="machine", entity_id=machine.pk
        ).select_related("uploaded_by").order_by("-uploaded_at")
    return render(request, "maintenance/machine_detail.html", context)


@login_required
@role_required(User.Role.OPERATOR, User.Role.MANAGER, User.Role.SUPERVISOR, User.Role.SUPER_ADMIN)
def issue_list(request):
    qs = MaintenanceIssue.objects.select_related("machine", "reported_by")
    if request.user.role == User.Role.OPERATOR and not request.user.is_super_admin_role():
        qs = qs.filter(reported_by=request.user)
    return render(request, "maintenance/issue_list.html", {"issues": qs[:200]})


@login_required
@role_required(User.Role.OPERATOR, User.Role.MANAGER, User.Role.SUPERVISOR, User.Role.SUPER_ADMIN)
def issue_detail(request, pk):
    """Operator issue detail page — read only."""
    issue = get_object_or_404(
        MaintenanceIssue.objects.select_related("machine", "component", "reported_by", "validated_by"),
        pk=pk,
    )
    if request.user.role == User.Role.OPERATOR and not request.user.is_super_admin_role():
        if issue.reported_by_id != request.user.id:
            raise Http404()

    # Compute the vertical chain (ancestors) for the asset tree
    ancestors = []
    current = issue.component if issue.component_id else issue.machine
    if current is not None:
        parent = current.parent
        while parent is not None:
            ancestors.insert(0, parent)
            parent = parent.parent

    # The "current" node for the tree highlight is the issue's component (or machine if no component)
    tree_node = issue.component if issue.component_id else issue.machine

    return render(request, "maintenance/issue_detail.html", {
        "issue": issue,
        "machine": tree_node,
        "ancestors": ancestors,
        "related_issues": MaintenanceIssue.objects.filter(
            machine=issue.machine, component=issue.component
        ).exclude(pk=issue.pk)[:10],
        "related_wos": WorkOrder.objects.filter(
            machine=issue.machine, component=issue.component
        )[:10],
        "related_pms": PMSchedule.objects.filter(
            machine=issue.machine, component=issue.component
        )[:10],
        "related_eros": ExternalRepairOrder.objects.filter(
            machine=issue.machine, component=issue.component
        )[:10],
        "related_prs": PurchaseRequest.objects.filter(
            machine=issue.machine, component=issue.component
        )[:10],
    })


@login_required
def failure_modes_by_category(request):
    """API endpoint: return failure modes for a given category_id. Used for cascading dropdown."""
    category_id = request.GET.get("category_id", "")
    modes = []
    if category_id:
        from maintenance.models import FailureMode
        qs = FailureMode.objects.filter(is_active=True, category_id=category_id).order_by("code")
        modes = [{"id": fm.pk, "code": fm.code, "name": fm.name} for fm in qs]
    return JsonResponse({"modes": modes})


@login_required
@role_required(User.Role.OPERATOR, User.Role.SUPERVISOR, User.Role.MANAGER, User.Role.SUPER_ADMIN)
def issue_create(request):
    qr = request.GET.get("qr", "").strip()
    matched_machine = Machine.objects.filter(qr_code=qr).first() if qr else None
    locked_asset = None
    if request.method == "POST":
        form = IssueReportForm(request.POST)
        if form.is_valid():
            issue = form.save(commit=False)
            issue.reported_by = request.user
            # P3.3: if operator flagged as emergency, auto-set priority
            # to CRITICAL (form's clean() already did this; save it now).
            if issue.is_emergency and not issue.priority:
                issue.priority = MaintenanceIssue.Priority.CRITICAL
            issue.save()
            log_audit(
                actor=request.user, action="issue_created",
                entity="MaintenanceIssue", object_id=issue.pk,
                payload={"is_emergency": issue.is_emergency},
            )
            from .notifications import notify_new_issue

            notify_new_issue(issue)
            if issue.is_emergency:
                try:
                    from .notifications import notify_emergency_issue_reported
                    notify_emergency_issue_reported(issue)
                except ImportError:
                    pass
                messages.warning(
                    request,
                    "Emergency issue reported. Manager has been paged.",
                )
            else:
                messages.success(request, "Issue reported.")

            files = request.FILES.getlist("issue_photos")
            if files:
                from .models import Attachment
                for f in files:
                    content_type = getattr(f, 'content_type', '') or ''
                    att = Attachment.objects.create(
                        entity_type='maintenance_issue',
                        entity_id=issue.pk,
                        file=f,
                        filename=f.name,
                        size_bytes=f.size or 0,
                        mime_type=content_type,
                        uploaded_by=request.user,
                    )

            # v4.9 B2: link pending voice attachment to this issue
            pending_voice_id = request.POST.get("voice_attachment_id", "").strip()
            if pending_voice_id and pending_voice_id.isdigit():
                try:
                    from .models import Attachment
                    att = Attachment.objects.get(
                        pk=int(pending_voice_id),
                        entity_type='pending_voice',
                        uploaded_by=request.user,
                    )
                    att.entity_type = 'maintenance_issue'
                    att.entity_id = issue.pk
                    att.save(update_fields=['entity_type', 'entity_id'])
                except Attachment.DoesNotExist:
                    pass

            if files:
                return JsonResponse({"redirect_url": reverse("issue_list")})

            return redirect("issue_list")
    else:
        # Pre-fill from URL params. If component is a level-5 Component, walk
        # the parent chain to find the level-3 Machine (since Issue.machine
        # doesn't have a strict level requirement, but we lock at the level-3
        # ancestor for consistency with other forms).
        initial = {}
        if matched_machine:
            initial["machine"] = matched_machine.pk
        machine_param = request.GET.get("machine")
        component_param = request.GET.get("component")
        resolved_machine_id = None
        resolved_component_id = None
        if component_param:
            try:
                comp = Machine.objects.get(pk=int(component_param))
                resolved_component_id = comp.pk
                root_machine = comp.get_ancestor_machines()
                if root_machine:
                    resolved_machine_id = root_machine[0].pk
                elif comp.asset_level == 3:
                    resolved_machine_id = comp.pk
            except (Machine.DoesNotExist, ValueError, TypeError):
                pass
        if machine_param and not resolved_machine_id:
            try:
                m = Machine.objects.get(pk=int(machine_param))
                if m.asset_level == 3:
                    resolved_machine_id = m.pk
                elif m.asset_level == 5:
                    resolved_machine_id = m.pk
                    resolved_component_id = m.pk
            except (Machine.DoesNotExist, ValueError, TypeError):
                pass
        if resolved_machine_id:
            initial["machine"] = resolved_machine_id
        if resolved_component_id:
            initial["component"] = resolved_component_id

        # Determine if the user came from a deep-link (asset page). If so,
        # LOCK the machine + component fields so the user can't accidentally
        # attach the record to a different asset.
        has_deep_link = bool(initial.get("machine") and initial.get("component"))
        lock_asset = has_deep_link
        form = IssueReportForm(initial=initial, lock_asset=lock_asset)

        def _ancestors(node):
            result = []
            current = node.parent
            while current is not None:
                result.insert(0, current.name)
                current = current.parent
            return result

        if has_deep_link and resolved_machine_id:
            target = Machine.objects.filter(pk=resolved_component_id or resolved_machine_id).first()
            if target:
                breadcrumb = _ancestors(target) + [target.name]
                locked_asset = {
                    "machine_pk": resolved_machine_id,
                    "component_pk": resolved_component_id,
                    "breadcrumb": " > ".join(breadcrumb),
                }
    return render(
        request,
        "maintenance/issue_form.html",
        {
            "form": form,
            "qr_value": qr,
            "matched_machine": matched_machine,
            "locked_asset": locked_asset,
            "machine": Machine.objects.filter(pk=resolved_machine_id).first() if resolved_machine_id else Machine.objects.filter(parent__isnull=True, is_active=True).order_by("pk").first(),
            "ancestors": [],
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
@require_POST
@role_required(User.Role.SUPERVISOR, User.Role.MANAGER, User.Role.SUPER_ADMIN)
def issue_escalate(request, pk):
    """P3.3: escalate a NEW or VALIDATED normal issue to emergency.
    Visible to supervisor+manager+super_admin. Idempotent.
    """
    issue = get_object_or_404(MaintenanceIssue, pk=pk)
    if issue.is_emergency:
        messages.info(request, "Issue is already flagged as emergency.")
        return redirect("issue_detail", pk=issue.pk)
    escalate_issue_to_emergency(issue, actor=request.user)
    messages.warning(
        request,
        f"Issue escalated to EMERGENCY (priority CRITICAL). "
        f"Manager has been paged.",
    )
    return redirect("issue_detail", pk=issue.pk)


def _technician_queue_queryset(user):
    """Return the prioritized work-order queryset for a technician's own queue.

    Same data shape as /work-orders/ for technicians — only the user's
    assigned, non-closed WOs, ordered by queue priority + created_at.
    Used by both the technician view of /work-orders/ and the dedicated
    /work-orders/my/ URL.
    """
    return (
        WorkOrder.objects.select_related("machine", "assigned_technician", "issue")
        .filter(assigned_technician=user)
        .exclude(lifecycle_status=WorkOrder.LifecycleStatus.CLOSED)
        .annotate(queue_rank=_queue_priority_and_status_rank())
        .order_by("queue_rank", "created_at")
    )


@login_required
@role_required(User.Role.MANAGER, User.Role.TECHNICIAN, User.Role.SUPERVISOR, User.Role.SUPER_ADMIN)
def work_order_list(request):
    wos = WorkOrder.objects.select_related("machine", "assigned_technician", "issue").annotate(
        queue_rank=_queue_priority_and_status_rank(),
    )
    if request.user.role == User.Role.TECHNICIAN and not request.user.is_super_admin_role():
        wos = _technician_queue_queryset(request.user)
    st = request.GET.get("status")
    if st in dict(WorkOrder.LifecycleStatus.choices):
        wos = wos.filter(lifecycle_status=st)
    elif st in dict(WorkOrder.OperationalStatus.choices):
        wos = wos.filter(operational_status=st)
    if request.GET.get("overdue") == "1":
        seven_days_ago = timezone.now() - timedelta(days=7)
        wos = wos.filter(
            created_at__lt=seven_days_ago,
        ).exclude(
            lifecycle_status=WorkOrder.LifecycleStatus.CLOSED,
        )
    if request.GET.get("has_pending_part") == "1":
        from inventory.models import PartIssueLine
        wos = wos.filter(part_issues__status=PartIssueLine.Status.PENDING).distinct()
    # v4.8: filter by shortage status (any report in that state on the WO)
    shortage_status = request.GET.get("shortage_status")
    if shortage_status and shortage_status in dict(PartShortageReport.Status.choices):
        wos = wos.filter(shortage_reports__status=shortage_status).distinct()
    # v4.8: WOs that have any shortage report
    if request.GET.get("has_pending_shortage") == "1":
        wos = wos.filter(shortage_reports__isnull=False).distinct()
    wos = wos.order_by("queue_rank", "created_at")[:400]
    return render(
        request,
        "maintenance/workorder_list.html",
        {"work_orders": wos, "status_filter": st or "", "status_choices": WorkOrder.LifecycleStatus.choices, "operational_status_choices": WorkOrder.OperationalStatus.choices},
    )


@login_required
@role_required(User.Role.TECHNICIAN, User.Role.SUPER_ADMIN)
def my_work_orders(request):
    """Technician's personal queue (/work-orders/my/).

    Same data as /work-orders/ for technicians, but with a clearer
    page title, an "open WO" badge, and a back-to-dashboard link.
    Super admins may also view the page (for support purposes).
    """
    wos = list(_technician_queue_queryset(request.user)[:200])
    in_progress_count = sum(1 for wo in wos if wo.lifecycle_status == WorkOrder.LifecycleStatus.IN_PROGRESS)
    return render(
        request,
        "maintenance/my_workorders.html",
        {
            "work_orders": wos,
            "in_progress_count": in_progress_count,
            "queue_total": len(wos),
        },
    )


@login_required
@role_required(User.Role.MANAGER, User.Role.TECHNICIAN, User.Role.SUPERVISOR, User.Role.SUPER_ADMIN)
def work_order_detail(request, pk):
    # Sprint 1 (Step 6): pop the stashed result from the part-request redirect
    # so the alert block in the template can show the outcome.
    last_request_result = request.session.pop("wo_last_request_result", None)

    wo = get_object_or_404(
        WorkOrder.objects.select_related("machine", "assigned_technician", "issue"),
        pk=pk,
    )
    if request.user.role == User.Role.TECHNICIAN and wo.assigned_technician_id != request.user.id:
        raise Http404()
    part_issues = wo.part_issues.select_related("part", "issued_by", "requested_by", "approved_by")
    pending_part_requests = part_issues.filter(status=PartIssueLine.Status.PENDING)
    # v4.9 A5: refused part requests (for tech re-review panel)
    refused_part_requests = part_issues.filter(status=PartIssueLine.Status.REJECTED)
    if request.user.role not in (User.Role.MANAGER, User.Role.SUPER_ADMIN):
        refused_part_requests = refused_part_requests.filter(requested_by=request.user)
    external_repair_requests = wo.external_repair_requests.select_related(
        "requested_by", "reviewed_by", "repair_order"
    )
    pending_external_repair_requests = external_repair_requests.filter(
        status=ExternalRepairRequest.Status.PENDING
    )
    logs = wo.state_logs.select_related("actor")[:50]
    issue_attachments = []
    if wo.issue_id:
        issue_attachments = Attachment.objects.filter(
            entity_type='maintenance_issue',
            entity_id=wo.issue_id
        ).select_related('uploaded_by')[:10]
    assign_form = AssignTechnicianForm()
    complete_form = WorkOrderCompleteForm(instance=wo)
    issue_part_form = IssuePartForm()
    part_request_form = PartRequestForm()
    part_decision_form = PartRequestDecisionForm()
    external_repair_request_form = ExternalRepairRequestForm()
    external_repair_decision_form = ExternalRepairRequestDecisionForm()

    # P3.1 P1.5: last received supplier price per part — shown as a
    # hint under the Part select in the request form so the technician
    # knows roughly what the manager will see in the procurement note.
    # SQLite doesn't support DISTINCT ON, so we do the dedupe in Python.
    last_prices_by_part = {}
    last_movements = (
        StockMovement.objects.filter(
            movement_type=StockMovement.MovementType.STOCK_IN,
            unit_cost__gt=0,
        )
        .order_by("part_id", "-created_at")
        .values("part_id", "unit_cost", "supplier_name", "created_at")
    )
    for m in last_movements:
        pid = str(m["part_id"])
        if pid in last_prices_by_part:
            continue  # already have a more-recent one for this part
        quantized = Decimal(m["unit_cost"]).quantize(Decimal("0.001"))
        last_prices_by_part[pid] = {
            "unit_cost": str(quantized),
            "supplier_name": m["supplier_name"] or "—",
            "date": m["created_at"].strftime("%Y-%m-%d"),
        }
    last_prices_json = json.dumps(last_prices_by_part)
    linked_prs = wo.purchase_requests.select_related("part", "supplier")[:25]
    active_conflict = None
    emergency_blocks_resume = False
    if request.user.role == User.Role.TECHNICIAN and wo.assigned_technician_id == request.user.id:
        active_conflict = get_other_active_work_order(request.user, except_pk=wo.pk)
        # True when another emergency WO is IN_PROGRESS for this tech and
        # the current WO is NOT the emergency itself. Used to disable
        # the start/resume button with a clear warning.
        if not wo.is_emergency and has_active_emergency(request.user, except_pk=wo.pk):
            emergency_blocks_resume = True

    # Compute the vertical chain (ancestors) for the asset tree widget.
    # The "current" node is the WO's component (if set) else the WO's machine.
    # The tree highlights that node; ancestors are the parent chain above it.
    ancestors = []
    tree_node = wo.component if wo.component_id else wo.machine
    if tree_node is not None:
        parent = tree_node.parent
        while parent is not None:
            ancestors.insert(0, parent)
            parent = parent.parent

    # Related records attached to this WO's asset (machine+component pair).
    # A WO is filed at both levels: it has a machine FK and an optional
    # component FK. Records that share that pair are "related" — they're
    # either earlier WOs on the same machine, PM schedules, etc.
    from django.db.models import Q

    related_issues = MaintenanceIssue.objects.filter(
        Q(machine=wo.machine) & Q(component=wo.component)
    ).select_related("reported_by", "machine", "component").order_by("-created_at")[:10]

    related_wos = WorkOrder.objects.filter(
        Q(machine=wo.machine) & Q(component=wo.component)
    ).select_related("machine", "component", "assigned_technician").order_by("-created_at")[:10]

    related_pms = PMSchedule.objects.filter(
        Q(machine=wo.machine) & Q(component=wo.component)
    ).select_related("machine", "component").order_by("-created_at")[:10]

    related_eros = ExternalRepairOrder.objects.filter(
        Q(machine=wo.machine) & Q(component=wo.component)
    ).select_related("machine", "component").order_by("-created_at")[:10]

    related_prs = PurchaseRequest.objects.filter(
        Q(machine=wo.machine) & Q(component=wo.component)
    ).select_related("machine", "component", "part", "supplier").order_by("-created_at")[:10]

    # Sprint 1 (Step 6): shortage reports + components for the new template sections.
    pending_shortage_reports = PartShortageReport.objects.filter(
        work_order=wo, status=PartShortageReport.Status.PENDING_REVIEW,
    ).select_related("part", "reported_by").order_by("-created_at")
    approved_shortage_reports = PartShortageReport.objects.filter(
        work_order=wo, status=PartShortageReport.Status.APPROVED,
    ).select_related("part", "reported_by", "reviewed_by").order_by("-reviewed_at")

    # v4.8: All decided shortage reports (approved, in_fulfillment, fulfilled,
    # blocked, closed, rejected) for the "Decisions Recorded" panel.
    from inventory.models import PartShortageDecision
    decided_shortage_reports = (
        PartShortageReport.objects
        .filter(work_order=wo)
        .exclude(status=PartShortageReport.Status.PENDING_REVIEW)
        .select_related("part", "decision", "reported_by", "reviewed_by")
        .order_by("-reviewed_at")
    )

    # v4.8: Pending Warehouse Issue items — approved reports with an active
    # decision where approved_issue_qty > qty_issued.
    pending_warehouse_issues = []
    for r in approved_shortage_reports:
        if not hasattr(r, "decision") or r.decision is None:
            continue
        if r.decision.decision_type != "approve":
            continue
        if r.decision.approved_issue_qty <= 0:
            continue
        remaining = r.decision.approved_issue_qty - r.qty_issued
        if remaining > 0:
            pending_warehouse_issues.append({
                "report": r,
                "decision": r.decision,
                "remaining_to_issue": remaining,
            })

    active_parts = SparePart.objects.filter(status="active").order_by("name")

    # Phase 3A: Health card + active blockers + blocker history.
    # The HealthCard is a frozen dataclass produced by WorkOrderHealthService;
    # the blocker querysets are used to render the Active Blockers panel and
    # the Blocker History panel on the WO detail page.
    from maintenance.models import WorkOrderBlocker
    from maintenance.services_wo_health import WorkOrderHealthService

    health_card = WorkOrderHealthService.compute(wo)
    active_blockers = (
        WorkOrderBlocker.objects
        .filter(work_order=wo, status=WorkOrderBlocker.Status.OPEN)
        .select_related("opened_by", "source_work_order", "resolved_by", "cancelled_by", "related_ero")
        .order_by("opened_at")
    )
    blocker_history = (
        WorkOrderBlocker.objects
        .filter(work_order=wo)
        .exclude(status=WorkOrderBlocker.Status.OPEN)
        .select_related("opened_by", "resolved_by", "cancelled_by", "related_ero")
        .prefetch_related("events", "events__actor")
        .order_by("-opened_at")[:20]
    )

    return render(
        request,
        "maintenance/workorder_detail.html",
        {
            "wo": wo,
            "logs": logs,
            "issue_attachments": issue_attachments,
            "part_issues": part_issues,
            "pending_part_requests": pending_part_requests,
            "refused_part_requests": refused_part_requests,
            "external_repair_requests": external_repair_requests,
            "pending_external_repair_requests": pending_external_repair_requests,
            "assign_form": assign_form,
            "complete_form": complete_form,
            "issue_part_form": issue_part_form,
            "part_request_form": part_request_form,
            "part_decision_form": part_decision_form,
            "external_repair_request_form": external_repair_request_form,
            "external_repair_decision_form": external_repair_decision_form,
            "linked_prs": linked_prs,
            "active_conflict": active_conflict,
            "emergency_blocks_resume": emergency_blocks_resume,
            "last_prices_json": last_prices_json,
            "ancestors": ancestors,
            "machine": tree_node,
            "related_issues": related_issues,
            "related_wos": related_wos,
            "related_pms": related_pms,
            "related_eros": related_eros,
            "related_prs": related_prs,
            "pending_shortage_reports": pending_shortage_reports,
            "approved_shortage_reports": approved_shortage_reports,
            "decided_shortage_reports": decided_shortage_reports,
            "pending_warehouse_issues": pending_warehouse_issues,
            "active_parts": active_parts,
            "last_request_result": last_request_result,
            # Phase 3A additions (health card + blocker panels)
            "health_card": health_card,
            "active_blockers": active_blockers,
            "blocker_history": blocker_history,
        },
    )


@login_required
@role_required(User.Role.MANAGER, User.Role.SUPER_ADMIN)
def work_order_create_from_issue(request, issue_pk):
    issue = get_object_or_404(
        MaintenanceIssue.objects.select_related("machine", "component", "reported_by", "validated_by"),
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
        component=issue.component,
        lifecycle_status=WorkOrder.LifecycleStatus.ASSIGNED,
        created_by=request.user,
        # P3.3: if the issue is flagged as emergency, the WO inherits it.
        is_emergency=bool(issue.is_emergency),
    )
    # v4.9 A7: carry over all issue attachments (photos, voice, PDFs) to the new WO
    from .models import Attachment
    issue_atts = Attachment.objects.filter(
        entity_type='maintenance_issue',
        entity_id=issue.pk,
    )
    for att in issue_atts:
        att.pk = None
        att.entity_type = 'work_order'
        att.entity_id = wo.pk
        att.uploaded_at = timezone.now()
        att.save()
    issue.status = MaintenanceIssue.Status.CONVERTED
    issue.save(update_fields=["status"])
    transition_work_order(
        wo,
        WorkOrder.LifecycleStatus.ASSIGNED,
        actor=request.user,
        note=f"Created from validated issue #{issue.pk}",
    )
    log_audit(actor=request.user, action="wo_created", entity="WorkOrder", object_id=wo.pk)
    from .notifications import notify_wo_created
    notify_wo_created(wo)
    messages.success(request, f"Work order WO-{wo.number} created.")
    return redirect("work_order_detail", pk=wo.pk)


@login_required
@role_required(User.Role.MANAGER, User.Role.SUPER_ADMIN)
def work_order_assign(request, pk):
    wo = get_object_or_404(WorkOrder, pk=pk)
    previous_technician_id = wo.assigned_technician_id
    if wo.lifecycle_status not in (
        WorkOrder.LifecycleStatus.ASSIGNED,
    ) and wo.operational_status != WorkOrder.OperationalStatus.PAUSED:
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
    transition_work_order(wo, WorkOrder.LifecycleStatus.ASSIGNED, actor=request.user, note="Technician assigned")
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
    if wo.lifecycle_status not in (
        WorkOrder.LifecycleStatus.ASSIGNED,
        WorkOrder.LifecycleStatus.IN_PROGRESS,
    ) and wo.operational_status not in (
        WorkOrder.OperationalStatus.PAUSED,
        WorkOrder.OperationalStatus.PENDING_PARTS,
        WorkOrder.OperationalStatus.WAITING_VENDOR,
    ):
        messages.error(request, "Cannot start work in this state.")
        return redirect("work_order_detail", pk=pk)
    # Emergency precedence check (SRS UC-06 step 2D).
    # A non-emergency WO cannot be transitioned to IN_PROGRESS while
    # another emergency WO is already IN_PROGRESS for the same technician.
    # Starting an emergency itself is always allowed.
    if not wo.is_emergency and has_active_emergency(request.user, except_pk=wo.pk):
        messages.error(
            request,
            "You have an active emergency work order. Finish it before starting another task.",
        )
        return redirect("work_order_detail", pk=wo.pk)
    conflicting_wo = get_other_active_work_order(request.user, except_pk=wo.pk)
    if conflicting_wo and wo.lifecycle_status != WorkOrder.LifecycleStatus.IN_PROGRESS and request.POST.get("confirm_switch") != "1":
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
    if wo.assigned_technician_id != request.user.id and not request.user.is_super_admin_role():
        raise Http404()
    if wo.assigned_technician != request.user:
        messages.error(request, "You can only pause work orders assigned to you.")
        return redirect("work_order_detail", pk=wo.pk)
    if wo.lifecycle_status != WorkOrder.LifecycleStatus.IN_PROGRESS:
        messages.error(request, "Not in progress.")
        return redirect("work_order_detail", pk=wo.pk)
    form = WorkOrderPauseForm(request.POST)
    if not form.is_valid():
        for err in form.non_field_errors():
            messages.error(request, err)
        for field, errs in form.errors.items():
            for err in errs:
                messages.error(request, f"{field}: {err}")
        return redirect("work_order_detail", pk=wo.pk)
    wo_pause_service(
        wo=wo,
        pause_reason=form.cleaned_data["pause_reason"],
        pause_note=(form.cleaned_data.get("pause_note") or "").strip(),
        actor=request.user,
    )
    messages.info(request, "Paused.")
    return redirect("work_order_detail", pk=wo.pk)


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
    if wo.lifecycle_status != WorkOrder.LifecycleStatus.IN_PROGRESS:
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
    if wo.lifecycle_status != WorkOrder.LifecycleStatus.PENDING_REVIEW:
        messages.error(request, "Work order is not pending review.")
        return redirect("work_order_detail", pk=pk)
    if request.method != "POST":
        return redirect("work_order_detail", pk=pk)
    action = request.POST.get("action")
    rejection_reason = request.POST.get("rejection_reason", "").strip()
    if action != "approve" and not rejection_reason:
        messages.error(request, "Rejection reason is required when returning to technician.")
        return redirect("work_order_detail", pk=pk)
    try:
        manager_close_work_order(wo, request.user, approve=(action == "approve"), rejection_reason=rejection_reason)
        if action == "approve":
            messages.success(request, "Work order closed.")
        else:
            messages.info(request, "Work order returned to technician.")
    except ValueError as e:
        messages.error(request, str(e))
        return redirect("work_order_detail", pk=pk)
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


# ---------------------------------------------------------------------------
# Phase 2.1 — Hybrid part request workflow (technician request → manager approval)
# ---------------------------------------------------------------------------


@login_required
@require_POST
@role_required(User.Role.TECHNICIAN, User.Role.SUPER_ADMIN)
def work_order_request_part(request, pk):
    """Technician adds a PENDING part request to their own assigned WO.

    No inventory change. Manager reviews via work_order_approve_part.
    Emergency exception: when wo.is_emergency=True, the request is
    auto-approved and stock deducted immediately.
    """
    wo = get_object_or_404(WorkOrder, pk=pk)
    if wo.assigned_technician_id != request.user.id and not request.user.is_super_admin_role():
        messages.error(request, "You can only request parts on work orders assigned to you.")
        return redirect("work_order_detail", pk=wo.pk)
    if wo.lifecycle_status == WorkOrder.LifecycleStatus.CLOSED:
        messages.error(request, "Cannot request parts on a closed work order.")
        return redirect("work_order_detail", pk=wo.pk)

    form = PartRequestForm(request.POST)
    if not form.is_valid():
        for field, errs in form.errors.items():
            for err in errs:
                messages.error(request, f"{field}: {err}")
        return redirect("work_order_detail", pk=wo.pk)

    try:
        result = request_part_on_wo(
            wo=wo,
            part=form.cleaned_data["part"],
            quantity=form.cleaned_data["quantity"],
            technician=request.user,
            note=form.cleaned_data.get("note") or "",
        )
        line = result["line"]
    except ValueError as e:
        messages.error(request, str(e))
        return redirect("work_order_detail", pk=wo.pk)

    if line.status == PartIssueLine.Status.PENDING:
        messages.success(
            request,
            f"Part request submitted ({form.cleaned_data['quantity']} × "
            f"{form.cleaned_data['part'].name}). Awaiting manager approval.",
        )
    else:
        messages.success(
            request,
            f"Emergency auto-approval: {form.cleaned_data['quantity']} × "
            f"{form.cleaned_data['part'].name} deducted from stock immediately. "
            f"Manager will review.",
        )
    # Sprint 1 (Step 6): stash a JSON-serializable summary so the redirect-target
    # can render an alert block (and the "📦 Raise Shortage Request" button when
    # there's a shortage). The dict is JSON-safe — no model instances.
    request.session["wo_last_request_result"] = {
        "line_id": line.pk,
        "line_quantity": str(line.quantity),
        "part_id": line.part_id,
        "part_name": line.part.name,
        "shortage": result["shortage"],
        "shortage_qty": str(result["shortage_qty"]),
        "usable_qty_snapshot": str(result.get("usable_qty_snapshot", "0")),
        "suggested_action": result["suggested_action"],
    }
    return redirect("work_order_detail", pk=wo.pk)


@login_required
def work_order_request_part_re_review(request, line_pk):
    """v4.9.2 A5: Tech edits a refused part line and submits it for re-review.

    GET → shows an edit form pre-filled with the refused line's part, qty, note.
          Tech can change the part (different spare), adjust qty, or rewrite the note.
    POST → creates a NEW PENDING line with the edited values. The original
           REJECTED line stays intact for audit trail. The new line has
           `previous_attempt` FK pointing to the old one.
    """
    line = get_object_or_404(PartIssueLine, pk=line_pk)
    if line.requested_by_id != request.user.id:
        return HttpResponseForbidden("You can only re-review your own requests.")
    if line.status != PartIssueLine.Status.REJECTED:
        messages.error(request, "Only rejected lines can be re-reviewed.")
        return redirect("work_order_detail", pk=line.work_order_id)

    # Load active parts for the picker (same as part request form)
    active_parts = (
        SparePart.objects.filter(status="active")
        .order_by("name")
        .select_related()
    )

    if request.method != "POST":
        # GET — show the edit form
        form = PartRequestForm(initial={
            "part": line.part_id,
            "quantity": line.quantity,
            "note": line.manager_note or "",
        })
        return render(
            request,
            "maintenance/workorder_part_re_review.html",
            {
                "line": line,
                "form": form,
                "active_parts": active_parts,
            },
        )

    # POST — process the edited re-review
    form = PartRequestForm(request.POST)
    if not form.is_valid():
        for field, errs in form.errors.items():
            for err in errs:
                messages.error(request, f"{field}: {err}")
        return render(
            request,
            "maintenance/workorder_part_re_review.html",
            {
                "line": line,
                "form": form,
                "active_parts": active_parts,
            },
        )

    new_part = form.cleaned_data["part"]
    new_qty = form.cleaned_data["quantity"]
    new_note = form.cleaned_data.get("note") or ""

    # v4.9.3 A5 enhancement: same stock check as the part-request flow.
    # If usable stock < requested, refuse with a clear message. Tech must
    # either lower the qty, switch to a different part, or rely on the
    # shortage flow (raise_shortage_request). When no Inventory row exists
    # at all, the part is treated as out-of-stock (the shortage flow will
    # be raised by the manager after submission).
    from inventory.services import _get_default_site
    site = _get_default_site()
    inv = Inventory.objects.filter(part=new_part, site=site).first() if site else None
    if inv is None:
        on_hand = Decimal("0")
        reserved = Decimal("0")
    else:
        on_hand = inv.quantity_available
        reserved = inv.quantity_reserved
    usable = on_hand - reserved
    if usable < new_qty:
        # Only refuse if SOME stock exists but it's insufficient. If
        # zero stock exists, allow the request — the manager will see
        # a shortage and raise a shortage report.
        if on_hand > 0:
            stock_error = (
                f"Only {usable:g} in stock for {new_part.sku}. "
                f"Edit qty to {usable:g}, switch to a different part, or use the shortage flow."
            )
            messages.error(request, stock_error)
            return render(
                request,
                "maintenance/workorder_part_re_review.html",
                {
                    "line": line,
                    "form": form,
                    "active_parts": active_parts,
                    "stock_error": stock_error,
                },
            )
        # else: zero stock — allow submission; manager handles via shortage flow

    # Compare to original — if nothing changed, still allow (the act of re-reviewing
    # after seeing the reason is meaningful) but mention it.
    unchanged = (new_part.pk == line.part_id and new_qty == line.quantity)

    new_line = PartIssueLine.objects.create(
        work_order=line.work_order,
        part=new_part,
        quantity=new_qty,
        requested_qty=new_qty,
        requested_by=request.user,
        status=PartIssueLine.Status.PENDING,
        manager_note=(
            f"Re-review of #{line.pk}"
            + ("" if unchanged else f" — edited: {line.part.sku} qty {line.quantity} → {new_part.sku} qty {new_qty}")
            + (f". Tech note: {new_note}" if new_note else "")
        ),
        previous_attempt=line,
        unit_cost=new_part.last_purchase_cost or new_part.avg_cost or 0,
        issued_by=request.user,
    )
    from .notifications import notify_part_request_re_review
    notify_part_request_re_review(new_line, line)
    messages.success(
        request,
        f"Re-review submitted: {new_qty:g}× {new_part.name}. "
        f"{'Same as before' if unchanged else 'Edits sent to manager'}.",
    )
    return redirect("work_order_detail", pk=line.work_order_id)


@login_required
@require_POST
def work_order_add_voice(request, pk):
    """v4.9.5: Add a voice note to an existing WO.

    POST-only. The voice is uploaded via /attachments/upload-pending/ which
    creates a pending Attachment. This view re-links it to the WO and
    optionally stores a short note in the attachment's note field.
    """
    wo = get_object_or_404(WorkOrder, pk=pk)
    if request.method != "POST":
        return redirect("work_order_detail", pk=pk)
    pending_voice_id = request.POST.get("voice_attachment_id", "").strip()
    note = request.POST.get("note", "").strip()
    if not (pending_voice_id and pending_voice_id.isdigit()):
        messages.error(request, "Please record a voice note before submitting.")
        return redirect("work_order_detail", pk=pk)
    try:
        from .models import Attachment
        att = Attachment.objects.get(
            pk=int(pending_voice_id),
            entity_type='pending_voice',
            uploaded_by=request.user,
        )
        att.entity_type = 'work_order'
        att.entity_id = wo.pk
        if note:
            att.note = note[:500]
            att.save(update_fields=['entity_type', 'entity_id', 'note'])
        else:
            att.save(update_fields=['entity_type', 'entity_id'])
    except Attachment.DoesNotExist:
        messages.error(request, "Voice attachment not found or not owned by you.")
        return redirect("work_order_detail", pk=pk)
    messages.success(request, f"Voice note added to WO-{wo.number}.")
    return redirect("work_order_detail", pk=pk)


@login_required
@require_POST
@role_required(User.Role.MANAGER, User.Role.SUPER_ADMIN)
def work_order_approve_part(request, pk, line_id):
    """Manager approves a PENDING part request (deducts stock).

    Reject and edit-qty are handled by work_order_decide_part to keep
    decisioning in one place.
    """
    wo = get_object_or_404(WorkOrder, pk=pk)
    line = get_object_or_404(PartIssueLine, pk=line_id, work_order=wo)
    if line.status != PartIssueLine.Status.PENDING:
        messages.error(request, "Only PENDING requests can be approved.")
        return redirect("work_order_detail", pk=wo.pk)
    try:
        approve_part_request(line=line, manager=request.user)
        messages.success(
            request,
            f"Approved {line.quantity} × {line.part.name} — stock deducted.",
        )
    except ValueError as e:
        messages.error(request, str(e))
    return redirect("work_order_detail", pk=wo.pk)


@login_required
@require_POST
@role_required(User.Role.MANAGER, User.Role.SUPER_ADMIN)
def work_order_decide_part(request, pk, line_id):
    """Manager chooses approve / reject / edit on a PENDING request.

    One endpoint to keep decision flow tight. The form's `action` field
    drives the behavior.
    """
    wo = get_object_or_404(WorkOrder, pk=pk)
    line = get_object_or_404(PartIssueLine, pk=line_id, work_order=wo)
    if line.status != PartIssueLine.Status.PENDING:
        messages.error(request, "Only PENDING requests can be decided.")
        return redirect("work_order_detail", pk=wo.pk)

    form = PartRequestDecisionForm(request.POST)
    if not form.is_valid():
        for field, errs in form.errors.items():
            for err in errs:
                messages.error(request, f"{field}: {err}")
        return redirect("work_order_detail", pk=wo.pk)

    action = form.cleaned_data["action"]
    try:
        if action == "approve":
            approve_part_request(line=line, manager=request.user)
            messages.success(
                request,
                f"Approved {line.quantity} × {line.part.name} — stock deducted.",
            )
        elif action == "reject":
            reason = form.cleaned_data["rejection_reason"]
            reject_part_request(
                line=line,
                manager=request.user,
                reason=reason,
            )
            # v4.9.3: notify the tech (and WO creator) so they can re-submit,
            # switch parts, or use the shortage flow.
            from .notifications import notify_wo_part_rejected
            notify_wo_part_rejected(line, reason, request.user)
            messages.info(
                request,
                f"Rejected {line.part.name} request. Tech has been notified.",
            )
        elif action == "edit":
            edit_part_request_qty(
                line=line,
                manager=request.user,
                new_quantity=form.cleaned_data["new_qty"],
            )
            messages.success(
                request,
                f"Updated qty to {form.cleaned_data['new_qty']} for {line.part.name}.",
            )
    except ValueError as e:
        messages.error(request, str(e))
    return redirect("work_order_detail", pk=wo.pk)


# ---------------------------------------------------------------------------
# External Repair Request flow (Phase 2.2)
# Technician requests, manager creates the ERO.
# ---------------------------------------------------------------------------

@login_required
@require_POST
def work_order_request_external_repair(request, pk):
    """Assigned technician submits a PENDING external-repair request.

    The technician cannot create the ERO directly — EROs create vendor
    engagement and financial obligations. The manager reviews the
    request and either approves (which creates a DRAFT ERO on the WO)
    or rejects it with a reason.
    """
    wo = get_object_or_404(WorkOrder, pk=pk)
    if wo.assigned_technician_id != request.user.id:
        raise Http404()
    form = ExternalRepairRequestForm(request.POST)
    if not form.is_valid():
        for field, errs in form.errors.items():
            for err in errs:
                messages.error(request, f"{field}: {err}")
        return redirect("work_order_detail", pk=wo.pk)
    try:
        err = request_external_repair(
            work_order=wo,
            requested_by=request.user,
            diagnosis_note=form.cleaned_data["diagnosis_note"],
            part_description=form.cleaned_data["part_description"],
        )
    except ValueError as e:
        messages.error(request, str(e))
        return redirect("work_order_detail", pk=wo.pk)
    # Notify managers (best-effort; ignore if no notification helper)
    try:
        from .notifications import notify_repair_request_created
        notify_repair_request_created(err)
    except Exception:
        pass
    messages.success(
        request,
        "External repair request submitted. Manager has been notified.",
    )
    return redirect("work_order_detail", pk=wo.pk)


@login_required
@require_POST
@role_required(User.Role.MANAGER, User.Role.SUPER_ADMIN)
def work_order_decide_external_repair(request, pk, err_id):
    """Manager approves or rejects a PENDING external-repair request."""
    wo = get_object_or_404(WorkOrder, pk=pk)
    err = get_object_or_404(ExternalRepairRequest, pk=err_id, work_order=wo)
    if err.status != ExternalRepairRequest.Status.PENDING:
        messages.error(request, "Only PENDING requests can be decided.")
        return redirect("work_order_detail", pk=wo.pk)

    form = ExternalRepairRequestDecisionForm(request.POST)
    if not form.is_valid():
        for field, errs in form.errors.items():
            for err in errs:
                messages.error(request, f"{field}: {err}")
        return redirect("work_order_detail", pk=wo.pk)

    action = form.cleaned_data["action"]
    note = form.cleaned_data.get("manager_note", "")
    try:
        if action == "approve":
            ero = approve_external_repair_request(
                err=err, manager=request.user, manager_note=note
            )
            messages.success(
                request,
                f"External Repair Order #{ero.pk} created. The supply officer "
                "can now send the part to the vendor.",
            )
        else:
            reject_external_repair_request(
                err=err, manager=request.user, manager_note=note
            )
            messages.info(request, "External repair request rejected.")
    except ValueError as e:
        messages.error(request, str(e))
    return redirect("work_order_detail", pk=wo.pk)


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
                component=form.cleaned_data.get("component"),
                lifecycle_status=WorkOrder.LifecycleStatus.ASSIGNED,
                created_by=request.user,
                notes=f"{form.cleaned_data['title']}\n\n{form.cleaned_data['detail']}",
            )
            transition_work_order(wo, WorkOrder.LifecycleStatus.ASSIGNED, actor=request.user, note="Emergency WO created")
            log_audit(actor=request.user, action="emergency_wo", entity="WorkOrder", object_id=wo.pk)
            from .notifications import notify_emergency_work_order

            notify_emergency_work_order(wo)
            messages.success(request, f"Emergency work order WO-{wo.number} created.")
            return redirect("work_order_detail", pk=wo.pk)
    else:
        # Pre-fill from URL params so the asset tree can deep-link here.
        # If the user came from a Component-level page, both machine and
        # component params point to the Component. We need to walk the parent
        # chain to find the level-3 Machine (since WO.machine is required to
        # be level-3 per the asset FK pattern).
        initial = {}
        machine_id = request.GET.get("machine")
        component_id = request.GET.get("component")
        resolved_machine_id = None
        resolved_component_id = None
        if component_id:
            try:
                comp = Machine.objects.get(pk=int(component_id))
                resolved_component_id = comp.pk
                root_machine = comp.get_ancestor_machines()
                if root_machine:
                    resolved_machine_id = root_machine[0].pk
                else:
                    if comp.asset_level == 3:
                        resolved_machine_id = comp.pk
            except (Machine.DoesNotExist, ValueError, TypeError):
                pass
        if machine_id and not resolved_machine_id:
            try:
                m = Machine.objects.get(pk=int(machine_id))
                if m.asset_level == 3:
                    resolved_machine_id = m.pk
                elif m.asset_level == 5:
                    resolved_machine_id = m.pk
                    resolved_component_id = m.pk
            except (Machine.DoesNotExist, ValueError, TypeError):
                pass
        if resolved_machine_id:
            initial["machine"] = resolved_machine_id
        if resolved_component_id:
            initial["component"] = resolved_component_id

        # Determine if the user came from a deep-link (asset page). If so,
        # LOCK the machine + component fields so the user can't accidentally
        # attach the record to a different asset.
        has_deep_link = bool(machine_id and component_id)
        lock_asset = has_deep_link
        form = EmergencyWOForm(initial=initial, lock_asset=lock_asset)

        def _ancestors(node):
            result = []
            current = node.parent
            while current is not None:
                result.insert(0, current.name)
                current = current.parent
            return result

        locked_asset = None
        if has_deep_link and resolved_machine_id:
            target = Machine.objects.filter(pk=resolved_component_id or resolved_machine_id).first()
            if target:
                breadcrumb = _ancestors(target) + [target.name]
                locked_asset = {
                    "machine_pk": resolved_machine_id,
                    "component_pk": resolved_component_id,
                    "breadcrumb": " > ".join(breadcrumb),
                }
    return render(
        request,
        "maintenance/emergency_wo.html",
        {
            "form": form,
            "locked_asset": locked_asset,
            "machine": Machine.objects.filter(pk=resolved_machine_id).first() if resolved_machine_id else Machine.objects.filter(parent__isnull=True, is_active=True).order_by("pk").first(),
            "ancestors": [],
        },
    )


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
    """Site-aware stock dashboard with search, filters, and per-site inventory."""
    from django.db.models import OuterRef, Subquery
    from inventory.models import Inventory, SparePart
    from maintenance.models import Site

    sites = Site.objects.filter(is_active=True).order_by("name")
    selected_site_id = request.GET.get("site")
    selected_site = None
    if selected_site_id:
        try:
            selected_site = sites.get(pk=int(selected_site_id))
        except (ValueError, Site.DoesNotExist):
            selected_site = sites.filter(is_default=True).first()
    if not selected_site:
        selected_site = sites.filter(is_default=True).first()

    parts_qs = SparePart.objects.annotate(
        inv_available=Subquery(
            Inventory.objects.filter(
                part=OuterRef("pk"),
                site=selected_site
            ).values("quantity_available")[:1]
        ),
        inv_reserved=Subquery(
            Inventory.objects.filter(
                part=OuterRef("pk"),
                site=selected_site
            ).values("quantity_reserved")[:1]
        ),
        inv_rack=Subquery(
            Inventory.objects.filter(
                part=OuterRef("pk"),
                site=selected_site
            ).values("rack_location")[:1]
        ),
        inv_pk=Subquery(
            Inventory.objects.filter(
                part=OuterRef("pk"),
                site=selected_site
            ).values("pk")[:1]
        ),
    )

    q = request.GET.get("q", "").strip()
    if q:
        parts_qs = parts_qs.filter(
            Q(sku__icontains=q) | Q(name__icontains=q)
        )

    category = request.GET.get("category", "").strip()
    if category:
        parts_qs = parts_qs.filter(category=category)

    if request.GET.get("low_stock") == "1":
        parts_qs = parts_qs.filter(
            Q(min_stock_level__gt=0)
            & (
                Q(inv_available__isnull=True)
                | Q(inv_available=0)
                | Q(inv_available__lte=models.F("min_stock_level"))
            )
        )

    if request.GET.get("consumable") == "1":
        parts_qs = parts_qs.filter(is_consumable=True)

    if request.GET.get("missing_image") == "1":
        from maintenance.models import Attachment
        parts_with_primary = Attachment.objects.filter(
            entity_type="spare_part", is_primary=True
        ).values_list("entity_id", flat=True)
        parts_qs = parts_qs.exclude(pk__in=parts_with_primary)

    parts_qs = parts_qs.order_by("name")[:500]

    categories = list(
        SparePart.objects.exclude(category="")
        .values_list("category", flat=True)
        .distinct()
        .order_by("category")
    )

    return render(request, "maintenance/stock_dashboard.html", {
        "parts": parts_qs,
        "sites": sites,
        "selected_site": selected_site,
        "categories": categories,
        "q": q,
        "category": category,
        "low_stock_only": request.GET.get("low_stock") == "1",
        "consumable_only": request.GET.get("consumable") == "1",
    })


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
            uploaded_file = request.FILES.get("invoice_attachment")
            if uploaded_file:
                from maintenance.models import Attachment
                Attachment.objects.create(
                    entity_type=Attachment.EntityType.SPARE_PART,
                    entity_id=form.cleaned_data["part"].pk,
                    file=uploaded_file,
                    filename=uploaded_file.name,
                    size_bytes=uploaded_file.size,
                    mime_type=getattr(uploaded_file, "content_type", "") or "",
                    uploaded_by=request.user,
                    note="Invoice attachment from stock-in",
                )
            return redirect("stock_dashboard")
    else:
        form = StockInForm()
    return render(request, "maintenance/stock_in.html", {"form": form})


@login_required
@role_required(User.Role.OPERATOR, User.Role.SUPERVISOR, User.Role.TECHNICIAN, User.Role.MANAGER, User.Role.SUPER_ADMIN)
def consumables_view(request):
    from inventory.forms import IssueConsumableForm
    from inventory.services import issue_consumable
    caps = get_mms_capabilities(request.user)
    consume_form = ConsumableUseForm()
    issue_form = IssueConsumableForm() if caps.get("issue_consumables") else None

    if request.method == "POST":
        action = request.POST.get("action", "")
        if action == "issue" and issue_form:
            issue_form = IssueConsumableForm(request.POST)
            if issue_form.is_valid():
                ok, msg = issue_consumable(
                    part=issue_form.cleaned_data["part"],
                    quantity=issue_form.cleaned_data["quantity"],
                    consumed_by=issue_form.cleaned_data["consumed_by"],
                    issued_by=request.user,
                    note=issue_form.cleaned_data.get("note", ""),
                    machine_id=issue_form.cleaned_data.get("machine_id"),
                )
                (messages.success if ok else messages.error)(request, msg)
                return redirect("consumables")
        elif action == "consume" and caps.get("consume_consumables"):
            consume_form = ConsumableUseForm(request.POST)
            if consume_form.is_valid():
                ok, msg = consumable_use(
                    part=consume_form.cleaned_data["part"],
                    quantity=consume_form.cleaned_data["quantity"],
                    consumed_by=request.user,
                    note="",
                    machine_id=consume_form.cleaned_data.get("machine_id"),
                )
                (messages.success if ok else messages.error)(request, msg)
                return redirect("consumables")
        else:
            messages.error(request, "You do not have permission to perform this action.")
            return redirect("consumables")
    from inventory.models import ConsumableAssignment
    assignments = ConsumableAssignment.objects.filter(
        consumed_by=request.user
    ).select_related("part", "machine").order_by("-created_at")[:20]
    issued_assignments = ConsumableAssignment.objects.none()
    if caps.get("issue_consumables"):
        issued_assignments = ConsumableAssignment.objects.filter(
            issued_by=request.user
        ).exclude(consumed_by=request.user).select_related("part", "machine", "consumed_by").order_by("-created_at")[:20]
    return render(request, "maintenance/consumables.html", {
        "consume_form": consume_form,
        "issue_form": issue_form,
        "assignments": assignments,
        "issued_assignments": issued_assignments,
    })


@login_required
@role_required(User.Role.MANAGER, User.Role.PROCUREMENT, User.Role.SUPER_ADMIN)
def supplier_list(request):
    """Operational supplier list — /stock/suppliers/"""
    suppliers = Supplier.objects.filter(is_active=True).order_by("code")
    q = request.GET.get("q", "").strip()
    if q:
        suppliers = suppliers.filter(
            models.Q(name__icontains=q)
            | models.Q(code__icontains=q)
            | models.Q(contact_person__icontains=q)
            | models.Q(phone__icontains=q)
            | models.Q(email__icontains=q)
        )
    return render(request, "maintenance/supplier_list.html", {"suppliers": suppliers, "q": q})


@login_required
@role_required(User.Role.MANAGER, User.Role.PROCUREMENT, User.Role.SUPER_ADMIN)
def supplier_detail(request, pk):
    """Supplier detail with linked parts and recent PRs."""
    supplier = get_object_or_404(Supplier, pk=pk)
    linked_parts = supplier.parts.order_by("name")[:50]
    recent_prs = supplier.purchase_requests.filter(
        status=PurchaseRequest.Status.PENDING
    ).order_by("-created_at")[:10]
    return render(request, "maintenance/supplier_detail.html", {
        "supplier": supplier,
        "linked_parts": linked_parts,
        "recent_prs": recent_prs,
    })


@login_required
@role_required(User.Role.MANAGER, User.Role.PROCUREMENT, User.Role.SUPER_ADMIN)
def supplier_create(request):
    """Create a new supplier."""
    if request.method == "POST":
        form = SupplierForm(request.POST)
        if form.is_valid():
            supplier = form.save()
            messages.success(request, f"Supplier '{supplier.name}' created with code {supplier.code}.")
            return redirect("supplier_detail", pk=supplier.pk)
    else:
        form = SupplierForm()
    return render(request, "maintenance/supplier_form.html", {
        "form": form,
        "supplier": None,
        "page_heading": "New supplier",
    })


@login_required
@role_required(User.Role.MANAGER, User.Role.PROCUREMENT, User.Role.SUPER_ADMIN)
def supplier_edit(request, pk):
    """Edit an existing supplier."""
    supplier = get_object_or_404(Supplier, pk=pk)
    if request.method == "POST":
        form = SupplierForm(request.POST, instance=supplier)
        if form.is_valid():
            supplier = form.save()
            messages.success(request, f"Supplier '{supplier.name}' updated.")
            return redirect("supplier_detail", pk=supplier.pk)
    else:
        form = SupplierForm(instance=supplier)
    return render(request, "maintenance/supplier_form.html", {
        "form": form,
        "supplier": supplier,
        "page_heading": f"Edit supplier — {supplier.code}",
    })


@login_required
@role_required(User.Role.MANAGER, User.Role.PROCUREMENT, User.Role.TECHNICIAN, User.Role.SUPER_ADMIN)
def spare_part_detail(request, pk):
    """Operational spare part detail — /stock/<pk>/"""
    from inventory.models import Inventory, StockMovement, SparePart as SP
    from maintenance.models import Site

    part = get_object_or_404(SP, pk=pk)

    sites = Site.objects.filter(is_active=True).order_by("name")
    selected_site_id = request.GET.get("site")
    if selected_site_id:
        try:
            selected_site = sites.get(pk=int(selected_site_id))
        except (ValueError, Site.DoesNotExist):
            selected_site = sites.filter(is_default=True).first()
    else:
        selected_site = sites.filter(is_default=True).first()

    inv = None
    if selected_site:
        inv = part.inventory_items.filter(site=selected_site).first()

    movements_qs = StockMovement.objects.filter(part=part).select_related(
        "performed_by", "work_order", "site"
    ).order_by("-created_at")

    recent_wo_ids = movements_qs.filter(
        work_order__isnull=False
    ).values_list("work_order", flat=True).distinct()[:10]
    recent_wos = (
        WorkOrder.objects.filter(pk__in=list(recent_wo_ids))
        .select_related("machine")
        .order_by("-created_at")[:10]
    )

    recent_prs = part.purchase_requests.filter(
        status=PurchaseRequest.Status.PENDING
    ).order_by("-created_at")[:5]

    return render(request, "maintenance/spare_part_detail.html", {
        "part": part,
        "sites": sites,
        "selected_site": selected_site,
        "inv": inv,
        "movements": list(movements_qs[:50]),
        "recent_wos": recent_wos,
        "recent_prs": recent_prs,
    })


@login_required
@role_required(User.Role.MANAGER, User.Role.PROCUREMENT, User.Role.SUPER_ADMIN)
def spare_part_create(request):
    """Create a new spare part from operations — /stock/new/"""
    from decimal import Decimal
    from inventory.models import Inventory, SparePart as SP
    from maintenance.models import Site

    if request.method == "POST":
        form = SparePartCreateForm(request.POST)
        if form.is_valid():
            part = form.save()
            from maintenance.models import Attachment
            uploaded_file = request.FILES.get("part_attachment")
            if uploaded_file:
                Attachment.objects.create(
                    entity_type=Attachment.EntityType.SPARE_PART,
                    entity_id=part.pk,
                    file=uploaded_file,
                    filename=uploaded_file.name,
                    size_bytes=uploaded_file.size,
                    mime_type=getattr(uploaded_file, "content_type", "") or "",
                    uploaded_by=request.user,
                    note="Datasheet / certificate attached during part creation",
                )
            opening_qty = form.cleaned_data.get("opening_qty") or Decimal("0")
            rack = form.cleaned_data.get("rack_location", "").strip()
            if opening_qty > 0:
                default_site = Site.objects.filter(is_default=True).first()
                if default_site:
                    Inventory.objects.create(
                        part=part,
                        site=default_site,
                        quantity_available=opening_qty,
                        rack_location=rack,
                    )
                part.quantity_on_hand = opening_qty
                part.save(update_fields=["quantity_on_hand"])
            messages.success(request, f"Part '{part.name}' created. SKU: {part.sku}")
            return redirect("spare_part_detail", pk=part.pk)
    else:
        form = SparePartCreateForm()

    return render(request, "maintenance/spare_part_form.html", {
        "form": form,
        "part": None,
        "page_heading": "New spare part",
    })


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
    locked_asset = None
    if request.method == "POST":
        form = PMScheduleForm(request.POST)
        if form.is_valid():
            form.save()
            messages.success(request, "PM schedule saved.")
            return redirect("pm_list")
    else:
        # Pre-fill from URL params. If component is a level-5 Component, walk
        # the parent chain to find the level-3 Machine (since PMSchedule.machine
        # is required to be level-3).
        initial = {"next_due_at": timezone.now()}
        machine_param = request.GET.get("machine")
        component_param = request.GET.get("component")
        resolved_machine_id = None
        resolved_component_id = None
        if component_param:
            try:
                comp = Machine.objects.get(pk=int(component_param))
                resolved_component_id = comp.pk
                root_machine = comp.get_ancestor_machines()
                if root_machine:
                    resolved_machine_id = root_machine[0].pk
                elif comp.asset_level == 3:
                    resolved_machine_id = comp.pk
            except (Machine.DoesNotExist, ValueError, TypeError):
                pass
        if machine_param and not resolved_machine_id:
            try:
                m = Machine.objects.get(pk=int(machine_param))
                if m.asset_level == 3:
                    resolved_machine_id = m.pk
                elif m.asset_level == 5:
                    resolved_machine_id = m.pk
                    resolved_component_id = m.pk
            except (Machine.DoesNotExist, ValueError, TypeError):
                pass
        if resolved_machine_id:
            initial["machine"] = resolved_machine_id
        if resolved_component_id:
            initial["component"] = resolved_component_id

        # Determine if the user came from a deep-link (asset page). If so,
        # LOCK the machine + component fields so the user can't accidentally
        # attach the record to a different asset.
        has_deep_link = bool(machine_param and component_param)
        lock_asset = has_deep_link
        form = PMScheduleForm(initial=initial, lock_asset=lock_asset)

        def _ancestors(node):
            result = []
            current = node.parent
            while current is not None:
                result.insert(0, current.name)
                current = current.parent
            return result

        locked_asset = None
        if has_deep_link and resolved_machine_id:
            target = Machine.objects.filter(pk=resolved_component_id or resolved_machine_id).first()
            if target:
                breadcrumb = _ancestors(target) + [target.name]
                locked_asset = {
                    "machine_pk": resolved_machine_id,
                    "component_pk": resolved_component_id,
                    "breadcrumb": " > ".join(breadcrumb),
                }
    return render(
        request,
        "maintenance/pm_form.html",
        {
            "form": form,
            "locked_asset": locked_asset,
            "machine": Machine.objects.filter(pk=resolved_machine_id).first() if resolved_machine_id else Machine.objects.filter(parent__isnull=True, is_active=True).order_by("pk").first(),
            "ancestors": [],
        },
    )


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
                lifecycle_status=WorkOrder.LifecycleStatus.ASSIGNED,
                created_by=request.user,
                notes=f"PM: {sched.title}",
            )
            transition_work_order(wo, WorkOrder.LifecycleStatus.ASSIGNED, actor=request.user, note="PM work order")
            if form.cleaned_data.get("propagate_to_children") and sched.machine.children.exists():
                child_count = 0
                for child_machine in sched.machine.children.all():
                    child_wo = WorkOrder.objects.create(
                        category=WorkOrder.Category.PREVENTIVE,
                        machine=child_machine,
                        lifecycle_status=WorkOrder.LifecycleStatus.ASSIGNED,
                        created_by=request.user,
                        notes=f"PM spawned from {sched.machine.name} → {child_machine.name}",
                    )
                    transition_work_order(child_wo, WorkOrder.LifecycleStatus.ASSIGNED, actor=request.user,
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
@role_required(User.Role.TECHNICIAN, User.Role.MANAGER, User.Role.SUPER_ADMIN)
def pm_execute(request, pk):
    """Execute a PM work order with checklist."""
    wo = get_object_or_404(
        WorkOrder.objects.select_related("machine"),
        pk=pk,
    )
    if wo.category != WorkOrder.Category.PREVENTIVE:
        messages.error(request, "This is not a preventive maintenance work order.")
        return redirect("work_order_detail", pk=pk)

    sched = PMSchedule.objects.filter(machine=wo.machine).order_by("-created_at").first()

    checklist_items = []
    if sched and sched.checklist:
        for line in sched.checklist.strip().split("\n"):
            line = line.strip()
            if line:
                checklist_items.append({"text": line, "checked": False})

    if request.method == "POST":
        form = WorkOrderCompleteForm(request.POST, instance=wo)
        if form.is_valid():
            checklist_results = []
            for i, item in enumerate(checklist_items):
                key = f"checklist_{i}"
                checked = request.POST.get(key) == "on"
                note_key = f"note_{i}"
                note = request.POST.get(note_key, "").strip()
                checklist_results.append({"text": item["text"], "checked": checked, "note": note})

            action_lines = []
            for result in checklist_results:
                status = "✓" if result["checked"] else "✗"
                action_lines.append(f"[{status}] {result['text']}")
                if result["note"]:
                    action_lines.append(f"  Note: {result['note']}")

            wo.action_taken = "\n".join(action_lines)
            wo.save(update_fields=["action_taken"])

            form.save()
            technician_submit_for_review(wo, request.user)
            messages.success(request, "PM submitted for manager review.")
            return redirect("work_order_detail", pk=pk)
    else:
        form = WorkOrderCompleteForm(instance=wo)

    from procurement.models import PurchaseRequest

    tree_node = wo.component if wo.component_id else wo.machine
    ancestors = []
    current = tree_node.parent if tree_node is not None else None
    while current is not None:
        ancestors.insert(0, current)
        current = current.parent

    related_issues = MaintenanceIssue.objects.filter(
        machine=wo.machine, component=wo.component
    )[:10]
    related_wos = WorkOrder.objects.filter(
        machine=wo.machine, component=wo.component
    )[:10]
    related_pms = PMSchedule.objects.filter(
        machine=wo.machine, component=wo.component
    ).exclude(pk=sched.pk if sched else None)[:10]
    related_eros = ExternalRepairOrder.objects.filter(
        machine=wo.machine, component=wo.component
    )[:10]
    related_prs = PurchaseRequest.objects.filter(
        machine=wo.machine, component=wo.component
    )[:10]

    return render(request, "maintenance/pm_execute.html", {
        "wo": wo,
        "sched": sched,
        "checklist_items": checklist_items,
        "schedule_attachments": Attachment.objects.filter(
            entity_type="pm_schedule", entity_id=sched.pk
        ).select_related("uploaded_by").order_by("-uploaded_at") if sched else Attachment.objects.none(),
        "form": form,
        "machine": tree_node,
        "ancestors": ancestors,
        "related_issues": related_issues,
        "related_wos": related_wos,
        "related_pms": related_pms,
        "related_eros": related_eros,
        "related_prs": related_prs,
    })


@login_required
@role_required(User.Role.MANAGER, User.Role.TECHNICIAN, User.Role.OPERATOR, User.Role.SUPERVISOR, User.Role.SUPER_ADMIN)
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
@role_required(User.Role.MANAGER, User.Role.SUPERVISOR, User.Role.SUPER_ADMIN)
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
@role_required(User.Role.MANAGER, User.Role.SUPERVISOR, User.Role.SUPER_ADMIN)
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
@role_required(User.Role.MANAGER, User.Role.SUPERVISOR, User.Role.SUPER_ADMIN)
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
@role_required(User.Role.OPERATOR, User.Role.TECHNICIAN, User.Role.MANAGER, User.Role.SUPERVISOR, User.Role.SUPER_ADMIN)
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
                ta.tool.save(update_fields=["status"])
                log_audit(actor=request.user, action="tool_returned", entity="Tool", object_id=ta.tool.pk, payload={"condition": cond})
                messages.success(request, "Return recorded — tool is available.")

            elif cond == ToolAssignment.ReturnCondition.DAMAGED:
                ta.tool.status = Tool.Status.OUT_OF_SERVICE
                ta.tool.save(update_fields=["status"])
                wo = WorkOrder(
                    category=WorkOrder.Category.REPAIR,
                    lifecycle_status=WorkOrder.LifecycleStatus.PENDING_REVIEW,
                    tool=ta.tool,
                    created_by=request.user,
                    notes=f"Tool returned damaged: {ta.tool.name} (code: {ta.tool.code})",
                )
                wo.save()
                log_audit(actor=request.user, action="tool_returned_damaged", entity="Tool", object_id=ta.tool.pk, payload={"condition": cond, "wo_pk": wo.pk})
                messages.success(request, f"Return recorded. Damaged tool flagged — repair work order WO-{wo.number} created for manager review.")

            else:  # LOST
                ta.tool.status = Tool.Status.OUT_OF_SERVICE
                ta.tool.save(update_fields=["status"])
                Incident.objects.create(
                    title=f"Lost Tool: {ta.tool.name}",
                    description=f"Tool {ta.tool.name} (code: {ta.tool.code}) reported lost by user {request.user.username}.",
                    reported_by=request.user,
                    tool=ta.tool,
                    status=Incident.Status.OPEN,
                )
                log_audit(actor=request.user, action="tool_returned_lost", entity="Tool", object_id=ta.tool.pk, payload={"condition": cond})
                messages.warning(request, "Return recorded. Lost tool incident created — investigating.")

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
            return redirect("repair_officer", pk=r.pk)
        locked_asset = None
    else:
        # Pre-fill from URL params. If component is a level-5 Component, walk
        # the parent chain to find the level-3 Machine.
        initial = {}
        machine_param = request.GET.get("machine")
        component_param = request.GET.get("component")
        resolved_machine_id = None
        resolved_component_id = None
        if component_param:
            try:
                comp = Machine.objects.get(pk=int(component_param))
                resolved_component_id = comp.pk
                root_machine = comp.get_ancestor_machines()
                if root_machine:
                    resolved_machine_id = root_machine[0].pk
                elif comp.asset_level == 3:
                    resolved_machine_id = comp.pk
            except (Machine.DoesNotExist, ValueError, TypeError):
                pass
        if machine_param and not resolved_machine_id:
            try:
                m = Machine.objects.get(pk=int(machine_param))
                if m.asset_level == 3:
                    resolved_machine_id = m.pk
                elif m.asset_level == 5:
                    resolved_machine_id = m.pk
                    resolved_component_id = m.pk
            except (Machine.DoesNotExist, ValueError, TypeError):
                pass
        if resolved_machine_id:
            initial["machine"] = resolved_machine_id
        if resolved_component_id:
            initial["component"] = resolved_component_id

        # Determine if the user came from a deep-link (asset page). If so,
        # LOCK the machine + component fields so the user can't accidentally
        # attach the record to a different asset.
        has_deep_link = bool(machine_param and component_param)
        lock_asset = has_deep_link
        form = ExternalRepairForm(initial=initial, lock_asset=lock_asset)

        def _ancestors(node):
            result = []
            current = node.parent
            while current is not None:
                result.insert(0, current.name)
                current = current.parent
            return result

        locked_asset = None
        if has_deep_link and resolved_machine_id:
            target = Machine.objects.filter(pk=resolved_component_id or resolved_machine_id).first()
            if target:
                breadcrumb = _ancestors(target) + [target.name]
                locked_asset = {
                    "machine_pk": resolved_machine_id,
                    "component_pk": resolved_component_id,
                    "breadcrumb": " > ".join(breadcrumb),
                }
    return render(
        request,
        "maintenance/repair_form.html",
        {
            "form": form,
            "locked_asset": locked_asset,
            "machine": Machine.objects.filter(pk=resolved_machine_id).first() if resolved_machine_id else Machine.objects.filter(parent__isnull=True, is_active=True).order_by("pk").first(),
            "ancestors": [],
        },
    )


@login_required
@role_required(User.Role.PROCUREMENT, User.Role.MANAGER, User.Role.SUPERVISOR, User.Role.SUPER_ADMIN)
def repair_list(request):
    qs = ExternalRepairOrder.objects.order_by("-created_at")
    st = request.GET.get("status")
    if st in dict(ExternalRepairOrder.Status.choices):
        qs = qs.filter(status=st)
    rows = qs[:200]
    return render(request, "maintenance/repair_list.html", {"repairs": rows, "status_filter": st or ""})


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
                inst.status == ExternalRepairOrder.Status.SENT_TO_VENDOR
                and old_status != ExternalRepairOrder.Status.SENT_TO_VENDOR
            ):
                from maintenance.notifications import notify_repair_sent_to_vendor

                notify_repair_sent_to_vendor(inst)
            if (
                inst.status == ExternalRepairOrder.Status.RETURNED
                and old_status != ExternalRepairOrder.Status.RETURNED
            ):
                from maintenance.notifications import notify_repair_returned, notify_wo_part_returned
                from inventory.models import SparePart

                notify_repair_returned(inst)

                # v4.9.3: also notify the WO tech/manager if this ERO is
                # linked to a WO. Helps them re-install the part promptly.
                if inst.work_order_id:
                    # Try to find a related part by title keyword (best-effort)
                    part_obj = None
                    if inst.title:
                        part_obj = SparePart.objects.filter(
                            name__icontains=inst.title.split()[0] if inst.title else ""
                        ).first()
                    if part_obj is None and inst.work_order_id:
                        # Fallback: pick the most-recently-issued part on the WO
                        from inventory.models import PartIssueLine
                        part_obj = PartIssueLine.objects.filter(
                            work_order=inst.work_order,
                            status=PartIssueLine.Status.APPROVED,
                        ).order_by("-approved_at").values_list("part", flat=True).first()
                        if part_obj:
                            part_obj = SparePart.objects.filter(pk=part_obj).first()
                    if part_obj:
                        notify_wo_part_returned(
                            work_order=inst.work_order,
                            part=part_obj,
                            ero=inst,
                            actor=request.user,
                        )
            messages.success(request, "Repair order updated.")
            return redirect("repair_list")
    else:
        form = ExternalRepairOfficerForm(instance=rwo)

    ancestors = []
    current = rwo.component if rwo.component_id else rwo.machine
    if current is not None:
        parent = current.parent
        while parent is not None:
            ancestors.insert(0, parent)
            parent = parent.parent

    tree_node = rwo.component if rwo.component_id else rwo.machine

    return render(
        request,
        "maintenance/repair_officer.html",
        {
            "rwo": rwo,
            "form": form,
            "machine": tree_node,
            "ancestors": ancestors,
            "related_issues": MaintenanceIssue.objects.filter(
                machine=rwo.machine, component=rwo.component
            )[:10],
            "related_wos": WorkOrder.objects.filter(
                machine=rwo.machine, component=rwo.component
            )[:10],
            "related_pms": PMSchedule.objects.filter(
                machine=rwo.machine, component=rwo.component
            )[:10],
            "related_eros": ExternalRepairOrder.objects.filter(
                machine=rwo.machine, component=rwo.component
            ).exclude(pk=rwo.pk)[:10],
            "related_prs": PurchaseRequest.objects.filter(
                machine=rwo.machine, component=rwo.component
            )[:10],
        },
    )


@login_required
@role_required(User.Role.PROCUREMENT, User.Role.MANAGER, User.Role.SUPER_ADMIN)
def repair_order_pdf(request, pk):
    """Generate a PDF export of an external repair order (vendor handover document)."""
    from maintenance.pdf_utils import (
        _field_table,
        _header_table,
        _section,
        build_pdf_response,
        getSampleStyleSheet,
        Paragraph,
        Spacer,
        colors,
        mm,
    )

    rwo = get_object_or_404(
        ExternalRepairOrder.objects.select_related("work_order__machine"),
        pk=pk,
    )
    buf, doc = build_pdf_response(f"ERO-{rwo.pk}.pdf")
    styles = getSampleStyleSheet()
    elements = []

    elements.append(_header_table())
    elements.append(Spacer(1, 4 * mm))
    elements.append(Paragraph(f"<b>EXTERNAL REPAIR ORDER</b>", styles["Normal"]))

    elements += _section("Repair Order")
    elements.append(_field_table([
        ("ERO Number", f"#{rwo.pk}"),
        ("WO Number", f"WO-{rwo.work_order.number}" if rwo.work_order else "—"),
        ("Asset / Machine", rwo.work_order.machine.name if rwo.work_order and rwo.work_order.machine else "—"),
        ("Serial Number", getattr(rwo.work_order.machine, "serial_number", "—") if rwo.work_order and rwo.work_order.machine else "—"),
        ("Description", rwo.description or "—"),
    ]))

    elements += _section("Vendor")
    elements.append(_field_table([
        ("Vendor", rwo.vendor_name or "—"),
    ]))

    elements += _section("Timeline")
    elements.append(_field_table([
        ("Sent Date", rwo.sent_at.strftime("%Y-%m-%d %H:%M") if rwo.sent_at else "—"),
    ]))

    elements += _section("Cost")
    elements.append(_field_table([
        ("Estimated Cost", str(rwo.estimated_cost or "—")),
        ("Actual Cost", str(rwo.actual_cost or "—")),
    ]))

    elements += _section("Approval")
    elements.append(Paragraph("Authorised by Maintenance Manager:", styles["Normal"]))
    elements.append(Spacer(1, 6 * mm))
    from maintenance.pdf_utils import signature_block
    elements.append(signature_block())

    doc.build(elements)
    pdf_bytes = buf.getvalue()
    buf.close()
    response = HttpResponse(pdf_bytes, content_type="application/pdf")
    response["Content-Disposition"] = f'inline; filename="ERO-{rwo.pk}.pdf"'
    return response


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
    """P3.4 — section-level role filter + hub with preview tables + View All links.

    Section matrix:
    | Section              | Admin | Manager | Supervisor | Supply |
    |----------------------|-------|---------|------------|--------|
    | wo_performance       |  ✓    |   ✓     |    ✓       |   ✗    |
    | tech_performance     |  ✓    |   ✓     |    ✓       |   ✗    |
    | spare_parts          |  ✓    |   ✓     |    ✓       |   ✓    |
    """
    role = request.user.role
    ctx = {}
    if role in (User.Role.MANAGER, User.Role.SUPERVISOR, User.Role.SUPER_ADMIN):
        ctx["wo_performance"] = {
            "most_issues": Machine.objects.annotate(ic=Count("issues")).order_by("-ic")[:5],
            "status_counts": [
                {
                    "code": row["lifecycle_status"],
                    "label": dict(WorkOrder.LifecycleStatus.choices).get(row["lifecycle_status"], row["lifecycle_status"]),
                    "count": row["c"],
                }
                for row in WorkOrder.objects.values("lifecycle_status").annotate(c=Count("id")).order_by("lifecycle_status")
            ],
            "most_issues_count": Machine.objects.annotate(ic=Count("issues")).count(),
            "view_all_wo_url": "reports_work_orders",
            "view_all_machines_url": "reports_machines",
        }
        ctx["tech_performance"] = {
            "tech_done": (
                User.objects.filter(role=User.Role.TECHNICIAN)
                .annotate(
                    closed_wos=Count(
                        "assigned_work_orders",
                        filter=Q(assigned_work_orders__lifecycle_status=WorkOrder.LifecycleStatus.CLOSED),
                    ),
                )
                .order_by("-closed_wos")[:5]
            ),
            "tech_count": User.objects.filter(role=User.Role.TECHNICIAN).count(),
            "view_all_url": "reports_technicians",
        }
    if role in (User.Role.MANAGER, User.Role.SUPERVISOR, User.Role.PROCUREMENT, User.Role.SUPER_ADMIN):
        from decimal import Decimal
        from maintenance.models import Site
        default_site = Site.objects.filter(is_default=True).first()
        low_stock_qs = SparePart.objects.filter(status="active", min_stock_level__gt=0).annotate(
            effective_qty=Coalesce(
                Subquery(
                    Inventory.objects.filter(part=OuterRef("pk"), site=default_site)
                    .values("quantity_available")[:1]
                ),
                Value(Decimal("0")),
            ),
        ).filter(
            Q(effective_qty=0) | Q(effective_qty__lte=F("min_stock_level"))
        )
        ctx["spare_parts"] = {
            "low_stock": low_stock_qs.order_by("sku")[:5],
            "low_stock_count": low_stock_qs.count(),
            "top_parts": (
                PartIssueLine.objects.values("part__sku", "part__name")
                .annotate(total_qty=Sum("quantity"))
                .order_by("-total_qty")[:5]
            ),
            "top_parts_count": PartIssueLine.objects.values("part__sku").distinct().count(),
            "view_all_low_stock_url": "reports_low_stock",
            "view_all_parts_url": "reports_parts_issued",
        }
    return render(request, "maintenance/reports.html", ctx)


@login_required
def technician_report_detail(request, user_id):
    """Per-technician drill-down /reports/technicians/<id>/.

    Reports on:
      - Completed WOs, in-progress WOs
      - Average repair duration (labor minutes)
      - Average response time (assignment → first start)
      - Reopened jobs (sum of rejection_count)
      - External repair count (WOs that went through WAITING_FOR_VENDOR)
    Technicians may also view their own report.
    Managers/supervisors/super admins can view any technician's report.
    Other roles (operator) get a 404.
    """
    technician = get_object_or_404(User, pk=user_id, role=User.Role.TECHNICIAN)
    is_self = request.user.id == technician.id
    is_manager = request.user.role in (
        User.Role.MANAGER,
        User.Role.SUPER_ADMIN,
        User.Role.SUPERVISOR,
    )
    if not (is_self or is_manager):
        raise Http404()
    stats = technician_stats(technician)
    return render(
        request,
        "maintenance/technician_report.html",
        {"technician": technician, "stats": stats},
    )


@login_required
@role_required(User.Role.MANAGER, User.Role.SUPER_ADMIN)
def machine_cost_report(request):
    """Hierarchical asset maintenance cost report + current stock value.

    Builds a three-level cost tree (Machine → Subassembly → Component) of
    CLOSED work orders in the period. Each node's own_cost is the sum of
    cost buckets of WOs filed directly against it; descendant_cost rolls
    up from the children; total = own + descendant.

    Also computes a balance-sheet stock summary across all sites (qty on
    hand + estimated value at last_purchase_cost, falling back to
    avg_cost when null).
    """
    now = timezone.now()
    period_days = int(request.GET.get("period", 90))
    period_start = now - timedelta(days=period_days)

    def zero_buckets():
        return {
            "parts": Decimal("0"),
            "vendor": Decimal("0"),
            "consumables": Decimal("0"),
            "additional": Decimal("0"),
            "total": Decimal("0"),
            "wo_count": 0,
        }

    def add_buckets(target, parts, vendor, consumables, additional):
        target["parts"] += parts
        target["vendor"] += vendor
        target["consumables"] += consumables
        target["additional"] += additional
        target["total"] = (
            target["parts"] + target["vendor"]
            + target["consumables"] + target["additional"]
        )

    wos = (
        WorkOrder.objects
        .filter(lifecycle_status=WorkOrder.LifecycleStatus.CLOSED, updated_at__gte=period_start)
        .select_related("machine", "component")
        .prefetch_related("part_issues", "external_repairs", "cost_record")
    )

    own_cost_by_node = {}
    for wo in wos:
        cr = getattr(wo, "cost_record", None)
        if cr is not None:
            parts = cr.material_cost or Decimal("0")
            vendor = cr.vendor_repair_cost or Decimal("0")
            consumables = cr.consumables_cost or Decimal("0")
            additional = cr.additional_cost or Decimal("0")
        else:
            parts = sum(
                (pi.quantity or Decimal("0")) * (pi.unit_cost or Decimal("0"))
                for pi in wo.part_issues.all()
            )
            vendor = sum(
                er.actual_cost or Decimal("0")
                for er in wo.external_repairs.all()
            )
            consumables = Decimal("0")
            additional = Decimal("0")

        for target in (wo.machine, wo.component):
            if target is None:
                continue
            node = own_cost_by_node.get(target.id)
            if node is None:
                node = {"machine": target, **zero_buckets()}
                own_cost_by_node[target.id] = node
            add_buckets(node, parts, vendor, consumables, additional)
            node["wo_count"] += 1

    def has_any_cost(buckets):
        return buckets["total"] > Decimal("0") or buckets["wo_count"] > 0

    def build_row(machine, depth):
        own = own_cost_by_node.get(machine.id, {"machine": machine, **zero_buckets()})
        descendant_buckets = zero_buckets()
        child_rows = []
        for child in machine.children.all():
            child_row = build_row(child, depth + 1)
            for key in ("parts", "vendor", "consumables", "additional", "total"):
                descendant_buckets[key] += child_row["total"][key]
            descendant_buckets["wo_count"] += child_row["own"]["wo_count"] + child_row["descendant"]["wo_count"]
            child_rows.append(child_row)

        total_buckets = zero_buckets()
        for key in ("parts", "vendor", "consumables", "additional", "total"):
            total_buckets[key] = own[key] + descendant_buckets[key]
        total_buckets["wo_count"] = own["wo_count"] + descendant_buckets["wo_count"]

        return {
            "machine": machine,
            "own": own,
            "descendant": descendant_buckets,
            "total": total_buckets,
            "children": child_rows,
            "depth": depth,
        }

    site_roots = Machine.objects.filter(parent__isnull=True).order_by("name")
    tree_rows = []
    for root in site_roots:
        row = build_row(root, 0)
        # Always include site roots so the full hierarchy is visible.
        # Leaf subassemblies/components with no cost are still shown as $0 rows.
        tree_rows.append(row)

    grand = zero_buckets()
    for row in tree_rows:
        for key in ("parts", "vendor", "consumables", "additional", "total"):
            grand[key] += row["total"][key]
        grand["wo_count"] += row["total"]["wo_count"]

    inv_qs = (
        Inventory.objects
        .select_related("part", "site")
        .all()
    )
    total_qty = Decimal("0")
    total_value = Decimal("0")
    sites_map = {}
    multiple_sites = Site.objects.filter(is_active=True).count() > 1
    for inv in inv_qs:
        qty = inv.quantity_available or Decimal("0")
        total_qty += qty
        unit_cost = inv.part.last_purchase_cost
        if unit_cost is None:
            unit_cost = inv.part.avg_cost
        line_value = qty * (unit_cost or Decimal("0")) if unit_cost is not None else None
        if line_value is not None:
            total_value += line_value
        if multiple_sites:
            site_entry = sites_map.setdefault(
                inv.site_id,
                {
                    "site": inv.site,
                    "qty": Decimal("0"),
                    "value": Decimal("0"),
                },
            )
            site_entry["qty"] += qty
            if line_value is not None:
                site_entry["value"] += line_value

    sites_list = []
    if multiple_sites:
        for entry in sites_map.values():
            sites_list.append(entry)
        sites_list.sort(key=lambda s: s["site"].name)

    default_site = Site.objects.filter(is_default=True).first() or Site.objects.first()
    site_label = default_site.name if default_site else ""

    stock_summary = {
        "total_qty": total_qty,
        "total_value": total_value,
        "sites": sites_list,
    }

    def flatten(rows, out):
        for row in rows:
            out.append(row)
            flatten(row["children"], out)

    flat_rows = []
    flatten(tree_rows, flat_rows)

    return render(request, "maintenance/machine_cost_report.html", {
        "tree_rows": tree_rows,
        "flat_rows": flat_rows,
        "stock_summary": stock_summary,
        "site_label": site_label,
        "period_days": period_days,
        "period_start": period_start,
        "period_choices": [30, 90, 180, 365],
        "grand_parts": grand["parts"],
        "grand_vendor": grand["vendor"],
        "grand_consumables": grand["consumables"],
        "grand_additional": grand["additional"],
        "grand_total": grand["total"],
        "grand_wo_count": grand["wo_count"],
    })


@login_required
@role_required(User.Role.MANAGER, User.Role.SUPERVISOR, User.Role.PROCUREMENT, User.Role.SUPER_ADMIN)
def reports_parts_issued(request):
    parts = (
        PartIssueLine.objects.values("part__sku", "part__name", "part__unit")
        .annotate(total_qty=Sum("quantity"), total_cost=Sum(F("quantity") * F("unit_cost")))
        .order_by("-total_qty")
    )
    paginator = Paginator(parts, 50)
    page = request.GET.get("page")
    return render(request, "maintenance/reports_parts_issued.html", {
        "page_obj": paginator.get_page(page),
    })


@login_required
@role_required(User.Role.MANAGER, User.Role.SUPERVISOR, User.Role.PROCUREMENT, User.Role.SUPER_ADMIN)
def reports_low_stock(request):
    from decimal import Decimal
    from inventory.models import StockMovement
    from maintenance.models import Site

    default_site = Site.objects.filter(is_default=True).first()
    low_stock_parts = SparePart.objects.filter(status="active", min_stock_level__gt=0).annotate(
        effective_qty=Coalesce(
            Subquery(
                Inventory.objects.filter(part=OuterRef("pk"), site=default_site)
                .values("quantity_available")[:1]
            ),
            Value(Decimal("0")),
        ),
    ).filter(
        Q(effective_qty=0) | Q(effective_qty__lte=F("min_stock_level"))
    ).order_by("sku")
    all_parts = SparePart.objects.filter(status="active").order_by("sku")
    paginator = Paginator(all_parts, 50)
    page = request.GET.get("page")
    return render(request, "maintenance/reports_low_stock.html", {
        "page_obj": paginator.get_page(page),
        "low_stock_count": low_stock_parts.count(),
        "all_count": SparePart.objects.filter(status="active").count(),
    })


@login_required
@role_required(User.Role.MANAGER, User.Role.SUPERVISOR, User.Role.PROCUREMENT, User.Role.SUPER_ADMIN)
def reports_machines(request):
    most_issues = Machine.objects.annotate(
        ic=Count("issues"),
        open_wos=Count("work_orders", filter=~Q(work_orders__lifecycle_status=WorkOrder.LifecycleStatus.CLOSED)),
    ).order_by("-ic")
    paginator = Paginator(most_issues, 50)
    page = request.GET.get("page")
    return render(request, "maintenance/reports_machines.html", {
        "page_obj": paginator.get_page(page),
    })


@login_required
@role_required(User.Role.MANAGER, User.Role.SUPERVISOR, User.Role.SUPER_ADMIN)
def reports_work_orders(request):
    from datetime import timedelta

    st = request.GET.get("status")
    qs = WorkOrder.objects.select_related("machine", "assigned_technician").order_by("-created_at")
    if st in dict(WorkOrder.LifecycleStatus.choices):
        qs = qs.filter(lifecycle_status=st)
    elif st in dict(WorkOrder.OperationalStatus.choices):
        qs = qs.filter(operational_status=st)
    status_counts = [
        {"code": row["lifecycle_status"], "label": dict(WorkOrder.LifecycleStatus.choices).get(row["lifecycle_status"], row["lifecycle_status"]), "count": row["c"]}
        for row in WorkOrder.objects.values("lifecycle_status").annotate(c=Count("id")).order_by("lifecycle_status")
    ]
    now = timezone.now()
    overdue_threshold = now - timedelta(days=7)
    overdue_count = qs.filter(
        created_at__lt=overdue_threshold,
    ).exclude(
        lifecycle_status=WorkOrder.LifecycleStatus.CLOSED,
    ).count()
    paginator = Paginator(qs, 50)
    page = request.GET.get("page")
    return render(request, "maintenance/reports_work_orders.html", {
        "page_obj": paginator.get_page(page),
        "status_counts": status_counts,
        "status_filter": st or "",
        "overdue_count": overdue_count,
        "status_choices": WorkOrder.LifecycleStatus.choices,
    })


@login_required
@role_required(User.Role.MANAGER, User.Role.SUPERVISOR, User.Role.PROCUREMENT, User.Role.SUPER_ADMIN)
def reports_technicians(request):
    from datetime import timedelta
    now = timezone.now()
    td90 = now - timedelta(days=90)
    techs = (
        User.objects.filter(role=User.Role.TECHNICIAN)
        .annotate(
            closed_wos=Count("assigned_work_orders", filter=Q(assigned_work_orders__lifecycle_status=WorkOrder.LifecycleStatus.CLOSED)),
            in_progress_wos=Count("assigned_work_orders", filter=Q(assigned_work_orders__lifecycle_status=WorkOrder.LifecycleStatus.IN_PROGRESS)),
        )
        .order_by("-closed_wos")
    )
    for tech in techs:
        tech_wos = list(tech.assigned_work_orders.filter(
            lifecycle_status=WorkOrder.LifecycleStatus.CLOSED, updated_at__gte=td90
        ))
        labor_vals = []
        for wo in tech_wos:
            if wo.labor_started_at and wo.labor_stopped_at:
                h = (wo.labor_stopped_at - wo.labor_started_at).total_seconds() / 3600
                if h >= 0:
                    labor_vals.append(h)
        tech.avg_repair_hours = round(sum(labor_vals) / len(labor_vals), 1) if labor_vals else None
        tech.rejection_sum = sum(wo.rejection_count for wo in tech_wos)
    paginator = Paginator(techs, 50)
    page = request.GET.get("page")
    return render(request, "maintenance/reports_technicians.html", {
        "page_obj": paginator.get_page(page),
    })


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
    td365 = now - timedelta(days=365)

    closed = list(
        WorkOrder.objects.filter(
            lifecycle_status=WorkOrder.LifecycleStatus.CLOSED,
            downtime_started_at__isnull=False,
            downtime_ended_at__isnull=False,
            downtime_ended_at__gte=td90,
        )
        .select_related("machine", "assigned_technician", "issue")
        .order_by("downtime_ended_at")[:500]
    )
    downtime_hours = []
    repair_hours = []
    for w in closed:
        down_h = (w.downtime_ended_at - w.downtime_started_at).total_seconds() / 3600
        if down_h >= 0:
            downtime_hours.append(down_h)
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
    prev_by_machine = {}
    for issue in issue_rows:
        prev_issue = prev_by_machine.get(issue.machine_id)
        if prev_issue:
            gap_h = (issue.created_at - prev_issue.created_at).total_seconds() / 3600
            if gap_h >= 0:
                gaps.append(gap_h)
        prev_by_machine[issue.machine_id] = issue
    mtbf_hours = sum(gaps) / len(gaps) if gaps else None

    pm_closed = WorkOrder.objects.filter(
        category=WorkOrder.Category.PREVENTIVE,
        lifecycle_status=WorkOrder.LifecycleStatus.CLOSED,
        updated_at__gte=td90,
    ).count()
    pm_due = PMSchedule.objects.filter(is_active=True, next_due_at__lt=now).count()
    pm_active = PMSchedule.objects.filter(is_active=True).count()
    pm_compliance_pct = int((pm_closed / max(pm_closed + pm_due, 1)) * 100) if (pm_closed or pm_due) else None

    tool_returns = ToolAssignment.objects.exclude(returned_at__isnull=True)
    tool_lost_count = tool_returns.filter(return_condition=ToolAssignment.ReturnCondition.LOST).count()
    tool_returned_count = tool_returns.count()
    tool_loss_rate_pct = (
        round((tool_lost_count / tool_returned_count) * 100, 1) if tool_returned_count else None
    )

    ctx = {
        "mttr_hours": mttr_hours,
        "mttw_hours": mttw_hours,
        "mtbf_hours": mtbf_hours,
        "avg_downtime_hours": avg_downtime_hours,
        "pm_compliance_pct": pm_compliance_pct,
        "pm_closed_90d": pm_closed,
        "pm_active_schedules": pm_active,
        "pm_due_count": pm_due,
        "open_emergency_wos": WorkOrder.objects.filter(is_emergency=True)
        .exclude(lifecycle_status=WorkOrder.LifecycleStatus.CLOSED)
        .count(),
        "tool_lost_count": tool_lost_count,
        "tool_loss_rate_pct": tool_loss_rate_pct,
    }

    return render(request, "maintenance/kpi.html", ctx)


@login_required
@role_required(User.Role.MANAGER, User.Role.SUPER_ADMIN)
def repair_manager_accept(request, pk):
    rwo = get_object_or_404(ExternalRepairOrder, pk=pk)
    if rwo.status != ExternalRepairOrder.Status.RETURNED:
        messages.error(request, "Repair must be in Returned status before manager acceptance (UC-20).")
        return redirect("repair_list")
    if request.method == "POST":
        form = RepairManagerAcceptForm(request.POST)
        if not form.is_valid():
            for field, errs in form.errors.items():
                for err in errs:
                    messages.error(request, f"{field}: {err}")
            return render(request, "maintenance/repair_accept.html", {"rwo": rwo, "form": form})
        rwo.actual_cost = form.cleaned_data["actual_cost"]
        rwo.invoice_ref = form.cleaned_data["invoice_ref"]
        rwo.status = ExternalRepairOrder.Status.CLOSED
        rwo.closed_at = timezone.now()
        rwo.closed_by = request.user
        rwo.save(update_fields=[
            "actual_cost", "invoice_ref", "status", "closed_at", "closed_by",
        ])
        log_audit(
            actor=request.user, action="repair_manager_accept",
            entity="ExternalRepairOrder", object_id=rwo.pk,
            payload={
                "actual_cost": str(rwo.actual_cost),
                "invoice_ref": rwo.invoice_ref,
            },
        )
        # Phase 2B-6: VENDOR_REPAIR blocker resolves on ERO accept (close).
        # Dispatch string is uppercase to match the event_type switch in
        # WorkOrderBlockerService.sync_from_external_event.
        try:
            from maintenance.services_blocker import WorkOrderBlockerService
            WorkOrderBlockerService.sync_from_external_event(
                external_obj=rwo,
                event_type="ERO_ACCEPTED",
                actor=request.user,
                payload={
                    "ero_id": rwo.pk,
                    "actual_cost": str(getattr(rwo, "actual_cost", "0")),
                    "invoice_ref": rwo.invoice_ref,
                },
            )
        except Exception as _e:
            import logging
            logging.getLogger(__name__).warning(f"Failed to resolve VENDOR_REPAIR blocker: {_e}")

        from maintenance.services_wo_status import WorkOrderService
        try:
            WorkOrderService.recompute_operational_status(rwo.work_order)
        except Exception:
            pass
        # P3.2: push vendor_repair_cost into WorkOrderCost so it rolls up into
        # the machine cost report.
        if rwo.work_order_id:
            try:
                cost = WorkOrderCost.objects.get(work_order_id=rwo.work_order_id)
                cost.recalculate()
            except WorkOrderCost.DoesNotExist:
                WorkOrderCost.objects.create(work_order_id=rwo.work_order_id).recalculate()
        messages.success(
            request,
            f"Repair verified and closed. Cost {rwo.actual_cost} added to WO cost rollup.",
        )
        return redirect("repair_list")
    return render(
        request,
        "maintenance/repair_accept.html",
        {"rwo": rwo, "form": RepairManagerAcceptForm()},
    )


MAX_IMAGE_SIZE = 5 * 1024 * 1024       # 5MB for images and PDF
MAX_VIDEO_SIZE = 30 * 1024 * 1024      # 30MB for video
MAX_AUDIO_SIZE = 30 * 1024 * 1024     # 30MB for audio (Sprint 1 voice notes)
MAX_ATTACHMENTS_PER_ENTITY = 10
ALLOWED_CONTENT_TYPES = [
    'image/jpeg',
    'image/png',
    'image/webp',
    'application/pdf',
    'video/mp4',
    'video/quicktime',
    'audio/webm',   # Sprint 1: voice notes
    'audio/mp4',    # Safari voice m4a
    'audio/ogg',
    'audio/wav',
]


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

    # Get content type early so we can validate it and use it for size limits
    content_type = getattr(file, 'content_type', '') or ''

    # Validate content type
    if content_type not in ALLOWED_CONTENT_TYPES:
        return JsonResponse(
            {"error": "Only JPG, PNG, WEBP, PDF, MP4, and MOV files are allowed."},
            status=400,
        )

    # Validate file size (split by MIME: images/PDF 5MB, video 30MB, audio 30MB)
    content_type_lower = content_type.lower()
    if content_type_lower.startswith('video/'):
        max_size = MAX_VIDEO_SIZE
        size_limit_mb = 30
    elif content_type_lower.startswith('audio/'):
        max_size = MAX_AUDIO_SIZE
        size_limit_mb = 30
    else:
        max_size = MAX_IMAGE_SIZE
        size_limit_mb = 5
    if file.size > max_size:
        return JsonResponse(
            {"error": f"File exceeds {size_limit_mb}MB limit for this file type."},
            status=400,
        )

    # Validate entity_type is a valid choice
    try:
        Attachment.EntityType(entity_type)
    except ValueError:
        return JsonResponse({"error": f"Invalid entity_type: {entity_type}"}, status=400)

    # Check max attachments per entity
    existing_count = Attachment.objects.filter(
        entity_type=entity_type,
        entity_id=int(entity_id)
    ).count()
    if existing_count >= MAX_ATTACHMENTS_PER_ENTITY:
        return JsonResponse({"error": f"Maximum {MAX_ATTACHMENTS_PER_ENTITY} attachments per {entity_type}."}, status=400)

    is_primary_raw = request.POST.get("is_primary", "false")
    is_primary = is_primary_raw.lower() in ("true", "1", "on", "yes")

    category = request.POST.get("category", "PRODUCT")
    valid_categories = [c[0] for c in Attachment._meta.get_field("category").choices]
    if category not in valid_categories:
        category = "PRODUCT"

    is_first_upload = existing_count == 0
    if is_first_upload:
        is_primary = True

    with transaction.atomic():
        att = Attachment.objects.create(
            entity_type=entity_type,
            entity_id=int(entity_id),
            file=file,
            filename=file.name,
            size_bytes=file.size or 0,
            mime_type=content_type,
            uploaded_by=request.user,
            note=note,
            is_primary=is_primary,
            category=category,
        )
        if is_primary:
            Attachment.objects.filter(
                entity_type=entity_type,
                entity_id=int(entity_id),
                is_primary=True,
            ).exclude(pk=att.pk).update(is_primary=False)

    return JsonResponse({
        "id": att.pk,
        "filename": att.filename,
        "size": att.size_bytes,
        "mime_type": att.mime_type,
        "uploaded_at": att.uploaded_at.isoformat(),
        "uploaded_by": att.uploaded_by.username if att.uploaded_by else "",
        "thumbnail_url": att.thumbnail.url if att.thumbnail else "",
        "url": att.file.url if att.file else "",
        "width": att.width,
        "height": att.height,
        "is_primary": att.is_primary,
        "category": att.category,
        "note": att.note,
    })


@login_required
@require_POST
def attachment_upload_pending(request):
    """v4.9 B2: Upload a file (typically voice) before the parent entity is saved.

    Creates an Attachment with entity_type='pending_voice' and entity_id=0.
    Caller must re-link via voice_attachment_id form field after parent is saved.
    """
    from .models import Attachment
    f = request.FILES.get("file")
    if not f:
        return JsonResponse({"error": "No file"}, status=400)
    if f.size > MAX_AUDIO_SIZE:
        return JsonResponse({"error": f"File exceeds 30MB audio limit."}, status=400)
    content_type = getattr(f, "content_type", "") or "audio/webm"
    att = Attachment.objects.create(
        entity_type='pending_voice',
        entity_id=0,
        file=f,
        filename=f.name,
        size_bytes=f.size,
        mime_type=content_type,
        category="OTHER",
        uploaded_by=request.user,
    )
    return JsonResponse({"id": att.pk, "url": att.file.url})


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
        "thumbnail_url": a.thumbnail.url if a.thumbnail else "",
        "width": a.width,
        "height": a.height,
        "is_primary": a.is_primary,
        "category": a.category,
    } for a in attachments]
    return JsonResponse({"attachments": data})


@login_required
def machine_components(request, pk):
    """Return the descendant level-5 Components of a given Machine.

    Used by the issue report form to populate a cascading Component dropdown
    that only shows components belonging to the selected Machine.

    Response shape:
        {"components": [{"id": 5, "name": "Hydraulic Pump", "asset_code": "PUMP-01"}], "has_components": true}

    Returns an empty list (and has_components=False) when the machine has no
    descendant components, or when the machine itself is a level-5 Component
    (in which case the machine IS the component — return [self]).
    """
    machine = get_object_or_404(Machine, pk=pk)
    if machine.asset_level == 5:
        comps = [machine]
    elif machine.asset_level == 3:
        comps = machine.get_descendant_components()
    else:
        comps = [c for c in machine.get_descendants() if c.asset_level == 5]
    data = [
        {
            "id": c.pk,
            "name": c.name,
            "asset_code": c.asset_code or "",
        }
        for c in comps
    ]
    return JsonResponse({
        "components": data,
        "has_components": len(data) > 0,
    })


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
def attachment_set_primary(request, pk):
    """Atomically set an attachment as the primary for its entity. Unsets the previous primary."""
    from .models import Attachment
    att = get_object_or_404(Attachment, pk=pk)

    if att.uploaded_by != request.user and not request.user.is_super_admin_role():
        return JsonResponse({"error": "Not authorized."}, status=403)

    with transaction.atomic():
        Attachment.objects.filter(
            entity_type=att.entity_type,
            entity_id=att.entity_id,
            is_primary=True,
        ).exclude(pk=att.pk).update(is_primary=False)
        att.is_primary = True
        att.save(update_fields=["is_primary"])

    return JsonResponse({
        "id": att.pk,
        "is_primary": att.is_primary,
        "message": "Primary image updated.",
    })


@login_required
@require_POST
@role_required(User.Role.MANAGER, User.Role.SUPER_ADMIN)
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



@login_required
@role_required(User.Role.MANAGER, User.Role.PROCUREMENT, User.Role.SUPER_ADMIN)
def part_stock_in(request, pk):
    """Stock-in form for a specific part — /stock/<pk>/stock-in/"""
    from inventory.forms import StockInForm

    part = get_object_or_404(SparePart, pk=pk)
    selected_site_id = request.GET.get("site")
    from maintenance.models import Site
    sites = Site.objects.filter(is_active=True).order_by("name")
    selected_site = sites.filter(is_default=True).first()
    if selected_site_id:
        try:
            selected_site = sites.get(pk=int(selected_site_id))
        except (ValueError, Site.DoesNotExist):
            pass

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
                site=selected_site,
            )
            messages.success(request, f"Stock-in recorded for {part.name}.")
            uploaded_file = request.FILES.get("invoice_attachment")
            if uploaded_file:
                from maintenance.models import Attachment
                Attachment.objects.create(
                    entity_type=Attachment.EntityType.SPARE_PART,
                    entity_id=part.pk,
                    file=uploaded_file,
                    filename=uploaded_file.name,
                    size_bytes=uploaded_file.size,
                    mime_type=getattr(uploaded_file, "content_type", "") or "",
                    uploaded_by=request.user,
                    note="Invoice attachment from stock-in",
                )
            return redirect("spare_part_detail", pk=part.pk)
    else:
        form = StockInForm(initial={"part": part.pk})

    return render(request, "maintenance/stock_in.html", {
        "form": form,
        "part": part,
        "page_heading": f"Stock-in — {part.name}",
    })


@login_required
@role_required(User.Role.MANAGER, User.Role.SUPER_ADMIN)
def part_adjust(request, pk):
    """Inventory adjustment for a specific part — /stock/<pk>/adjust/"""
    from decimal import Decimal
    from inventory.models import Inventory
    from maintenance.models import Site

    part = get_object_or_404(SparePart, pk=pk)
    sites = Site.objects.filter(is_active=True).order_by("name")
    selected_site_id = request.GET.get("site")
    if selected_site_id:
        try:
            selected_site = sites.get(pk=int(selected_site_id))
        except (ValueError, Site.DoesNotExist):
            selected_site = sites.filter(is_default=True).first()
    else:
        selected_site = sites.filter(is_default=True).first()

    inv = None
    if selected_site:
        inv = part.inventory_items.filter(site=selected_site).first()

    current_qty = inv.quantity_available if inv else Decimal("0")

    if request.method == "POST":
        new_qty = request.POST.get("new_quantity")
        reason = request.POST.get("reason", "").strip()
        note = request.POST.get("note", "").strip()
        rack_location = request.POST.get("rack_location", "").strip()
        is_cycle_count = request.POST.get("is_cycle_count") == "on"
        if new_qty and reason:
            try:
                new_qty_dec = Decimal(str(new_qty))
                if selected_site:
                    if inv:
                        inv.quantity_available = new_qty_dec
                        inv.last_counted_at = timezone.now()
                        inv.last_counted_by = request.user
                        if rack_location:
                            inv.rack_location = rack_location
                        inv.save()
                    else:
                        Inventory.objects.create(
                            part=part,
                            site=selected_site,
                            quantity_available=new_qty_dec,
                            last_counted_at=timezone.now(),
                            last_counted_by=request.user,
                            rack_location=rack_location,
                        )
                StockMovement.objects.create(
                    part=part,
                    movement_type=StockMovement.MovementType.ADJUSTMENT,
                    quantity=new_qty_dec - current_qty,
                    quantity_before=current_qty,
                    quantity_after=new_qty_dec,
                    performed_by=request.user,
                    site=selected_site,
                    reference={"reason": reason, "note": note, "approved_by": request.user.username, "is_cycle_count": is_cycle_count},
                )
                messages.success(request, f"Adjusted {part.name} from {current_qty} to {new_qty_dec}.")
                return redirect("spare_part_detail", pk=part.pk)
            except Exception as e:
                messages.error(request, f"Adjustment failed: {e}")
        else:
            messages.error(request, "New quantity and reason are required.")

    return render(request, "maintenance/part_adjust.html", {
        "part": part,
        "inv": inv,
        "current_qty": current_qty,
        "selected_site": selected_site,
        "sites": sites,
        "is_cycle_count": False,
    })


@login_required
@role_required(User.Role.SUPERVISOR, User.Role.TECHNICIAN, User.Role.MANAGER, User.Role.SUPER_ADMIN)
def stock_lookup(request):
    """Read-only stock lookup for technicians — /stock/lookup/"""
    from django.db.models import OuterRef, Subquery
    from inventory.models import Inventory, SparePart
    from maintenance.models import Site

    sites = Site.objects.filter(is_active=True).order_by("name")
    selected_site_id = request.GET.get("site")
    if selected_site_id:
        try:
            selected_site = sites.get(pk=int(selected_site_id))
        except (ValueError, Site.DoesNotExist):
            selected_site = sites.filter(is_default=True).first()
    else:
        selected_site = sites.filter(is_default=True).first()

    parts_qs = SparePart.objects.annotate(
        inv_available=Subquery(
            Inventory.objects.filter(
                part=OuterRef("pk"),
                site=selected_site
            ).values("quantity_available")[:1]
        ),
        inv_rack=Subquery(
            Inventory.objects.filter(
                part=OuterRef("pk"),
                site=selected_site
            ).values("rack_location")[:1]
        ),
    ).filter(status="active")

    q = request.GET.get("q", "").strip()
    if q:
        parts_qs = parts_qs.filter(
            Q(sku__icontains=q) | Q(name__icontains=q)
        )

    parts_qs = parts_qs.order_by("name")[:500]

    return render(request, "maintenance/stock_lookup.html", {
        "parts": parts_qs,
        "sites": sites,
        "selected_site": selected_site,
        "q": q,
    })


# ---------------------------------------------------------------------------
# Sprint 1: shortage flow + stock badge + component-first selector
# (Plan v7 Step 4 — Views + URLs)
# ---------------------------------------------------------------------------


@login_required
@require_GET
def work_order_part_availability(request, pk, part_id):
    """Live stock badge for a part on a WO.

    Response shape:
        {
            "part_id": int, "part_name": str, "part_sku": str,
            "image_url": str|null,
            "on_hand": str, "reserved": str, "usable": str,
            "min_level": str, "is_low": bool,
            "stock_state": "available"|"low"|"out",
            "stock_label": "🟢 Available"|"🟡 Low Stock"|"🔴 Out of Stock",
            "stock_count": "N pcs"|"0 pcs",
            "min_label": "Min: 5",
            "used_on_asset": int,                # count of past PartIssueLine for this WO's machine/component
            "last_replaced_days": int,           # -1 if never
            "site": "Main Factory"|null
        }
    """
    from inventory.models import Inventory, SparePart, PartIssueLine
    from inventory.services import _get_default_site

    wo = get_object_or_404(WorkOrder, pk=pk)
    part = get_object_or_404(SparePart, pk=part_id)
    site = wo.machine.site if wo.machine and wo.machine.site else _get_default_site()
    inv = Inventory.objects.filter(part=part, site=site).first()
    on_hand   = (inv.quantity_available if inv else Decimal("0"))
    reserved  = (inv.quantity_reserved  if inv else Decimal("0"))
    usable    = on_hand - reserved

    # 3-tier rule
    if usable <= 0:
        stock_state = "out"
        stock_label = "🔴 Out of Stock"
        stock_count = "0 pcs"
    elif usable <= part.min_stock_level:
        stock_state = "low"
        stock_label = "🟡 Low Stock"
        stock_count = f"{usable:g} pcs left"  # :g strips trailing zeros
    else:
        stock_state = "available"
        stock_label = "🟢 Available"
        stock_count = f"{usable:g} pcs available"

    # Maintenance intelligence: count + last-replaced on this asset
    asset_q = Q(work_order__machine=wo.machine)
    if wo.component_id:
        asset_q |= Q(work_order__component=wo.component)
    past = PartIssueLine.objects.filter(
        asset_q,
        part=part,
        status=PartIssueLine.Status.APPROVED,
    )
    used_count = past.count()
    last_dt = past.aggregate(Max("created_at"))["created_at__max"]
    last_days = (timezone.now().date() - last_dt.date()).days if last_dt else -1

    # Primary image (first Attachment with is_primary=True)
    primary = Attachment.objects.filter(
        entity_type="spare_part", entity_id=part.pk, is_primary=True
   ).first()
    image_url = None
    if primary and primary.thumbnail:
        try:
            image_url = primary.thumbnail.url
        except ValueError:
            pass

    # v4.9.2 B1: pending-request breakdown by WO — helps tech see what's
    # requested (but not yet approved) when they switch contexts. This is
    # informational only; the actual reservation lock is held in
    # approve_part_request via select_for_update.
    pending_lines = PartIssueLine.objects.filter(
        part=part,
        status=PartIssueLine.Status.PENDING,
    ).select_related("work_order").order_by("-created_at")[:5]
    pending_total = PartIssueLine.objects.filter(
        part=part,
        status=PartIssueLine.Status.PENDING,
    ).aggregate(total=Sum("quantity"))["total"] or Decimal("0")
    # Normalize to 3-decimal string for consistency with on_hand/reserved
    pending_total_str = f"{pending_total:.3f}"
    pending_breakdown = [
        {
            "wo_number": rl.work_order.number if rl.work_order_id else None,
            "wo_pk": rl.work_order_id,
            "quantity": str(rl.quantity),
            "requested_by": rl.requested_by.username if rl.requested_by_id else None,
        }
        for rl in pending_lines
    ]

    return JsonResponse({
        "part_id": part.pk,
        "part_name": part.name,
        "part_sku": part.sku,
        "image_url": image_url,
        "on_hand": str(on_hand),
        "reserved": str(reserved),
        "usable": str(usable),
        "min_level": str(part.min_stock_level),
        "is_low": usable <= part.min_stock_level,
        "stock_state": stock_state,
        "stock_label": stock_label,
        "stock_count": stock_count,
        "min_label": f"Min: {part.min_stock_level:g}",
        "used_on_asset": used_count,
        "last_replaced_days": last_days,
        "site": site.name if site else None,
        "pending_total": pending_total_str,
        "pending_breakdown": pending_breakdown,
    })


@login_required
@require_GET
def work_order_components(request, pk):
    """Level-5 components of the WO's machine (or self if WO.component is set)."""
    wo = get_object_or_404(WorkOrder.objects.select_related("machine", "component"), pk=pk)
    if wo.component_id and wo.component.asset_level == 5:
        comps = [wo.component]
    elif wo.machine_id:
        comps = list(wo.machine.get_descendant_components())
    else:
        comps = []

    return JsonResponse({
        "components": [
            {
                "id": c.pk,
                "name": c.name,
                "asset_code": c.asset_code or "",
                "criticality": c.criticality or "",
            }
            for c in comps
        ],
        "has_components": bool(comps),
    })


@login_required
@require_GET
def work_order_component_parts(request, pk):
    """Parts used on the given component, ordered by recent usage.

    ?component=<id> is required. If the component has no history,
    returns the 50 most recently used active parts across the whole site.
    """
    from inventory.models import SparePart, PartIssueLine

    pk = int(pk)  # unused parameter, but keep the URL signature
    component_id = request.GET.get("component")
    if not component_id:
        return JsonResponse({"parts": []})

    try:
        component_pk = int(component_id)
    except (TypeError, ValueError):
        return JsonResponse({"parts": []})

    # Parts used on this component (WOs that targeted this component)
    used = (
        PartIssueLine.objects
        .filter(work_order__component_id=component_pk)
        .values("part_id")
        .annotate(uses=Count("id"), last=Max("created_at"))
        .order_by("-last")[:50]
    )
    part_ids = [row["part_id"] for row in used]

    if not part_ids:
        # Fallback: all active parts, alphabetical
        part_ids = list(
            SparePart.objects.filter(status="active")
            .order_by("name")
            .values_list("pk", flat=True)[:50]
        )

    # Preserve the "recently used" order
    order = {pid: i for i, pid in enumerate(part_ids)}
    parts = list(SparePart.objects.filter(pk__in=part_ids, status="active"))
    parts.sort(key=lambda p: order.get(p.pk, 999))

    return JsonResponse({
        "parts": [
            {
                "id": p.pk,
                "name": p.name,
                "sku": p.sku,
                "is_consumable": p.is_consumable,
            }
            for p in parts
        ],
    })


@login_required
@require_POST
@role_required(User.Role.TECHNICIAN, User.Role.SUPER_ADMIN)
def work_order_request_shortage(request, pk):
    """Technician's "📦 Raise Shortage Request" button handler.

    Calls raise_shortage_request() in inventory.services, which creates
    (or updates) a PENDING PartShortageReport and notifies the managers.
    """
    from inventory.services import raise_shortage_request
    from inventory.models import SparePart

    wo = get_object_or_404(WorkOrder, pk=pk)
    if wo.assigned_technician_id != request.user.id and not request.user.is_super_admin_role():
        messages.error(request, "You can only raise shortage requests on work orders assigned to you.")
        return redirect("work_order_detail", pk=wo.pk)

    part_id = request.POST.get("part_id")
    if not part_id:
        messages.error(request, "Missing part_id.")
        return redirect("work_order_detail", pk=wo.pk)

    part = get_object_or_404(SparePart, pk=part_id)
    note = request.POST.get("note", "")

    try:
        report = raise_shortage_request(
            wo=wo, part=part, technician=request.user, note=note,
        )
    except ValueError as e:
        messages.error(request, str(e))
        return redirect("work_order_detail", pk=wo.pk)

    messages.success(
        request,
        f"📦 Shortage raised for {report.shortage_qty:g} × {part.name}. Manager has been notified.",
    )
    return redirect("work_order_detail", pk=wo.pk)


@login_required
@require_POST
@role_required(User.Role.MANAGER, User.Role.SUPER_ADMIN)
def work_order_decide_shortage(request, pk, report_id):
    """Manager records the first decision on a PartShortageReport (v4.8).

    Creates a PartShortageDecision (OneToOne). Editing an existing
    decision (before execution) is handled by work_order_edit_shortage.
    """
    from datetime import date
    from decimal import Decimal, InvalidOperation
    from inventory.models import PartShortageReport
    from inventory.services import create_shortage_decision

    wo = get_object_or_404(WorkOrder, pk=pk)
    report = get_object_or_404(PartShortageReport, pk=report_id, work_order=wo)
    if report.status != PartShortageReport.Status.PENDING_REVIEW:
        messages.error(request, f"Report is in {report.status}; only PENDING_REVIEW reports can be decided.")
        return redirect("work_order_detail", pk=wo.pk)

    decision_type = request.POST.get("decision_type", "").strip()
    if decision_type not in ("approve", "reject"):
        messages.error(request, "Invalid decision type.")
        return redirect("work_order_detail", pk=wo.pk)

    note = (request.POST.get("decision_note") or "").strip()
    reason = ""
    rejected = Decimal("0")
    approved_issue = Decimal("0")
    approved_procure = Decimal("0")

    if decision_type == "reject":
        reason = (request.POST.get("rejection_reason") or "").strip()
        if len(reason) < 15:
            messages.error(request, "Rejection reason is required (min 15 characters).")
            return redirect("work_order_detail", pk=wo.pk)
        rejected = report.qty_requested
    else:
        try:
            approved_issue   = Decimal(request.POST.get("approved_issue_qty") or "0")
            approved_procure = Decimal(request.POST.get("approved_procurement_qty") or "0")
            rejected         = Decimal(request.POST.get("rejected_qty") or "0")
        except (InvalidOperation, ValueError):
            messages.error(request, "Quantities must be numbers.")
            return redirect("work_order_detail", pk=wo.pk)
        if any(v < 0 for v in (approved_issue, approved_procure, rejected)):
            messages.error(request, "Quantities cannot be negative.")
            return redirect("work_order_detail", pk=wo.pk)
        if approved_issue + approved_procure + rejected != report.qty_requested:
            messages.error(
                request,
                f"Books must balance: issue({approved_issue:g}) + procure({approved_procure:g}) + "
                f"reject({rejected:g}) = {approved_issue + approved_procure + rejected:g} "
                f"!= requested({report.qty_requested:g}).",
            )
            return redirect("work_order_detail", pk=wo.pk)
        if approved_issue == 0 and approved_procure == 0:
            messages.error(request, "Approve with both 0 makes no sense — use Reject instead.")
            return redirect("work_order_detail", pk=wo.pk)

    eta_raw = (request.POST.get("expected_availability_date") or "").strip()
    eta = None
    if eta_raw:
        try:
            eta = date.fromisoformat(eta_raw)
        except ValueError:
            messages.warning(request, "Invalid expected_availability_date; skipped.")

    try:
        decision = create_shortage_decision(
            report=report,
            decision_type=decision_type,
            approved_issue_qty=approved_issue,
            approved_procurement_qty=approved_procure,
            rejected_qty=rejected,
            decided_by=request.user,
            expected_availability_date=eta,
            decision_note=note,
            rejection_reason=reason,
        )
    except Exception as e:
        messages.error(request, str(e))
        return redirect("work_order_detail", pk=wo.pk)

    log_audit(
        actor=request.user, action="part_shortage_decided",
        entity="PartShortageReport", object_id=str(report.pk),
        payload={
            "decision_id": str(decision.pk),
            "decision_type": decision_type,
            "wo": str(wo.pk), "part": report.part.sku,
            "qty_requested": str(report.qty_requested),
            "approved_issue_qty": str(approved_issue) if decision_type == "approve" else "0",
            "approved_procurement_qty": str(approved_procure) if decision_type == "approve" else "0",
            "rejected_qty": str(rejected),
            "expected_availability_date": str(eta) if eta else "",
        },
    )

    if decision_type == "approve":
        messages.success(
            request,
            f"✅ Shortage decided for {report.part.name}: "
            f"issue {approved_issue:g}, procure {approved_procure:g}, reject {rejected:g}.",
        )
    else:
        messages.success(request, f"❌ Shortage rejected for {report.part.name}.")
    return redirect("work_order_detail", pk=wo.pk)


@login_required
@require_POST
@role_required(User.Role.MANAGER, User.Role.SUPER_ADMIN)
def work_order_edit_shortage(request, pk, report_id):
    """Edit a decision on an APPROVED report. Refused once execution starts.

    v4.8 procurement lock: refuses to change approved_procurement_qty
    after a PurchaseRequest has been auto-created.
    """
    from decimal import Decimal, InvalidOperation
    from inventory.models import PartShortageReport
    from inventory.services import edit_shortage_decision

    wo = get_object_or_404(WorkOrder, pk=pk)
    report = get_object_or_404(PartShortageReport, pk=report_id, work_order=wo)

    if report.is_decision_locked:
        messages.error(
            request,
            f"🔒 Decision is locked: report is in {report.status}. "
            f"Close this report and create a new shortage if fulfillment needs to change."
        )
        return redirect("work_order_detail", pk=wo.pk)

    try:
        approved_issue   = Decimal(request.POST.get("approved_issue_qty") or "0")
        approved_procure = Decimal(request.POST.get("approved_procurement_qty") or "0")
        rejected         = Decimal(request.POST.get("rejected_qty") or "0")
    except (InvalidOperation, ValueError):
        messages.error(request, "Quantities must be numbers.")
        return redirect("work_order_detail", pk=wo.pk)
    if any(v < 0 for v in (approved_issue, approved_procure, rejected)):
        messages.error(request, "Quantities cannot be negative.")
        return redirect("work_order_detail", pk=wo.pk)
    if approved_issue + approved_procure + rejected != report.qty_requested:
        messages.error(
            request,
            f"Books must balance: {approved_issue}+{approved_procure}+{rejected}={approved_issue+approved_procure+rejected} != {report.qty_requested}.",
        )
        return redirect("work_order_detail", pk=wo.pk)

    note = (request.POST.get("decision_note") or "").strip()

    try:
        decision = edit_shortage_decision(
            report=report,
            approved_issue_qty=approved_issue,
            approved_procurement_qty=approved_procure,
            rejected_qty=rejected,
            edited_by=request.user,
            decision_note=note,
        )
    except Exception as e:
        messages.error(request, str(e))
        return redirect("work_order_detail", pk=wo.pk)

    log_audit(
        actor=request.user, action="part_shortage_decision_edited",
        entity="PartShortageReport", object_id=str(report.pk),
        payload={
            "decision_id": str(decision.pk),
            "wo": str(wo.pk), "part": report.part.sku,
            "approved_issue_qty": str(approved_issue),
            "approved_procurement_qty": str(approved_procure),
            "rejected_qty": str(rejected),
        },
    )
    messages.success(request, f"✎ Decision edited for {report.part.name}.")
    return redirect("work_order_detail", pk=wo.pk)


@login_required
@require_POST
@role_required(User.Role.MANAGER, User.Role.SUPER_ADMIN)
def work_order_close_shortage(request, pk, report_id):
    """Manager closes a shortage report (releases reservation, cancels PRs)."""
    from inventory.models import PartShortageReport
    from inventory.services import transition_shortage_status

    wo = get_object_or_404(WorkOrder, pk=pk)
    report = get_object_or_404(PartShortageReport, pk=report_id, work_order=wo)
    note = (request.POST.get("note") or "").strip()

    try:
        transition_shortage_status(
            report, PartShortageReport.Status.CLOSED, actor=request.user, note=note,
        )
    except Exception as e:
        messages.error(request, str(e))
        return redirect("work_order_detail", pk=wo.pk)

    messages.success(request, f"🗙 Shortage closed for {report.part.name}.")
    return redirect("work_order_detail", pk=wo.pk)


@login_required
@require_POST
@role_required(User.Role.MANAGER, User.Role.SUPER_ADMIN)
def work_order_block_shortage(request, pk, report_id):
    """Manager blocks a shortage (operational problem). Strict v4.8: no reservation release."""
    from inventory.models import PartShortageReport
    from inventory.services import transition_shortage_status

    wo = get_object_or_404(WorkOrder, pk=pk)
    report = get_object_or_404(PartShortageReport, pk=report_id, work_order=wo)
    note = (request.POST.get("note") or "").strip()

    try:
        transition_shortage_status(
            report, PartShortageReport.Status.BLOCKED, actor=request.user, note=note,
        )
    except Exception as e:
        messages.error(request, str(e))
        return redirect("work_order_detail", pk=wo.pk)

    messages.warning(request, f"⚠ Shortage blocked for {report.part.name}.")
    return redirect("work_order_detail", pk=wo.pk)


@login_required
@require_POST
@role_required(User.Role.MANAGER, User.Role.SUPER_ADMIN)
def work_order_warehouse_issue(request, pk, line_id):
    """Warehouse executes the issue against current stock (v4.8).

    Validates quantity_available (physical on-hand), releases the
    shortage's reservation, deducts, and transitions the related
    shortage report to IN_FULFILLMENT on the first execution.
    """
    from decimal import Decimal, InvalidOperation
    from inventory.models import PartIssueLine
    from inventory.services import execute_warehouse_issue

    wo = get_object_or_404(WorkOrder, pk=pk)
    line = get_object_or_404(PartIssueLine, pk=line_id, work_order=wo)
    try:
        qty = Decimal(request.POST.get("qty") or "0")
    except (InvalidOperation, ValueError):
        messages.error(request, "Issue qty must be a number.")
        return redirect("work_order_detail", pk=wo.pk)

    try:
        result = execute_warehouse_issue(line=line, qty=qty, actor=request.user)
    except Exception as e:
        messages.error(request, f"⚠️ Could not issue {qty} × {line.part.name}: {e}")
        return redirect("work_order_detail", pk=wo.pk)

    messages.success(
        request,
        f"📤 Issued {result['actual_issued']:g} × {line.part.name}. "
        f"Stock: {result['stock_after']} remaining.",
    )
    return redirect("work_order_detail", pk=wo.pk)


@login_required
@require_POST
@role_required(User.Role.MANAGER, User.Role.SUPER_ADMIN)
def work_order_mark_fulfilled(request, pk, report_id):
    """Manager marks a shortage as FULFILLED (manual verification, v4.8)."""
    from inventory.models import PartShortageReport
    from inventory.services import mark_shortage_fulfilled

    wo = get_object_or_404(WorkOrder, pk=pk)
    report = get_object_or_404(PartShortageReport, pk=report_id, work_order=wo)

    try:
        mark_shortage_fulfilled(report=report, actor=request.user)
    except Exception as e:
        messages.error(request, str(e))
        return redirect("work_order_detail", pk=wo.pk)

    messages.success(request, f"✓ Shortage marked fulfilled for {report.part.name}.")
    return redirect("work_order_detail", pk=wo.pk)


@login_required
def shortage_dashboard(request):
    """Manager's shortage dashboard: counts by state, oldest pending, top parts."""
    from inventory.models import PartShortageReport
    from django.db.models import Count, F, ExpressionWrapper, fields

    qs = PartShortageReport.objects.all()
    counts = {s.value: qs.filter(status=s.value).count() for s in PartShortageReport.Status}

    pending_qs = qs.filter(status=PartShortageReport.Status.PENDING_REVIEW).order_by("created_at")
    oldest_pending = pending_qs.select_related("work_order", "part", "reported_by")[:10]

    in_fulfillment = qs.filter(status=PartShortageReport.Status.IN_FULFILLMENT).order_by("reviewed_at")
    in_fulfillment_list = in_fulfillment.select_related("work_order", "part")[:20]

    top_parts = (
        qs.filter(status__in=[
            PartShortageReport.Status.PENDING_REVIEW,
            PartShortageReport.Status.APPROVED,
            PartShortageReport.Status.IN_FULFILLMENT,
        ])
        .values("part__sku", "part__name")
        .annotate(active=Count("id"))
        .order_by("-active")[:10]
    )

    return render(request, "maintenance/shortage_dashboard.html", {
        "counts": counts,
        "oldest_pending": oldest_pending,
        "in_fulfillment_list": in_fulfillment_list,
        "top_parts": top_parts,
        "STATUS_CHOICES": PartShortageReport.Status.choices,
    })


def reconciliation_dashboard(request):
    """All WorkOrders are on the blocker system. Shows banner and empty results."""
    total_count = 0
    return render(request, "maintenance/reconciliation_dashboard.html", {
        "legacy_wos": [],
        "total_count": total_count,
        "filters": {},
        "status_choices": WorkOrder.LifecycleStatus.choices,
    })


def active_blockers_dashboard(request):
    """Shows all OPEN WorkOrderBlockers across all WOs, sorted by impact."""
    from maintenance.models import WorkOrderBlocker
    from inventory.services_impact import PartImpactService
    from django.contrib.contenttypes.models import ContentType
    from inventory.models import SparePart
    from django.db.models import Q

    qs = WorkOrderBlocker.objects.filter(
        status=WorkOrderBlocker.Status.OPEN
    ).select_related(
        "work_order", "work_order__machine", "opened_by", "related_ero"
    )

    kind = request.GET.get("kind", "")
    q = request.GET.get("q", "")
    impact_level = request.GET.get("impact_level", "")

    filters = {}
    if kind:
        qs = qs.filter(kind=kind)
        filters["kind"] = kind
    if q:
        import re
        num_match = re.search(r"(\d+)", q)
        if num_match:
            qs = qs.filter(Q(work_order__number=num_match.group(1)))
        else:
            qs = qs.filter(work_order__machine__name__icontains=q)
        filters["q"] = q
    if impact_level:
        filters["impact_level"] = impact_level

    # Compute impact scores for PART/SHORTAGE blockers
    part_ct = ContentType.objects.get_for_model(SparePart)
    blocker_list = list(qs)
    annotated = []
    for blocker in blocker_list:
        impact = None
        if blocker.kind in (WorkOrderBlocker.Kind.PART, WorkOrderBlocker.Kind.SHORTAGE):
            if blocker.content_type_id == part_ct.pk and blocker.object_id:
                try:
                    part = SparePart.objects.get(pk=blocker.object_id)
                    impact = PartImpactService.compute_impact(part)
                except SparePart.DoesNotExist:
                    pass

        annotated.append({"blocker": blocker, "impact": impact})

    # Sort: HIGH → MEDIUM → LOW → non-part (score=0), then by opened_at ascending
    def sort_key(item):
        impact = item["impact"]
        if impact is None:
            level_order = 3
        elif impact.level == "HIGH":
            level_order = 0
        elif impact.level == "MEDIUM":
            level_order = 1
        else:
            level_order = 2
        return (level_order, item["blocker"].opened_at)

    annotated.sort(key=sort_key)

    # Apply impact_level filter after sorting
    if impact_level:
        annotated = [
            item for item in annotated
            if item["impact"] and item["impact"].level == impact_level
        ]

    total_open = len(annotated)

    paginator = Paginator(annotated, 25)
    page = request.GET.get("page")
    blockers_page = paginator.get_page(page)

    return render(request, "maintenance/active_blockers_dashboard.html", {
        "blockers": blockers_page,
        "total_open": total_open,
        "filters": filters,
        "kind_choices": WorkOrderBlocker.Kind.choices,
        "impact_level_choices": [
            ("LOW", "Low Impact"),
            ("MEDIUM", "Medium Impact"),
            ("HIGH", "High Impact"),
        ],
    })
