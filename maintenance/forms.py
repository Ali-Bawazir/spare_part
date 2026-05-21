from django import forms
from django.contrib.auth import get_user_model

from .models import (
    ExternalRepairOrder,
    FailureCategory,
    Machine,
    MaintenanceIssue,
    PMSchedule,
    QuickMaintenanceLog,
    Tool,
    ToolAssignment,
    WorkOrder,
)

User = get_user_model()

_CTRL = {"class": "form-control"}
_SEL = {"class": "form-select"}


class IssueReportForm(forms.ModelForm):
    issue_type = forms.ModelChoiceField(
        queryset=FailureCategory.objects.filter(is_active=True),
        required=False,
        label="Issue Type",
        help_text="Optional failure classification",
    )

    class Meta:
        model = MaintenanceIssue
        fields = ("machine", "issue_type", "description")
        widgets = {
            "machine": forms.Select(attrs=_SEL),
            "description": forms.Textarea(attrs={**_CTRL, "rows": 4, "placeholder": "Describe the problem…"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["issue_type"].queryset = FailureCategory.objects.filter(is_active=True)
        if self.instance and self.instance.machine_id and self.instance.machine.failure_category_id:
            self.fields["issue_type"].initial = self.instance.machine.failure_category_id


class ValidateIssueForm(forms.Form):
    priority = forms.ChoiceField(
        choices=MaintenanceIssue.Priority.choices,
        widget=forms.Select(attrs=_SEL),
    )


class AssignTechnicianForm(forms.Form):
    technician = forms.ModelChoiceField(queryset=User.objects.none(), widget=forms.Select(attrs=_SEL))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["technician"].queryset = User.objects.filter(role=User.Role.TECHNICIAN, is_active=True)


class WorkOrderCompleteForm(forms.ModelForm):
    class Meta:
        model = WorkOrder
        fields = ("root_cause", "action_taken", "notes")
        widgets = {
            "root_cause": forms.Textarea(attrs={**_CTRL, "rows": 3}),
            "action_taken": forms.Textarea(attrs={**_CTRL, "rows": 3}),
            "notes": forms.Textarea(attrs={**_CTRL, "rows": 3}),
        }


class QuickLogForm(forms.ModelForm):
    class Meta:
        model = QuickMaintenanceLog
        fields = ("machine", "summary", "details")
        widgets = {
            "machine": forms.Select(attrs=_SEL),
            "summary": forms.TextInput(attrs={**_CTRL, "placeholder": "Short summary"}),
            "details": forms.Textarea(attrs={**_CTRL, "rows": 3}),
        }


class PMScheduleForm(forms.ModelForm):
    class Meta:
        model = PMSchedule
        fields = ("machine", "title", "frequency_days", "checklist", "next_due_at", "is_active")
        widgets = {
            "machine": forms.Select(attrs=_SEL),
            "title": forms.TextInput(attrs=_CTRL),
            "frequency_days": forms.NumberInput(attrs=_CTRL),
            "checklist": forms.Textarea(attrs={**_CTRL, "rows": 5, "placeholder": "One checklist item per line"}),
            "next_due_at": forms.DateTimeInput(attrs={**_CTRL, "type": "datetime-local"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["propagate_to_children"] = forms.BooleanField(
            required=False,
            label="Apply to all child machines",
            help_text="If this machine has child machines, create PM work orders for each of them.",
            widget=forms.CheckboxInput(attrs={"class": "form-check-input"}),
        )


class ToolAssignForm(forms.Form):
    tool = forms.ModelChoiceField(queryset=Tool.objects.none(), widget=forms.Select(attrs=_SEL))
    assignee = forms.ModelChoiceField(queryset=User.objects.none(), widget=forms.Select(attrs=_SEL))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["tool"].queryset = Tool.objects.filter(status=Tool.Status.AVAILABLE).order_by("name")
        self.fields["tool"].empty_label = "Select an available tool"
        self.fields["tool"].help_text = "Only tools in Available status can be assigned."
        self.fields["tool"].label_from_instance = lambda obj: f"{obj.name} ({obj.code})"
        self.fields["assignee"].queryset = User.objects.filter(
            role__in=[User.Role.OPERATOR, User.Role.TECHNICIAN],
            is_active=True,
        )


class ToolReturnForm(forms.Form):
    condition = forms.ChoiceField(
        choices=ToolAssignment.ReturnCondition.choices,
        widget=forms.Select(attrs=_SEL),
    )


class ToolForm(forms.ModelForm):
    class Meta:
        model = Tool
        fields = ("code", "name", "status")
        widgets = {
            "code": forms.TextInput(attrs={**_CTRL, "placeholder": "e.g. TOOL-01"}),
            "name": forms.TextInput(attrs={**_CTRL, "placeholder": "e.g. Torque wrench"}),
            "status": forms.Select(attrs=_SEL),
        }


class ExternalRepairForm(forms.ModelForm):
    class Meta:
        model = ExternalRepairOrder
        fields = ("title", "description", "work_order", "estimated_cost")
        widgets = {
            "title": forms.TextInput(attrs=_CTRL),
            "description": forms.Textarea(attrs={**_CTRL, "rows": 4}),
            "work_order": forms.Select(attrs=_SEL),
            "estimated_cost": forms.NumberInput(attrs=_CTRL),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["work_order"].required = False
        self.fields["work_order"].queryset = (
            WorkOrder.objects.exclude(status=WorkOrder.Status.CLOSED).select_related("machine").order_by("-number")[:300]
        )


class ExternalRepairOfficerForm(forms.ModelForm):
    class Meta:
        model = ExternalRepairOrder
        fields = ("vendor_name", "actual_cost", "status")
        widgets = {
            "vendor_name": forms.TextInput(attrs=_CTRL),
            "actual_cost": forms.NumberInput(attrs=_CTRL),
            "status": forms.Select(attrs=_SEL),
        }


class MachineForm(forms.ModelForm):
    class Meta:
        model = Machine
        fields = ("name", "qr_code", "location", "is_active", "site", "parent", "asset_level", "asset_type")
        widgets = {
            "name": forms.TextInput(attrs={**_CTRL, "placeholder": "e.g. Line A Press 1"}),
            "qr_code": forms.TextInput(attrs={**_CTRL, "placeholder": "e.g. PRESS-01"}),
            "location": forms.TextInput(attrs={**_CTRL, "placeholder": "e.g. Hall A"}),
            "site": forms.Select(attrs=_SEL),
            "parent": forms.Select(attrs=_SEL),
            "asset_level": forms.Select(attrs=_SEL),
            "asset_type": forms.Select(attrs=_SEL),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in ("parent", "asset_level", "asset_type"):
            self.fields[field].required = False


class EmergencyWOForm(forms.Form):
    machine = forms.ModelChoiceField(
        queryset=Machine.objects.filter(is_active=True),
        widget=forms.Select(attrs=_SEL),
    )
    title = forms.CharField(max_length=255, widget=forms.TextInput(attrs={**_CTRL, "placeholder": "e.g. Line stop — hydraulic leak"}))
    detail = forms.CharField(widget=forms.Textarea(attrs={**_CTRL, "rows": 4}))


class TechVendorNoteForm(forms.Form):
    note = forms.CharField(
        required=False,
        max_length=500,
        widget=forms.TextInput(attrs={**_CTRL, "placeholder": "Optional note for the log"}),
    )
