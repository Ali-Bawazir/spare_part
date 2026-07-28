"""Views for the reusable-tool pool.

Public views:
    tools_list         — GET /tools/?q=...&status=...         (everyone except procurement)
    tools_instance_detail — GET /tools/instance/<id>/        (history drill-down)
    tools_issue        — GET/POST /tools/issue/<part_id>/   (manager)
    tools_assign       — GET/POST /tools/assign/<inst_id>/  (operator/tech self; supervisor/manager to anyone)
    tools_return       — GET/POST /tools/return/<asg_id>/   (operator/tech/supervisor/manager)
    tools_damage_resolve — GET/POST /tools/damage/<rep_id>/ (manager)
    tools_dashboard    — GET /tools/dashboard/              (manager)

The list page doubles as a search: `?q=Knife+7` matches by name / SKU /
tool_number. Status filter chips let the user narrow to Available /
In Use / Out of Service (manager/supervisor only — operators see only
Available and their own held tools, with cost fields hidden).
"""
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Count, Q, Sum
from django.http import HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from django.views.decorators.http import require_http_methods

from accounts.permissions import role_required
from accounts.models import User
from inventory.models import Inventory, ReusableToolInstance, SparePart
from inventory.models_tools import (
    ToolAssignment,
    ToolDamageReport,
    ToolMovement,
)
from inventory.services_tools import (
    InventoryService,
    ToolAssignmentService,
    ToolDamageService,
)

from .forms import (
    ToolAssignForm,
    ToolDamageResolveForm,
    ToolIssueForm,
    ToolReturnForm,
)
from .models import Machine


# ---------------------------------------------------------------------------


def _render_single_tool(request, instance, tab):
    """Drill-down for one tool. Three tabs: current, history, damage."""
    active = instance.active_assignment
    history_qs = (
        ToolAssignment.objects
        .filter(instance=instance)
        .select_related("operator", "machine")
        .order_by("-checkout_at")[:50]
    )
    damage_qs = (
        ToolDamageReport.objects
        .filter(instance=instance)
        .select_related("reported_by", "machine", "assignment")
        .order_by("-damage_date")
    )
    movement_qs = (
        ToolMovement.objects
        .filter(instance=instance)
        .select_related("actor", "machine", "assignment", "damage_report")
        .order_by("-created_at")[:50]
    )
    context = {
        "tool": instance,
        "tab": tab,
        "active_assignment": active,
        "history": history_qs,
        "damage_reports": damage_qs,
        "movements": movement_qs,
    }
    return render(request, "maintenance/tools_instance_detail.html", context)


# ---------------------------------------------------------------------------
# Issue to Tool Pool
# ---------------------------------------------------------------------------

@login_required
@role_required(User.Role.MANAGER, User.Role.SUPER_ADMIN)
def tools_issue(request, part_id):
    """Manager issues N units from inventory into the tool pool."""
    part = get_object_or_404(
        SparePart,
        pk=part_id,
        item_type=SparePart.ItemType.REUSABLE_TOOL,
    )

    # Current on-hand qty (sum across sites, default site first)
    inv_qty = (
        Inventory.objects
        .filter(part=part)
        .aggregate(total=Sum("quantity_available"))["total"] or Decimal("0")
    )

    if request.method == "POST":
        form = ToolIssueForm(request.POST)
        if form.is_valid():
            try:
                instances = InventoryService.issue_to_tool_pool(
                    part=part,
                    qty=form.cleaned_data["quantity"],
                    actor=request.user,
                    note=form.cleaned_data.get("note", ""),
                )
            except ValueError as e:
                messages.error(request, str(e))
            else:
                numbers = ", ".join(
                    f"{part.name} #{i.tool_number}" for i in instances
                )
                messages.success(
                    request,
                    _("Issued %(qty)s. Inventory %(before)s → %(after)s. "
                      "Tool pool: %(numbers)s.") % {
                        "qty": len(instances),
                        "before": inv_qty,
                        "after": inv_qty - Decimal(len(instances)),
                        "numbers": numbers,
                    },
                )
                return redirect("tools_list")
    else:
        form = ToolIssueForm(initial={"quantity": int(inv_qty)})

    return render(request, "maintenance/tools_issue.html", {
        "part": part,
        "form": form,
        "on_hand": inv_qty,
    })


# ---------------------------------------------------------------------------
# Assign / Return
# ---------------------------------------------------------------------------

@login_required
@require_http_methods(["GET", "POST"])
def tools_assign(request, instance_id):
    instance = get_object_or_404(
        ReusableToolInstance.objects.select_related("part"),
        pk=instance_id, is_active=True,
    )
    user = request.user

    is_manager_or_supervisor = user.role in (
        User.Role.MANAGER, User.Role.SUPERVISOR, User.Role.SUPER_ADMIN,
    )
    is_self_assign = not is_manager_or_supervisor

    if is_self_assign:
        operator = user
        form = ToolAssignForm(request.POST or None, operator=operator)
    else:
        operator = None
        form = ToolAssignForm(request.POST or None)

    if request.method == "POST" and form.is_valid():
        chosen_operator = operator or form.cleaned_data["operator"]
        try:
            a = ToolAssignmentService.assign(
                instance=instance,
                operator=chosen_operator,
                machine=form.cleaned_data["machine"],
                condition_out=form.cleaned_data["condition_out"],
                actor=user,
                notes=form.cleaned_data.get("notes", ""),
            )
        except ValueError as e:
            messages.error(request, str(e))
        else:
            messages.success(
                request,
                _("Assigned %(tool)s to %(user)s.") % {
                    "tool": instance.display_name,
                    "user": chosen_operator.username,
                },
            )
            return redirect("tools_instance_detail", instance_id=instance.pk)

    # ── Tool context for the form header ──
    last_assignment = (
        ToolAssignment.objects
        .filter(instance=instance)
        .order_by("-checkout_at")
        .select_related("operator", "machine")
        .first()
    )
    total_assignments = ToolAssignment.objects.filter(instance=instance).count()
    total_damage = ToolDamageReport.objects.filter(instance=instance).count()

    from inventory.models import ReusableToolInstance as RTI
    siblings_count = RTI.objects.filter(
        part=instance.part, is_active=True,
    ).count()

    return render(request, "maintenance/tools_assign.html", {
        "tool": instance,
        "form": form,
        "self_assign_locked": is_self_assign,
        "is_manager_or_supervisor": is_manager_or_supervisor,
        "last_assignment": last_assignment,
        "total_assignments": total_assignments,
        "total_damage": total_damage,
        "siblings_count": siblings_count,
    })


@login_required
@require_http_methods(["GET", "POST"])
def tools_return(request, assignment_id):
    a = get_object_or_404(ToolAssignment, pk=assignment_id, return_at__isnull=True)

    user = request.user
    is_holder = a.operator_id == user.id
    is_manager = user.role in (User.Role.MANAGER, User.Role.SUPERVISOR, User.Role.SUPER_ADMIN)
    if not (is_holder or is_manager):
        return HttpResponseForbidden(_("You cannot return this assignment."))

    form = ToolReturnForm(request.POST or None)

    if request.method == "POST" and form.is_valid():
        try:
            a, damage_report = ToolAssignmentService.return_tool(
                assignment=a,
                condition_in=form.cleaned_data["condition_in"],
                actor=user,
                damage_reason=form.cleaned_data.get("damage_reason") or None,
            )
        except ValueError as e:
            messages.error(request, str(e))
        else:
            if damage_report:
                messages.warning(
                    request,
                    _("Returned as damaged. Damage report opened. Manager will review."),
                )
            else:
                messages.success(
                    request,
                    _("Returned %(tool)s.") % {"tool": a.instance.display_name},
                )
            return redirect("tools_instance_detail", instance_id=a.instance_id)

    return render(request, "maintenance/tools_return.html", {
        "assignment": a,
        "form": form,
    })


# ---------------------------------------------------------------------------
# Damage resolution
# ---------------------------------------------------------------------------

@login_required
@role_required(User.Role.MANAGER, User.Role.SUPER_ADMIN)
def tools_damage_resolve(request, report_id):
    report = get_object_or_404(ToolDamageReport, pk=report_id)
    if report.status != ToolDamageReport.Status.OPEN:
        messages.info(request, _("This report is already resolved."))
        return redirect("tools_instance_detail", instance_id=report.instance_id)

    form = ToolDamageResolveForm(request.POST or None)

    if request.method == "POST" and form.is_valid():
        action = form.cleaned_data["action"]
        try:
            if action == "repair":
                ToolDamageService.repair(
                    report=report,
                    repair_cost=form.cleaned_data["repair_cost"],
                    actor=request.user,
                )
                messages.success(request, _("Marked as repaired."))
            else:
                ToolDamageService.write_off(report=report, actor=request.user)
                messages.success(request, _("Written off. Tool marked inactive."))
        except ValueError as e:
            messages.error(request, str(e))
        else:
            return redirect("tools_instance_detail", instance_id=report.instance_id)

    return render(request, "maintenance/tools_damage_resolve.html", {
        "report": report,
        "form": form,
    })


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

@login_required
@role_required(User.Role.MANAGER, User.Role.SUPER_ADMIN)
def tools_dashboard(request):
    instances = ReusableToolInstance.objects.filter(is_active=True)

    counts = {
        "available":     instances.filter(status=ReusableToolInstance.Status.AVAILABLE).count(),
        "in_use":        instances.filter(status=ReusableToolInstance.Status.IN_USE).count(),
        "out_of_service": instances.filter(status=ReusableToolInstance.Status.OUT_OF_SERVICE).count(),
    }

    total_value_sum = Decimal("0")
    for inst in instances.select_related("source_stock_movement"):
        if inst.purchase_cost is not None:
            total_value_sum += inst.purchase_cost

    open_assignments = (
        ToolAssignment.objects
        .filter(return_at__isnull=True, instance__is_active=True)
        .select_related("instance__part", "operator", "machine")
        .order_by("checkout_at")
    )

    # Group by holder + machine for the "currently held by" card
    by_holder = {}
    for a in open_assignments:
        key = a.operator_id
        slot = by_holder.setdefault(key, {"operator": a.operator, "items": []})
        slot["items"].append(a)
    holder_rows = sorted(
        by_holder.values(),
        key=lambda r: len(r["items"]),
        reverse=True,
    )

    # Distinct tool-parts in the pool (for "Tools in pool" card)
    pool_parts = (
        SparePart.objects
        .filter(item_type=SparePart.ItemType.REUSABLE_TOOL, status="active")
        .annotate(
            n_total=Count("tool_instances", filter=Q(tool_instances__is_active=True)),
            n_available=Count("tool_instances", filter=Q(
                tool_instances__is_active=True,
                tool_instances__status=ReusableToolInstance.Status.AVAILABLE,
            )),
            n_in_use=Count("tool_instances", filter=Q(
                tool_instances__is_active=True,
                tool_instances__status=ReusableToolInstance.Status.IN_USE,
            )),
            n_oos=Count("tool_instances", filter=Q(
                tool_instances__is_active=True,
                tool_instances__status=ReusableToolInstance.Status.OUT_OF_SERVICE,
            )),
        )
        .order_by("-n_total", "name")
    )

    open_damage = (
        ToolDamageReport.objects
        .filter(status=ToolDamageReport.Status.OPEN)
        .order_by("-damage_date")
        .select_related("instance__part", "reported_by", "machine")[:10]
    )
    recent_damage = (
        ToolDamageReport.objects
        .exclude(status=ToolDamageReport.Status.OPEN)
        .order_by("-damage_date")
        .select_related("instance__part", "reported_by", "machine")[:10]
    )

    return render(request, "maintenance/tools_dashboard.html", {
        "counts": counts,
        "total_value": total_value_sum,
        "holder_rows": holder_rows,
        "pool_parts": pool_parts,
        "open_assignments": open_assignments[:25],
        "open_damage": open_damage,
        "recent_damage": recent_damage,
    })


# ---------------------------------------------------------------------------
# Tools list (all instances, with search + filters)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Tools list (unified — single page replaces the old search + list)
# ---------------------------------------------------------------------------

@login_required
@role_required(User.Role.MANAGER, User.Role.SUPERVISOR, User.Role.TECHNICIAN,
               User.Role.OPERATOR, User.Role.SUPER_ADMIN)
def tools_list(request):
    """Unified tools page.

    Visibility by role:
        Manager / Supervisor / Super Admin:
            - Sees every active tool (Available / In Use / Out of Service)
            - Sees cost fields
            - Sees quick "Assign to…" button on available rows
        Operator / Technician:
            - Sees only AVAILABLE tools + their own currently-held tools
            - Cost fields hidden
            - "Take this tool" button (self-assign) on available rows
            - "Return" button on their own held rows

    Status filter chips work for everyone; the search box matches name / SKU /
    tool_number.
    """
    user = request.user
    is_manager_or_above = user.role in (
        User.Role.MANAGER, User.Role.SUPERVISOR, User.Role.SUPER_ADMIN,
    )
    show_costs = is_manager_or_above

    q = (request.GET.get("q") or "").strip()
    status_filter = request.GET.get("status") or "all"

    base = ReusableToolInstance.objects.select_related("part").filter(is_active=True)

    # Restrict what operators/technicians see
    if not is_manager_or_above:
        # Available tools (anyone can see + take)
        # PLUS tools currently held by the current user (so they can return)
        held_by_me = ToolAssignment.objects.filter(
            operator=user, return_at__isnull=True,
        ).values_list("instance_id", flat=True)
        base = base.filter(
            Q(status=ReusableToolInstance.Status.AVAILABLE)
            | Q(pk__in=list(held_by_me))
        )

    if q:
        base = base.filter(
            Q(part__name__icontains=q)
            | Q(part__sku__icontains=q)
            | Q(tool_number__icontains=q)
        )

    # Operator/tech cannot filter to Out of Service (no useful rows for them)
    allowed_statuses = ("available", "in_use") if is_manager_or_above else ("available",)
    if status_filter in allowed_statuses:
        base = base.filter(status=status_filter)
    else:
        status_filter = "all" if is_manager_or_above else "available"

    instances = list(base.order_by("part__name", "tool_number"))

    # Counts (respecting role visibility)
    visible_q = ReusableToolInstance.objects.filter(is_active=True)
    if not is_manager_or_above:
        visible_q = visible_q.filter(
            Q(status=ReusableToolInstance.Status.AVAILABLE)
            | Q(assignments__operator=user, assignments__return_at__isnull=True)
        ).distinct()
    chip_counts = {
        "available":     visible_q.filter(status=ReusableToolInstance.Status.AVAILABLE).distinct().count(),
        "in_use":        visible_q.filter(status=ReusableToolInstance.Status.IN_USE).distinct().count() if is_manager_or_above else 0,
        "out_of_service": visible_q.filter(status=ReusableToolInstance.Status.OUT_OF_SERVICE).distinct().count() if is_manager_or_above else 0,
    }

    assignments_by_inst = {
        a.instance_id: a
        for a in ToolAssignment.objects
            .filter(return_at__isnull=True, instance__in=instances)
            .select_related("operator", "machine")
    }
    rows = [(inst, assignments_by_inst.get(inst.pk)) for inst in instances]

    return render(request, "maintenance/tools_list.html", {
        "q": q,
        "status_filter": status_filter,
        "rows": rows,
        "chip_counts": chip_counts,
        "show_costs": show_costs,
        "is_manager_or_above": is_manager_or_above,
        "self_id": user.id,
    })

