from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from decimal import Decimal

from django.utils import timezone
from django.db import models

from accounts.models import User
from accounts.permissions import role_required
from inventory.models import Inventory, StockMovement
from inventory.services import stock_in
from maintenance.models import Site

from .forms import PurchaseOfficerForm, PurchaseRequestForm, PurchaseOrderForm
from .models import PurchaseRequest, PurchaseOrder, PurchaseOrderItem


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
            messages.success(request, "Purchase request sent to procurement.")
            return redirect("purchase_list")
    else:
        initial = {}
        wo_id = request.GET.get("wo")
        if wo_id and wo_id.isdigit():
            initial["work_order"] = int(wo_id)
        form = PurchaseRequestForm(initial=initial)
    return render(request, "procurement/pr_form.html", {"form": form})


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
            messages.success(request, "Purchase request updated.")
            return redirect("purchase_list")
    else:
        form = PurchaseOfficerForm(instance=pr)
    return render(request, "procurement/pr_officer.html", {"pr": pr, "form": form})


@login_required
@role_required(User.Role.MANAGER, User.Role.PROCUREMENT, User.Role.SUPER_ADMIN)
def purchase_receive(request, pk):
    """When status set to RECEIVED, perform stock-in for ordered quantity."""
    pr = get_object_or_404(PurchaseRequest, pk=pk)
    if pr.status != PurchaseRequest.Status.ORDERED:
        messages.error(request, "Request must be ORDERED before receiving.")
        return redirect("purchase_list")
    if request.method != "POST":
        return redirect("purchase_officer", pk=pk)
    supplier = pr.supplier.name if pr.supplier else "Unknown"
    unit = pr.unit_price or Decimal("0")
    stock_in(
        part=pr.part,
        quantity=pr.quantity,
        performed_by=request.user,
        supplier_name=supplier,
        unit_cost=unit,
        invoice_ref=f"PR-{pr.pk}",
        note="From procurement receive",
    )
    pr.status = PurchaseRequest.Status.RECEIVED
    pr.save(update_fields=["status", "updated_at"])
    messages.success(request, "Stock updated from purchase request.")
    return redirect("purchase_list")


@login_required
@role_required(User.Role.MANAGER, User.Role.PROCUREMENT, User.Role.SUPER_ADMIN)
def purchase_request_detail(request, pk):
    pr = get_object_or_404(PurchaseRequest, pk=pk)
    return render(request, "procurement/pr_detail.html", {"pr": pr})


@login_required
@role_required(User.Role.PROCUREMENT, User.Role.MANAGER, User.Role.SUPER_ADMIN)
def purchase_order_list(request):
    """List all purchase orders."""
    rows = PurchaseOrder.objects.select_related("supplier", "created_by", "handled_by").order_by("-created_at")[:200]
    return render(request, "procurement/po_list.html", {"pos": rows})


@login_required
@role_required(User.Role.PROCUREMENT, User.Role.MANAGER, User.Role.SUPER_ADMIN)
def purchase_order_create(request):
    """Create a new purchase order."""
    if request.method == "POST":
        form = PurchaseOrderForm(request.POST)
        if form.is_valid():
            po = form.save(commit=False)
            po.created_by = request.user
            po.save()
            messages.success(request, f"Purchase order {po.po_number} created.")
            return redirect("purchase_order_detail", pk=po.pk)
    else:
        initial = {}
        pr_id = request.GET.get("pr")
        if pr_id:
            initial["purchase_request"] = pr_id
        form = PurchaseOrderForm(initial=initial)
    return render(request, "procurement/po_form.html", {"form": form, "po": None, "page_heading": "New purchase order"})


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
                unit_price=pr.unit_price or pr.part.last_purchase_cost or Decimal("0"),
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
    """Receive items against a PO — separate endpoint per spec."""
    po = get_object_or_404(
        PurchaseOrder.objects.prefetch_related("items", "items__part"),
        pk=pk
    )
    if po.status == PurchaseOrder.Status.RECEIVED:
        messages.warning(request, "PO is already fully received.")
        return redirect("purchase_order_detail", pk=pk)
    if po.status == PurchaseOrder.Status.CLOSED_SHORT:
        messages.warning(request, "PO is closed short.")
        return redirect("purchase_order_detail", pk=pk)
    if po.status == PurchaseOrder.Status.CANCELLED:
        messages.warning(request, "PO is cancelled.")
        return redirect("purchase_order_detail", pk=pk)

    if request.method == "POST":
        received_data = []
        for item in po.items.all():
            received_key = f"received_qty_{item.pk}"
            received_qty = request.POST.get(received_key)
            if received_qty and Decimal(str(received_qty)) > 0:
                qty = Decimal(str(received_qty))
                remaining = item.ordered_qty - item.received_qty
                if qty > remaining:
                    qty = remaining

                recent = StockMovement.objects.filter(
                    part=item.part,
                    invoice_ref=f"PO-{po.po_number}",
                    quantity=qty,
                    created_at__gte=timezone.now() - timezone.timedelta(seconds=10),
                ).exists()
                if recent:
                    messages.warning(request, f"Duplicate receipt detected for {item.part.sku}.")
                    continue

                site = Site.objects.filter(is_default=True).first()
                stock_in(
                    part=item.part,
                    quantity=qty,
                    performed_by=request.user,
                    supplier_name=po.supplier.name if po.supplier else "",
                    unit_cost=item.unit_price,
                    invoice_ref=f"PO-{po.po_number}",
                    note=f"Received against PO {po.po_number}",
                    site=site,
                )

                item.received_qty += qty
                item.save(update_fields=["received_qty"])

                received_data.append({"part": item.part.name, "qty": qty})

        if po.items.filter(received_qty__lt=models.F("ordered_qty")).exists():
            po.status = PurchaseOrder.Status.PARTIAL_RECEIVED
        else:
            po.status = PurchaseOrder.Status.RECEIVED
        po.save(update_fields=["status", "updated_at"])

        for pr in po.purchase_requests.all():
            if po.status == PurchaseOrder.Status.RECEIVED:
                pr.status = PurchaseRequest.Status.FULFILLED
            else:
                pr.status = PurchaseRequest.Status.PARTIALLY_FULFILLED
            pr.save(update_fields=["status"])

        if received_data:
            items_str = ", ".join([f"{d['part']} x{d['qty']}" for d in received_data])
            messages.success(request, f"Received: {items_str}")
        return redirect("purchase_order_detail", pk=pk)

    return render(request, "procurement/po_receive.html", {
        "po": po,
    })


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
