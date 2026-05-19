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
    part = forms.ModelChoiceField(queryset=SparePart.objects.filter(is_consumable=True), widget=forms.Select(attrs=_SEL))
    quantity = forms.DecimalField(min_value=Decimal("0.001"), max_digits=14, decimal_places=3, widget=forms.NumberInput(attrs=_CTRL))
    machine_id = forms.IntegerField(required=False, min_value=1, widget=forms.NumberInput(attrs=_CTRL))
