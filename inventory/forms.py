from decimal import Decimal

from django import forms

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
            "sku": forms.TextInput(attrs={**_CTRL, "placeholder": "BRG-6006"}),
            "name": forms.TextInput(attrs={**_CTRL, "placeholder": "Ball bearing 6006"}),
            "description": forms.Textarea(attrs={**_CTRL, "rows": 2}),
            "category": forms.TextInput(attrs={**_CTRL, "placeholder": "Bearings"}),
            "unit": forms.TextInput(attrs={**_CTRL, "placeholder": "pcs"}),
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
            raise forms.ValidationError("SKU is required.")
        if SparePart.objects.filter(sku=sku).exclude(pk=self.instance.pk if self.instance.pk else None).exists():
            raise forms.ValidationError(f"SKU '{sku}' is already in use.")
        return sku


class SparePartCreateForm(SparePartForm):
    """Extended form for part creation with opening inventory setup."""

    opening_qty = forms.DecimalField(
        required=False,
        min_value=Decimal("0"),
        max_digits=14,
        decimal_places=3,
        label="Opening quantity",
        help_text="Initial stock at default site. Leave blank or 0 if no opening stock.",
        widget=forms.NumberInput(attrs={**_CTRL, "min": "0", "step": "1", "placeholder": "0"}),
    )
    rack_location = forms.CharField(
        required=False,
        max_length=64,
        label="Rack location",
        help_text="Storage location at default site.",
        widget=forms.TextInput(attrs={**_CTRL, "placeholder": "A-01-03"}),
    )
