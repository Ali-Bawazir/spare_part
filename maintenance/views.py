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
from django.utils.translation import gettext as _
from django.views.decorators.http import require_GET, require_POST
from datetime import timedelta
from datetime import timedelta as td
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
import csv
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
from inventory.models import (
    ConsumableAssignment, Inventory, PartIssueLine, PartShortageReport,
    SparePart, StockMovement,
)

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
from procurement.models import PurchaseOrder, PurchaseRequest, Supplier
from procurement.forms import SupplierForm

from .forms import (
    AssignTechnicianForm,
    CostAdjustmentForm,
    EmergencyWOForm,
    ExternalRepairForm,
    ExternalRepairOfficerForm,
    ExternalRepairRequestDecisionForm,
    ExternalRepairRequestForm,
    IssueReportForm,
    MachineForm,
    PMChecklistItemFormSet,
    PMTemplateForm,
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
    CostAdjustment,
    CostCategory,
    CostTransaction,
    ExternalRepairOrder,
    ExternalRepairRequest,
    Machine,
    MaintenanceIssue,
    Notification,
    PMChecklistItem,
    PMExecution,
    PMSchedule,
    PMTemplate,
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
    compute_compliance,
    create_pm_execution_for_wo,
    escalate_issue_to_emergency,
    get_other_active_work_order,
    has_active_emergency,
    log_audit,
    manager_approve_pm_execution,
    manager_close_work_order,
    manager_reject_pm_execution,
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
@role_required(User.Role.MANAGER, User.Role.PROCUREMENT, User.Role.SUPER_ADMIN)
def reservations_dashboard(request):
    """Phase 7.8: global view of all ACTIVE inventory reservations.

    Shows every soft-claim (InventoryReservation) per part + per WO, with
    the underlying PartIssueLine. Useful for the warehouse/procurement
    team to see "what's claimed for which WO" without running SQL.
    Recent RELEASED + CANCELLED rows are also shown (collapsed).
    """
    from inventory.models import InventoryReservation
    from django.db.models import Sum, Count

    status_filter = request.GET.get("status", "active")
    qs = (
        InventoryReservation.objects
        .select_related("part", "work_order", "source_line")
        .order_by("-created_at")
    )
    if status_filter in (
        InventoryReservation.Status.ACTIVE,
        InventoryReservation.Status.RELEASED,
        InventoryReservation.Status.CANCELLED,
    ):
        qs = qs.filter(status=status_filter)
    elif status_filter != "all":
        status_filter = "active"
        qs = qs.filter(status=InventoryReservation.Status.ACTIVE)

    # Per-part summary (only the ACTIVE filter, always)
    by_part = (
        InventoryReservation.objects
        .filter(status=InventoryReservation.Status.ACTIVE)
        .values("part__sku", "part__name")
        .annotate(
            total_reserved=Sum("quantity"),
            wo_count=Count("work_order", distinct=True),
        )
        .order_by("-total_reserved")[:25]
    )

    return render(request, "maintenance/reservations_dashboard.html", {
        "reservations": qs[:200],
        "by_part": by_part,
        "status_filter": status_filter,
        "active_count": InventoryReservation.objects.filter(
            status=InventoryReservation.Status.ACTIVE
        ).count(),
        "released_count": InventoryReservation.objects.filter(
            status=InventoryReservation.Status.RELEASED
        ).count(),
        "cancelled_count": InventoryReservation.objects.filter(
            status=InventoryReservation.Status.CANCELLED
        ).count(),
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
        my_issues_qs = MaintenanceIssue.objects.filter(reported_by=request.user)
        ctx["my_issues"] = my_issues_qs[:10]
        # Phase 6: per-user 30-day reporting counts for the operator
        # dashboard. Surfaces motivation (e.g. "you've reported 4 issues
        # this month") and a drill-down link to the filtered issue list.
        now = timezone.now()
        td30 = now - timedelta(days=30)
        td7 = now - timedelta(days=7)
        ctx["my_issues_count_30d"] = my_issues_qs.filter(created_at__gte=td30).count()
        ctx["my_issues_count_7d"] = my_issues_qs.filter(created_at__gte=td7).count()
        # Phase 6: "Unresolved" = not yet converted to a WO. The Issue
        # lifecycle is new → validated → converted; once converted the
        # issue is effectively done (it lives on as the WO).
        ctx["my_issues_count_unresolved"] = my_issues_qs.exclude(
            status=MaintenanceIssue.Status.CONVERTED,
        ).count()
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
            messages.error(request, _("Choose a QR image to upload, or use manual entry."))
        else:
            decoded_value = _decode_uploaded_qr(upload)
            if decoded_value:
                return redirect(_append_query_value(next_path, param, decoded_value))
            messages.error(
                request,
                _(f"We could not read a QR code from that image. Try a clearer {label} photo or enter the code manually."),
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
    page_title = _("Add machine")

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
                level_label = {1: _("Area"), 2: _("Production Line"), 3: _("Machine"), 4: _("Subassembly"), 5: _("Component")}.get(initial["asset_level"], _("Asset"))
                page_title = _(f"Add {level_label.lower()} under {preselected_parent.name}")
            except (Machine.DoesNotExist, ValueError):
                pass
        elif asset_level:
            try:
                initial["asset_level"] = int(asset_level)
                level_label = {1: _("Area"), 2: _("Production Line"), 3: _("Machine"), 4: _("Subassembly"), 5: _("Component")}.get(initial["asset_level"], _("Asset"))
                page_title = _(f"Add {level_label.lower()}")
            except (ValueError, TypeError):
                pass

    if request.method == "POST":
        form = MachineForm(request.POST)
        if form.is_valid():
            machine = form.save()
            log_audit(actor=request.user, action="machine_created", entity="Machine", object_id=machine.pk)
            messages.success(request, _(f"{machine.name} saved."))
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
            messages.success(request, _("Machine updated."))
            return redirect("machine_list")
    else:
        form = MachineForm(instance=machine)
    return render(request, "maintenance/machine_form.html", {"form": form, "page_title": _("Edit machine"), "machine": machine})


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
    ).select_related("reported_by", "machine", "component").order_by("-created_at")[:50]

    related_wos = (
        WorkOrder.objects
        .filter(Q(machine=machine) | Q(component=machine))
        .select_related("machine", "component", "assigned_technician", "cost_record")
        .order_by("-created_at")[:50]
    )

    # Total WO count (not just the displayed 50) for the hero stats strip
    total_wo_count = WorkOrder.objects.filter(
        Q(machine=machine) | Q(component=machine)
    ).count()

    related_pms = PMSchedule.objects.filter(
        Q(machine=machine) | Q(component=machine)
    ).select_related("template", "machine", "component").order_by("next_due_at")[:20]

    from maintenance.models import PMExecution
    from datetime import timedelta
    now = timezone.now()
    seven_days = now + timedelta(days=7)
    all_pms_for_asset = PMSchedule.objects.filter(
        Q(machine=machine) | Q(component=machine)
    )
    pm_active_count = all_pms_for_asset.filter(is_active=True).count()
    pm_overdue_count = all_pms_for_asset.filter(
        is_active=True,
        next_due_at__lt=now,
    ).count()
    pm_due_this_week_count = all_pms_for_asset.filter(
        is_active=True,
        next_due_at__gte=now,
        next_due_at__lte=seven_days,
    ).count()

    ninety_days_ago = now - timedelta(days=90)
    recent_executions = PMExecution.objects.filter(
        pm_schedule__in=all_pms_for_asset,
        scheduled_due_at__gte=ninety_days_ago,
    )
    scheduled_count = recent_executions.count()
    approved_on_time = recent_executions.filter(
        status=PMExecution.Status.APPROVED,
        approved_at__lte=F("scheduled_due_at"),
    ).count()
    pm_compliance_pct = (
        int((approved_on_time / scheduled_count) * 100) if scheduled_count > 0 else None
    )

    last_executions_by_schedule = {}
    for schedule in related_pms:
        last = PMExecution.objects.filter(pm_schedule=schedule).order_by("-scheduled_due_at").first()
        last_executions_by_schedule[schedule.pk] = last

    pm_stats = {
        "active_count": pm_active_count,
        "due_this_week_count": pm_due_this_week_count,
        "overdue_count": pm_overdue_count,
        "compliance_pct": pm_compliance_pct,
    }

    related_eros = ExternalRepairOrder.objects.filter(
        Q(machine=machine) | Q(component=machine)
    ).select_related("machine", "component").order_by("-created_at")[:20]

    related_prs = PurchaseRequest.objects.filter(
        Q(machine=machine) | Q(component=machine)
    ).select_related("machine", "component", "part", "supplier").order_by("-created_at")[:50]

    # Cost rollup for the Costs tab (Phase 3). Live aggregation from
    # the CostTransaction ledger. Same shape for machines and components.
    from .cost_views import machine_costs_for_periods, component_costs_for_periods
    if machine.asset_level == 5:
        cost_periods = component_costs_for_periods(machine)
    else:
        cost_periods = machine_costs_for_periods(machine)

    # Hero stats for the top strip:
    # - 90d cost total (from the cost ledger dataclass)
    # - 90d failure count (PartIssueLine ISSUED in last 90d for this asset)
    # - days since last activity (most recent WO update, falling back to machine.updated_at)
    cost_90d = cost_periods.get(90)
    cost_90d_total = cost_90d.total if cost_90d else Decimal("0")
    failure_count_90d = cost_90d.failure_count if cost_90d else 0
    last_activity = (
        related_wos[0].updated_at if related_wos else machine.created_at
    )
    last_activity_days = (timezone.now() - last_activity).days

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
        "cost_periods": cost_periods,
        "can_see_cost": request.user.role in (
            User.Role.MANAGER, User.Role.SUPERVISOR,
            User.Role.PROCUREMENT, User.Role.SUPER_ADMIN,
        ),
        "hero_stats": {
            "total_wo_count": total_wo_count,
            "cost_90d_total": cost_90d_total,
            "failure_count_90d": failure_count_90d,
            "last_activity_days": last_activity_days,
        },
        "pm_stats": pm_stats,
        "pm_last_executions": last_executions_by_schedule,
        "now": timezone.now(),
    }
    if machine is not None:
        context["attachments"] = Attachment.objects.filter(
            entity_type="machine", entity_id=machine.pk
        ).select_related("uploaded_by").order_by("-uploaded_at")
    return render(request, "maintenance/machine_detail.html", context)


@login_required
@role_required(User.Role.OPERATOR, User.Role.MANAGER, User.Role.SUPERVISOR, User.Role.SUPER_ADMIN)
def issue_list(request):
    """List maintenance issues.

    Phase 6 filters:
    - ?mine=1        → restrict to issues reported by the current user.
                       Operators see their own issues by default; this lets
                       managers/supervisors also scope the list to one
                       reporter via a follow-up ?reported_by=<id> filter
                       (TODO: not wired in this commit).
    - ?period=<days> → restrict to issues created within the last N days.
                       Valid values: 7, 30, 90. Default: no period filter.
    """
    qs = MaintenanceIssue.objects.select_related("machine", "reported_by")
    if request.user.role == User.Role.OPERATOR and not request.user.is_super_admin_role():
        qs = qs.filter(reported_by=request.user)
    # Phase 6: per-user scoping
    if request.GET.get("mine") == "1":
        qs = qs.filter(reported_by=request.user)
    # Phase 6: period filter
    try:
        period_days = int(request.GET.get("period", "0"))
    except (TypeError, ValueError):
        period_days = 0
    if period_days in (7, 30, 90):
                qs = qs.filter(created_at__gte=timezone.now() - timedelta(days=period_days))
    else:
        # Invalid or missing period — treat as "all time" for the template
        # so the filter bar shows the All Time pill as active.
        period_days = 0
    # Counts for the operator's "reports this month" panel
    mine_count = qs.filter(reported_by=request.user).count() if request.GET.get("mine") == "1" else None
    return render(
        request,
        "maintenance/issue_list.html",
        {
            "issues": qs[:200],
            "period_days": period_days,
            "mine_only": request.GET.get("mine") == "1",
            "mine_count": mine_count,
        },
    )


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
    resolved_machine_id = None
    resolved_component_id = None
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
                    _("Emergency issue reported. Manager has been paged."),
                )
            else:
                messages.success(request, _("Issue reported."))

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
        # POST form is invalid — fall through to re-render with errors.
        # Populate resolved ids from POST data so the asset tree still works.
        resolved_machine_id = request.POST.get("machine") or None
        resolved_component_id = request.POST.get("component") or None
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
        messages.warning(request, _("Issue is not in NEW state."))
        return redirect("issue_list")
    if request.method == "POST":
        form = ValidateIssueForm(request.POST)
        if form.is_valid():
            validate_issue(issue, actor=request.user, priority=form.cleaned_data["priority"])
            messages.success(request, _("Issue validated."))
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
        messages.info(request, _("Issue is already flagged as emergency."))
        return redirect("issue_detail", pk=issue.pk)
    escalate_issue_to_emergency(issue, actor=request.user)
    messages.warning(
        request,
        _("Issue escalated to EMERGENCY (priority CRITICAL). Manager has been paged."),
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

    # Phase 7.6: build parts_display with effective unit cost + committed/issued
    # amounts for the Parts table. Uses the same fallback chain as the
    # _effective_unit_cost helper in inventory/services.py and the model
    # recalculate method (line.unit_cost > 0 → last_purchase_cost → avg_cost).
    parts_display = []
    for line in part_issues:
        eff_uc = (
            line.unit_cost
            if (line.unit_cost and line.unit_cost > 0)
            else (line.part.last_purchase_cost or line.part.avg_cost or Decimal("0"))
        )
        parts_display.append({
            "line": line,
            "effective_unit_cost": eff_uc,
            "committed_amount": (line.approved_qty or Decimal("0")) * eff_uc,
            "issued_amount": (line.issued_qty or Decimal("0")) * eff_uc,
        })
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

    # Inline PM checklist: when the WO is preventive and assigned to the
    # current technician (or any manager-level reviewer), expose the
    # checklist so it can be rendered inside the action panel without
    # requiring the technician to navigate to /pm/wo/<pk>/.
    pm_checklist_items = []
    can_complete_pm = False
    if wo.category == WorkOrder.Category.PREVENTIVE:
        pm_checklist_items = _resolve_pm_checklist(wo)
        if pm_checklist_items:
            can_complete_pm = (
                wo.lifecycle_status in (
                    WorkOrder.LifecycleStatus.ASSIGNED,
                    WorkOrder.LifecycleStatus.IN_PROGRESS,
                    WorkOrder.LifecycleStatus.PENDING_REVIEW,
                )
                and (
                    wo.assigned_technician_id == request.user.id
                    or request.user.role in (
                        User.Role.MANAGER, User.Role.SUPER_ADMIN, User.Role.SUPERVISOR,
                    )
                )
            )

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

    # UX: pre-fill the shortage decision form with realistic defaults so the
    # manager doesn't have to do the math (issue+procure+reject must sum to
    # qty_requested, and only `usable_qty_snapshot` units can actually be
    # issued from stock today). Default plan: issue the max we can, procure
    # the rest, reject nothing. The form fields are still editable.
    # Also tag each report with `line_id` (the related PartIssueLine PK, used
    # by the shortage badge anchor on the Parts table).
    _pending_shortage_part_ids = set()
    for _r in pending_shortage_reports:
        _usable = _r.usable_qty_snapshot or Decimal("0")
        _requested = _r.qty_requested or Decimal("0")
        # Treat negative usable snapshots as 0 (defensive).
        if _usable < 0:
            _usable = Decimal("0")
        _r.suggested_issue_qty = min(_requested, _usable)
        _r.suggested_procure_qty = max(Decimal("0"), _requested - _usable)
        _r.suggested_reject_qty = Decimal("0")
        # Use a normalized form (no trailing zeros) for the hint so the
        # UI text reads naturally ("Stock has 2 of 4 needed…" not
        # "Stock has 2.000 of 4.000 needed…").
        def _fmt(d: Decimal) -> str:
            d = d.normalize() if d != 0 else Decimal("0")
            return format(d, "f")
        _r.form_default_hint = (
            f"Stock has {_fmt(_usable)} of {_fmt(_requested)} needed. "
            f"Default: issue {_fmt(_r.suggested_issue_qty)}, "
            f"procure {_fmt(_r.suggested_procure_qty)}."
        )
        # Anchor link target: prefer the related PartIssueLine PK if any
        # (the shortage form lives once per report, but the badge on the
        # Parts table points to the line).
        _first_line = _r.issue_lines.first()
        _r.line_id = _first_line.pk if _first_line else _r.pk
        _pending_shortage_part_ids.add(_r.part_id)

    # UX: annotate each parts_display row with whether a pending shortage
    # exists for (wo, part) — drives the "⚠ N in shortage" badge on the
    # Parts table.
    for _item in parts_display:
        _item["has_pending_shortage"] = (
            _item["line"].part_id in _pending_shortage_part_ids
        )

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

    # Bug #1 fix: resolve the default site ONCE, up front. It is needed both
    # for per-line free-stock computation below AND for the part dropdown
    # annotation a few lines further down.
    _default_site = Site.objects.filter(is_default=True).first()

    # Phase 7.5: All PartIssueLines on this WO that are awaiting
    # warehouse execution (status=APPROVED or ALLOCATED, issued_qty <
    # approved_qty, with stock available). Includes lines that came
    # through the simple request_part_on_wo + approve_part_request
    # flow (not just the shortage-decision flow).
    lines_awaiting_issue = []
    for line in part_issues.filter(
        status__in=[PartIssueLine.Status.APPROVED, PartIssueLine.Status.ALLOCATED],
    ).select_related("part"):
        approved = line.approved_qty or Decimal("0")
        issued = line.issued_qty or Decimal("0")
        if approved <= 0 or issued >= approved:
            continue
        remaining = approved - issued
        # Only show if there's stock available (free_stock >= remaining).
        # Phase 7.8: use PartAllocationService.free_stock_for_part (live
        # aggregate of ACTIVE reservations) instead of the deprecated
        # `quantity_reserved` DB field.
        if _default_site:
            from inventory.services_allocation import PartAllocationService
            free = PartAllocationService.free_stock_for_part(line.part)
        else:
            free = Decimal("0")
        lines_awaiting_issue.append({
            "line": line,
            "remaining": remaining,
            "free_stock": free,
            "has_stock": free >= remaining,
        })

    # Annotate free_stock = on-hand minus reserved so the dropdown shows
    # the actual usable quantity per part (visible without JS interaction).
    # Phase 7.8: the dropdown is a UX display, so we use the cached
    # `quantity_reserved` field (which the signal keeps in sync). Business
    # logic (shortage decisions, allocation) MUST use
    # `PartAllocationService.free_stock_for_part` which queries the live
    # InventoryReservation aggregate.
    if _default_site:
        # Phase 7.8+: free_stock is computed via a subquery that aggregates
        # ACTIVE InventoryReservation rows (the source of truth since
        # the deprecated Inventory.quantity_reserved DB field was dropped).
        from django.db.models import OuterRef, Subquery, Sum
        from inventory.models import InventoryReservation
        _reserved_subq = (
            InventoryReservation.objects
            .filter(part=OuterRef("pk"), status=InventoryReservation.Status.ACTIVE)
            .values("part")
            .annotate(total=Sum("quantity"))
            .values("total")[:1]
        )
        _free_subq = (
            Inventory.objects.filter(part=OuterRef("pk"), site=_default_site)
            .annotate(
                free=F("quantity_available") - Coalesce(Subquery(_reserved_subq), Value(Decimal("0")))
            )
            .values("free")[:1]
        )
        active_parts = (
            SparePart.objects.filter(status="active")
            .annotate(free_stock=Coalesce(Subquery(_free_subq), Value(Decimal("0"))))
            .order_by("name")
        )
    else:
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

    # Phase 7.8: per-WO reservation panel. ACTIVE rows = current soft
    # claims; recent RELEASED/CANCELLED rows show the recent lifecycle.
    from inventory.models import InventoryReservation
    active_reservations = (
        InventoryReservation.objects
        .filter(work_order=wo, status=InventoryReservation.Status.ACTIVE)
        .select_related("part", "source_line")
        .order_by("created_at")
    )
    historical_reservations = (
        InventoryReservation.objects
        .filter(work_order=wo)
        .exclude(status=InventoryReservation.Status.ACTIVE)
        .select_related("part", "source_line")
        .order_by("-released_at", "-created_at")[:20]
    )

    return render(
        request,
        "maintenance/workorder_detail.html",
        {
            "wo": wo,
            "logs": logs,
            "issue_attachments": issue_attachments,
            "part_issues": part_issues,
            "parts_display": parts_display,
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
            # v5: ERO that's RETURNED but not yet CLOSED — shows the "vendor
            # returned, awaiting acceptance" badge in the health card.
            # Order by -pk since ExternalRepairOrder has no `returned_at`
            # timestamp field (the RETURNED transition is set via status
            # field directly without a dedicated timestamp).
            "pending_returned_ero": ExternalRepairOrder.objects.filter(
                work_order=wo,
                status=ExternalRepairOrder.Status.RETURNED,
            ).order_by("-pk").first(),
            "pending_shortage_reports": pending_shortage_reports,
            "approved_shortage_reports": approved_shortage_reports,
            "decided_shortage_reports": decided_shortage_reports,
            "pending_warehouse_issues": pending_warehouse_issues,
            "lines_awaiting_issue": lines_awaiting_issue,
            "active_parts": active_parts,
            "last_request_result": last_request_result,
            # Inline PM checklist (rendered in _wo_actions_technician.html
            # so the technician can tick items without leaving the WO page).
            "pm_checklist_items": pm_checklist_items,
            "can_complete_pm": can_complete_pm,
            # Phase 3A additions (health card + blocker panels)
            "health_card": health_card,
            "active_blockers": active_blockers,
            "blocker_history": blocker_history,
            # Phase 7.8: per-WO reservation panel data
            "active_reservations": active_reservations,
            "historical_reservations": historical_reservations,
            # Phase 1+2 Cost Ledger additions
            # Cost is visible only to roles that need it for planning/procurement.
            # Operators and technicians do not see cost anywhere in the system.
            "cost": getattr(wo, "cost_record", None) if request.user.role in (
                User.Role.MANAGER, User.Role.SUPERVISOR,
                User.Role.PROCUREMENT, User.Role.SUPER_ADMIN,
            ) else None,
            "can_see_cost": request.user.role in (
                User.Role.MANAGER, User.Role.SUPERVISOR,
                User.Role.PROCUREMENT, User.Role.SUPER_ADMIN,
            ),
            "can_manage_costs": request.user.role in (
                User.Role.MANAGER, User.Role.SUPER_ADMIN,
            ),
            "adjustment_form": CostAdjustmentForm(),
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
        messages.error(request, _("Issue must be validated first."))
        return redirect("issue_list")
    if hasattr(issue, "work_order") and issue.work_order_id:
        messages.info(request, _("Work order already exists for this issue."))
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
        note=_(f"Created from validated issue #{issue.pk}"),
    )
    log_audit(actor=request.user, action="wo_created", entity="WorkOrder", object_id=wo.pk)
    from .notifications import notify_wo_created
    notify_wo_created(wo)
    messages.success(request, _(f"Work order WO-{wo.number} created."))
    return redirect("work_order_detail", pk=wo.pk)


@login_required
@role_required(User.Role.MANAGER, User.Role.SUPER_ADMIN)
def work_order_assign(request, pk):
    wo = get_object_or_404(WorkOrder, pk=pk)
    previous_technician_id = wo.assigned_technician_id
    if wo.lifecycle_status not in (
        WorkOrder.LifecycleStatus.ASSIGNED,
    ) and wo.operational_status != WorkOrder.OperationalStatus.PAUSED:
        messages.error(request, _("Work order cannot be assigned in current state."))
        return redirect("work_order_detail", pk=pk)
    if request.method != "POST":
        return redirect("work_order_detail", pk=pk)
    form = AssignTechnicianForm(request.POST)
    if not form.is_valid():
        messages.error(request, _("Invalid assignment."))
        return redirect("work_order_detail", pk=pk)
    new_technician = form.cleaned_data["technician"]
    wo.assigned_technician = new_technician
    wo.save(update_fields=["assigned_technician", "updated_at"])
    transition_work_order(wo, WorkOrder.LifecycleStatus.ASSIGNED, actor=request.user, note=_("Technician assigned"))
    WorkOrderAssignmentHistory.objects.create(
        work_order=wo,
        technician=new_technician,
        action=WorkOrderAssignmentHistory.Action.ASSIGNED,
        assigned_by=request.user,
        reason=_(f"Assigned by {request.user.get_full_name() or request.user.username}"),
    )
    if previous_technician_id and previous_technician_id != new_technician.id:
        old_technician = User.objects.get(pk=previous_technician_id)
        prev = WorkOrderAssignmentHistory.objects.filter(
            work_order=wo, technician=old_technician, unassigned_at__isnull=True
        ).first()
        if prev:
            prev.unassigned_at = timezone.now()
            prev.reason = _(f"Reassigned to {new_technician.get_full_name() or new_technician.username}")
            prev.save()
    from .notifications import notify_wo_assigned

    notify_wo_assigned(wo)
    messages.success(request, _("Technician assigned."))
    return redirect("work_order_detail", pk=pk)


@login_required
@require_POST
@role_required(User.Role.TECHNICIAN, User.Role.SUPER_ADMIN)
def work_order_start(request, pk):
    wo = get_object_or_404(WorkOrder, pk=pk)
    if wo.assigned_technician != request.user:
        messages.error(request, _("You can only start work orders assigned to you."))
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
        messages.error(request, _("Cannot start work in this state."))
        return redirect("work_order_detail", pk=pk)
    # Emergency precedence check (SRS UC-06 step 2D).
    # A non-emergency WO cannot be transitioned to IN_PROGRESS while
    # another emergency WO is already IN_PROGRESS for the same technician.
    # Starting an emergency itself is always allowed.
    if not wo.is_emergency and has_active_emergency(request.user, except_pk=wo.pk):
        messages.error(
            request,
            _("You have an active emergency work order. Finish it before starting another task."),
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
    messages.success(request, _("Work started."))
    return redirect("work_order_detail", pk=pk)


@login_required
@require_POST
@role_required(User.Role.TECHNICIAN, User.Role.SUPER_ADMIN)
def work_order_pause(request, pk):
    wo = get_object_or_404(WorkOrder, pk=pk)
    if wo.assigned_technician_id != request.user.id and not request.user.is_super_admin_role():
        raise Http404()
    if wo.assigned_technician != request.user:
        messages.error(request, _("You can only pause work orders assigned to you."))
        return redirect("work_order_detail", pk=wo.pk)
    if wo.lifecycle_status != WorkOrder.LifecycleStatus.IN_PROGRESS:
        messages.error(request, _("Not in progress."))
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
    messages.info(request, _("Paused."))
    return redirect("work_order_detail", pk=wo.pk)


@login_required
@require_POST
@role_required(User.Role.TECHNICIAN, User.Role.SUPER_ADMIN)
def work_order_submit(request, pk):
    wo = get_object_or_404(WorkOrder, pk=pk)
    if wo.assigned_technician != request.user:
        messages.error(request, _("You can only submit work orders assigned to you."))
        return redirect("work_order_detail", pk=wo.pk)
    if wo.assigned_technician_id != request.user.id and not request.user.is_super_admin_role():
        raise Http404()

    # PM WOs may submit directly from `assigned` lifecycle (UX shortcut):
    # technician fills the checklist, clicks "Start work & submit", and the
    # view auto-starts labor then submits for review in one transaction.
    # Non-PM WOs still require explicit Start work first.
    is_pm = wo.category == WorkOrder.Category.PREVENTIVE
    if not is_pm and wo.lifecycle_status != WorkOrder.LifecycleStatus.IN_PROGRESS:
        messages.error(request, _("Submit for review is only available while work is in progress."))
        return redirect("work_order_detail", pk=pk)
    if is_pm and wo.lifecycle_status not in (
        WorkOrder.LifecycleStatus.ASSIGNED,
        WorkOrder.LifecycleStatus.IN_PROGRESS,
    ):
        messages.error(request, _("Submit for review is not available in this state."))
        return redirect("work_order_detail", pk=pk)

    form = WorkOrderCompleteForm(request.POST, instance=wo)
    if not form.is_valid():
        messages.error(request, _("Check completion fields."))
        return redirect("work_order_detail", pk=pk)

    # Auto-start labor for PM WOs that are still in `assigned` state.
    # This stitches Start + Complete + Submit into a single click.
    if is_pm and wo.lifecycle_status == WorkOrder.LifecycleStatus.ASSIGNED:
        technician_start_work(wo, request.user)
        wo.refresh_from_db()

    form.save()

    # Inline PM checklist: when the WO is preventive and the POST carries
    # checklist_<i> / note_<i> keys, overwrite action_taken with the
    # structured summary expected by pm_review ([✓]/[✗] markers).
    # Same format as _pm_wo_detail_context/PM work order page.
    if is_pm:
        has_checklist_data = any(k.startswith("checklist_") for k in request.POST)
        if has_checklist_data:
            checklist_items = _resolve_pm_checklist(wo)
            if checklist_items:
                wo.action_taken = _build_pm_action_taken(checklist_items, request.POST)
                wo.save(update_fields=["action_taken"])

    # Phase 3+ reject-loop: if the PMExecution was REJECTED, this resubmit
    # transitions it back to SUBMITTED so the manager sees the next attempt.
    if is_pm:
        pm_execution = getattr(wo, "pm_execution", None)
        if pm_execution and pm_execution.status == PMExecution.Status.REJECTED:
            pm_execution.status = PMExecution.Status.SUBMITTED
            pm_execution.approved_by = None
            pm_execution.approved_at = None
            pm_execution.completed_by = request.user
            pm_execution.completed_at = timezone.now()
            pm_execution.save(update_fields=[
                "status", "approved_by", "approved_at",
                "completed_by", "completed_at",
            ])

    technician_submit_for_review(wo, request.user)
    messages.success(request, _("Submitted for manager review."))
    return redirect("work_order_detail", pk=pk)


@login_required
@role_required(User.Role.MANAGER, User.Role.SUPER_ADMIN)
def work_order_close(request, pk):
    wo = get_object_or_404(WorkOrder, pk=pk)
    if wo.lifecycle_status != WorkOrder.LifecycleStatus.PENDING_REVIEW:
        messages.error(request, _("Work order is not pending review."))
        return redirect("work_order_detail", pk=pk)
    if request.method != "POST":
        return redirect("work_order_detail", pk=pk)
    action = request.POST.get("action")
    rejection_reason = request.POST.get("rejection_reason", "").strip()
    if action != "approve" and not rejection_reason:
        messages.error(request, _("Rejection reason is required when returning to technician."))
        return redirect("work_order_detail", pk=pk)
    try:
        manager_close_work_order(wo, request.user, approve=(action == "approve"), rejection_reason=rejection_reason)
        if action == "approve":
            messages.success(request, _("Work order closed."))
        else:
            messages.info(request, _("Work order returned to technician."))
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
        messages.error(request, _("Invalid part issue."))
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
        messages.error(request, _("You can only request parts on work orders assigned to you."))
        return redirect("work_order_detail", pk=wo.pk)
    if wo.lifecycle_status == WorkOrder.LifecycleStatus.CLOSED:
        messages.error(request, _("Cannot request parts on a closed work order."))
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
            _(f"Part request submitted ({form.cleaned_data['quantity']} × "
              f"{form.cleaned_data['part'].name}). Awaiting manager approval."),
        )
    else:
        messages.success(
            request,
            _(f"Emergency auto-approval: {form.cleaned_data['quantity']} × "
              f"{form.cleaned_data['part'].name} deducted from stock immediately. "
              f"Manager will review."),
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
        return HttpResponseForbidden(_("You can only re-review your own requests."))
    if line.status != PartIssueLine.Status.REJECTED:
        messages.error(request, _("Only rejected lines can be re-reviewed."))
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
        reserved = inv.compute_quantity_reserved()
    usable = on_hand - reserved
    if usable < new_qty:
        # Only refuse if SOME stock exists but it's insufficient. If
        # zero stock exists, allow the request — the manager will see
        # a shortage and raise a shortage report.
        if on_hand > 0:
            stock_error = (
                _(f"Only {usable:g} in stock for {new_part.sku}. "
                  f"Edit qty to {usable:g}, switch to a different part, or use the shortage flow.")
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
            _(f"Re-review of #{line.pk}")
            + ("" if unchanged else _(f" — edited: {line.part.sku} qty {line.quantity} → {new_part.sku} qty {new_qty}"))
            + (_(f". Tech note: {new_note}") if new_note else "")
        ),
        previous_attempt=line,
        unit_cost=new_part.last_purchase_cost or new_part.avg_cost or 0,
        issued_by=request.user,
    )
    from .notifications import notify_part_request_re_review
    notify_part_request_re_review(new_line, line)
    messages.success(
        request,
        _(f"Re-review submitted: {new_qty:g}× {new_part.name}. "
          f"{'Same as before' if unchanged else _('Edits sent to manager')}."),
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
        messages.error(request, _("Please record a voice note before submitting."))
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
        messages.error(request, _("Voice attachment not found or not owned by you."))
        return redirect("work_order_detail", pk=pk)
    messages.success(request, _(f"Voice note added to WO-{wo.number}."))
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
        messages.error(request, _("Only PENDING requests can be approved."))
        return redirect("work_order_detail", pk=wo.pk)
    try:
        approve_part_request(line=line, manager=request.user)
        messages.success(
            request,
            _(f"Approved {line.quantity} × {line.part.name} — stock deducted."),
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
        messages.error(request, _("Only PENDING requests can be decided."))
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
                _(f"Approved {line.quantity} × {line.part.name} — stock deducted."),
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
                _(f"Rejected {line.part.name} request. Tech has been notified."),
            )
        elif action == "edit":
            edit_part_request_qty(
                line=line,
                manager=request.user,
                new_quantity=form.cleaned_data["new_qty"],
            )
            messages.success(
                request,
                _(f"Updated qty to {form.cleaned_data['new_qty']} for {line.part.name}."),
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
        _("External repair request submitted. Manager has been notified."),
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
        messages.error(request, _("Only PENDING requests can be decided."))
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
                _(f"External Repair Order #{ero.pk} created. The supply officer "
                  "can now send the part to the vendor."),
            )
        else:
            reject_external_repair_request(
                err=err, manager=request.user, manager_note=note
            )
            messages.info(request, _("External repair request rejected."))
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
            transition_work_order(wo, WorkOrder.LifecycleStatus.ASSIGNED, actor=request.user, note=_("Emergency WO created"))
            log_audit(actor=request.user, action="emergency_wo", entity="WorkOrder", object_id=wo.pk)
            from .notifications import notify_emergency_work_order

            notify_emergency_work_order(wo)
            messages.success(request, _(f"Emergency work order WO-{wo.number} created."))
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
        messages.error(request, _("You can only mark parts for work orders assigned to you."))
        return redirect("work_order_detail", pk=wo.pk)
    if wo.assigned_technician_id != request.user.id and not request.user.is_super_admin_role():
        raise Http404()
    form = TechVendorNoteForm(request.POST, prefix="parts")
    if not form.is_valid():
        messages.error(request, _("Invalid form."))
        return redirect("work_order_detail", pk=pk)
    try:
        technician_mark_pending_parts(wo, request.user, note=form.cleaned_data.get("note") or "")
        messages.warning(request, _("Work order set to waiting for parts (labor timer stopped)."))
    except ValueError as e:
        messages.error(request, str(e))
    return redirect("work_order_detail", pk=pk)


@login_required
@require_POST
@role_required(User.Role.TECHNICIAN, User.Role.SUPER_ADMIN)
def work_order_mark_vendor(request, pk):
    wo = get_object_or_404(WorkOrder, pk=pk)
    if wo.assigned_technician != request.user:
        messages.error(request, _("You can only mark vendor status for work orders assigned to you."))
        return redirect("work_order_detail", pk=wo.pk)
    if wo.assigned_technician_id != request.user.id and not request.user.is_super_admin_role():
        raise Http404()
    form = TechVendorNoteForm(request.POST, prefix="vendor")
    if not form.is_valid():
        messages.error(request, _("Invalid form."))
        return redirect("work_order_detail", pk=pk)
    try:
        technician_mark_waiting_vendor(wo, request.user, note=form.cleaned_data.get("note") or "")
        messages.warning(request, _("Work order set to waiting for vendor (labor timer stopped)."))
    except ValueError as e:
        messages.error(request, str(e))
    return redirect("work_order_detail", pk=pk)


@login_required
@role_required(User.Role.MANAGER, User.Role.PROCUREMENT, User.Role.SUPER_ADMIN)
def stock_dashboard(request):
    """Site-aware stock dashboard with search, filters, and per-site inventory."""
    from django.db.models import OuterRef, Subquery, Sum, Value
    from django.db.models.functions import Coalesce
    from inventory.models import Inventory, InventoryReservation, SparePart
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

    # Phase 7.8+: reserved qty is computed from ACTIVE InventoryReservation rows
    # (the legacy Inventory.quantity_reserved DB field was dropped in migration 0017).
    _reserved_subq = (
        InventoryReservation.objects
        .filter(
            part=OuterRef("pk"),
            status=InventoryReservation.Status.ACTIVE,
        )
        .values("part")
        .annotate(total=Sum("quantity"))
        .values("total")[:1]
    )

    parts_qs = SparePart.objects.annotate(
        inv_available=Subquery(
            Inventory.objects.filter(
                part=OuterRef("pk"),
                site=selected_site
            ).values("quantity_available")[:1]
        ),
        inv_reserved=Coalesce(Subquery(_reserved_subq), Value(Decimal("0"))),
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


# NOTE: The stock_in_view and part_stock_in view functions previously lived
# here. They were moved to inventory.views as part of the supplier-intelligence
# refactor (Commit 2). The URL name `stock_in` now resolves to
# inventory.views.stock_in_view via inventory.urls.


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
            messages.error(request, _("You do not have permission to perform this action."))
            return redirect("consumables")
    from inventory.models import ConsumableAssignment
    from django.db.models import Sum
    from datetime import timedelta as _td
    now = timezone.now()
    # Phase 6: history is now period-filterable via ?period=7|30|90.
    try:
        period_days = int(request.GET.get("period", "0"))
    except (TypeError, ValueError):
        period_days = 0
    if period_days not in (7, 30, 90):
        period_days = 0
    my_assignments = ConsumableAssignment.objects.filter(consumed_by=request.user)
    if period_days:
        my_assignments = my_assignments.filter(created_at__gte=now - _td(days=period_days))
    assignments = my_assignments.select_related("part", "machine").order_by("-created_at")[:20]
    issued_assignments = ConsumableAssignment.objects.none()
    if caps.get("issue_consumables"):
        issued_assignments = ConsumableAssignment.objects.filter(
            issued_by=request.user
        ).exclude(consumed_by=request.user).select_related("part", "machine", "consumed_by").order_by("-created_at")[:20]
    # Phase 6: per-user consumption counters for the badge bar.
    # today = since midnight, 7d = last 7 days, 30d = last 30 days.
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    my_all = ConsumableAssignment.objects.filter(consumed_by=request.user)
    my_today = my_all.filter(created_at__gte=midnight)
    my_7d = my_all.filter(created_at__gte=now - _td(days=7))
    my_30d = my_all.filter(created_at__gte=now - _td(days=30))
    # Most-used part in the last 30d (operator self-reflection)
    top_part_30d = (
        my_30d.values("part__name", "part__sku")
        .annotate(total=Sum("quantity"))
        .order_by("-total")
        .first()
    )
    return render(request, "maintenance/consumables.html", {
        "consume_form": consume_form,
        "issue_form": issue_form,
        "assignments": assignments,
        "issued_assignments": issued_assignments,
        "period_days": period_days,
        "my_today_count": my_today.count(),
        "my_today_qty": my_today.aggregate(total=Sum("quantity"))["total"] or 0,
        "my_7d_count": my_7d.count(),
        "my_7d_qty": my_7d.aggregate(total=Sum("quantity"))["total"] or 0,
        "my_30d_count": my_30d.count(),
        "my_30d_qty": my_30d.aggregate(total=Sum("quantity"))["total"] or 0,
        "top_part_30d": top_part_30d,
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
    """Supplier detail with linked parts, recent PRs, repair history,
    and stock-received history.

    Per MVP (locked plan Phase 6+7): two simple chronological tables
    instead of a complex overview dashboard.
    """
    from decimal import Decimal
    from django.db.models import Sum, Count, Q

    supplier = get_object_or_404(Supplier, pk=pk)

    linked_parts = supplier.parts.order_by("name")[:50]
    recent_prs = supplier.purchase_requests.filter(
        status=PurchaseRequest.Status.PENDING
    ).order_by("-created_at")[:10]

    # Repair history (FK match; vendor_name snapshot rows with NULL FK
    # are surfaced too, marked "(legacy)").
    repair_history = (
        supplier.external_repair_orders
        .select_related("machine", "component")
        .order_by("-created_at")[:50]
    )
    legacy_repairs = (
        ExternalRepairOrder.objects
        .filter(supplier__isnull=True, vendor_name__iexact=supplier.name)
        .exclude(pk__in=supplier.external_repair_orders.values_list("pk", flat=True))
        .select_related("machine", "component")
        .order_by("-created_at")[:50]
    )

    # Repair totals (use annotate + aggregate; never load rows for sums).
    repair_agg = supplier.external_repair_orders.aggregate(
        total=Sum("actual_cost"),
        count=Count("id"),
    )
    repair_total = repair_agg["total"] or Decimal("0")
    repair_count = repair_agg["count"] or 0
    repair_avg = (
        (repair_total / repair_count) if repair_count else Decimal("0")
    )

    # Stock-received history (STOCK_IN movements linked via FK).
    stock_history = (
        supplier.stock_movements
        .filter(movement_type=StockMovement.MovementType.STOCK_IN)
        .select_related("part", "work_order")
        .order_by("-created_at")[:50]
    )
    legacy_stock = (
        StockMovement.objects
        .filter(
            supplier__isnull=True,
            supplier_name__iexact=supplier.name,
            movement_type=StockMovement.MovementType.STOCK_IN,
        )
        .exclude(pk__in=supplier.stock_movements.values_list("pk", flat=True))
        .select_related("part", "work_order")
        .order_by("-created_at")[:50]
    )

    # Parts purchases totals — sum of (qty * unit_cost) for STOCK_IN movements.
    # Use F() expressions so we don't load every movement into Python.
    from django.db.models import F, FloatField
    from django.db.models.functions import Cast
    parts_purchases_agg = (
        supplier.stock_movements
        .filter(movement_type=StockMovement.MovementType.STOCK_IN)
        .aggregate(
            total=Sum(
                F("quantity") * F("unit_cost"),
                output_field=FloatField(),
            ),
            count=Count("id"),
        )
    )
    parts_total = parts_purchases_agg["total"] or Decimal("0")
    parts_count = parts_purchases_agg["count"] or 0

    return render(request, "maintenance/supplier_detail.html", {
        "supplier": supplier,
        "linked_parts": linked_parts,
        "recent_prs": recent_prs,
        "repair_history": repair_history,
        "legacy_repairs": legacy_repairs,
        "repair_count": repair_count,
        "repair_total": repair_total,
        "repair_avg": repair_avg,
        "stock_history": stock_history,
        "legacy_stock": legacy_stock,
        "parts_count": parts_count,
        "parts_total": parts_total,
    })


@login_required
@role_required(User.Role.MANAGER, User.Role.PROCUREMENT, User.Role.SUPER_ADMIN)
def supplier_create(request):
    """Create a new supplier."""
    if request.method == "POST":
        form = SupplierForm(request.POST)
        if form.is_valid():
            supplier = form.save()
            messages.success(request, _(f"Supplier '{supplier.name}' created with code {supplier.code}."))
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
            messages.success(request, _(f"Supplier '{supplier.name}' updated."))
            return redirect("supplier_detail", pk=supplier.pk)
    else:
        form = SupplierForm(instance=supplier)
    return render(request, "maintenance/supplier_form.html", {
        "form": form,
        "supplier": supplier,
        "page_heading": f"Edit supplier — {supplier.code}",
    })


@login_required
@role_required(User.Role.MANAGER, User.Role.PROCUREMENT, User.Role.SUPER_ADMIN)
def supplier_export_csv(request, pk):
    """Sectioned CSV export for a single supplier: header + Repairs + Stock.

    Writes UTF-8 with BOM so Excel auto-detects encoding and renders Arabic
    supplier names, notes, and part names correctly. Filename uses RFC 5987
    for non-ASCII filename support.
    """
    import csv
    from urllib.parse import quote

    from django.http import HttpResponse
    from inventory.models import StockMovement
    from maintenance.models import ExternalRepairOrder

    supplier = get_object_or_404(Supplier, pk=pk)

    response = HttpResponse(content_type="text/csv; charset=utf-8")
    ascii_name = f"supplier_{supplier.code or supplier.pk}.csv"
    utf8_name = ascii_name
    response["Content-Disposition"] = (
        f'attachment; filename="{ascii_name}"; '
        f"filename*=UTF-8''{quote(utf8_name)}"
    )
    # UTF-8 BOM so Excel detects encoding (Arabic renders correctly).
    response.write("\ufeff")

    writer = csv.writer(response)

    # Section: Supplier header
    writer.writerow(["Supplier Information"])
    writer.writerow(["Code", "Name", "Contact", "Phone", "Email", "Status"])
    writer.writerow([
        supplier.code or "",
        supplier.name,
        supplier.contact_person or "",
        supplier.phone or "",
        supplier.email or "",
        "Active" if supplier.is_active else "Inactive",
    ])
    writer.writerow([])

    # Section: Repairs
    eros = (
        ExternalRepairOrder.objects
        .filter(supplier=supplier)
        .select_related("machine", "component")
        .order_by("-created_at")
    )
    writer.writerow(["Repair History"])
    writer.writerow([
        "Date", "Repair #", "Machine", "Component", "Vendor", "Invoice",
        "Invoice Date", "Cost", "Status",
    ])
    for ero in eros:
        writer.writerow([
            ero.created_at.date().isoformat(),
            f"ERO-{ero.pk}",
            ero.machine.name if ero.machine_id else "",
            ero.component.name if ero.component_id else "",
            ero.vendor_name or supplier.name,
            ero.invoice_ref or "",
            ero.invoice_date.isoformat() if ero.invoice_date else "",
            str(ero.actual_cost) if ero.actual_cost is not None else "",
            ero.get_status_display(),
        ])
    writer.writerow([])

    # Section: Stock received
    movements = (
        StockMovement.objects
        .filter(
            supplier=supplier,
            movement_type=StockMovement.MovementType.STOCK_IN,
        )
        .select_related("part", "work_order")
        .order_by("-created_at")
    )
    writer.writerow(["Stock Received"])
    writer.writerow([
        "Date", "Part SKU", "Part Name", "Qty", "Unit Cost", "Invoice",
        "Work Order",
    ])
    for m in movements:
        writer.writerow([
            m.created_at.date().isoformat(),
            m.part.sku if m.part_id else "",
            m.part.name if m.part_id else "",
            str(m.quantity),
            str(m.unit_cost) if m.unit_cost is not None else "",
            m.invoice_ref or "",
            f"WO-{m.work_order.number}" if m.work_order_id else "",
        ])

    return response


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
            messages.success(request, _(f"Part '{part.name}' created. SKU: {part.sku}"))
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

    active_filter = request.GET.get("active", "active")
    if active_filter not in ("active", "inactive", "all"):
        active_filter = "active"
    status_filter = request.GET.get("status", "all")
    if status_filter not in ("all", "overdue", "due_soon", "future"):
        status_filter = "all"
    machine_filter = request.GET.get("machine", "")

    qs = PMSchedule.objects.select_related("template", "machine")
    if active_filter == "active":
        qs = qs.filter(is_active=True)
    elif active_filter == "inactive":
        qs = qs.filter(is_active=False)

    now = timezone.now()
    seven_days = now + timedelta(days=7)

    if status_filter == "overdue":
        qs = qs.filter(next_due_at__lt=now)
    elif status_filter == "due_soon":
        qs = qs.filter(next_due_at__gte=now, next_due_at__lte=seven_days)
    elif status_filter == "future":
        qs = qs.filter(next_due_at__gt=seven_days)

    if machine_filter:
        try:
            qs = qs.filter(machine_id=int(machine_filter))
        except (ValueError, TypeError):
            machine_filter = ""

    schedules = qs.order_by("next_due_at")

    schedule_data = []
    for s in schedules:
        days = (s.next_due_at.date() - now.date()).days
        if days < 0:
            color = "danger"
            label = f"{abs(days)}d overdue"
        elif days <= 7:
            color = "warning"
            label = f"in {days}d"
        else:
            color = "muted"
            label = f"in {days}d"
        schedule_data.append({
            "schedule": s,
            "days_until_due": days,
            "days_color": color,
            "days_label": label,
        })

    machines = Machine.objects.filter(is_active=True, asset_level=3).order_by("name")

    return render(request, "maintenance/pm_list.html", {
        "schedules": schedules,
        "schedule_data": schedule_data,
        "machines": machines,
        "active_filter": active_filter,
        "status_filter": status_filter,
        "machine_filter": machine_filter,
        "now": now,
    })


@login_required
@role_required(User.Role.MANAGER, User.Role.SUPER_ADMIN)
def pm_create(request):
    locked_asset = None
    resolved_machine_id = None
    resolved_component_id = None
    if request.method == "POST":
        form = PMScheduleForm(request.POST)
        if form.is_valid():
            schedule = form.save(commit=False)
            if request.user.is_authenticated:
                schedule.created_by = request.user
            schedule.save()
            messages.success(request, _("PM schedule saved."))
            return redirect("pm_list")
    else:
        # Pre-fill from URL params. If component is a level-5 Component, walk
        # the parent chain to find the level-3 Machine (since PMSchedule.machine
        # is required to be level-3).
        initial = {"next_due_at": timezone.now()}
        machine_param = request.GET.get("machine")
        component_param = request.GET.get("component")
        template_param = request.GET.get("template")
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
        if template_param:
            try:
                tpl = PMTemplate.objects.get(pk=int(template_param))
                initial["template"] = tpl.pk
            except (PMTemplate.DoesNotExist, ValueError, TypeError):
                pass

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
            "components_by_machine_json": _components_by_machine_json(),
        },
    )


def _components_by_machine_json():
    """JSON-serializable map: machine_pk → [{pk, name, qr_code}, ...] for level-5
    children of each level-3 machine. Used by the template JS to filter the
    component dropdown when the user changes the machine field.
    """
    import json
    out = {}
    for machine in Machine.objects.filter(is_active=True, asset_level=3):
        comps = [
            {"pk": c.pk, "name": c.name, "qr_code": c.qr_code}
            for c in machine.get_descendant_components()
            if c.is_active
        ]
        if comps:
            out[machine.pk] = comps
    return json.dumps(out)


@login_required
@role_required(User.Role.MANAGER, User.Role.SUPER_ADMIN)
def pm_spawn_wo(request, pk):
    sched = get_object_or_404(
        PMSchedule.objects.select_related("template", "machine"),
        pk=pk,
    )
    if not sched.is_active:
        messages.error(request, _("Cannot spawn a WO from an inactive PM schedule."))
        return redirect("pm_list")
    existing = PMExecution.objects.filter(
        pm_schedule=sched,
        scheduled_due_at=sched.next_due_at,
        status__in=[PMExecution.Status.SUBMITTED, PMExecution.Status.REJECTED],
    ).first()
    if existing:
        messages.warning(
            request,
            _("A PM work order for this schedule's current due date is already in progress."),
        )
        return redirect("work_order_detail", pk=existing.work_order_id)
    has_children = sched.machine.children.filter(is_active=True).exists()
    if request.method == "POST":
        propagate = request.POST.get("propagate_to_children") == "on"
        wo = WorkOrder.objects.create(
            category=WorkOrder.Category.PREVENTIVE,
            machine=sched.machine,
            lifecycle_status=WorkOrder.LifecycleStatus.ASSIGNED,
            created_by=request.user,
            notes=f"PM: {sched.template.title}",
        )
        transition_work_order(wo, WorkOrder.LifecycleStatus.ASSIGNED, actor=request.user, note=_("PM work order"))
        create_pm_execution_for_wo(sched, wo, actor=request.user)
        child_count = 0
        if propagate and has_children:
            for child_machine in sched.machine.children.filter(is_active=True):
                child_wo = WorkOrder.objects.create(
                    category=WorkOrder.Category.PREVENTIVE,
                    machine=child_machine,
                    lifecycle_status=WorkOrder.LifecycleStatus.ASSIGNED,
                    created_by=request.user,
                    notes=f"PM spawned from {sched.machine.name} → {child_machine.name}",
                )
                transition_work_order(
                    child_wo, WorkOrder.LifecycleStatus.ASSIGNED, actor=request.user,
                    note=_(f"PM for {child_machine.name} (child of {sched.machine.name})"),
                )
                child_count += 1
        if child_count:
            messages.success(request, _(f"Created {child_count} child PM work orders."))
        messages.success(request, _(f"PM work order WO-{wo.number} created."))
        return redirect("work_order_detail", pk=wo.pk)
    return render(request, "maintenance/pm_spawn_wo.html", {"schedule": sched, "has_children": has_children})


def _resolve_pm_schedule_for_wo(wo):
    """Return (schedule, pm_execution_or_None) for a PM WO.

    Resolution order:
    1. PMExecution.pm_schedule (authoritative for new WOs)
    2. Most recent PMSchedule for the WO's machine (fallback for legacy
       WOs created before PMExecution existed — picks the schedule that
       was active when the WO was filed).
    """
    pm_execution = None
    try:
        pm_execution = wo.pm_execution
    except PMExecution.DoesNotExist:
        pm_execution = None
    if pm_execution and pm_execution.pm_schedule_id:
        sched = PMSchedule.objects.select_related("template", "machine").filter(
            pk=pm_execution.pm_schedule_id,
        ).first()
        if sched:
            return sched, pm_execution
    sched = (
        PMSchedule.objects.select_related("template", "machine")
        .filter(machine=wo.machine)
        .order_by("-created_at")
        .first()
    )
    return sched, pm_execution


def _resolve_pm_checklist(wo):
    """Return the active checklist for a PM work order, or [].

    Resolution order:
    1. PMExecution.template_snapshot_json.checklist (historical snapshot)
    2. PMExecution.pm_schedule.template.checklist_items (live template)
    3. Most recent PMSchedule for the WO's machine (legacy fallback for
       WOs without a PMExecution — common in pre-Phase-8 data)

    Returns a list of {"text": str, "checked": False}. The `checked` value
    is always False at render time — the technician marks it during submission.
    Returns [] only when no schedule / template can be resolved.
    """
    sched, pm_execution = _resolve_pm_schedule_for_wo(wo)
    if not sched:
        return []

    if pm_execution:
        snapshot_items = (pm_execution.template_snapshot_json or {}).get("checklist", [])
        if snapshot_items:
            return [{"text": item.get("text", ""), "checked": False} for item in snapshot_items]

    return [
        {"text": item.text, "checked": False}
        for item in sched.template.checklist_items.all().order_by("order", "pk")
    ]


def _build_pm_action_taken(checklist_items, post_data):
    """Build the structured action_taken string from checklist submissions.

    Format (consumed by pm_review view):
        [✓] Step 1
          Note: optional note
        [✗] Step 2
          Note: ...

    `checklist_items` is the resolved list of {"text", "checked"} items.
    `post_data` is request.POST — keys "checklist_<i>" = "on" if ticked,
    keys "note_<i>" = note text.
    """
    lines = []
    for i, item in enumerate(checklist_items):
        checked = post_data.get(f"checklist_{i}") == "on"
        note = (post_data.get(f"note_{i}", "") or "").strip()
        marker = "✓" if checked else "✗"
        lines.append(f"[{marker}] {item['text']}")
        if note:
            lines.append(f"  Note: {note}")
    return "\n".join(lines)


def _pm_wo_detail_context(wo, request):
    """Build context for the PM work order detail/execute page.

    Resolves the schedule from the WO's PMExecution (with a legacy fallback
    for WOs without PMExecution). Reads checklist from the schedule's
    template OR the PMExecution snapshot if available. Builds asset tree
    and related-record context.
    """
    from procurement.models import PurchaseRequest

    sched, pm_execution = _resolve_pm_schedule_for_wo(wo)
    checklist_items = _resolve_pm_checklist(wo)

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
    ).exclude(pk=wo.pk)[:10]
    related_pms = PMSchedule.objects.filter(
        machine=wo.machine, component=wo.component
    ).exclude(pk=sched.pk if sched else None)[:10]
    related_eros = ExternalRepairOrder.objects.filter(
        machine=wo.machine, component=wo.component
    )[:10]
    related_prs = PurchaseRequest.objects.filter(
        machine=wo.machine, component=wo.component
    )[:10]

    return {
        "wo": wo,
        "sched": sched,
        "pm_execution": pm_execution,
        "checklist_items": checklist_items,
        "schedule_attachments": Attachment.objects.filter(
            entity_type="pm_schedule", entity_id=sched.pk
        ).select_related("uploaded_by").order_by("-uploaded_at") if sched else Attachment.objects.none(),
        "machine": tree_node,
        "ancestors": ancestors,
        "related_issues": related_issues,
        "related_wos": related_wos,
        "related_pms": related_pms,
        "related_eros": related_eros,
        "related_prs": related_prs,
    }


@login_required
@role_required(
    User.Role.MANAGER, User.Role.SUPER_ADMIN,
    User.Role.TECHNICIAN, User.Role.SUPERVISOR,
)
def pm_wo_detail(request, pk):
    """Dedicated PM work order page for technicians.

    URL: /pm/wo/<pk>/  — where pk is the WORK ORDER pk.
    Shows PM context (template, schedule, machine/component, last completed)
    + the checklist (read-only when lifecycle not in assigned/in_progress)
    + completion notes form. POST transitions the WO to pending_review and
    the PMExecution to submitted.
    """
    wo = get_object_or_404(
        WorkOrder.objects.select_related(
            "machine", "component", "assigned_technician", "created_by",
        ),
        pk=pk,
    )
    if wo.category != WorkOrder.Category.PREVENTIVE:
        messages.error(request, _("This is not a preventive maintenance work order."))
        return redirect("work_order_detail", pk=pk)

    u = request.user
    can_execute = (
        (wo.assigned_technician_id == u.id)
        or (wo.assigned_technician_id is None and u.role == User.Role.TECHNICIAN)
        or u.role in (User.Role.MANAGER, User.Role.SUPER_ADMIN)
    )
    can_execute = can_execute and wo.lifecycle_status in (
        WorkOrder.LifecycleStatus.ASSIGNED,
        WorkOrder.LifecycleStatus.IN_PROGRESS,
        WorkOrder.LifecycleStatus.PENDING_REVIEW,
    )

    ctx = _pm_wo_detail_context(wo, request)
    ctx["can_execute"] = can_execute

    if request.method == "POST":
        if not can_execute:
            messages.error(request, _("You can't execute this PM."))
            return redirect("pm_wo_detail", pk=pk)
        checklist_items = ctx["checklist_items"]
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

        form = WorkOrderCompleteForm(request.POST, instance=wo)
        if form.is_valid():
            form.save()
        wo.action_taken = "\n".join(action_lines)
        wo.save(update_fields=["action_taken"])

        pm_execution = ctx["pm_execution"]
        if pm_execution and pm_execution.status == PMExecution.Status.REJECTED:
            pm_execution.status = PMExecution.Status.SUBMITTED
            pm_execution.approved_by = None
            pm_execution.approved_at = None
            pm_execution.completed_by = request.user
            pm_execution.completed_at = timezone.now()
            pm_execution.save(update_fields=[
                "status", "approved_by", "approved_at",
                "completed_by", "completed_at",
            ])

        technician_submit_for_review(wo, request.user)
        messages.success(request, _("PM submitted for manager review."))
        return redirect("work_order_detail", pk=pk)

    ctx["form"] = WorkOrderCompleteForm(instance=wo)
    return render(request, "maintenance/pm_wo_detail.html", ctx)


@login_required
@role_required(
    User.Role.MANAGER, User.Role.SUPER_ADMIN,
    User.Role.TECHNICIAN, User.Role.SUPERVISOR,
)
def pm_execute(request, pk):
    """Legacy PM execute URL — redirects to the new PM work order page.

    URL: /pm/<pk>/execute/  — redirects to /pm/wo/<pk>/
    """
    return redirect("pm_wo_detail", pk=pk)


@login_required
@role_required(User.Role.MANAGER, User.Role.SUPER_ADMIN)
def pm_review(request, pk):
    wo = get_object_or_404(
        WorkOrder.objects.select_related("machine", "assigned_technician"),
        pk=pk,
    )
    if wo.category != WorkOrder.Category.PREVENTIVE:
        messages.error(request, _("This is not a preventive maintenance work order."))
        return redirect("work_order_detail", pk=pk)

    pm_execution = getattr(wo, "pm_execution", None)
    if pm_execution is None:
        messages.error(request, _("No PM execution record found for this work order."))
        return redirect("work_order_detail", pk=pk)

    if request.method == "POST":
        action = request.POST.get("action", "")
        reason = request.POST.get("reason", "").strip()

        try:
            if action == "approve":
                manager_approve_pm_execution(pm_execution, manager=request.user)
                schedule = pm_execution.pm_schedule
                schedule.refresh_from_db()
                messages.success(request, _(f"PM approved. Schedule advanced to {schedule.next_due_at:%Y-%m-%d %H:%M}."))
                return redirect("work_order_detail", pk=pk)
            elif action == "reject":
                manager_reject_pm_execution(pm_execution, manager=request.user, reason=reason)
                messages.warning(request, _("PM rejected. Returned to technician."))
                return redirect("work_order_detail", pk=pk)
            else:
                messages.error(request, _("Invalid action."))
        except ValueError as e:
            messages.error(request, str(e))
            return redirect("pm_review", pk=pk)

    checklist_results = []
    if wo.action_taken:
        for line in wo.action_taken.split("\n"):
            line = line.strip()
            if line.startswith("[✓]"):
                checklist_results.append({"checked": True, "text": line[3:].strip(), "note": "", "is_required": True})
            elif line.startswith("[✗]"):
                checklist_results.append({"checked": False, "text": line[3:].strip(), "note": "", "is_required": True})
            elif line.startswith("Note:"):
                if checklist_results:
                    checklist_results[-1]["note"] = line[5:].strip()

    snapshot = pm_execution.template_snapshot_json or {}
    snapshot_checklist = snapshot.get("checklist", [])

    if not checklist_results and snapshot_checklist:
        checklist_results = [
            {"checked": None, "text": item.get("text", ""), "note": "", "is_required": item.get("is_required", True)}
            for item in snapshot_checklist
        ]

    return render(request, "maintenance/pm_review.html", {
        "wo": wo,
        "pm_execution": pm_execution,
        "schedule": pm_execution.pm_schedule,
        "template": pm_execution.pm_schedule.template,
        "snapshot": snapshot,
        "checklist_results": checklist_results,
        "is_approved": pm_execution.status == PMExecution.Status.APPROVED,
        "is_rejected": pm_execution.status == PMExecution.Status.REJECTED,
        "is_submitted": pm_execution.status == PMExecution.Status.SUBMITTED,
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
            messages.success(request, _("Tool saved."))
            return redirect("tool_list")
    else:
        form = ToolForm(initial={"status": Tool.Status.AVAILABLE})
    return render(request, "maintenance/tool_form.html", {"form": form, "page_title": _("Add tool")})


@login_required
@role_required(User.Role.MANAGER, User.Role.SUPERVISOR, User.Role.SUPER_ADMIN)
def tool_edit(request, pk):
    tool = get_object_or_404(Tool, pk=pk)
    if request.method == "POST":
        form = ToolForm(request.POST, instance=tool)
        if form.is_valid():
            tool = form.save()
            log_audit(actor=request.user, action="tool_updated", entity="Tool", object_id=tool.pk)
            messages.success(request, _("Tool updated."))
            return redirect("tool_list")
    else:
        form = ToolForm(instance=tool)
    return render(request, "maintenance/tool_form.html", {"form": form, "page_title": _("Edit tool"), "tool": tool})


@login_required
@role_required(User.Role.MANAGER, User.Role.SUPERVISOR, User.Role.SUPER_ADMIN)
def tool_assign(request):
    if request.method != "POST":
        return redirect("tool_list")
    form = ToolAssignForm(request.POST)
    if not form.is_valid():
        messages.error(request, _("Invalid tool assignment."))
        return redirect("tool_list")
    tool = form.cleaned_data["tool"]
    assignee = form.cleaned_data["assignee"]
    if tool.status != Tool.Status.AVAILABLE:
        messages.error(request, _("Tool not available."))
        return redirect("tool_list")
    tool.status = Tool.Status.IN_USE
    tool.save(update_fields=["status"])
    ToolAssignment.objects.create(tool=tool, user=assignee, assigned_by=request.user)
    log_audit(actor=request.user, action="tool_assigned", entity="Tool", object_id=tool.pk, payload={"to": assignee.username})
    messages.success(request, _("Tool assigned."))
    return redirect("tool_list")


@login_required
@role_required(User.Role.OPERATOR, User.Role.TECHNICIAN, User.Role.MANAGER, User.Role.SUPERVISOR, User.Role.SUPER_ADMIN)
def tool_return(request, assignment_pk):
    ta = get_object_or_404(ToolAssignment.objects.select_related("tool"), pk=assignment_pk)
    if ta.user_id != request.user.id and not request.user.is_super_admin_role():
        if request.user.role != User.Role.MANAGER:
            raise Http404()
    if ta.returned_at:
        messages.info(request, _("Already returned."))
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
                messages.success(request, _("Return recorded — tool is available."))

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
                messages.success(request, _(f"Return recorded. Damaged tool flagged — repair work order WO-{wo.number} created for manager review."))

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
                messages.warning(request, _("Return recorded. Lost tool incident created — investigating."))

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
            messages.success(request, _("Repair request created."))
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

                # v5: auto-resolve the VENDOR_REPAIR blocker on ERO RETURNED.
                # The tech can resume work as soon as the vendor has physically
                # returned the part; the manager still has to ACCEPT later
                # (which closes the ERO and posts vendor_repair cost to ledger).
                # Reuse the existing event_type switch in
                # WorkOrderBlockerService.sync_from_external_event.
                try:
                    from maintenance.services_blocker import WorkOrderBlockerService
                    from maintenance.services_wo_status import WorkOrderService
                    WorkOrderBlockerService.sync_from_external_event(
                        external_obj=inst,
                        event_type="ERO_RETURNED",
                        actor=request.user,
                        payload={
                            "ero_id": inst.pk,
                            "old_status": old_status,
                        },
                    )
                    if inst.work_order_id:
                        WorkOrderService.recompute_operational_status(inst.work_order)
                except Exception as _e:
                    import logging
                    logging.getLogger(__name__).warning(
                        f"Failed to resolve VENDOR_REPAIR blocker on ERO RETURNED: {_e}"
                    )

            messages.success(request, _("Repair order updated."))
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

    # Build repair timeline from explicit timestamp fields (cheap, no
    # separate event log table needed for MVP).
    timeline = []
    if rwo.created_at:
        timeline.append({"occurred_at": rwo.created_at, "label": _("Created"), "note": ""})
    if rwo.sent_at:
        timeline.append({"occurred_at": rwo.sent_at, "label": _("Sent to vendor"), "note": rwo.vendor_name})
    if rwo.diagnosed_at:
        timeline.append({"occurred_at": rwo.diagnosed_at, "label": _("Diagnosed by vendor"), "note": ""})
    if rwo.invoice_date:
        timeline.append({"occurred_at": rwo.invoice_date, "label": _("Vendor invoice"), "note": rwo.invoice_ref})
    if rwo.returned_at:
        timeline.append({"occurred_at": rwo.returned_at, "label": _("Returned from vendor"), "note": ""})
    if rwo.closed_at:
        timeline.append({"occurred_at": rwo.closed_at, "label": _("Closed / accepted"), "note": rwo.invoice_ref})
    timeline.sort(key=lambda e: e["occurred_at"])

    return render(
        request,
        "maintenance/repair_officer.html",
        {
            "rwo": rwo,
            "form": form,
            "machine": tree_node,
            "ancestors": ancestors,
            "timeline": timeline,
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
def repair_mark_diagnosed(request, pk):
    """Mark an ERO as 'diagnosed' by the vendor — stamps diagnosed_at."""
    rwo = get_object_or_404(ExternalRepairOrder, pk=pk)
    if rwo.status not in (
        ExternalRepairOrder.Status.SENT_TO_VENDOR,
        ExternalRepairOrder.Status.DRAFT,
    ):
        messages.error(
            request,
            _("ERO can only be marked diagnosed while in DRAFT or SENT status."),
        )
        return redirect("repair_officer", pk=pk)
    if not rwo.diagnosed_at:
        rwo.diagnosed_at = timezone.now()
    rwo.handled_by = request.user
    rwo.save(update_fields=["diagnosed_at", "handled_by"])
    messages.success(request, _("Marked as diagnosed by vendor."))
    return redirect("repair_officer", pk=pk)


@login_required
@role_required(User.Role.PROCUREMENT, User.Role.MANAGER, User.Role.SUPER_ADMIN)
def repair_mark_returned(request, pk):
    """Mark an ERO as physically returned from vendor — stamps returned_at
    and transitions status to RETURNED (if it was SENT_TO_VENDOR).
    """
    rwo = get_object_or_404(ExternalRepairOrder, pk=pk)
    if rwo.status != ExternalRepairOrder.Status.SENT_TO_VENDOR:
        messages.error(
            request,
            _("ERO must be SENT_TO_VENDOR before marking returned."),
        )
        return redirect("repair_officer", pk=pk)
    rwo.status = ExternalRepairOrder.Status.RETURNED
    if not rwo.returned_at:
        rwo.returned_at = timezone.now()
    rwo.handled_by = request.user
    rwo.save(update_fields=["status", "returned_at", "handled_by"])
    messages.success(request, _("Marked as returned from vendor."))
    return redirect("repair_officer", pk=pk)


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
    elements.append(Paragraph(_("<b>EXTERNAL REPAIR ORDER</b>"), styles["Normal"]))

    elements += _section(_("Repair Order"))
    elements.append(_field_table([
        (_("ERO Number"), f"#{rwo.pk}"),
        (_("WO Number"), f"WO-{rwo.work_order.number}" if rwo.work_order else "—"),
        (_("Asset / Machine"), rwo.work_order.machine.name if rwo.work_order and rwo.work_order.machine else "—"),
        (_("Serial Number"), getattr(rwo.work_order.machine, "serial_number", "—") if rwo.work_order and rwo.work_order.machine else "—"),
        (_("Description"), rwo.description or "—"),
    ]))

    elements += _section(_("Vendor"))
    elements.append(_field_table([
        (_("Vendor"), rwo.vendor_name or "—"),
    ]))

    elements += _section(_("Timeline"))
    elements.append(_field_table([
        (_("Sent Date"), rwo.sent_at.strftime("%Y-%m-%d %H:%M") if rwo.sent_at else "—"),
    ]))

    elements += _section(_("Cost"))
    elements.append(_field_table([
        (_("Estimated Cost"), str(rwo.estimated_cost or "—")),
        (_("Actual Cost"), str(rwo.actual_cost or "—")),
    ]))

    elements += _section(_("Approval"))
    elements.append(Paragraph(_("Authorised by Maintenance Manager:"), styles["Normal"]))
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
    """Legacy standalone Quick Log entry — redirects to the machine list
    so the user can pick a machine and use the per-machine inline form
    on the History tab.

    Kept as a 302 instead of a 404 because:
    - existing bookmarks may point here
    - the perm_quick_log capability already references it
    - tests may invoke the URL name
    - a redirect costs nothing and breaks nothing

    The POST behaviour is preserved for callers that still submit here
    (e.g. the old form template), but new submissions should come from
    /machines/<pk>/quick-log/.
    """
    if request.method == "POST":
        form = QuickLogForm(request.POST, request.FILES)
        if form.is_valid():
            log = form.save(commit=False)
            log.author = request.user
            log.save()
            messages.success(request, _("Quick log saved."))
            return redirect("machine_detail", pk=log.machine.pk)
        # POST with invalid form — fall through and re-render with errors.
        messages.error(request, _("Please correct the errors below."))
    else:
        messages.info(request, _("Please choose a machine to log against."))
        return redirect("machine_list")

    return render(request, "maintenance/quick_log.html", {"form": form})


@login_required
@role_required(User.Role.OPERATOR, User.Role.SUPERVISOR, User.Role.TECHNICIAN, User.Role.MANAGER, User.Role.SUPER_ADMIN)
def machine_quick_log_create(request, pk):
    """Per-machine Quick Log creation endpoint.

    - GET: render the standalone form page (used when the user clicks
      "Quick Log" from outside the History tab — e.g. from a context
      menu). The History tab uses an inline form and POSTs here directly.
    - POST: validate, save, redirect back to the machine detail page
      with an anchor to the History tab so the new log is visible at
      the top of the timeline.
    """
    machine = get_object_or_404(Machine, pk=pk)

    if request.method == "POST":
        form = QuickLogForm(request.POST, request.FILES)
        if form.is_valid():
            log = form.save(commit=False)
            log.machine = machine
            log.author = request.user
            log.save()
            messages.success(request, _("Log added to machine history."))
            return redirect(f"{reverse('machine_detail', kwargs={'pk': pk})}#history")
        messages.error(request, _("Please correct the errors below."))
    else:
        # Pre-fill default values for GET so the page is meaningful
        # even before the user starts typing.
        form = QuickLogForm(initial={"type": QuickMaintenanceLog.Type.OBSERVATION})

    return render(
        request,
        "maintenance/machine_quick_log_create.html",
        {"form": form, "machine": machine},
    )


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

    # Phase 4 — failure mode distribution (top 5 by occurrence in last 90 days)
    failure_mode_rows = (
        MaintenanceIssue.objects.filter(
            created_at__gte=td90,
            failure_mode__isnull=False,
        )
        .values("failure_mode_id", "failure_mode__code", "failure_mode__name")
        .annotate(count=Count("id"))
        .order_by("-count")[:5]
    )
    failure_mode_distribution = [
        {
            "code": r["failure_mode__code"] or f"FM-{r['failure_mode_id']}",
            "name": r["failure_mode__name"] or "Unknown",
            "count": r["count"],
        }
        for r in failure_mode_rows
    ]

    # Phase 4 — top failing assets (by closed corrective WO count in last 90 days)
    top_failing_assets = list(
        WorkOrder.objects.filter(
            category=WorkOrder.Category.BREAKDOWN,
            lifecycle_status=WorkOrder.LifecycleStatus.CLOSED,
            updated_at__gte=td90,
        )
        .values("machine_id", "machine__name", "component_id", "component__name")
        .annotate(
            failure_count=Count("id"),
            total_cost=Coalesce(
                Sum(
                    F("cost_record__material_cost")
                    + F("cost_record__vendor_repair_cost")
                    + F("cost_record__consumables_cost")
                    + F("cost_record__additional_cost")
                ),
                Value(0),
                output_field=IntegerField(),
            ),
        )
        .order_by("-failure_count")[:5]
    )
    for row in top_failing_assets:
        row["total_cost"] = float(row["total_cost"] or 0)
        row["name"] = row["component__name"] or row["machine__name"] or "—"

    # Phase 4 — per-operator report counts (top 5 reporters in last 30 days)
    operator_report_rows = (
        MaintenanceIssue.objects.filter(
            created_at__gte=now - timedelta(days=30),
            reported_by__isnull=False,
        )
        .values(
            "reported_by_id",
            "reported_by__username",
            "reported_by__first_name",
            "reported_by__last_name",
        )
        .annotate(reports=Count("id"))
        .order_by("-reports")[:5]
    )
    top_reporters = [
        {
            "username": r["reported_by__username"],
            "display": (
                (r["reported_by__first_name"] or "") + " " + (r["reported_by__last_name"] or "")
            ).strip() or r["reported_by__username"],
            "reports": r["reports"],
        }
        for r in operator_report_rows
    ]

    # Phase 4 — failure count + cost per failure in last 90 days
    failures_90d = WorkOrder.objects.filter(
        category=WorkOrder.Category.BREAKDOWN,
        lifecycle_status=WorkOrder.LifecycleStatus.CLOSED,
        updated_at__gte=td90,
    ).count()
    cost_90d_qs = CostTransaction.objects.filter(
        occurred_at__gte=td90,
    )
    cost_90d_total = (
        sum(t.amount for t in cost_90d_qs)
    )
    cost_per_failure = (cost_90d_total / failures_90d) if failures_90d else None

    # Phase 6 — 30-day supply counters for the manager KPI dashboard.
    # These were previously absent despite being trivially derivable
    # from existing models. Gated by can_see_procurement so operators
    # and technicians do not see supply-pipeline data.
    td30 = now - timedelta(days=30)
    parts_consumed_30d = ConsumableAssignment.objects.filter(
        created_at__gte=td30,
    ).aggregate(
        total_qty=Coalesce(Sum("quantity"), Value(0), output_field=IntegerField()),
        total_logs=Count("id"),
    )
    eros_accepted_30d = ExternalRepairOrder.objects.filter(
        status=ExternalRepairOrder.Status.CLOSED,
        closed_at__gte=td30,
    ).count()
    pos_received_30d = PurchaseOrder.objects.filter(
        status__in=[
            PurchaseOrder.Status.RECEIVED,
            PurchaseOrder.Status.PARTIAL_RECEIVED,
        ],
        received_at__gte=td30,
    ).count()
    pos_fully_received_30d = PurchaseOrder.objects.filter(
        status=PurchaseOrder.Status.RECEIVED,
        received_at__gte=td30,
    ).count()
    can_see_procurement = request.user.role in (
        User.Role.MANAGER, User.Role.SUPERVISOR,
        User.Role.PROCUREMENT, User.Role.SUPER_ADMIN,
    )

    ctx = {
        "mttr_hours": mttr_hours,
        "mttw_hours": mttw_hours,
        "mtbf_hours": mtbf_hours,
        "avg_downtime_hours": avg_downtime_hours,
        "failure_mode_distribution": failure_mode_distribution,
        "top_failing_assets": top_failing_assets,
        "top_reporters": top_reporters,
        "failures_90d": failures_90d,
        "cost_per_failure": cost_per_failure,
        # Cost is visible only to roles that manage or plan around cost.
        # Operator and technician see operational KPIs but not financial ones.
        "can_see_cost": request.user.role in (
            User.Role.MANAGER, User.Role.SUPERVISOR,
            User.Role.PROCUREMENT, User.Role.SUPER_ADMIN,
        ),
        "pm_compliance_pct": pm_compliance_pct,
        "pm_closed_90d": pm_closed,
        "pm_active_schedules": pm_active,
        "pm_due_count": pm_due,
        "open_emergency_wos": WorkOrder.objects.filter(is_emergency=True)
        .exclude(lifecycle_status=WorkOrder.LifecycleStatus.CLOSED)
        .count(),
        "tool_lost_count": tool_lost_count,
        "tool_loss_rate_pct": tool_loss_rate_pct,
        # Phase 6 — 30-day supply counters
        "parts_consumed_30d_qty": parts_consumed_30d["total_qty"],
        "parts_consumed_30d_logs": parts_consumed_30d["total_logs"],
        "eros_accepted_30d": eros_accepted_30d,
        "pos_received_30d": pos_received_30d,
        "pos_fully_received_30d": pos_fully_received_30d,
        "can_see_procurement": can_see_procurement,
    }

    return render(request, "maintenance/kpi.html", ctx)


@login_required
@role_required(User.Role.MANAGER, User.Role.SUPERVISOR, User.Role.SUPER_ADMIN)
def failure_mode_report(request):
    """Detailed failure mode report — all failure modes with occurrence
    count, affected assets, and recent incidents. Shows full data beyond
    the top 5 on the KPI dashboard.
    """
    now = timezone.now()
    td90 = now - timedelta(days=90)

    # All failure modes with their counts in last 90 days
    rows = (
        MaintenanceIssue.objects.filter(
            created_at__gte=td90,
            failure_mode__isnull=False,
        )
        .values(
            "failure_mode_id",
            "failure_mode__code",
            "failure_mode__name",
            "failure_mode__category__name",
            "failure_mode__category__code",
        )
        .annotate(
            count=Count("id"),
            affected_machines=Count("machine_id", distinct=True),
        )
        .order_by("-count", "failure_mode__code")
    )
    failure_modes = list(rows)
    total_occurrences = sum(r["count"] for r in failure_modes)
    total_modes_affected = len(failure_modes)

    # Recent issues per mode for the top 10 (limit query)
    top_mode_ids = [r["failure_mode_id"] for r in failure_modes[:10]]
    recent_by_mode = {}
    if top_mode_ids:
        recent = (
            MaintenanceIssue.objects.filter(
                created_at__gte=td90,
                failure_mode_id__in=top_mode_ids,
            )
            .select_related("machine", "reported_by")
            .order_by("-created_at")[:50]
        )
        for issue in recent:
            recent_by_mode.setdefault(issue.failure_mode_id, []).append(issue)

    ctx = {
        "failure_modes": failure_modes,
        "total_occurrences": total_occurrences,
        "total_modes_affected": total_modes_affected,
        "recent_by_mode": recent_by_mode,
        "period_days": 90,
    }
    return render(request, "maintenance/failure_mode_report.html", ctx)


@login_required
@role_required(User.Role.MANAGER, User.Role.SUPERVISOR, User.Role.SUPER_ADMIN)
def failing_assets_report(request):
    """Detailed failing assets report — all assets with failure counts
    in the last 90 days, total cost, and most recent failure.
    """
    now = timezone.now()
    td90 = now - timedelta(days=90)

    rows = (
        WorkOrder.objects.filter(
            category=WorkOrder.Category.BREAKDOWN,
            lifecycle_status=WorkOrder.LifecycleStatus.CLOSED,
            updated_at__gte=td90,
        )
        .values(
            "machine_id",
            "machine__name",
            "machine__asset_code",
            "component_id",
            "component__name",
        )
        .annotate(
            failure_count=Count("id"),
            total_cost_material=Coalesce(Sum("cost_record__material_cost"), Value(0), output_field=IntegerField()),
            total_cost_vendor=Coalesce(Sum("cost_record__vendor_repair_cost"), Value(0), output_field=IntegerField()),
            total_cost_consumable=Coalesce(Sum("cost_record__consumables_cost"), Value(0), output_field=IntegerField()),
            total_cost_additional=Coalesce(Sum("cost_record__additional_cost"), Value(0), output_field=IntegerField()),
            last_failure=Max("updated_at"),
        )
        .order_by("-failure_count", "-total_cost_material")
    )
    assets = []
    for r in rows:
        total = float(r.get("total_cost_material") or 0) + float(r.get("total_cost_vendor") or 0) + float(r.get("total_cost_consumable") or 0) + float(r.get("total_cost_additional") or 0)
        r["total_cost"] = total
        r["name"] = r["component__name"] or r["machine__name"] or "—"
        r["asset_code"] = (
            r["component__name"] and r["machine__asset_code"]
        ) or r["machine__asset_code"] or "—"
        r["cost_per_failure"] = (
            r["total_cost"] / r["failure_count"] if r["failure_count"] else 0
        )
        assets.append(r)

    total_failures = sum(a["failure_count"] for a in assets)
    total_cost = sum(a["total_cost"] for a in assets)

    ctx = {
        "assets": assets,
        "total_failures": total_failures,
        "total_cost": total_cost,
        "period_days": 90,
    }
    return render(request, "maintenance/failing_assets_report.html", ctx)


@login_required
@role_required(User.Role.MANAGER, User.Role.SUPERVISOR, User.Role.SUPER_ADMIN)
def reporters_report(request):
    """Detailed reporters report — all operators with their issue
    reporting activity in the last 30 days, plus WOs converted.
    """
    now = timezone.now()
    td30 = now - timedelta(days=30)

    rows = (
        MaintenanceIssue.objects.filter(
            created_at__gte=td30,
            reported_by__isnull=False,
        )
        .values(
            "reported_by_id",
            "reported_by__username",
            "reported_by__first_name",
            "reported_by__last_name",
            "reported_by__role",
        )
        .annotate(
            reports=Count("id"),
            converted=Count("id", filter=Q(work_order__isnull=False)),
        )
        .order_by("-reports", "reported_by__username")
    )
    reporters = []
    for r in rows:
        first = r["reported_by__first_name"] or ""
        last = r["reported_by__last_name"] or ""
        full = (first + " " + last).strip()
        reporters.append({
            "username": r["reported_by__username"],
            "display": full or r["reported_by__username"],
            "role": r["reported_by__role"],
            "reports": r["reports"],
            "converted": r["converted"],
            "conversion_rate": (
                round((r["converted"] / r["reports"]) * 100, 1)
                if r["reports"] else 0
            ),
        })

    total_reports = sum(r["reports"] for r in reporters)
    total_converted = sum(r["converted"] for r in reporters)

    ctx = {
        "reporters": reporters,
        "total_reports": total_reports,
        "total_converted": total_converted,
        "period_days": 30,
    }
    return render(request, "maintenance/reporters_report.html", ctx)


@login_required
@role_required(User.Role.MANAGER, User.Role.SUPER_ADMIN)
def repair_manager_accept(request, pk):
    rwo = get_object_or_404(ExternalRepairOrder, pk=pk)
    if rwo.status != ExternalRepairOrder.Status.RETURNED:
        messages.error(request, _("Repair must be in Returned status before manager acceptance (UC-20)."))
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
        # Bug #5 fix: use the ledger-based refresh so the cache reflects the
        # CostTransaction rows, not the legacy PartIssueLine aggregation.
        if rwo.work_order_id:
            try:
                from maintenance.cost_ledger import CostLedgerService
                CostLedgerService._refresh_wo_cache(rwo.work_order_id)
            except Exception as _e:
                import logging
                logging.getLogger(__name__).warning(
                    f"_refresh_wo_cache failed for WO {rwo.work_order_id}: {_e}"
                )
        # Phase 1+2 Cost Ledger: post the vendor_repair cost for this closed ERO.
        # Bug fix: surface ledger failures as messages.error instead of swallowing.
        try:
            from maintenance.cost_ledger import CostLedgerService
            CostLedgerService.post_vendor_repair(
                external_repair_order=rwo,
                actor=request.user,
                memo=f"ERO #{rwo.pk} closed: {rwo.invoice_ref or 'no invoice ref'}",
            )
        except Exception as _ledger_err:
            import logging
            logging.getLogger(__name__).exception(
                f"Cost ledger post_vendor_repair failed for ERO {rwo.pk}: {_ledger_err}"
            )
            messages.error(
                request,
                f"ERO closed, but cost ledger post failed: {_ledger_err}. "
                f"Re-run from the cost ledger page.",
            )
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
        messages.error(request, _("Issue is already archived."))
        return redirect("issue_list")
    if issue.status == MaintenanceIssue.Status.CONVERTED:
        messages.error(request, _("Cannot archive a converted issue. Archive the linked work order instead."))
        return redirect("issue_list")
    archive_maintenance_issue(issue, request.user)
    messages.success(request, f"Issue #{issue.pk} has been archived.")
    return redirect("issue_list")


@login_required
@require_POST
def work_order_archive(request, pk):
    wo = get_object_or_404(WorkOrder, pk=pk)
    if wo.is_archived:
        messages.error(request, _("Work order is already archived."))
        return redirect("work_order_list")
    archive_work_order(wo, request.user)
    messages.success(request, f"Work order WO-{wo.number} has been archived.")
    return redirect("work_order_list")



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
            messages.error(request, _("New quantity and reason are required."))

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
    reserved  = (inv.compute_quantity_reserved() if inv else Decimal("0"))
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
        messages.error(request, _("You can only raise shortage requests on work orders assigned to you."))
        return redirect("work_order_detail", pk=wo.pk)

    part_id = request.POST.get("part_id")
    if not part_id:
        messages.error(request, _("Missing part_id."))
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
        messages.error(request, _("Invalid decision type."))
        return redirect("work_order_detail", pk=wo.pk)

    note = (request.POST.get("decision_note") or "").strip()
    reason = ""
    rejected = Decimal("0")
    approved_issue = Decimal("0")
    approved_procure = Decimal("0")

    if decision_type == "reject":
        reason = (request.POST.get("rejection_reason") or "").strip()
        if len(reason) < 15:
            messages.error(request, _("Rejection reason is required (min 15 characters)."))
            return redirect("work_order_detail", pk=wo.pk)
        rejected = report.qty_requested
    else:
        try:
            approved_issue   = Decimal(request.POST.get("approved_issue_qty") or "0")
            approved_procure = Decimal(request.POST.get("approved_procurement_qty") or "0")
            rejected         = Decimal(request.POST.get("rejected_qty") or "0")
        except (InvalidOperation, ValueError):
            messages.error(request, _("Quantities must be numbers."))
            return redirect("work_order_detail", pk=wo.pk)
        if any(v < 0 for v in (approved_issue, approved_procure, rejected)):
            messages.error(request, _("Quantities cannot be negative."))
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
            messages.error(request, _("Approve with both 0 makes no sense — use Reject instead."))
            return redirect("work_order_detail", pk=wo.pk)

    eta_raw = (request.POST.get("expected_availability_date") or "").strip()
    eta = None
    if eta_raw:
        try:
            eta = date.fromisoformat(eta_raw)
        except ValueError:
            messages.warning(request, _("Invalid expected_availability_date; skipped."))

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
        messages.error(request, _("Quantities must be numbers."))
        return redirect("work_order_detail", pk=wo.pk)
    if any(v < 0 for v in (approved_issue, approved_procure, rejected)):
        messages.error(request, _("Quantities cannot be negative."))
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
@login_required
@require_POST
@role_required(User.Role.MANAGER, User.Role.SUPER_ADMIN)
def work_order_cancel_part_line(request, pk, line_id):
    """Manager cancels an APPROVED or ALLOCATED part line that was
    never warehouse-issued. Releases the reservation and fires
    PART_REJECTED so the PART blocker is cancelled.

    Use this when the manager no longer needs a part that was
    approved+allocated (e.g. wrong part ordered, scope changed).
    Rejection reason is required (min 15 chars) for the audit trail.
    """
    from inventory.models import PartIssueLine
    from inventory.services import cancel_approved_part_request

    wo = get_object_or_404(WorkOrder, pk=pk)
    line = get_object_or_404(PartIssueLine, pk=line_id, work_order=wo)
    reason = (request.POST.get("reason") or "").strip()
    if len(reason) < 15:
        messages.error(
            request,
            f"Cancellation reason must be at least 15 characters "
            f"(got {len(reason)}).",
        )
        return redirect("work_order_detail", pk=wo.pk)
    try:
        cancel_approved_part_request(
            line=line, manager=request.user, reason=reason,
        )
    except ValueError as e:
        messages.error(request, f"⚠️ Could not cancel line: {e}")
        return redirect("work_order_detail", pk=wo.pk)
    messages.success(
        request,
        f"✗ Cancelled {line.part.name} request. Reservation released, "
        f"PART blocker cleared.",
    )
    return redirect("work_order_detail", pk=wo.pk)


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
        messages.error(request, _("Issue qty must be a number."))
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


@login_required
@role_required(User.Role.MANAGER, User.Role.SUPER_ADMIN)
def legacy_reconciliation_dashboard(request):
    """Phase 7.8.1: show WOs still on the legacy state model.

    These are pre-blocker-system WOs that haven't had any domain event
    (part request, pause, etc.) since the migration. Their state may
    be inconsistent. Use this dashboard to:
    1. Spot-check that closed WOs are truly closed.
    2. Identify WOs that need manual migration to the new model.
    """
    legacy_wos = (
        WorkOrder.objects
        .filter(blocker_system_version=0)
        .select_related("machine", "assigned_technician")
        .order_by("-number")[:200]
    )
    total = WorkOrder.objects.count()
    legacy_count = WorkOrder.objects.filter(blocker_system_version=0).count()
    v1_count = WorkOrder.objects.filter(blocker_system_version=1).count()
    return render(request, "maintenance/legacy_reconciliation.html", {
        "legacy_wos": legacy_wos,
        "total": total,
        "legacy_count": legacy_count,
        "v1_count": v1_count,
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


# ---------------------------------------------------------------------------
# Phase 1+2 — Cost Ledger views (manager adjustment, drilldown, CSV export)
# ---------------------------------------------------------------------------


@login_required
@role_required(User.Role.MANAGER, User.Role.SUPER_ADMIN)
def work_order_adjust_cost(request, pk):
    """Manager creates a manual cost adjustment on a WorkOrder.

    Wraps `CostLedgerService.post_adjustment`, which:
      - creates a CostAdjustment (immutable; 10+ char memo)
      - creates a CostTransaction (category=ADJUSTMENT, linked to the adj)
      - refreshes the WorkOrderCost cache
    """
    wo = get_object_or_404(WorkOrder, pk=pk)
    if request.method != "POST":
        return redirect("work_order_detail", pk=pk)
    form = CostAdjustmentForm(request.POST)
    if not form.is_valid():
        for field, errs in form.errors.items():
            for err in errs:
                messages.error(request, f"{field}: {err}")
        return redirect("work_order_detail", pk=pk)
    from maintenance.cost_ledger import CostLedgerService
    try:
        CostLedgerService.post_adjustment(
            work_order=wo,
            amount=form.cleaned_data["amount"],
            memo=form.cleaned_data["memo"],
            actor=request.user,
        )
    except Exception as e:
        messages.error(request, f"Could not post adjustment: {e}")
        return redirect("work_order_detail", pk=pk)
    messages.success(
        request,
        f"Adjustment of {form.cleaned_data['amount']} posted to WO-{wo.number} cost ledger.",
    )
    return redirect("work_order_detail", pk=pk)


@login_required
@role_required(User.Role.MANAGER, User.Role.SUPERVISOR, User.Role.PROCUREMENT, User.Role.SUPER_ADMIN)
def work_order_cost_ledger(request, pk):
    """Drilldown page: every CostTransaction for a WO, in reverse-chronological order.

    Restricted to roles that manage or plan around cost. Operators and
    technicians do not have permission to view cost data at all.
    The page renders `_cost_ledger_table.html`.
    """
    wo = get_object_or_404(
        WorkOrder.objects.select_related("machine", "component"),
        pk=pk,
    )
    txns = (
        CostTransaction.objects
        .filter(work_order=wo)
        .select_related("actor", "adjustment", "supersedes", "machine", "component")
        .order_by("-occurred_at", "-pk")
    )

    # By-category totals, used for the summary card.
    from django.db.models.functions import Coalesce
    sums = (
        CostTransaction.objects
        .filter(work_order=wo)
        .values("category")
        .annotate(total=Coalesce(Sum("amount"), Decimal("0")))
    )
    by_cat = {row["category"]: row["total"] for row in sums}
    category_totals = {
        "material":      by_cat.get(CostCategory.MATERIAL, Decimal("0")),
        "vendor_repair": by_cat.get(CostCategory.VENDOR_REPAIR, Decimal("0")),
        "consumable":    by_cat.get(CostCategory.CONSUMABLE, Decimal("0")),
        "adjustment":    by_cat.get(CostCategory.ADJUSTMENT, Decimal("0")),
    }
    grand_total = sum(category_totals.values(), Decimal("0"))

    return render(
        request,
        "maintenance/work_order_cost_ledger.html",
        {
            "wo": wo,
            "txns": txns,
            "category_totals": category_totals,
            "grand_total": grand_total,
            "category_choices": CostCategory.choices,
            "source_choices": [
                ("part_issue_line",       "Part Issue Line"),
                ("external_repair_order", "External Repair Order"),
                ("stock_movement",        "Stock Movement"),
                ("cost_adjustment",       "Cost Adjustment"),
            ],
        },
    )


@login_required
@role_required(User.Role.MANAGER, User.Role.SUPER_ADMIN, User.Role.PROCUREMENT)
def cost_ledger_export_csv(request):
    """Export the global cost ledger as CSV.

    Query params:
        ?days=90   — last N days (default 90, ignored if ?all=1)
        ?all=1     — export everything
    Columns: occurred_at, work_order, category, source_type, source_id,
             amount, currency, quantity, unit_cost, memo, actor, is_reversal.
    """
    qs = CostTransaction.objects.select_related(
        "work_order", "actor", "adjustment", "machine", "component"
    )
    if request.GET.get("all") != "1":
        try:
            days = int(request.GET.get("days", "90"))
        except (TypeError, ValueError):
            days = 90
        days = max(0, days)
        cutoff = timezone.now() - timedelta(days=days)
        qs = qs.filter(occurred_at__gte=cutoff)
    qs = qs.order_by("-occurred_at", "-pk")

    response = HttpResponse(content_type="text/csv; charset=utf-8")
    # UTF-8 BOM so Excel on Windows reads Arabic headers correctly
    response.write("\ufeff")
    # ASCII filename (per i18n plan); headers translated via gettext
    filename = "cost_ledger_all.csv" if request.GET.get("all") == "1" else f"cost_ledger_last_{days}d.csv"
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    writer = csv.writer(response)
    writer.writerow([
        _("Occurred at"), _("Work order"), _("Category"), _("Source type"),
        _("Source ID"), _("Amount"), _("Currency"), _("Quantity"),
        _("Unit cost"), _("Memo"), _("Actor"), _("Is reversal"),
        _("Supersedes ID"), _("Adjustment ID"),
    ])
    for t in qs.iterator():
        writer.writerow([
            t.occurred_at.isoformat() if t.occurred_at else "",
            f"WO-{t.work_order.number}" if t.work_order_id else "",
            t.category,
            t.source_type,
            t.source_id if t.source_id is not None else "",
            str(t.amount),
            t.currency,
            str(t.quantity) if t.quantity is not None else "",
            str(t.unit_cost) if t.unit_cost is not None else "",
            t.memo,
            t.actor.username if t.actor_id else "",
            "1" if t.is_reversal else "0",
            t.supersedes_id if t.supersedes_id else "",
            t.adjustment_id if t.adjustment_id else "",
        ])
    return response


@login_required
@role_required(User.Role.MANAGER, User.Role.SUPER_ADMIN)
def pm_template_list(request):
    templates = PMTemplate.objects.all().prefetch_related("checklist_items", "schedules")
    return render(request, "maintenance/pm_template_list.html", {"templates": templates})


def _strip_empty_formset_forms(formset):
    """Remove extra forms with empty text so they don't get saved as blank items."""
    formset.forms = [
        f for f in formset.forms
        if f.instance.pk is not None
        or (f.cleaned_data.get("text") or "").strip()
    ]


def pm_template_create(request):
    if request.method == "POST":
        form = PMTemplateForm(request.POST)
        formset = PMChecklistItemFormSet(request.POST)
        if form.is_valid() and formset.is_valid():
            with transaction.atomic():
                template = form.save()
                formset.instance = template
                _strip_empty_formset_forms(formset)
                formset.save()
            messages.success(request, f"PM template {template.code} created.")
            return redirect("pm_template_detail", pk=template.pk)
    else:
        form = PMTemplateForm(initial={"estimated_duration_minutes": 30, "priority": "medium"})
        formset = PMChecklistItemFormSet()
    return render(request, "maintenance/pm_template_form.html", {
        "form": form, "formset": formset, "template": None, "mode": "create",
    })


@login_required
@role_required(User.Role.MANAGER, User.Role.SUPER_ADMIN)
def pm_template_edit(request, pk):
    template = get_object_or_404(PMTemplate, pk=pk)
    if request.method == "POST":
        form = PMTemplateForm(request.POST, instance=template)
        formset = PMChecklistItemFormSet(request.POST, instance=template)
        if form.is_valid() and formset.is_valid():
            with transaction.atomic():
                form.save()
                _strip_empty_formset_forms(formset)
                formset.save()
            messages.success(request, f"PM template {template.code} updated.")
            return redirect("pm_template_detail", pk=template.pk)
    else:
        form = PMTemplateForm(instance=template)
        formset = PMChecklistItemFormSet(instance=template)
    return render(request, "maintenance/pm_template_form.html", {
        "form": form, "formset": formset, "template": template, "mode": "edit",
    })


@login_required
@role_required(User.Role.MANAGER, User.Role.SUPER_ADMIN, User.Role.TECHNICIAN, User.Role.SUPERVISOR)
def pm_template_detail(request, pk):
    template = get_object_or_404(
        PMTemplate.objects.prefetch_related("checklist_items", "schedules__machine"),
        pk=pk,
    )
    schedules = template.schedules.select_related("machine").order_by("next_due_at")
    return render(request, "maintenance/pm_template_detail.html", {
        "template": template, "schedules": schedules,
    })


@login_required
@role_required(User.Role.MANAGER, User.Role.SUPER_ADMIN)
def pm_dashboard(request):
    compliance = compute_compliance()

    active_pms = PMSchedule.objects.filter(is_active=True)
    now = timezone.now()
    seven_days = now + timedelta(days=7)
    hero_stats = {
        "total_pms": active_pms.count(),
        "overdue_pms": active_pms.filter(next_due_at__lt=now).count(),
        "due_this_week": active_pms.filter(
            next_due_at__gte=now, next_due_at__lte=seven_days
        ).count(),
        "compliance_pct": compliance["pct"],
    }

    machines_with_pms = Machine.objects.filter(
        is_active=True,
        pm_schedules__is_active=True,
    ).distinct().order_by("name")

    per_machine = []
    for machine in machines_with_pms:
        machine_compliance = compute_compliance(machine=machine)
        machine_pms_qs = PMSchedule.objects.filter(machine=machine, is_active=True)
        per_machine.append({
            "machine": machine,
            "total_pms": machine_pms_qs.count(),
            "overdue_pms": machine_pms_qs.filter(next_due_at__lt=now).count(),
            "compliance": machine_compliance,
        })

    late_count = max(compliance["approved_total"] - compliance["on_time"], 0)

    return render(request, "maintenance/pm_dashboard.html", {
        "compliance": compliance,
        "hero_stats": hero_stats,
        "per_machine": per_machine,
        "late_count": late_count,
        "now": now,
    })


@login_required
@role_required(User.Role.MANAGER, User.Role.SUPER_ADMIN)
def pm_batch_spawn_wo(request):
    """Spawn PM work orders for multiple schedules at once.

    GET ?schedule_ids=1,2,3  → confirmation page (lists what would be spawned)
    POST schedule_ids=1,2,3  → actually spawn the WOs and redirect to pm_list
    """
    if request.method == "GET":
        ids_param = request.GET.get("schedule_ids", "")
        try:
            ids = [int(s) for s in ids_param.split(",") if s.strip().isdigit()]
        except (ValueError, TypeError):
            ids = []
        schedules = (
            PMSchedule.objects.select_related("template", "machine")
            .filter(pk__in=ids, is_active=True)
            if ids else PMSchedule.objects.none()
        )
        return render(request, "maintenance/pm_batch_spawn_confirm.html", {
            "schedules": schedules,
            "requested_ids": ids,
        })

    schedule_ids = request.POST.getlist("schedule_ids")
    if not schedule_ids:
        messages.error(request, _("No schedules selected."))
        return redirect("pm_list")

    created_count = 0
    skipped_count = 0
    skipped_pending = 0
    for sid in schedule_ids:
        try:
            sched = PMSchedule.objects.select_related("template", "machine").get(pk=int(sid))
        except (PMSchedule.DoesNotExist, ValueError, TypeError):
            skipped_count += 1
            continue
        if not sched.is_active:
            skipped_count += 1
            continue
        # Dedupe: skip schedules that already have a SUBMITTED/REJECTED
        # PMExecution at the current next_due_at. Mirrors pm_spawn_wo.
        existing = PMExecution.objects.filter(
            pm_schedule=sched,
            scheduled_due_at=sched.next_due_at,
            status__in=[PMExecution.Status.SUBMITTED, PMExecution.Status.REJECTED],
        ).first()
        if existing:
            skipped_pending += 1
            continue
        wo = WorkOrder.objects.create(
            category=WorkOrder.Category.PREVENTIVE,
            machine=sched.machine,
            lifecycle_status=WorkOrder.LifecycleStatus.ASSIGNED,
            created_by=request.user,
            notes=f"PM: {sched.template.title} (batch spawn)",
        )
        transition_work_order(wo, WorkOrder.LifecycleStatus.ASSIGNED, actor=request.user, note="Batch PM spawn")
        create_pm_execution_for_wo(sched, wo, actor=request.user)
        created_count += 1

    if created_count:
        msg = f"Created {created_count} PM work order{'s' if created_count != 1 else ''}."
        skipped_notes = []
        if skipped_pending:
            skipped_notes.append(f"{skipped_pending} already pending")
        if skipped_count:
            skipped_notes.append(f"{skipped_count} invalid or inactive")
        if skipped_notes:
            msg += f" Skipped: {', '.join(skipped_notes)}."
        messages.success(request, msg)
    elif skipped_pending and not skipped_count:
        messages.warning(
            request,
            f"No new work orders created — all {skipped_pending} selected schedule{'s' if skipped_pending != 1 else ''} already have a pending execution.",
        )
    else:
        messages.warning(request, _("No work orders created. All schedules were invalid or inactive."))
    return redirect("pm_list")
