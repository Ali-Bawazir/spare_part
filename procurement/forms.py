from decimal import Decimal

from django import forms

from maintenance.models import WorkOrder

from .models import PurchaseRequest


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
