from decimal import Decimal

from django import forms
from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

from maintenance.models import Machine, WorkOrder

from .models import PurchaseOrder, PurchaseOrderItem, PurchaseRequest, Supplier


_CTRL = {"class": "form-control"}
_SEL = {"class": "form-select"}


class PurchaseRequestForm(forms.ModelForm):
    machine = forms.ModelChoiceField(
        queryset=Machine.objects.filter(is_active=True, asset_level=3),
        # Bug fix (Phase 7.7): machine is now OPTIONAL. The model allows null,
        # and the only reason this used to be required was to force users to
        # attribute the PR to a specific machine. For pure stock
        # replenishment (no machine, no WO), users should be able to create
        # a PR by leaving both fields blank. The asset tree is now opt-in,
        # not mandatory.
        required=False,
        widget=forms.Select(attrs=_SEL),
        label=_("Machine"),
    )
    component = forms.ModelChoiceField(
        queryset=Machine.objects.filter(is_active=True, asset_level=5),
        required=False,
        widget=forms.Select(attrs=_SEL),
        label=_("Component"),
    )

    class Meta:
        model = PurchaseRequest
        fields = ("part", "machine", "component", "work_order", "quantity", "notes")
        labels = {
            "part": _("Part"),
            "work_order": _("Work order"),
            "quantity": _("Quantity"),
            "notes": _("Notes"),
        }
        widgets = {
            "part": forms.Select(attrs=_SEL),
            "work_order": forms.Select(attrs=_SEL),
            "quantity": forms.NumberInput(attrs=_CTRL),
            "notes": forms.Textarea(attrs={**_CTRL, "rows": 3}),
        }

    def __init__(self, *args, lock_asset=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["quantity"].min_value = Decimal("0.001")
        self.fields["work_order"].required = False
        # Bug fix: do NOT slice the queryset with [:N]. Slicing caches the
        # result and breaks ModelChoiceField.clean(), which calls
        # `queryset.get(pk=value)` and raises TypeError. The previous code
        # silently turned this into a "Select a valid choice" error, blocking
        # the manager from saving any PR with a work_order selected. We still
        # cap the rendered options via the widget's `choices` so the dropdown
        # stays bounded, while the field's queryset stays un-sliced so
        # `get(pk=value)` works for selected values.
        work_order_qs = (
            WorkOrder.objects.exclude(lifecycle_status=WorkOrder.LifecycleStatus.CLOSED)
            .select_related("machine")
            .order_by("-number")
        )
        self.fields["work_order"].queryset = work_order_qs
        # Bind the rendered dropdown to the first 500 WOs so very large
        # factories don't render thousands of <option> tags. The form
        # validation still accepts any WO in the un-sliced queryset.
        self.fields["work_order"].widget.choices = [
            (wo.pk, str(wo)) for wo in work_order_qs[:500]
        ]
        if lock_asset:
            self.fields["machine"].disabled = True
            self.fields["component"].disabled = True

    def clean(self):
        cleaned = super().clean()
        machine = cleaned.get("machine")
        component = cleaned.get("component")
        if machine and component:
            from maintenance.validators import validate_component_belongs_to_machine
            try:
                validate_component_belongs_to_machine(component, machine)
            except ValidationError as e:
                for field, errors in e.message_dict.items():
                    for error in errors:
                        self.add_error(field, error)
        return cleaned


class PurchaseOfficerForm(forms.ModelForm):
    class Meta:
        model = PurchaseRequest
        fields = ("supplier", "unit_price", "status", "notes")
        labels = {
            "supplier": _("Supplier"),
            "unit_price": _("Unit price"),
            "status": _("Status"),
            "notes": _("Notes"),
        }
        widgets = {
            "supplier": forms.Select(attrs=_SEL),
            "unit_price": forms.NumberInput(attrs=_CTRL),
            "status": forms.Select(attrs=_SEL),
            "notes": forms.Textarea(attrs={**_CTRL, "rows": 3}),
        }


class PurchaseOrderForm(forms.ModelForm):
    """Form for creating and editing a purchase order."""

    class Meta:
        model = PurchaseOrder
        fields = ["supplier", "invoice_ref", "expected_delivery", "status", "notes", "handled_by"]
        labels = {
            "supplier": _("Supplier"),
            "invoice_ref": _("Internal ref"),
            "expected_delivery": _("Expected delivery"),
            "status": _("Status"),
            "notes": _("Notes"),
            "handled_by": _("Handled by"),
        }
        widgets = {
            "expected_delivery": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
            "notes": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["handled_by"].required = False
        self.fields["invoice_ref"].required = False


class PurchaseOrderItemForm(forms.ModelForm):
    """Form for a PO line item."""

    class Meta:
        model = PurchaseOrderItem
        fields = ["part", "ordered_qty", "negotiated_unit_price"]
        labels = {
            "part": _("Part"),
            "ordered_qty": _("Ordered qty"),
            "negotiated_unit_price": _("Unit price"),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["negotiated_unit_price"].label = _("Unit price")

    def clean(self):
        cleaned = super().clean()
        qty = cleaned.get("ordered_qty")
        price = cleaned.get("negotiated_unit_price")
        if qty and price:
            cleaned["total_price"] = qty * price
        return cleaned


POItemFormSet = forms.inlineformset_factory(
    PurchaseOrder,
    PurchaseOrderItem,
    fields=["part", "ordered_qty", "negotiated_unit_price"],
    extra=1,
    can_delete=True,
)


class SupplierForm(forms.ModelForm):
    """Operational supplier form — used in stock module (not Django admin).

    supplier_type is rendered as radio buttons (Parts supplier / Repair vendor)
    via the supplier_form.html template — see the {% for radio in
    form.supplier_type %} loop. The deprecated is_repair_vendor boolean is
    kept in Meta for back-compat with admin import-export but is not exposed
    in this form; it is auto-synced from supplier_type on save.
    """

    class Meta:
        model = Supplier
        fields = (
            "code",
            "name",
            "contact_person",
            "phone",
            "email",
            "address",
            "supplier_type",
            "is_active",
            "notes",
        )
        labels = {
            "code": _("Code"),
            "name": _("Name"),
            "contact_person": _("Contact person"),
            "phone": _("Phone"),
            "email": _("Email"),
            "address": _("Address"),
            "supplier_type": _("Supplier type"),
            "is_active": _("Active"),
            "notes": _("Notes"),
        }
        widgets = {
            "code": forms.TextInput(attrs={**_CTRL, "placeholder": "SUP-001"}),
            "name": forms.TextInput(attrs={**_CTRL, "placeholder": _("ACME Parts Ltd")}),
            "contact_person": forms.TextInput(attrs={**_CTRL}),
            "phone": forms.TextInput(attrs={**_CTRL, "placeholder": "+966 11 555 0100"}),
            "email": forms.EmailInput(attrs={**_CTRL, "placeholder": "contact@acme.com"}),
            "address": forms.Textarea(attrs={**_CTRL, "rows": 3}),
            "supplier_type": forms.RadioSelect(),
            "is_active": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "notes": forms.Textarea(attrs={**_CTRL, "rows": 2}),
        }

    def clean_code(self):
        code = (self.cleaned_data.get("code") or "").strip().upper()
        if not code:
            raise forms.ValidationError(_("Supplier code is required."))
        if Supplier.objects.filter(code=code).exclude(pk=self.instance.pk if self.instance.pk else None).exists():
            raise forms.ValidationError(_("Supplier code '%(code)s' is already in use.") % {"code": code})
        return code


class SupplierQuickForm(forms.ModelForm):
    """Quick supplier create — name + code only (used for rapid entry)."""

    class Meta:
        model = Supplier
        fields = ("code", "name")
        widgets = {
            "code": forms.TextInput(attrs={**_CTRL, "placeholder": "SUP-001"}),
            "name": forms.TextInput(attrs={**_CTRL, "placeholder": "Supplier name"}),
        }
