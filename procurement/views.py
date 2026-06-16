import logging
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import models, transaction
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from accounts.models import User
from accounts.permissions import role_required
from inventory.models import Inventory, StockMovement
from inventory.services import stock_in
from inventory.services_allocation import PartAllocationService
from maintenance.models import Machine, Site
from maintenance.services_notifications import notify_po_received_summary

from .forms import PurchaseOfficerForm, PurchaseRequestForm, PurchaseOrderForm, SupplierQuickForm
from .models import PurchaseRequest, PurchaseOrder, PurchaseOrderItem, Supplier

logger = logging.getLogger(__name__)


@login_required
@role_required(User.Role.MANAGER, User.Role.SUPER_ADMIN)
def purchase_request_create(request):
    if request.method == "POST":
        form = PurchaseRequestForm(request.POST)
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

            messages.success(request, "Purchase request sent to procurement.")
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
            messages.success(request, "Purchase request updated.")
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
        messages.error(request, "Please record a voice note before submitting.")
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
        messages.error(request, "Voice attachment not found or not owned by you.")
        return redirect("pr_detail", pk=pk)
    messages.success(request, f"Voice note added to PR #{pr.pk}.")
    return redirect("pr_detail", pk=pk)


@login_required
@role_required(User.Role.PROCUREMENT, User.Role.MANAGER, User.Role.SUPER_ADMIN)
def purchase_order_list(request):
    """List all purchase orders with optional status filter."""
    qs = PurchaseOrder.objects.select_related(
        "supplier", "created_by", "handled_by"
    )
    status_filter = request.GET.get("status", "").strip()
    if status_filter and status_filter in dict(PurchaseOrder.Status.choices):
        qs = qs.filter(status=status_filter)
    rows = qs.order_by("-created_at")[:200]
    return render(
        request,
        "procurement/po_list.html",
        {
            "pos": rows,
            "status_filter": status_filter,
            "status_choices": PurchaseOrder.Status.choices,
        },
    )


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

            messages.success(request, f"Purchase order {po.po_number} created.")
            return redirect("purchase_order_detail", pk=po.pk)
    else:
        initial = {}
        if pr_id:
            initial["purchase_request"] = pr_id
        form = PurchaseOrderForm(initial=initial)
        formset = POItemFormSet(prefix="items", queryset=PurchaseOrderItem.objects.none())

    # Available PENDING PRs for selection
    pending_prs = PurchaseRequest.objects.filter(
        status=PurchaseRequest.Status.PENDING
    ).select_related("part", "created_by").order_by("-created_at")[:50]

    return render(request, "procurement/po_form.html", {
        "form": form,
        "formset": formset,
        "po": None,
        "page_heading": "New purchase order",
        "pending_prs": pending_prs,
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
            messages.success(request, f"PO {po.po_number} created from PR #{pr.pk}.")
            return redirect("purchase_order_detail", pk=po.pk)
    else:
        form = PurchaseOrderForm()
    return render(request, "procurement/po_form.html", {
        "form": form,
        "po": None,
        "page_heading": f"New PO from PR #{pr.pk}",
        "pr": pr,
        "pending_prs": [],
    })


@login_required
@role_required(User.Role.PROCUREMENT, User.Role.MANAGER, User.Role.SUPER_ADMIN)
def purchase_order_detail(request, pk):
    """View and edit a purchase order."""
    po = get_object_or_404(
        PurchaseOrder.objects.prefetch_related("items", "items__part", "supplier", "created_by", "handled_by"),
        pk=pk
    )
    sites = Site.objects.filter(is_active=True).order_by("name")
    selected_site = sites.filter(is_default=True).first()

    items_with_inventory = []
    for item in po.items.all():
        inv = item.part.inventory_items.filter(site=selected_site).first() if selected_site else None
        items_with_inventory.append({
            "item": item,
            "inventory_qty": inv.quantity_available if inv else Decimal("0"),
        })

    if request.method == "POST":
        if po.is_locked:
            messages.error(request, "This PO is locked and cannot be edited.")
            return redirect("purchase_order_detail", pk=pk)
        form = PurchaseOrderForm(request.POST, instance=po)
        if form.is_valid():
            form.save()
            messages.success(request, "Purchase order updated.")
            return redirect("purchase_order_detail", pk=pk)
    else:
        form = PurchaseOrderForm(instance=po)
    return render(request, "procurement/po_detail.html", {
        "po": po,
        "form": form,
        "items_with_inventory": items_with_inventory,
        "sites": sites,
        "selected_site": selected_site,
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
      - rejected_qty_<pk>: units rejected at inspection (no stock change)
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
        messages.warning(request, f"PO is {po.get_status_display().lower()} — cannot receive.")
        return redirect("purchase_order_detail", pk=pk)

    if request.method != "POST":
        return render(request, "procurement/po_receive.html", {"po": po})

    # Pre-flight: validate totals
    site = Site.objects.filter(is_default=True).first()
    if not site:
        messages.error(request, "No default site configured.")
        return redirect("purchase_order_detail", pk=pk)

    supplier_invoice_ref = (request.POST.get("supplier_invoice_ref") or "").strip()
    line_changes: list[dict] = []  # for the summary notification

    with transaction.atomic():
        for item in po.items.all():
            good = _to_decimal(request.POST.get(f"good_qty_{item.pk}")) or Decimal("0")
            damaged = _to_decimal(request.POST.get(f"damaged_qty_{item.pk}")) or Decimal("0")
            rejected = _to_decimal(request.POST.get(f"rejected_qty_{item.pk}")) or Decimal("0")
            actual_price = _to_decimal(request.POST.get(f"actual_unit_price_{item.pk}"))
            total_received = good + damaged + rejected

            if total_received <= 0 and not actual_price:
                continue  # nothing to do for this line

            remaining = item.ordered_qty - item.received_qty
            if total_received > remaining and remaining > 0:
                messages.warning(
                    request,
                    f"{item.part.sku}: receiving {total_received} exceeds remaining {remaining} — capped.",
                )
                # proportionally scale (or just cap the good portion)
                ratio = remaining / total_received
                good = (good * ratio).quantize(Decimal("0.001"))
                damaged = (damaged * ratio).quantize(Decimal("0.001"))
                rejected = (rejected * ratio).quantize(Decimal("0.001"))
                total_received = good + damaged + rejected

            # Update item fields
            item.received_qty += total_received
            item.damaged_qty += damaged
            item.rejected_qty += rejected
            if actual_price and actual_price > 0:
                item.actual_unit_price = actual_price
            update_fields = ["received_qty", "damaged_qty", "rejected_qty"]
            if actual_price and actual_price > 0:
                update_fields.append("actual_unit_price")
            item.save(update_fields=update_fields)

            # Stock in good units (to available)
            if good > 0:
                stock_in(
                    part=item.part,
                    quantity=good,
                    performed_by=request.user,
                    supplier_name=po.supplier.name if po.supplier else "",
                    unit_cost=actual_price or item.negotiated_unit_price,
                    invoice_ref=supplier_invoice_ref or f"{po.po_number}",
                    note=f"Received against PO {po.po_number} (good)",
                    site=site,
                )

            # Damaged → quarantine
            if damaged > 0:
                inv, _ = Inventory.objects.select_for_update().get_or_create(
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

            if total_received > 0 or actual_price:
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
        else:
            po.status = PurchaseOrder.Status.PARTIAL_RECEIVED
        po.save(update_fields=["status", "updated_at"])

        # Update PR statuses
        for pr in po.purchase_requests.all():
            pr.status = (
                PurchaseRequest.Status.FULFILLED
                if po.status == PurchaseOrder.Status.RECEIVED
                else PurchaseRequest.Status.PARTIALLY_FULFILLED
            )
            pr.save(update_fields=["status"])

    # Outside atomic: fire summary notification
    if line_changes:
        notify_po_received_summary(po, request.user)
        items_str = ", ".join(
            f"{c['sku']} ({c['good']}g/{c['damaged']}d/{c['rejected']}r)"
            for c in line_changes
        )
        messages.success(request, f"Received: {items_str}")
        if supplier_invoice_ref:
            messages.info(request, f"Supplier invoice ref: {supplier_invoice_ref}")

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

    from maintenance.pdf_utils import build_pdf_response, _header_table, _section, _field_table, signature_block
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
    elements.append(Paragraph(f"<b>PURCHASE ORDER</b>", styles["Normal"]))

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
    item_data = [["SKU", "Part Name", "Qty", "Unit Cost", "Total"]]
    grand_total = Decimal("0")
    for item in po.items.all():
        total = item.ordered_qty * item.negotiated_unit_price
        grand_total += total
        item_data.append([
            item.part.sku,
            item.part.name,
            f"{item.ordered_qty:.3f}",
            f"{item.negotiated_unit_price:.4f}",
            f"{total:.4f}",
        ])
    item_data.append(["", "", "", "Grand Total", f"{grand_total:.4f}"])

    col_widths = [60, 200, 50, 70, 70]
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

    # Delivery & Notes
    elements += _section("Delivery & Notes")
    elements.append(Paragraph(f"Delivery Location: Main Factory — Stores", styles["Normal"]))
    elements.append(Spacer(1, 3 * mm))
    elements.append(Paragraph(f"Notes: {po.notes or '—'}", styles["Normal"]))

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
    return JsonResponse({"error": "POST required"}, status=405)


@login_required
@role_required(User.Role.PROCUREMENT, User.Role.MANAGER, User.Role.SUPER_ADMIN)
def purchase_order_close_short(request, pk):
    """Close a PO as short (cancel remaining quantities)."""
    po = get_object_or_404(PurchaseOrder, pk=pk)
    if po.status not in (PurchaseOrder.Status.PARTIAL_RECEIVED, PurchaseOrder.Status.SENT):
        messages.error(request, "PO cannot be closed short in current status.")
        return redirect("purchase_order_detail", pk=pk)
    if request.method == "POST":
        po.status = PurchaseOrder.Status.CLOSED_SHORT
        po.save(update_fields=["status", "updated_at"])
        for pr in po.purchase_requests.all():
            if pr.status not in (PurchaseRequest.Status.FULFILLED,):
                pr.status = PurchaseRequest.Status.PARTIALLY_FULFILLED
                pr.save(update_fields=["status"])
        messages.success(request, f"PO {po.po_number} closed short.")
        return redirect("purchase_order_detail", pk=pk)
    return render(request, "procurement/po_close_short.html", {"po": po})
