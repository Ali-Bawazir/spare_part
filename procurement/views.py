from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from decimal import Decimal

from accounts.models import User
from accounts.permissions import role_required
from inventory.services import stock_in

from .forms import PurchaseOfficerForm, PurchaseRequestForm
from .models import PurchaseRequest


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
    rows = PurchaseRequest.objects.select_related("part", "created_by", "supplier", "work_order").order_by("-created_at")[:300]
    return render(request, "procurement/pr_list.html", {"requests": rows})


@login_required
@role_required(User.Role.PROCUREMENT, User.Role.SUPER_ADMIN)
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
