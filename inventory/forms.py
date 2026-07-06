from decimal import Decimal

from django import forms
from django.utils.translation import gettext_lazy as _

from .models import SparePart

_CTRL = {"class": "form-control"}
_SEL = {"class": "form-select"}


class StockInForm(forms.Form):
    part = forms.ModelChoiceField(queryset=SparePart.objects.all(), widget=forms.Select(attrs=_SEL))
    quantity = forms.DecimalField(min_value=Decimal("0.001"), max_digits=14, decimal_places=3, widget=forms.NumberInput(attrs=_CTRL))
    supplier_name = forms.CharField(max_length=255, widget=forms.TextInput(attrs=_CTRL))
    unit_cost = forms.DecimalField(min_value=Decimal("0"), max_digits=12, decimal_places=4, widget=forms.NumberInput(attrs=_CTRL))
    invoice_ref = forms.CharField(max_length=120, widget=forms.TextInput(attrs=_CTRL))
    note = forms.CharField(required=False, widget=forms.Textarea(attrs={**_CTRL, "rows": 2}))


class IssuePartForm(forms.Form):
    part = forms.ModelChoiceField(queryset=SparePart.objects.all(), widget=forms.Select(attrs=_SEL))
    quantity = forms.DecimalField(min_value=Decimal("0.001"), max_digits=14, decimal_places=3, widget=forms.NumberInput(attrs=_CTRL))
    unit_cost = forms.DecimalField(min_value=Decimal("0"), max_digits=12, decimal_places=4, widget=forms.NumberInput(attrs=_CTRL))
    invoice_ref = forms.CharField(max_length=120, widget=forms.TextInput(attrs=_CTRL))
    supplier_name = forms.CharField(max_length=255, required=False, widget=forms.TextInput(attrs=_CTRL))


class PartRequestForm(forms.Form):
    """Phase 2.1: technician-initiated part request.

    Only the part and quantity are required; cost/supplier/invoice
    are filled by the manager at approval time.
    """
    part = forms.ModelChoiceField(
        queryset=SparePart.objects.filter(status="active"),
        widget=forms.Select(attrs=_SEL),
    )
    quantity = forms.DecimalField(
        min_value=Decimal("0.001"),
        max_digits=14,
        decimal_places=3,
        widget=forms.NumberInput(attrs={**_CTRL, "step": "0.001"}),
    )
    note = forms.CharField(
        required=False,
        max_length=500,
        widget=forms.Textarea(attrs={**_CTRL, "rows": 2, "placeholder": _("Optional note for the manager")}),
    )


class PartRequestDecisionForm(forms.Form):
    """Phase 2.1: manager decision form for approve / edit / reject.

    On reject, a reason is required. On edit, a new_qty is required.
    """
    action = forms.ChoiceField(
        choices=[("approve", _("Approve")), ("reject", _("Reject")), ("edit", _("Edit Qty"))],
        widget=forms.Select(attrs=_SEL),
    )
    new_qty = forms.DecimalField(
        required=False,
        min_value=Decimal("0.001"),
        max_digits=14,
        decimal_places=3,
        widget=forms.NumberInput(attrs={**_CTRL, "step": "0.001"}),
    )
    rejection_reason = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={**_CTRL, "rows": 2, "placeholder": _("Required when rejecting")}),
    )

    def clean(self):
        cleaned = super().clean()
        action = cleaned.get("action")
        if action == "reject" and not (cleaned.get("rejection_reason") or "").strip():
            raise forms.ValidationError(
                {"rejection_reason": _("Rejection reason is required.")}
            )
        if action == "edit":
            new_qty = cleaned.get("new_qty")
            if not new_qty or new_qty <= 0:
                raise forms.ValidationError(
                    {"new_qty": _("New qty is required when editing.")}
                )
        return cleaned


class ConsumableUseForm(forms.Form):
    part = forms.ModelChoiceField(
        queryset=SparePart.objects.filter(
            is_consumable=True,
            allow_operator_consumption=True,
            status="active",
        ),
        widget=forms.Select(attrs=_SEL),
    )
    quantity = forms.DecimalField(min_value=Decimal("0.001"), max_digits=14, decimal_places=3, widget=forms.NumberInput(attrs=_CTRL))
    machine_id = forms.IntegerField(required=False, min_value=1, widget=forms.NumberInput(attrs=_CTRL))


class IssueConsumableForm(forms.Form):
    """Manager/supervisor issues consumable to an operator (source = SUPERVISOR_ISSUE)."""
    consumed_by = forms.ModelChoiceField(
        queryset=None,  # Set in __init__
        widget=forms.Select(attrs=_SEL),
        label=_("Operator"),
    )
    part = forms.ModelChoiceField(
        queryset=SparePart.objects.filter(
            is_consumable=True,
            status="active",
        ),
        widget=forms.Select(attrs=_SEL),
    )
    quantity = forms.DecimalField(min_value=Decimal("0.001"), max_digits=14, decimal_places=3, widget=forms.NumberInput(attrs=_CTRL))
    machine_id = forms.IntegerField(required=False, min_value=1, widget=forms.NumberInput(attrs=_CTRL))
    note = forms.CharField(required=False, max_length=500, widget=forms.Textarea(attrs={**_CTRL, "rows": 2, "placeholder": _("Optional note")}))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from accounts.models import User
        self.fields["consumed_by"].queryset = User.objects.filter(
            is_active=True,
            role__in=[User.Role.OPERATOR, User.Role.TECHNICIAN],
        ).order_by("username")


class SparePartForm(forms.ModelForm):
    """Operational spare part form — used in stock module (not Django admin)."""

    class Meta:
        model = SparePart
        fields = (
            "sku",
            "name",
            "description",
            "category",
            "unit",
            "supplier",
            "is_consumable",
            "is_repairable",
            "allow_operator_consumption",
            "min_stock_level",
            "max_stock_level",
            "avg_cost",
            "last_purchase_cost",
            "status",
        )
        widgets = {
            "sku": forms.TextInput(attrs={**_CTRL, "placeholder": _("BRG-6006")}),
            "name": forms.TextInput(attrs={**_CTRL, "placeholder": _("Ball bearing 6006")}),
            "description": forms.Textarea(attrs={**_CTRL, "rows": 2}),
            "category": forms.TextInput(attrs={**_CTRL, "placeholder": _("Bearings")}),
            "unit": forms.TextInput(attrs={**_CTRL, "placeholder": _("pcs")}),
            "supplier": forms.Select(attrs={**_SEL}),
            "is_consumable": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "is_repairable": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "allow_operator_consumption": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "min_stock_level": forms.NumberInput(attrs={**_CTRL, "min": "0", "step": "0.001"}),
            "max_stock_level": forms.NumberInput(attrs={**_CTRL, "min": "0", "step": "0.001"}),
            "avg_cost": forms.NumberInput(attrs={**_CTRL, "min": "0", "step": "0.0001"}),
            "last_purchase_cost": forms.NumberInput(attrs={**_CTRL, "min": "0", "step": "0.0001"}),
            "status": forms.Select(attrs={**_SEL}),
        }

    def clean_sku(self):
        sku = (self.cleaned_data.get("sku") or "").strip().upper()
        if not sku:
            raise forms.ValidationError(_("SKU is required."))
        if SparePart.objects.filter(sku=sku).exclude(pk=self.instance.pk if self.instance.pk else None).exists():
            raise forms.ValidationError(_("SKU '%(sku)s' is already in use.") % {"sku": sku})
        return sku


class SparePartCreateForm(SparePartForm):
    """Extended form for part creation with opening inventory setup."""

    opening_qty = forms.DecimalField(
        required=False,
        min_value=Decimal("0"),
        max_digits=14,
        decimal_places=3,
        label=_("Opening quantity"),
        help_text=_("Initial stock at default site. Leave blank or 0 if no opening stock."),
        widget=forms.NumberInput(attrs={**_CTRL, "min": "0", "step": "1", "placeholder": "0"}),
    )
    rack_location = forms.CharField(
        required=False,
        max_length=64,
        label=_("Rack location"),
        help_text=_("Storage location at default site."),
        widget=forms.TextInput(attrs={**_CTRL, "placeholder": _("A-01-03")}),
    )
