from decimal import Decimal

from django import forms

from maintenance.models import WorkOrder

from .models import PurchaseOrder, PurchaseOrderItem, PurchaseRequest, Supplier


_CTRL = {"class": "form-control"}
_SEL = {"class": "form-select"}


class PurchaseRequestForm(forms.ModelForm):
    class Meta:
        model = PurchaseRequest
        fields = ("part", "work_order", "quantity", "urgency", "is_emergency", "notes")
        widgets = {
            "part": forms.Select(attrs=_SEL),
            "work_order": forms.Select(attrs=_SEL),
            "quantity": forms.NumberInput(attrs=_CTRL),
            "urgency": forms.TextInput(attrs=_CTRL),
            "is_emergency": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "notes": forms.Textarea(attrs={**_CTRL, "rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["quantity"].min_value = Decimal("0.001")
        self.fields["work_order"].required = False
        self.fields["work_order"].queryset = (
            WorkOrder.objects.exclude(status=WorkOrder.Status.CLOSED).select_related("machine").order_by("-number")[:500]
        )


class PurchaseOfficerForm(forms.ModelForm):
    class Meta:
        model = PurchaseRequest
        fields = ("supplier", "unit_price", "status", "notes")
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
        fields = ["part", "ordered_qty", "unit_price"]

    def clean(self):
        cleaned = super().clean()
        qty = cleaned.get("ordered_qty")
        price = cleaned.get("unit_price")
        if qty and price:
            cleaned["total_price"] = qty * price
        return cleaned


POItemFormSet = forms.inlineformset_factory(
    PurchaseOrder,
    PurchaseOrderItem,
    fields=["part", "ordered_qty", "unit_price"],
    extra=1,
    can_delete=True,
)


class SupplierForm(forms.ModelForm):
    """Operational supplier form — used in stock module (not Django admin)."""

    class Meta:
        model = Supplier
        fields = (
            "code",
            "name",
            "contact_person",
            "phone",
            "email",
            "address",
            "is_repair_vendor",
            "is_active",
            "notes",
        )
        widgets = {
            "code": forms.TextInput(attrs={**_CTRL, "placeholder": "SUP-001"}),
            "name": forms.TextInput(attrs={**_CTRL, "placeholder": "ACME Parts Ltd"}),
            "contact_person": forms.TextInput(attrs={**_CTRL}),
            "phone": forms.TextInput(attrs={**_CTRL, "placeholder": "+966 11 555 0100"}),
            "email": forms.EmailInput(attrs={**_CTRL, "placeholder": "contact@acme.com"}),
            "address": forms.Textarea(attrs={**_CTRL, "rows": 3}),
            "is_repair_vendor": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "is_active": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "notes": forms.Textarea(attrs={**_CTRL, "rows": 2}),
        }

    def clean_code(self):
        code = (self.cleaned_data.get("code") or "").strip().upper()
        if not code:
            raise forms.ValidationError("Supplier code is required.")
        if Supplier.objects.filter(code=code).exclude(pk=self.instance.pk if self.instance.pk else None).exists():
            raise forms.ValidationError(f"Supplier code '{code}' is already in use.")
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
