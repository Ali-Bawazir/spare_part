import json
import logging
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import models, transaction
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.http import require_GET, require_POST

from accounts.models import User
from accounts.permissions import role_required
from inventory.models import Inventory, StockMovement
from inventory.services import stock_in
from inventory.services_allocation import PartAllocationService
from maintenance.models import Machine, Site
from maintenance.services import log_audit
from maintenance.services_notifications import notify_po_received_summary

from .forms import PurchaseOfficerForm, PurchaseRequestForm, PurchaseOrderForm, SupplierQuickForm
from .models import PurchaseRequest, PurchaseOrder, PurchaseOrderItem, Supplier

logger = logging.getLogger(__name__)


@login_required
@role_required(User.Role.MANAGER, User.Role.SUPER_ADMIN)
def purchase_request_create(request):
    if request.method == "POST":
        form = PurchaseRequestForm(request.POST)
        # Live stock badge: build {part_id: quantity_available} from the
        # form's part queryset (used on re-render after validation failure
        # and on initial GET below).
        stock_data = {
            inv.part_id: float(inv.quantity_available)
            for inv in Inventory.objects.filter(part__in=form.fields["part"].queryset)
        }
        import json
        stock_data_json = json.dumps(stock_data)
        if form.is_valid():
            pr = form.save(commit=False)
            pr.created_by = request.user
            pr.save()
            from maintenance.notifications import notify_procurement_request

            notify_procurement_request(pr)

            # v4.9 B3: link pending voice attachment to this PR (manager records)
            pending_voice_id = request.POST.get("voice_attachment_id", "").strip()
            if pending_voice_id and pending_voice_id.isdigit():
                try:
                    from maintenance.models import Attachment
                    att = Attachment.objects.get(
                        pk=int(pending_voice_id),
                        entity_type='pending_voice',
                        uploaded_by=request.user,
                    )
                    att.entity_type = 'purchase_request'
                    att.entity_id = pr.pk
                    att.save(update_fields=['entity_type', 'entity_id'])
                except Attachment.DoesNotExist:
                    pass

            messages.success(request, _("Purchase request sent to procurement."))
            return redirect("purchase_list")
        # If form is invalid, fall through to render the form with errors.
        locked_asset = None
        machine_param = request.GET.get("machine")
        component_param = request.GET.get("component")
        has_deep_link = False
        resolved_machine_id = None
        resolved_component_id = None
    else:
        initial = {}
        wo_id = request.GET.get("wo")
        if wo_id and wo_id.isdigit():
            initial["work_order"] = int(wo_id)
        # Pre-fill from URL params. If component is level-5, walk the parent
        # chain to find the level-3 Machine.
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
        form = PurchaseRequestForm(initial=initial, lock_asset=lock_asset)
        locked_asset = None
        # Live stock badge: build {part_id: quantity_available} from the
        # form's part queryset. Manager/procurement see current stock
        # before raising a purchase request.
        stock_data = {
            inv.part_id: float(inv.quantity_available)
            for inv in Inventory.objects.filter(part__in=form.fields["part"].queryset)
        }
        import json
        stock_data_json = json.dumps(stock_data)

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
        "procurement/pr_form.html",
        {
            "form": form,
            "locked_asset": locked_asset,
            "machine": Machine.objects.filter(pk=resolved_machine_id).first() if resolved_machine_id else Machine.objects.filter(parent__isnull=True, is_active=True).order_by("pk").first(),
            "ancestors": [],
            "stock_data_json": stock_data_json,
        },
    )


@login_required
@role_required(User.Role.MANAGER, User.Role.PROCUREMENT, User.Role.SUPERVISOR, User.Role.SUPER_ADMIN)
def purchase_list(request):
    rows = PurchaseRequest.objects.select_related("part", "created_by", "supplier", "work_order", "purchase_order").order_by("-created_at")[:300]
    return render(request, "procurement/pr_list.html", {"requests": rows})


@login_required
@role_required(User.Role.MANAGER, User.Role.PROCUREMENT, User.Role.SUPER_ADMIN)
def purchase_officer(request, pk):
    pr = get_object_or_404(PurchaseRequest, pk=pk)
    if request.method == "POST":
        form = PurchaseOfficerForm(request.POST, instance=pr)
        if form.is_valid():
            inst = form.save(commit=False)
            inst.handled_by = request.user
            inst.save()
            # v4.9.4: link pending voice attachment to this PR (officer's voice)
            pending_voice_id = request.POST.get("voice_attachment_id", "").strip()
            if pending_voice_id and pending_voice_id.isdigit():
                try:
                    from maintenance.models import Attachment
                    att = Attachment.objects.get(
                        pk=int(pending_voice_id),
                        entity_type='pending_voice',
                        uploaded_by=request.user,
                    )
                    att.entity_type = 'purchase_request'
                    att.entity_id = pr.pk
                    att.save(update_fields=['entity_type', 'entity_id'])
                except Attachment.DoesNotExist:
                    pass
            messages.success(request, _("Purchase request updated."))
            return redirect("purchase_list")
    else:
        form = PurchaseOfficerForm(instance=pr)
    return render(request, "procurement/pr_officer.html", {"pr": pr, "form": form})


@login_required
@role_required(User.Role.MANAGER, User.Role.PROCUREMENT, User.Role.SUPER_ADMIN)
def purchase_request_detail(request, pk):
    pr = get_object_or_404(PurchaseRequest, pk=pk)

    # Compute the vertical chain (ancestors) for the asset tree
    ancestors = []
    current = pr.component if pr.component_id else pr.machine
    if current is not None:
        parent = current.parent
        while parent is not None:
            ancestors.insert(0, parent)
            parent = parent.parent

    tree_node = pr.component if pr.component_id else pr.machine

    from maintenance.models import (
        MaintenanceIssue, WorkOrder, PMSchedule, ExternalRepairOrder,
    )

    related_issues = MaintenanceIssue.objects.filter(
        machine=pr.machine, component=pr.component
    )[:10]
    related_wos = WorkOrder.objects.filter(
        machine=pr.machine, component=pr.component
    )[:10]
    related_pms = PMSchedule.objects.filter(
        machine=pr.machine, component=pr.component
    )[:10]
    related_eros = ExternalRepairOrder.objects.filter(
        machine=pr.machine, component=pr.component
    )[:10]
    related_prs = PurchaseRequest.objects.filter(
        machine=pr.machine, component=pr.component
    ).exclude(pk=pr.pk)[:10]

    from maintenance.models import Attachment
    pr_voice_attachments = Attachment.objects.filter(
        entity_type='purchase_request',
        entity_id=pr.pk,
        mime_type__startswith='audio',
    ).order_by('-uploaded_at')

    context = {
        "pr": pr,
        "ancestors": ancestors,
        "related_issues": related_issues,
        "related_wos": related_wos,
        "related_pms": related_pms,
        "related_eros": related_eros,
        "related_prs": related_prs,
        "pr_voice_attachments": pr_voice_attachments,
    }
    if tree_node is not None:
        context["machine"] = tree_node
    return render(request, "procurement/pr_detail.html", context)


@login_required
@require_POST
@role_required(User.Role.MANAGER, User.Role.PROCUREMENT, User.Role.SUPER_ADMIN)
def create_pr_from_shortage(request, shortage_id):
    """Create a PurchaseRequest from a closed shortage report (one-click manual).

    Auto-creating a PR when a shortage closes is the wrong default: a shortage
    does not always mean "buy more" (cancelled, retired, alternative part,
    transfer from another warehouse, another WO already purchasing, etc.).
    The manager decides. This view is the manual trigger: one click from the
    WO shortage card, gated to roles that can create a PR.
    """
    from inventory.models import PartShortageReport

    shortage = get_object_or_404(PartShortageReport, pk=shortage_id)
    if shortage.status != PartShortageReport.Status.CLOSED:
        messages.error(
            request,
            _("Shortage must be closed before creating a backorder PR."),
        )
        return redirect("work_order_detail", pk=shortage.work_order_id)
    if shortage.shortage_qty <= 0:
        messages.error(request, _("No shortage to backorder."))
        return redirect("work_order_detail", pk=shortage.work_order_id)

    pr = PurchaseRequest.objects.create(
        part=shortage.part,
        machine=shortage.work_order.machine if shortage.work_order_id else None,
        quantity=shortage.shortage_qty,
        source_shortage_report=shortage,
        status=PurchaseRequest.Status.PENDING,
        created_by=request.user,
        notes=_(
            "Backorder PR created from shortage #%(shortage)s on WO-%(wo)s."
        )
        % {"shortage": shortage.pk, "wo": shortage.work_order.number},
    )
    log_audit(
        actor=request.user,
        action="pr_created_from_shortage",
        entity="PurchaseRequest",
        object_id=pr.pk,
        payload={
            "shortage_id": shortage.pk,
            "quantity": float(pr.quantity),
            "work_order_id": shortage.work_order_id,
        },
    )
    messages.success(
        request,
        _("Purchase Request #%(pk)s created (qty %(qty)s) for shortage #%(shortage)s.")
        % {"pk": pr.pk, "qty": pr.quantity, "shortage": shortage.pk},
    )
    return redirect("pr_detail", pk=pr.pk)


@login_required
@role_required(User.Role.MANAGER, User.Role.PROCUREMENT, User.Role.SUPERVISOR, User.Role.SUPER_ADMIN)
def purchase_request_add_voice(request, pk):
    """v4.9.4: Add a voice comment to an existing PR.

    POST-only. The voice is uploaded via /attachments/upload-pending/ which
    creates a pending Attachment. This view re-links it to the PR and
    optionally stores a short note in the attachment's note field.
    """
    pr = get_object_or_404(PurchaseRequest, pk=pk)
    if request.method != "POST":
        return redirect("pr_detail", pk=pk)
    pending_voice_id = request.POST.get("voice_attachment_id", "").strip()
    note = request.POST.get("note", "").strip()
    if not (pending_voice_id and pending_voice_id.isdigit()):
        messages.error(request, _("Please record a voice note before submitting."))
        return redirect("pr_detail", pk=pk)
    try:
        from maintenance.models import Attachment
        att = Attachment.objects.get(
            pk=int(pending_voice_id),
            entity_type='pending_voice',
            uploaded_by=request.user,
        )
        att.entity_type = 'purchase_request'
        att.entity_id = pr.pk
        if note:
            att.note = note[:500]
            att.save(update_fields=['entity_type', 'entity_id', 'note'])
        else:
            att.save(update_fields=['entity_type', 'entity_id'])
    except Attachment.DoesNotExist:
        messages.error(request, _("Voice attachment not found or not owned by you."))
        return redirect("pr_detail", pk=pk)
    messages.success(request, _("Voice note added to PR #%(pk)d.") % {"pk": pr.pk})
    return redirect("pr_detail", pk=pk)


@login_required
@role_required(User.Role.PROCUREMENT, User.Role.MANAGER, User.Role.SUPER_ADMIN)
def purchase_order_list(request):
    """List POs with optional ?status=, ?group_by=supplier, ?period=YYYY-MM."""
    import re
    from django.db.models import Count, Sum

    qs = PurchaseOrder.objects.select_related(
        "supplier", "created_by", "handled_by"
    ).prefetch_related("items")

    # Status filter
    status_filter = request.GET.get("status", "").strip()
    if status_filter and status_filter in dict(PurchaseOrder.Status.choices):
        qs = qs.filter(status=status_filter)

    # Period filter (YYYY-MM)
    period = request.GET.get("period", "").strip()
    if re.match(r"^\d{4}-\d{2}$", period):
        year, month = map(int, period.split("-"))
        qs = qs.filter(created_at__year=year, created_at__month=month)

    # Group-by-supplier flag
    group_by_supplier = request.GET.get("group_by", "").strip() == "supplier"

    base_ctx = {
        "status_filter": status_filter,
        "status_choices": PurchaseOrder.Status.choices,
        "group_by_supplier": group_by_supplier,
        "period": period,
        "period_choices": _recent_month_choices(12),
    }

    if group_by_supplier:
        supplier_rows = (
            qs.values("supplier__id", "supplier__name", "supplier__code")
              .annotate(
                  po_count=Count("id", distinct=True),
                  line_count=Count("items", distinct=True),
                  total=Sum("items__total_price"),
              )
              .order_by("-total", "supplier__name")
        )
        supplier_rows = list(supplier_rows)
        supplier_grand_total = sum((row["total"] or 0) for row in supplier_rows)
        pos = qs.order_by("-created_at")
        return render(
            request,
            "procurement/po_list.html",
            {
                **base_ctx,
                "pos": pos,
                "supplier_rows": supplier_rows,
                "supplier_grand_total": supplier_grand_total,
            },
        )

    rows = qs.order_by("-created_at")[:200]
    return render(
        request,
        "procurement/po_list.html",
        {**base_ctx, "pos": rows},
    )


def _recent_month_choices(n: int) -> list[tuple[str, str]]:
    """Return [(value, label), ...] for the last n months (most recent first).

    value='YYYY-MM', label='Jun 2026'.
    """
    from datetime import date
    today = date.today().replace(day=1)
    out: list[tuple[str, str]] = []
    y, m = today.year, today.month
    for _ in range(n):
        out.append((f"{y:04d}-{m:02d}", date(y, m, 1).strftime("%b %Y")))
        m -= 1
        if m == 0:
            m = 12
            y -= 1
    return out


@login_required
@role_required(User.Role.PROCUREMENT, User.Role.MANAGER, User.Role.SUPER_ADMIN)
def purchase_order_by_supplier(request):
    """Always-grouped supplier view. ?period=YYYY-MM honored."""
    request_get = request.GET.copy()
    request_get["group_by"] = "supplier"
    request.GET = request_get
    return purchase_order_list(request)


@login_required
@role_required(User.Role.PROCUREMENT, User.Role.MANAGER, User.Role.SUPER_ADMIN)
def purchase_order_supplier_csv(request, supplier_id):
    """Download all POs + line items for a single supplier as CSV.

    Writes UTF-8 with BOM so Excel auto-detects encoding and renders
    Arabic (and other non-ASCII) supplier names, PO notes, and part
    names correctly. Filename uses RFC 5987 for UTF-8 support.
    """
    import csv
    import re
    from urllib.parse import quote

    from django.http import HttpResponse

    supplier = get_object_or_404(Supplier, pk=supplier_id)

    qs = PurchaseOrder.objects.filter(supplier=supplier).prefetch_related("items__part")
    period = request.GET.get("period", "").strip()
    if re.match(r"^\d{4}-\d{2}$", period):
        year, month = map(int, period.split("-"))
        qs = qs.filter(created_at__year=year, created_at__month=month)
    qs = qs.order_by("-created_at")

    response = HttpResponse(content_type="text/csv; charset=utf-8")
    ascii_name = f"POs_{supplier.code or supplier.pk}_{period or 'all'}.csv"
    utf8_name = ascii_name
    # RFC 5987 encoding for non-ASCII filename support
    response["Content-Disposition"] = (
        f'attachment; filename="{ascii_name}"; '
        f"filename*=UTF-8''{quote(utf8_name)}"
    )

    # Bug fix: prepend UTF-8 BOM so Excel auto-detects encoding.
    # Without BOM, Excel reads the file as Windows-1252 and shows
    # Arabic as garbled text (mojibake). All modern tools (LibreOffice,
    # Google Sheets, Numbers) ignore the BOM and read UTF-8 directly.
    response.write("\ufeff")

    writer = csv.writer(response)
    writer.writerow([
        "PO Number", "Status", "Created", "Supplier Invoice",
        "Expected Delivery", "Notes",
        "Part SKU", "Part Name", "Ordered Qty", "Received Qty",
        "Negotiated Unit Price", "Actual Unit Price", "Line Total",
    ])
    for po in qs:
        for item in po.items.all():
            writer.writerow([
                po.po_number,
                po.get_status_display(),
                po.created_at.date().isoformat(),
                po.supplier_invoice_number or "",
                po.expected_delivery.isoformat() if po.expected_delivery else "",
                (po.notes or "").replace("\n", " "),
                item.part.sku,
                item.part.name,
                str(item.ordered_qty),
                str(item.received_qty),
                str(item.negotiated_unit_price or ""),
                str(item.actual_unit_price or ""),
                str(item.total_price or ""),
            ])
    return response


@login_required
@role_required(User.Role.PROCUREMENT, User.Role.MANAGER, User.Role.SUPER_ADMIN)
def purchase_order_create(request):
    """Create a new purchase order with inline line items and optional multi-PR merge."""
    from procurement.forms import POItemFormSet
    from procurement.models import PurchaseRequest

    pr_id = request.GET.get("pr")

    if request.method == "POST":
        form = PurchaseOrderForm(request.POST)
        formset = POItemFormSet(request.POST, prefix="items")
    else:
        initial = {}
        if pr_id:
            initial["purchase_request"] = pr_id
        supplier_id = request.GET.get("supplier")
        if supplier_id:
            try:
                from procurement.models import Supplier
                if Supplier.objects.filter(pk=supplier_id, is_active=True).exists():
                    initial["supplier"] = int(supplier_id)
            except (ValueError, TypeError):
                pass
        form = PurchaseOrderForm(initial=initial)
        formset = POItemFormSet(prefix="items", queryset=PurchaseOrderItem.objects.none())

        tool_id = request.GET.get("tool_id")
        if tool_id:
            from maintenance.models import Tool
            tool = get_object_or_404(Tool, pk=tool_id)
            first_form = formset.forms[0]
            first_form.initial = {
                "part": None,
                "tool": tool.pk,
                "ordered_qty": "1",
                "negotiated_unit_price": (
                    str(tool.purchase_cost) if tool.purchase_cost is not None else "0"
                ),
                "line_note": (
                    f"Replacement for damaged tool: {tool.name} ({tool.code})"
                ),
            }

    # Live stock badge: per-row badge needs {row_idx: {part_id: qty}}.
    # Walk the formset forms and build one dict per row.
    stock_data_per_row = {}
    for row_idx, item_form in enumerate(formset.forms):
        part_ids = list(item_form.fields["part"].queryset.values_list("pk", flat=True))
        invs = Inventory.objects.filter(part_id__in=part_ids)
        stock_data_per_row[str(row_idx)] = {
            inv.part_id: float(inv.quantity_available) for inv in invs
        }
    stock_data_json = json.dumps(stock_data_per_row)

    if request.method == "POST":
        if form.is_valid() and formset.is_valid():
            po = form.save(commit=False)
            po.created_by = request.user
            po.save()
            instances = formset.save(commit=False)
            for inst in instances:
                inst.purchase_order = po
                inst.save()
            for inst in formset.deleted_objects:
                inst.delete()

            # Handle PR selections
            selected_pr_ids = request.POST.getlist("selected_prs")
            if selected_pr_ids:
                from procurement.models import PurchaseRequest
                prs = PurchaseRequest.objects.filter(pk__in=selected_pr_ids, status=PurchaseRequest.Status.PENDING)
                for pr in prs:
                    # Add PR items as line items if not already added via formset
                    existing = po.items.filter(part=pr.part).first()
                    if not existing:
                        unit_price = pr.unit_price or pr.part.last_purchase_cost or Decimal("0")
                        PurchaseOrderItem.objects.create(
                            purchase_order=po,
                            part=pr.part,
                            ordered_qty=pr.quantity,
                            negotiated_unit_price=unit_price,
                            total_price=pr.quantity * unit_price,
                        )
                    pr.purchase_order = po
                    pr.status = PurchaseRequest.Status.CONVERTED_TO_PO
                    pr.save(update_fields=["purchase_order", "status"])

            messages.success(request, _("Purchase order %(po)s created.") % {"po": po.po_number})
            return redirect("purchase_order_detail", pk=po.pk)

    # Available PENDING PRs for selection
    pending_prs = PurchaseRequest.objects.filter(
        status=PurchaseRequest.Status.PENDING
    ).select_related("part", "created_by").order_by("-created_at")[:50]

    return render(request, "procurement/po_form.html", {
        "form": form,
        "formset": formset,
        "po": None,
        "page_heading": _("New purchase order"),
        "pending_prs": pending_prs,
        "stock_data_json": stock_data_json,
    })


@login_required
@role_required(User.Role.PROCUREMENT, User.Role.MANAGER, User.Role.SUPER_ADMIN)
def purchase_order_create_from_pr(request, pr_pk):
    """Create a PO pre-filled from a purchase request."""
    pr = get_object_or_404(PurchaseRequest, pk=pr_pk)
    if request.method == "POST":
        form = PurchaseOrderForm(request.POST)
        if form.is_valid():
            po = form.save(commit=False)
            po.created_by = request.user
            po.save()
            PurchaseOrderItem.objects.create(
                purchase_order=po,
                part=pr.part,
                ordered_qty=pr.quantity,
                negotiated_unit_price=pr.unit_price or pr.part.last_purchase_cost or Decimal("0"),
                total_price=pr.quantity * (pr.unit_price or pr.part.last_purchase_cost or Decimal("0")),
            )
            pr.purchase_order = po
            pr.status = PurchaseRequest.Status.CONVERTED_TO_PO
            pr.save(update_fields=["purchase_order", "status"])
            messages.success(request, _("PO %(po)s created from PR #%(pk)d.") % {"po": po.po_number, "pk": pr.pk})
            return redirect("purchase_order_detail", pk=po.pk)
    else:
        form = PurchaseOrderForm()
    return render(request, "procurement/po_form.html", {
        "form": form,
        "po": None,
        "page_heading": _("New PO from PR #%(pk)d") % {"pk": pr.pk},
        "pr": pr,
        "pending_prs": [],
    })


@login_required
@role_required(User.Role.PROCUREMENT, User.Role.MANAGER, User.Role.SUPER_ADMIN)
def purchase_order_detail(request, pk):
    """View and edit a purchase order."""
    po = get_object_or_404(
        PurchaseOrder.objects.prefetch_related(
            "items", "items__part", "items__tool", "supplier", "created_by", "handled_by",
        ),
        pk=pk
    )
    sites = Site.objects.filter(is_active=True).order_by("name")
    selected_site = sites.filter(is_default=True).first()

    items_with_inventory = []
    for item in po.items.all():
        if item.part and selected_site:
            inv = item.part.inventory_items.filter(site=selected_site).first()
        else:
            inv = None
        items_with_inventory.append({
            "item": item,
            "inventory_qty": inv.quantity_available if inv else Decimal("0"),
        })

    tool_line_count = sum(1 for it in po.items.all() if it.tool_id and not it.part_id)

    if request.method == "POST":
        if po.is_locked:
            messages.error(request, _("This PO is locked and cannot be edited."))
            return redirect("purchase_order_detail", pk=pk)
        form = PurchaseOrderForm(request.POST, instance=po)
        if form.is_valid():
            form.save()
            messages.success(request, _("Purchase order updated."))
            return redirect("purchase_order_detail", pk=pk)
    else:
        form = PurchaseOrderForm(instance=po)
    return render(request, "procurement/po_detail.html", {
        "po": po,
        "form": form,
        "items_with_inventory": items_with_inventory,
        "sites": sites,
        "selected_site": selected_site,
        "tool_line_count": tool_line_count,
    })


@login_required
@role_required(User.Role.PROCUREMENT, User.Role.MANAGER, User.Role.SUPER_ADMIN)
def purchase_order_receive(request, pk):
    """Receive items against a PO with v2 features: per-line condition,
    supplier invoice capture, reallocation, atomic transaction, and
    summary notification.

    POST form fields per line item:
      - good_qty_<pk>: units received in good condition
      - damaged_qty_<pk>: units received damaged (go to quarantine)
      - rejected_qty_<pk>: units rejected at inspection (no stock change;
        audit-only counter, does NOT count toward received_qty)
      - actual_unit_price_<pk>: actual invoiced unit price (overrides negotiated)

    PO-level:
      - supplier_invoice_ref: vendor invoice number (free text)
    """
    po = get_object_or_404(
        PurchaseOrder.objects.prefetch_related(
            "items", "items__part", "purchase_requests"
        ),
        pk=pk,
    )
    if po.status in {PurchaseOrder.Status.RECEIVED, PurchaseOrder.Status.CLOSED_SHORT, PurchaseOrder.Status.CANCELLED}:
        messages.warning(request, _("PO is %(status)s — cannot receive.") % {"status": po.get_status_display().lower()})
        return redirect("purchase_order_detail", pk=pk)

    if request.method != "POST":
        return render(request, "procurement/po_receive.html", {"po": po})

    # Pre-flight: validate totals
    site = Site.objects.filter(is_default=True).first()
    if not site:
        messages.error(request, _("No default site configured."))
        return redirect("purchase_order_detail", pk=pk)

    supplier_invoice_ref = (request.POST.get("supplier_invoice_ref") or "").strip()
    line_changes: list[dict] = []  # for the summary notification

    with transaction.atomic():
        # Persist supplier invoice ref to the PO (fix invoice-capture bug).
        if supplier_invoice_ref:
            po.supplier_invoice_number = supplier_invoice_ref
            po.save(update_fields=["supplier_invoice_number", "updated_at"])
        for item in po.items.all():
            good = _to_decimal(request.POST.get(f"good_qty_{item.pk}")) or Decimal("0")
            damaged = _to_decimal(request.POST.get(f"damaged_qty_{item.pk}")) or Decimal("0")
            rejected = _to_decimal(request.POST.get(f"rejected_qty_{item.pk}")) or Decimal("0")
            actual_price = _to_decimal(request.POST.get(f"actual_unit_price_{item.pk}"))

            # What physically arrived at the warehouse (excludes rejected units
            # which never made it past the dock). PO status is derived from
            # this — PO only flips to RECEIVED when arrived >= ordered.
            arrived = good + damaged

            if arrived <= 0 and rejected <= 0 and not actual_price:
                continue  # nothing to do for this line

            remaining = item.ordered_qty - item.received_qty
            if arrived > remaining and remaining > 0:
                messages.warning(
                    request,
                    _("%(sku)s: receiving %(arrived)s exceeds remaining %(remaining)s — capped.") % {
                        "sku": item.part.sku, "arrived": arrived, "remaining": remaining,
                    },
                )
                # proportionally scale (or just cap the good portion)
                ratio = remaining / arrived
                good = (good * ratio).quantize(Decimal("0.001"))
                damaged = (damaged * ratio).quantize(Decimal("0.001"))
                arrived = good + damaged

            # Update item fields
            # received_qty = units that physically arrived (good + damaged).
            # Rejected units do NOT count as received — they are an audit-only
            # counter tracked separately via rejected_qty.
            item.received_qty += arrived
            item.damaged_qty += damaged
            item.rejected_qty += rejected
            # Phase 4 BUG-5 fix: maintain the backordered_qty invariant
            # ordered_qty = received_qty + backordered_qty. Previously
            # backordered_qty was never updated and the invariant was
            # silently violated on every partial receive.
            remaining_after = item.ordered_qty - item.received_qty
            item.backordered_qty = (
                remaining_after if remaining_after > 0 else Decimal("0")
            )
            if actual_price and actual_price > 0:
                item.actual_unit_price = actual_price
            update_fields = [
                "received_qty", "damaged_qty", "rejected_qty", "backordered_qty",
            ]
            if actual_price and actual_price > 0:
                update_fields.append("actual_unit_price")
            item.save(update_fields=update_fields)

            # Stock in good units (to available)
            if good > 0:
                stock_in(
                    part=item.part,
                    quantity=good,
                    performed_by=request.user,
                    supplier=po.supplier,
                    supplier_name=po.supplier.name if po.supplier else "",
                    unit_cost=actual_price or item.negotiated_unit_price,
                    invoice_ref=supplier_invoice_ref or f"{po.po_number}",
                    note=f"Received against PO {po.po_number} (good)",
                    site=site,
                )

            # Damaged → quarantine
            if damaged > 0:
                inv, _created = Inventory.objects.select_for_update().get_or_create(
                    part=item.part, site=site,
                    defaults={"quantity_available": Decimal("0")},
                )
                inv.quantity_quarantine += damaged
                inv.save(update_fields=["quantity_quarantine"])
                StockMovement.objects.create(
                    part=item.part,
                    movement_type=StockMovement.MovementType.ADJUSTMENT,
                    quantity=damaged,
                    quantity_before=inv.quantity_quarantine - damaged,
                    quantity_after=inv.quantity_quarantine,
                    work_order=None,
                    site=site,
                    performed_by=request.user,
                    supplier=po.supplier,
                    supplier_name=po.supplier.name if po.supplier else "",
                    unit_cost=actual_price or item.negotiated_unit_price,
                    invoice_ref=supplier_invoice_ref or f"{po.po_number}",
                    reference={
                        "destination": "quarantine",
                        "reason": "damaged on receipt",
                        "po_number": po.po_number,
                    },
                )

            # Reallocate any part whose stock changed
            if good > 0 or damaged > 0:
                try:
                    PartAllocationService.reallocate_for_part(item.part)
                except Exception as e:
                    logger.warning("reallocate_for_part(%s) failed: %s", item.part.sku, e)

            # Per-line notification (legacy path — still works)
            from maintenance.notifications import notify_wo_part_received
            for pr in po.purchase_requests.all():
                if pr.work_order_id and good > 0:
                    notify_wo_part_received(
                        work_order=pr.work_order,
                        part=item.part,
                        qty=good,
                        po=po,
                        actor=request.user,
                    )

            if arrived > 0 or rejected > 0 or actual_price:
                line_changes.append({
                    "sku": item.part.sku,
                    "name": item.part.name,
                    "good": good,
                    "damaged": damaged,
                    "rejected": rejected,
                })

        # Update PO status
        if not po.items.filter(received_qty__lt=models.F("ordered_qty")).exists():
            po.status = PurchaseOrder.Status.RECEIVED
            po.received_at = timezone.now()
        else:
            po.status = PurchaseOrder.Status.PARTIAL_RECEIVED
        po.save(update_fields=["status", "received_at", "updated_at"])

        # Update PR statuses
        for pr in po.purchase_requests.all():
            pr.status = (
                PurchaseRequest.Status.FULFILLED
                if po.status == PurchaseOrder.Status.RECEIVED
                else PurchaseRequest.Status.PARTIALLY_FULFILLED
            )
            pr.save(update_fields=["status"])

        # Phase 7.7: auto-fulfill open PartIssueLines on WOs the linked
        # PRs are attached to. This closes the loop between the receive
        # and the WO warehouse-issue step so the user no longer has to
        # click "📤 Issue N from stock" manually after every receive.
        try:
            from inventory.services import auto_fulfill_wo_lines_from_po
            auto_fulfill_summary = auto_fulfill_wo_lines_from_po(
                po=po, actor=request.user,
            )
        except Exception as e:
            import logging
            logging.getLogger(__name__).exception(
                f"Auto-fulfill from PO {po.po_number} failed: {e}"
            )
            auto_fulfill_summary = {"enabled": True, "actions": [], "error": str(e)}

        # Phase 7.9: notify any open SHORTAGE WO Blockers linked to the
        # WO-bound PRs on this PO that the procurement is fulfilled.
        # The handler at WorkOrderBlockerService.sync_from_external_event
        # resolves SHORTAGE blockers matching the source PartShortageReport.
        try:
            from procurement.models import PurchaseRequest as _PR
            from maintenance.services_blocker import WorkOrderBlockerService
            for _pr in _PR.objects.filter(purchase_order=po, work_order__isnull=False).exclude(work_order=None):
                _report = getattr(_pr, "source_shortage_report", None)
                if _report is None:
                    continue
                WorkOrderBlockerService.sync_from_external_event(
                    external_obj=_report,
                    event_type="SHORTAGE_FULFILLED",
                    actor=request.user,
                    payload={"note": _("Auto-fulfilled via PO %s") % po.po_number},
                )
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning(
                f"Failed to emit SHORTAGE_FULFILLED for PO {po.po_number}: {e}"
            )

    # Outside atomic: fire summary notification
    if line_changes:
        notify_po_received_summary(po, request.user)
        items_str = ", ".join(
            f"{c['sku']} ({c['good']}g/{c['damaged']}d/{c['rejected']}r)"
            for c in line_changes
        )
        messages.success(request, _("Received: %(items)s") % {"items": items_str})
        if supplier_invoice_ref:
            messages.info(request, _("Supplier invoice ref: %(ref)s") % {"ref": supplier_invoice_ref})
        # Show auto-fulfill summary if any actions ran
        auto_actions = auto_fulfill_summary.get("actions", []) if auto_fulfill_summary else []
        issued = [a for a in auto_actions if a.get("type") == "auto_issued"]
        if issued:
            issue_str = ", ".join(
                f"{a['qty']} × {a['part']} → WO-{a['wo']}"
                for a in issued
            )
            messages.info(
                request,
                _("Auto-issued to WOs: %(items)s") % {"items": issue_str},
            )

    return redirect("purchase_order_detail", pk=pk)


def _to_decimal(value) -> Decimal:
    """Parse a Decimal from a string, returning Decimal('0') for None/empty/invalid."""
    if not value:
        return Decimal("0")
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal("0")


@login_required
@role_required(User.Role.PROCUREMENT, User.Role.MANAGER, User.Role.SUPER_ADMIN)
def purchase_order_pdf(request, pk):
    """Generate a professional PO PDF for suppliers."""
    po = get_object_or_404(
        PurchaseOrder.objects.prefetch_related("items", "items__part", "supplier"),
        pk=pk
    )

    from maintenance.pdf_utils import build_pdf_response, _header_table, _section, _field_table, _safe_paragraph, signature_block
    from reportlab.platypus import Paragraph, Spacer, Table, TableStyle
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib import colors
    from reportlab.lib.units import mm
    from decimal import Decimal

    buf, doc = build_pdf_response(f"{po.po_number}.pdf")
    styles = getSampleStyleSheet()
    elements = []

    # Header
    elements.append(_header_table())
    elements.append(Spacer(1, 4 * mm))
    elements.append(Paragraph(f"<b>{_('PURCHASE ORDER')}</b>", styles["Normal"]))

    # PO info
    elements += _section("Order Details")
    elements.append(_field_table([
        ("PO Number", po.po_number),
        ("Date", po.created_at.strftime("%Y-%m-%d")),
        ("Supplier", po.supplier.name if po.supplier else "—"),
        ("Expected Delivery", po.expected_delivery.strftime("%Y-%m-%d") if po.expected_delivery else "—"),
        ("Invoice Ref", po.invoice_ref or "—"),
        ("Notes", po.notes or "—"),
    ]))

    # Items table
    elements += _section("Items")
    item_data = [[
        Paragraph("<b>SKU</b>", styles["Normal"]),
        Paragraph("<b>Part Name</b>", styles["Normal"]),
        Paragraph("<b>Qty</b>", styles["Normal"]),
        Paragraph("<b>Unit Cost</b>", styles["Normal"]),
        Paragraph("<b>Total</b>", styles["Normal"]),
    ]]
    grand_total = Decimal("0")
    has_discrepancy = False

    # Build delivery location from the default Site (precedence: explicit default >
    # first active site > fallback "Main Factory"). Computed once and reused below.
    default_site = (
        Site.objects.filter(is_default=True, is_active=True).first()
        or Site.objects.filter(is_active=True).first()
    )
    if default_site and default_site.address:
        delivery_location = f"{default_site.name} — {default_site.address}"
    elif default_site:
        delivery_location = default_site.name
    else:
        delivery_location = "Main Factory"

    for item in po.items.all():
        # Prefer actual invoiced price if set, fallback to negotiated price.
        unit_cost = item.actual_unit_price if item.actual_unit_price is not None else item.negotiated_unit_price
        total = item.ordered_qty * unit_cost
        grand_total += total
        if item.actual_unit_price is not None and item.actual_unit_price != item.negotiated_unit_price:
            has_discrepancy = True
            cost_cell = Paragraph(
                f"{unit_cost:.4f} <font color='#b91c1c' size='8'>(was {item.negotiated_unit_price:.4f})</font>",
                styles["Normal"],
            )
        else:
            cost_cell = f"{unit_cost:.4f}"
        item_data.append([
            item.part.sku,
            _safe_paragraph(item.part.name, styles["Normal"]),
            f"{item.ordered_qty:.3f}",
            cost_cell,
            f"{total:.4f}",
        ])
    item_data.append(["", "", "", "Grand Total", f"{grand_total:.4f}"])

    col_widths = [110, 170, 50, 60, 90]  # SKU +50, Part Name -30, Total +20 (more room for "87.5000")
    item_table = Table(item_data, colWidths=col_widths, repeatRows=1)
    item_table.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -2), 0.5, colors.grey),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0047AB")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, 0), 10),
        ("ALIGN", (2, 0), (-1, -1), "RIGHT"),
        ("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#E8E8E8")),
        ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
        ("FONTSIZE", (0, -1), (-1, -1), 10),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    elements.append(item_table)

    if has_discrepancy:
        elements.append(Spacer(1, 3 * mm))
        elements.append(Paragraph(
            "<font color='#b91c1c'><b>Note:</b></font> prices reflect actual invoiced amounts; "
            "negotiated price shown in parentheses where different.",
            styles["Normal"],
        ))

    # Delivery & Notes
    elements += _section("Delivery & Notes")
    elements.append(Paragraph(f"Delivery Location: {delivery_location}", styles["Normal"]))
    elements.append(Spacer(1, 3 * mm))
    if po.notes:
        elements.append(Paragraph("Notes:", styles["Normal"]))
        elements.append(_safe_paragraph(po.notes, styles["Normal"]))
    else:
        elements.append(Paragraph("Notes: —", styles["Normal"]))

    # Signature
    elements += _section("Authorisation")
    elements.append(Paragraph("Maintenance Supply Officer:", styles["Normal"]))
    elements.append(Spacer(1, 6 * mm))
    elements.append(signature_block())

    doc.build(elements)
    pdf_bytes = buf.getvalue()
    buf.close()
    response = HttpResponse(pdf_bytes, content_type="application/pdf")
    response["Content-Disposition"] = f'inline; filename="PO-{po.po_number}.pdf"'
    return response


@login_required
@role_required(User.Role.PROCUREMENT, User.Role.MANAGER, User.Role.SUPER_ADMIN)
def supplier_quick_create(request):
    """AJAX endpoint: quick-create a supplier and return JSON."""
    if request.method == "POST":
        form = SupplierQuickForm(request.POST)
        if form.is_valid():
            supplier = form.save()
            return JsonResponse({"id": supplier.pk, "name": supplier.name, "code": supplier.code})
        return JsonResponse({"error": form.errors.as_json()}, status=400)
    return JsonResponse({"error": _("POST required")}, status=405)


@login_required
@require_POST
@role_required(User.Role.PROCUREMENT, User.Role.MANAGER, User.Role.SUPER_ADMIN)
def purchase_order_close_short(request, pk):
    """Phase 4 (BUG-5 fix): close a PO as short (cancel remaining quantities).

    Improvements over the previous version:
      - Wrapped in transaction.atomic so a mid-loop failure rolls back
        the PO status change AND PR updates atomically.
      - Skips PR statuses that should not be touched: FULFILLED,
        CANCELLED, CONVERTED_TO_PO (the previous version only skipped
        FULFILLED, so a manager-cancelled PR was silently flipped to
        PARTIALLY_FULFILLED).
      - Zeroes backordered_qty on every PO line that wasn't cancelled,
        matching the invariant ordered_qty = received_qty + backordered_qty.
    """
    po = get_object_or_404(PurchaseOrder, pk=pk)
    if po.status not in (PurchaseOrder.Status.PARTIAL_RECEIVED, PurchaseOrder.Status.SENT):
        messages.error(request, _("PO cannot be closed short in current status."))
        return redirect("purchase_order_detail", pk=pk)
    if request.method != "POST":
        return redirect("purchase_order_detail", pk=pk)

    with transaction.atomic():
        po.status = PurchaseOrder.Status.CLOSED_SHORT
        po.save(update_fields=["status", "updated_at"])
        for pr in po.purchase_requests.all():
            # Phase 4 BUG-5: skip terminal/converted PRs. Don't touch
            # FULFILLED, CANCELLED, or CONVERTED_TO_PO — they belong to
            # other POs or already completed before this close.
            if pr.status in (
                PurchaseRequest.Status.FULFILLED,
                PurchaseRequest.Status.CANCELLED,
                PurchaseRequest.Status.CONVERTED_TO_PO,
            ):
                continue
            pr.status = PurchaseRequest.Status.PARTIALLY_FULFILLED
            pr.save(update_fields=["status"])
        # Phase 4: clear backordered_qty on items that weren't cancelled
        # (the line is closed — there's no future shipment expected).
        for item in po.items.all():
            item.backordered_qty = Decimal("0")
            item.save(update_fields=["backordered_qty"])
    messages.success(request, _("PO %(po)s closed short.") % {"po": po.po_number})
    return redirect("purchase_order_detail", pk=pk)



@login_required
@role_required(User.Role.PROCUREMENT, User.Role.MANAGER, User.Role.SUPER_ADMIN)
def purchase_order_edit(request, pk):
    """Edit PO-level fields + per-line item.negotiated_unit_price.

    Allowed only when not locked (status ∈ {DRAFT, SENT, PARTIAL_RECEIVED}).
    """
    from django.db import transaction

    po = get_object_or_404(
        PurchaseOrder.objects.prefetch_related("items", "items__part", "supplier"),
        pk=pk,
    )
    if po.is_locked:
        messages.error(request, _("This PO is locked and cannot be edited."))
        return redirect("purchase_order_detail", pk=pk)

    items_with_inventory = []
    sites = Site.objects.filter(is_active=True).order_by("name")
    selected_site = sites.filter(is_default=True).first()
    for item in po.items.all():
        inv = item.part.inventory_items.filter(site=selected_site).first() if selected_site else None
        items_with_inventory.append({
            "item": item,
            "inventory_qty": inv.quantity_available if inv else Decimal("0"),
        })

    if request.method == "POST":
        with transaction.atomic():
            form = PurchaseOrderForm(request.POST, instance=po)
            if form.is_valid():
                form.save()
                for item in po.items.all():
                    key = f"unit_price_{item.pk}"
                    val = request.POST.get(key)
                    if val not in (None, ""):
                        try:
                            new_price = Decimal(val)
                            if new_price != item.negotiated_unit_price:
                                item.negotiated_unit_price = new_price
                                item.save()
                        except Exception:
                            pass
                log_audit(
                    actor=request.user, action="po_edited",
                    entity="PurchaseOrder", object_id=str(po.pk),
                    payload={
                        "supplier": po.supplier.name if po.supplier_id else None,
                        "expected_delivery": str(po.expected_delivery),
                        "notes": (po.notes or "")[:200],
                    },
                )
                messages.success(request, _("PO updated."))
                return redirect("purchase_order_detail", pk=pk)
    else:
        form = PurchaseOrderForm(instance=po)

    return render(request, "procurement/po_edit.html", {
        "po": po,
        "form": form,
        "items_with_inventory": items_with_inventory,
    })


@login_required
@role_required(User.Role.PROCUREMENT, User.Role.MANAGER, User.Role.SUPER_ADMIN)
def purchase_order_cancel(request, pk):
    """Cancel a PO with a mandatory reason (min 15 chars).

    Allowed only when status ∈ {DRAFT, SENT}. Releases linked PRs back to PENDING.
    """
    from django.db import transaction

    po = get_object_or_404(PurchaseOrder, pk=pk)
    if po.status not in (PurchaseOrder.Status.DRAFT, PurchaseOrder.Status.SENT):
        messages.error(request, _("PO can only be cancelled from DRAFT or SENT status."))
        return redirect("purchase_order_detail", pk=pk)

    if request.method == "POST":
        reason = (request.POST.get("reason") or "").strip()
        if len(reason) < 15:
            messages.error(request, _("Cancellation reason must be at least 15 characters."))
            return render(request, "procurement/po_cancel.html", {"po": po, "min_chars": 15})
        with transaction.atomic():
            po.status = PurchaseOrder.Status.CANCELLED
            po.cancellation_reason = reason
            po.save(update_fields=["status", "cancellation_reason", "updated_at"])
            released = 0
            for pr in po.purchase_requests.all():
                if pr.status not in (PurchaseRequest.Status.FULFILLED,
                                     PurchaseRequest.Status.CANCELLED):
                    pr.status = PurchaseRequest.Status.PENDING
                    pr.save(update_fields=["status", "updated_at"])
                    released += 1
            log_audit(
                actor=request.user, action="po_cancelled",
                entity="PurchaseOrder", object_id=str(po.pk),
                payload={
                    "reason": reason[:200],
                    "released_prs": released,
                },
            )
        messages.success(request, _("PO %(po)s cancelled. %(n)d PR(s) released.") % {
            "po": po.po_number, "n": released,
        })
        return redirect("purchase_order_list")

    return render(request, "procurement/po_cancel.html", {"po": po, "min_chars": 15})


@login_required
@role_required(User.Role.PROCUREMENT, User.Role.MANAGER, User.Role.SUPER_ADMIN)
def purchase_order_reorder(request, pk):
    from procurement.services import PurchaseOrderService
    source_po = get_object_or_404(PurchaseOrder, pk=pk)
    if request.method != "POST":
        messages.error(request, _("Use the Reorder button to reorder this PO."))
        return redirect("purchase_order_detail", pk=pk)
    try:
        new_po = PurchaseOrderService.reorder(source_po=source_po, created_by=request.user)
    except ValueError as exc:
        messages.error(request, str(exc))
        return redirect("purchase_order_detail", pk=pk)
    messages.success(
        request,
        _("Reorder created. New PO %(po)s is in Draft — review line items and send.") % {"po": new_po.po_number},
    )
    return redirect("purchase_order_detail", pk=new_po.pk)


@login_required
@role_required(User.Role.MANAGER, User.Role.PROCUREMENT, User.Role.SUPER_ADMIN)
def supplier_analytics(request):
    """Phase 2: supplier performance dashboard.

    Metrics per supplier over the last N days (default 90):
    - PO count
    - Total spend (sum of received_qty * actual_unit_price)
    - On-time delivery rate (% of POs fully received within expected window)
    - Average cycle time (days from PO creation to full receipt)

    Sorted by total spend DESC.
    """
    from datetime import timedelta
    from django.db.models import Sum, F, Avg, Count, Q

    days_options = [7, 30, 90, 365]
    try:
        days = int(request.GET.get("days", 90))
    except (TypeError, ValueError):
        days = 90
    if days not in days_options:
        days = 90
    cutoff = timezone.now() - timedelta(days=days)

    suppliers_qs = (
        Supplier.objects
        .annotate(
            po_count=Count(
                "purchase_orders",
                filter=Q(purchase_orders__created_at__gte=cutoff),
            ),
            total_spend=Sum(
                F("purchase_orders__items__received_qty") * F("purchase_orders__items__actual_unit_price"),
                filter=Q(
                    purchase_orders__created_at__gte=cutoff,
                    purchase_orders__items__received_qty__gt=0,
                ),
            ),
            avg_cycle=Avg(
                F("purchase_orders__received_at") - F("purchase_orders__created_at"),
                filter=Q(
                    purchase_orders__created_at__gte=cutoff,
                    purchase_orders__received_at__isnull=False,
                ),
            ),
        )
        .filter(po_count__gt=0)
        .order_by("-total_spend")
    )

    rows = []
    for s in suppliers_qs:
        pos = s.purchase_orders.filter(
            created_at__gte=cutoff,
            received_at__isnull=False,
        )
        total_pos = pos.count()
        on_time = 0
        for po in pos:
            deadline = po.expected_delivery if po.expected_delivery else (po.created_at.date() + timedelta(days=14))
            if po.received_at and po.received_at.date() <= deadline:
                on_time += 1
        on_time_rate = (on_time / total_pos * 100) if total_pos > 0 else 0.0
        cycle_days = s.avg_cycle.total_seconds() / 86400 if s.avg_cycle else 0.0
        rows.append({
            "supplier": s,
            "po_count": s.po_count,
            "total_spend": s.total_spend or Decimal("0"),
            "on_time_rate": on_time_rate,
            "avg_cycle_days": cycle_days,
        })

    totals = {
        "suppliers": len(rows),
        "pos": sum(r["po_count"] for r in rows),
        "spend": sum(r["total_spend"] for r in rows),
        "on_time": sum(r["on_time_rate"] for r in rows) / len(rows) if rows else 0.0,
    }

    return render(request, "procurement/supplier_analytics.html", {
        "rows": rows,
        "days": days,
        "days_options": days_options,
        "totals": totals,
    })
