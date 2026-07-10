"""Views for the inventory app.

Currently hosts the stock-in (goods receipt) workflow, which conceptually
belongs to inventory rather than maintenance. Migrated from maintenance.views
as part of the supplier-intelligence refactor.
"""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.translation import gettext_lazy as _

from accounts.models import User
from accounts.permissions import role_required
from inventory.forms import StockInForm
from inventory.models import SparePart
from inventory.services import stock_in
from maintenance.models import Site


def _new_supplier_url(next_url: str) -> str:
    """Build the supplier-create URL with a `next` param so the user lands
    back on the stock-in form after creating a supplier.
    """
    from django.utils.http import urlencode
    return f"{reverse('supplier_create')}?{urlencode({'next': next_url})}"


@login_required
@role_required(User.Role.MANAGER, User.Role.PROCUREMENT, User.Role.SUPER_ADMIN)
def stock_in_view(request):
    """Generic stock-in form: /stock/in/"""
    next_url = request.path
    if request.method == "POST":
        form = StockInForm(request.POST)
        if form.is_valid():
            stock_in(
                part=form.cleaned_data["part"],
                supplier=form.cleaned_data["supplier"],
                quantity=form.cleaned_data["quantity"],
                performed_by=request.user,
                unit_cost=form.cleaned_data["unit_cost"],
                invoice_ref=form.cleaned_data["invoice_ref"],
                note=form.cleaned_data.get("note") or "",
            )
            messages.success(request, _("Stock-in recorded."))
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

    return render(request, "inventory/stock_in.html", {
        "form": form,
        "part": None,
        "new_supplier_url": _new_supplier_url(next_url),
    })


@login_required
@role_required(User.Role.MANAGER, User.Role.PROCUREMENT, User.Role.SUPER_ADMIN)
def part_stock_in(request, pk):
    """Stock-in form pre-bound to a specific part: /stock/<pk>/stock-in/"""
    next_url = request.path
    part = get_object_or_404(SparePart, pk=pk)

    sites = Site.objects.filter(is_active=True).order_by("name")
    selected_site = sites.filter(is_default=True).first()
    selected_site_id = request.GET.get("site")
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
                supplier=form.cleaned_data["supplier"],
                quantity=form.cleaned_data["quantity"],
                performed_by=request.user,
                unit_cost=form.cleaned_data["unit_cost"],
                invoice_ref=form.cleaned_data["invoice_ref"],
                note=form.cleaned_data.get("note") or "",
                site=selected_site,
            )
            messages.success(
                request,
                _("Stock-in recorded for %(part)s.") % {"part": part.name},
            )
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

    return render(request, "inventory/stock_in.html", {
        "form": form,
        "part": part,
        "page_heading": _("Stock-in — %(part)s") % {"part": part.name},
        "new_supplier_url": _new_supplier_url(next_url),
    })