from decimal import Decimal

from django import forms
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.forms.models import inlineformset_factory
from django.utils.translation import gettext_lazy as _

from .models import (
    ExternalRepairOrder,
    ExternalRepairRequest,
    FailureCategory,
    FailureMode,
    Machine,
    MaintenanceIssue,
    PMChecklistItem,
    PMSchedule,
    PMTemplate,
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
        label=_("Failure Category"),
        help_text=_("Top-level failure classification"),
    )
    failure_mode = forms.ModelChoiceField(
        queryset=FailureMode.objects.filter(is_active=True),
        required=False,
        label=_("Failure Mode"),
        help_text=_("Specific failure pattern (optional)"),
    )
    is_emergency = forms.BooleanField(
        required=False,
        label=_("Mark as emergency"),
        help_text=_(
            "P3.3: tick if this issue is an emergency. The issue will be "
            "set to CRITICAL priority, and any WO created from it will "
            "be an Emergency WO."
        ),
    )

    class Meta:
        model = MaintenanceIssue
        fields = ("machine", "component", "issue_type", "failure_mode", "description", "is_emergency")
        labels = {
            "machine": _("Machine"),
            "component": _("Component"),
            "description": _("Description"),
        }
        widgets = {
            "machine": forms.Select(attrs=_SEL),
            "failure_mode": forms.Select(attrs=_SEL),
            "description": forms.Textarea(attrs={**_CTRL, "rows": 4, "placeholder": _("Describe the problem…")}),
        }

    def __init__(self, *args, lock_asset=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["issue_type"].queryset = FailureCategory.objects.filter(is_active=True)
        self.fields["failure_mode"].queryset = FailureMode.objects.filter(is_active=True)
        # Component: only show components that match the bound machine (if any).
        # The dropdown on the template uses the same recursive descendant logic
        # via /machines/<id>/components/, so we must mirror it here.
        from .models import Machine as _M
        bound_machine = None
        if self.is_bound:
            bound_machine_id = self.data.get("machine")
            if bound_machine_id:
                bound_machine = _M.objects.filter(pk=bound_machine_id).first()
        elif self.instance and self.instance.machine_id:
            bound_machine = self.instance.machine
        if bound_machine is not None:
            if bound_machine.asset_level == 5:
                comps = _M.objects.filter(pk=bound_machine.pk)
            else:
                # Mirror the JS endpoint's recursive lookup so the queryset
                # accepts grand-children and deeper descendants too.
                comp_ids = [c.pk for c in bound_machine.get_descendant_components()]
                comps = _M.objects.filter(pk__in=comp_ids).order_by("name")
            self.fields["component"].queryset = comps
        else:
            self.fields["component"].queryset = _M.objects.none()
        if self.instance and self.instance.machine_id and self.instance.machine.failure_category_id:
            self.fields["issue_type"].initial = self.instance.machine.failure_category_id
            self.fields["failure_mode"].queryset = FailureMode.objects.filter(
                is_active=True,
                category_id=self.instance.machine.failure_category_id
            )
        if lock_asset:
            self.fields["machine"].disabled = True
            if "component" in self.fields:
                self.fields["component"].disabled = True

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("is_emergency"):
            # Emergency issues default to CRITICAL priority.
            cleaned["priority"] = MaintenanceIssue.Priority.CRITICAL
        machine = cleaned.get("machine")
        component = cleaned.get("component")
        if machine and component:
            from .validators import validate_component_belongs_to_machine
            try:
                validate_component_belongs_to_machine(component, machine)
            except ValidationError as e:
                for field, errors in e.message_dict.items():
                    for error in errors:
                        self.add_error(field, error)
        return cleaned


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
        labels = {
            "root_cause": _("Root cause"),
            "action_taken": _("Action taken"),
            "notes": _("Notes"),
        }
        widgets = {
            "root_cause": forms.Textarea(attrs={**_CTRL, "rows": 3}),
            "action_taken": forms.Textarea(attrs={**_CTRL, "rows": 3}),
            "notes": forms.Textarea(attrs={**_CTRL, "rows": 3}),
        }


class WorkOrderPauseForm(forms.Form):
    """Categorized pause form. Reason is mandatory; note required for 'other'."""
    pause_reason = forms.ChoiceField(
        choices=WorkOrder.PauseReason.choices,
        widget=forms.Select(attrs=_SEL),
        label=_("Reason for pause"),
    )
    pause_note = forms.CharField(
        required=False,
        max_length=500,
        widget=forms.Textarea(
            attrs={**_CTRL, "rows": 2, "placeholder": _("Required when reason is 'Other'")}
        ),
        label=_("Note"),
    )

    def clean(self):
        cleaned = super().clean()
        reason = cleaned.get("pause_reason")
        note = (cleaned.get("pause_note") or "").strip()
        if reason == WorkOrder.PauseReason.OTHER and not note:
            raise forms.ValidationError(
                {"pause_note": _("Note is required when reason is 'Other'.")}
            )
        return cleaned


class QuickLogForm(forms.ModelForm):
    class Meta:
        model = QuickMaintenanceLog
        fields = ("machine", "summary", "details")
        labels = {
            "machine": _("Machine"),
            "summary": _("Summary"),
            "details": _("Details"),
        }
        widgets = {
            "machine": forms.Select(attrs=_SEL),
            "summary": forms.TextInput(attrs={**_CTRL, "placeholder": _("Short summary")}),
            "details": forms.Textarea(attrs={**_CTRL, "rows": 3}),
        }


class PMScheduleForm(forms.ModelForm):
    machine = forms.ModelChoiceField(
        queryset=Machine.objects.filter(is_active=True, asset_level=3),
        required=True,
        label=_("Machine"),
        widget=forms.Select(attrs=_SEL),
    )
    component = forms.ModelChoiceField(
        queryset=Machine.objects.filter(is_active=True, asset_level=5),
        required=False,
        label=_("Component"),
        widget=forms.Select(attrs=_SEL),
    )
    due_time = forms.TimeField(
        required=False,
        label=_("Due time"),
        help_text=_("Time-of-day for scheduled occurrences (default 08:00)"),
    )
    ends_at = forms.DateField(
        required=False,
        label=_("Ends at"),
        help_text=_("Schedule stops generating occurrences after this date"),
    )

    class Meta:
        model = PMSchedule
        fields = (
            "template", "machine", "component",
            "frequency_type", "interval", "start_date", "next_due_at",
            "due_time", "ends_at",
            "priority_override", "estimated_duration_override",
            "grace_days", "reminder_days_before",
            "trigger_type", "is_active",
        )
        labels = {
            "template": _("Template"),
            "frequency_type": _("Frequency"),
            "interval": _("Interval"),
            "start_date": _("Start date"),
            "next_due_at": _("Next due at"),
            "priority_override": _("Priority override"),
            "estimated_duration_override": _("Estimated duration override"),
            "grace_days": _("Grace days"),
            "reminder_days_before": _("Reminder days before"),
            "trigger_type": _("Trigger type"),
            "is_active": _("Is active"),
        }
        widgets = {
            "template": forms.Select(attrs=_SEL),
            "frequency_type": forms.Select(attrs=_SEL),
            "interval": forms.NumberInput(attrs={**_CTRL, "min": "1"}),
            "start_date": forms.DateInput(attrs={**_CTRL, "type": "date"}),
            "next_due_at": forms.DateTimeInput(attrs={**_CTRL, "type": "datetime-local"}),
            "priority_override": forms.Select(attrs=_SEL),
            "estimated_duration_override": forms.NumberInput(attrs={**_CTRL, "min": "1"}),
            "grace_days": forms.NumberInput(attrs={**_CTRL, "min": "0"}),
            "reminder_days_before": forms.NumberInput(attrs={**_CTRL, "min": "0"}),
            "trigger_type": forms.Select(attrs=_SEL),
        }

    def __init__(self, *args, lock_asset=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["template"].queryset = PMTemplate.objects.filter(is_active=True).order_by("code")
        for fname in ("template", "frequency_type", "interval", "start_date", "next_due_at",
                       "priority_override", "estimated_duration_override",
                       "grace_days", "reminder_days_before", "trigger_type"):
            if fname in self.fields:
                self.fields[fname].required = True
        self.fields["priority_override"].required = False
        self.fields["estimated_duration_override"].required = False
        self.fields["propagate_to_children"] = forms.BooleanField(
            required=False, label=_("Apply to all child machines"),
            help_text=_("If this machine has child machines, create PM work orders for each of them."),
            widget=forms.CheckboxInput(attrs={"class": "form-check-input"}),
        )
        if lock_asset:
            self.fields["machine"].disabled = True
            self.fields["component"].disabled = True

    def clean(self):
        cleaned = super().clean()
        machine = cleaned.get("machine")
        component = cleaned.get("component")
        if machine and component:
            from .validators import validate_component_belongs_to_machine
            try:
                validate_component_belongs_to_machine(component, machine)
            except ValidationError as e:
                for field, errors in e.message_dict.items():
                    for error in errors:
                        self.add_error(field, error)
        return cleaned


class BasePMChecklistItemForm(forms.ModelForm):
    text = forms.CharField(
        max_length=500,
        required=False,
        label=_("Text"),
        widget=forms.TextInput(attrs={**_CTRL, "placeholder": _("Checklist item text")}),
    )
    order = forms.IntegerField(
        required=False,
        label=_("Order"),
        widget=forms.NumberInput(attrs={**_CTRL, "min": "1"}),
    )

    class Meta:
        model = PMChecklistItem
        fields = ("order", "text", "is_required")
        labels = {
            "is_required": _("Required?"),
        }
        widgets = {
            "is_required": forms.CheckboxInput(attrs={"class": "form-check-input"}),
        }


class BasePMChecklistItemFormSet(forms.BaseInlineFormSet):
    def clean(self):
        super().clean()
        seen_texts = set()
        has_at_least_one = False
        for form in self.forms:
            if not hasattr(form, "cleaned_data"):
                continue
            if form.cleaned_data.get("DELETE"):
                continue
            text = (form.cleaned_data.get("text") or "").strip()
            if not text:
                continue
            has_at_least_one = True
            if text.lower() in seen_texts:
                form.add_error("text", _("Duplicate checklist item text."))
            seen_texts.add(text.lower())
        if not has_at_least_one:
            raise forms.ValidationError(_("At least one checklist item is required."))


PMChecklistItemFormSet = forms.inlineformset_factory(
    PMTemplate, PMChecklistItem,
    form=BasePMChecklistItemForm,
    formset=BasePMChecklistItemFormSet,
    fields=("order", "text", "is_required"),
    extra=3, can_delete=True,
    validate_min=False,
)


class PMTemplateForm(forms.ModelForm):
    requires_photo_min_count = forms.IntegerField(
        required=False, min_value=0, max_value=20,
        label=_("Requires photo min count"),
        help_text=_("Minimum number of photos the technician must attach"),
    )

    class Meta:
        model = PMTemplate
        fields = ("code", "title", "description", "estimated_duration_minutes",
                  "priority", "requires_manager_review", "requires_photo_min_count", "is_active")
        labels = {
            "code": _("Code"),
            "title": _("Title"),
            "description": _("Description"),
            "estimated_duration_minutes": _("Estimated duration (minutes)"),
            "priority": _("Priority"),
            "requires_manager_review": _("Requires manager review"),
            "is_active": _("Is active"),
        }
        widgets = {
            "code": forms.TextInput(attrs={**_CTRL, "placeholder": "e.g. PM-HYD-001"}),
            "title": forms.TextInput(attrs=_CTRL),
            "description": forms.Textarea(attrs={**_CTRL, "rows": 3}),
            "estimated_duration_minutes": forms.NumberInput(attrs={**_CTRL, "min": "1"}),
            "priority": forms.Select(attrs=_SEL),
            "requires_manager_review": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "is_active": forms.CheckboxInput(attrs={"class": "form-check-input"}),
        }


class ToolAssignForm(forms.Form):
    tool = forms.ModelChoiceField(queryset=Tool.objects.none(), widget=forms.Select(attrs=_SEL), label=_("Tool"))
    assignee = forms.ModelChoiceField(queryset=User.objects.none(), widget=forms.Select(attrs=_SEL), label=_("Assignee"))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["tool"].queryset = Tool.objects.filter(status=Tool.Status.AVAILABLE).order_by("name")
        self.fields["tool"].empty_label = _("Select an available tool")
        self.fields["tool"].help_text = _("Only tools in Available status can be assigned.")
        self.fields["tool"].label_from_instance = lambda obj: f"{obj.name} ({obj.code})"
        self.fields["assignee"].queryset = User.objects.filter(
            role__in=[User.Role.OPERATOR, User.Role.TECHNICIAN],
            is_active=True,
        )


class ToolReturnForm(forms.Form):
    condition = forms.ChoiceField(
        choices=ToolAssignment.ReturnCondition.choices,
        widget=forms.Select(attrs=_SEL),
        label=_("Condition"),
    )


class ToolForm(forms.ModelForm):
    class Meta:
        model = Tool
        fields = ("code", "name", "status")
        labels = {
            "code": _("Code"),
            "name": _("Name"),
            "status": _("Status"),
        }
        widgets = {
            "code": forms.TextInput(attrs={**_CTRL, "placeholder": "e.g. TOOL-01"}),
            "name": forms.TextInput(attrs={**_CTRL, "placeholder": _("e.g. Torque wrench")}),
            "status": forms.Select(attrs=_SEL),
        }


class ExternalRepairForm(forms.ModelForm):
    machine = forms.ModelChoiceField(
        queryset=Machine.objects.filter(is_active=True, asset_level=3),
        required=True,
        label=_("Machine"),
        widget=forms.Select(attrs=_SEL),
    )
    component = forms.ModelChoiceField(
        queryset=Machine.objects.filter(is_active=True, asset_level=5),
        required=False,
        label=_("Component"),
        widget=forms.Select(attrs=_SEL),
    )

    class Meta:
        model = ExternalRepairOrder
        fields = ("title", "description", "machine", "component", "work_order", "estimated_cost")
        labels = {
            "title": _("Title"),
            "description": _("Description"),
            "work_order": _("Work order"),
            "estimated_cost": _("Estimated cost"),
        }
        widgets = {
            "title": forms.TextInput(attrs=_CTRL),
            "description": forms.Textarea(attrs={**_CTRL, "rows": 4}),
            "work_order": forms.Select(attrs=_SEL),
            "estimated_cost": forms.NumberInput(attrs=_CTRL),
        }

    def __init__(self, *args, lock_asset=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["work_order"].required = False
        self.fields["work_order"].queryset = (
            WorkOrder.objects.exclude(lifecycle_status=WorkOrder.LifecycleStatus.CLOSED).select_related("machine").order_by("-number")[:300]
        )
        if lock_asset:
            self.fields["machine"].disabled = True
            self.fields["component"].disabled = True

    def clean(self):
        cleaned = super().clean()
        machine = cleaned.get("machine")
        component = cleaned.get("component")
        if machine and component:
            from .validators import validate_component_belongs_to_machine
            try:
                validate_component_belongs_to_machine(component, machine)
            except ValidationError as e:
                for field, errors in e.message_dict.items():
                    for error in errors:
                        self.add_error(field, error)
        return cleaned


class ExternalRepairOfficerForm(forms.ModelForm):
    """Officer-side repair form: pick vendor, set actual_cost, transition status.

    `supplier` is a Supplier FK (preferred). Filtered to suppliers whose
    `supplier_type == 'repair_vendor'` so only true repair vendors appear in
    the dropdown (parts suppliers never show up here, even if they have
    related parts). The `vendor_name` snapshot is auto-populated from
    `supplier.name` on save so historical rows always carry the vendor name
    as it was at the time of the repair, even if the Supplier is later
    renamed or deleted.

    `vendor_name` stays as a writable free-text field for legacy EROs that
    pre-date the Supplier FK — the snapshot is preserved either way.
    """
    supplier = forms.ModelChoiceField(
        queryset=None,
        required=False,
        widget=forms.Select(attrs=_SEL),
        label=_("Repair vendor"),
        help_text=_("Only suppliers classified as repair vendors."),
    )

    class Meta:
        model = ExternalRepairOrder
        fields = ("supplier", "vendor_name", "actual_cost", "status")
        labels = {
            "vendor_name": _("Vendor name (legacy snapshot)"),
            "actual_cost": _("Actual cost"),
            "status": _("Status"),
        }
        widgets = {
            "vendor_name": forms.TextInput(attrs=_CTRL),
            "actual_cost": forms.NumberInput(attrs=_CTRL),
            "status": forms.Select(attrs=_SEL),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from procurement.models import Supplier
        # Filter dropdown to active repair vendors only; alphabetized.
        self.fields["supplier"].queryset = (
            Supplier.objects
            .filter(is_active=True, supplier_type=Supplier.Type.REPAIR_VENDOR)
            .order_by("code", "name")
        )
        # If instance has supplier FK but no vendor_name yet, pre-fill the
        # snapshot for display. The clean() also re-syncs on save.
        if self.instance and self.instance.pk and self.instance.supplier_id:
            if not self.instance.vendor_name:
                self.fields["vendor_name"].initial = self.instance.supplier.name
            self.fields["supplier"].initial = self.instance.supplier_id

    def clean(self):
        cleaned = super().clean()
        supplier = cleaned.get("supplier")
        vendor_name = (cleaned.get("vendor_name") or "").strip()
        if supplier and not vendor_name:
            cleaned["vendor_name"] = supplier.name
        return cleaned


class MachineForm(forms.ModelForm):
    class Meta:
        model = Machine
        fields = ("name", "qr_code", "location", "is_active", "site", "parent", "asset_level", "asset_type",
                  "serial_number", "manufacturer", "model_number", "install_date", "expected_life_days",
                  "criticality", "status", "asset_code", "failure_category")
        labels = {
            "name": _("Name"),
            "qr_code": _("QR Code"),
            "location": _("Location"),
            "is_active": _("Is active"),
            "site": _("Site"),
            "parent": _("Parent machine"),
            "asset_level": _("Asset level"),
            "asset_type": _("Asset type"),
            "serial_number": _("Serial number"),
            "manufacturer": _("Manufacturer"),
            "model_number": _("Model number"),
            "install_date": _("Install date"),
            "expected_life_days": _("Expected life (days)"),
            "criticality": _("Criticality"),
            "status": _("Status"),
            "asset_code": _("Asset code"),
            "failure_category": _("Failure category"),
        }
        widgets = {
            "name": forms.TextInput(attrs={**_CTRL, "placeholder": _("e.g. Line A Press 1")}),
            "qr_code": forms.TextInput(attrs={**_CTRL, "placeholder": _("e.g. PRESS-01")}),
            "location": forms.TextInput(attrs={**_CTRL, "placeholder": _("e.g. Hall A")}),
            "site": forms.Select(attrs=_SEL),
            "parent": forms.Select(attrs=_SEL),
            "asset_level": forms.Select(attrs=_SEL),
            "asset_type": forms.Select(attrs=_SEL),
            "serial_number": forms.TextInput(attrs={**_CTRL, "placeholder": _("e.g. SN-12345")}),
            "manufacturer": forms.TextInput(attrs={**_CTRL, "placeholder": _("e.g. Siemens")}),
            "model_number": forms.TextInput(attrs={**_CTRL, "placeholder": _("e.g. MDL-X100")}),
            "install_date": forms.DateInput(attrs={**_CTRL, "type": "date"}),
            "expected_life_days": forms.NumberInput(attrs={**_CTRL, "placeholder": _("e.g. 3650")}),
            "criticality": forms.Select(attrs=_SEL),
            "status": forms.Select(attrs=_SEL),
            "asset_code": forms.TextInput(attrs={**_CTRL, "placeholder": _("e.g. FM-01-CONV-BRG-001")}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in ("parent", "asset_level", "asset_type"):
            self.fields[field].required = False


class EmergencyWOForm(forms.Form):
    machine = forms.ModelChoiceField(
        queryset=Machine.objects.filter(is_active=True, asset_level=3),
        widget=forms.Select(attrs=_SEL),
        label=_("Machine"),
    )
    component = forms.ModelChoiceField(
        queryset=Machine.objects.filter(is_active=True, asset_level=5),
        required=False,
        widget=forms.Select(attrs=_SEL),
        help_text=_("Optional: Target a specific component (level-5)"),
        label=_("Component"),
    )
    title = forms.CharField(
        max_length=255,
        widget=forms.TextInput(attrs={**_CTRL, "placeholder": _("e.g. Line stop — hydraulic leak")}),
        label=_("Title"),
    )
    detail = forms.CharField(
        widget=forms.Textarea(attrs={**_CTRL, "rows": 4}),
        label=_("Detail"),
    )

    def __init__(self, *args, lock_asset=False, **kwargs):
        super().__init__(*args, **kwargs)
        if lock_asset:
            self.fields["machine"].disabled = True
            self.fields["component"].disabled = True


class TechVendorNoteForm(forms.Form):
    note = forms.CharField(
        required=False,
        max_length=500,
        widget=forms.TextInput(attrs={**_CTRL, "placeholder": _("Optional note for the log")}),
    )


class ExternalRepairRequestForm(forms.ModelForm):
    """Technician-submitted form to request external (vendor) repair."""

    class Meta:
        model = ExternalRepairRequest
        fields = ("diagnosis_note", "part_description")
        widgets = {
            "diagnosis_note": forms.Textarea(
                attrs={
                    **_CTRL,
                    "rows": 3,
                    "placeholder": _("What's wrong with the part? Why does it need an external repair?"),
                }
            ),
            "part_description": forms.Textarea(
                attrs={
                    **_CTRL,
                    "rows": 2,
                    "placeholder": _("Part name, part number, qty (e.g. 'Servo drive S7-300, qty 1')"),
                }
            ),
        }

    def clean(self):
        cleaned = super().clean()
        if not (cleaned.get("diagnosis_note") or "").strip():
            self.add_error("diagnosis_note", _("Diagnosis note is required."))
        if not (cleaned.get("part_description") or "").strip():
            self.add_error("part_description", _("Part description is required."))
        return cleaned


class ExternalRepairRequestDecisionForm(forms.Form):
    """Manager approves or rejects a PENDING external-repair request."""

    ACTION_CHOICES = (
        ("approve", _("Approve (create ERO)")),
        ("reject", _("Reject")),
    )
    action = forms.ChoiceField(choices=ACTION_CHOICES, widget=forms.Select(attrs=_SEL))
    manager_note = forms.CharField(
        required=False,
        widget=forms.Textarea(
            attrs={**_CTRL, "rows": 2, "placeholder": _("Required on reject; optional on approve.")}
        ),
    )

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("action") == "reject" and not (cleaned.get("manager_note") or "").strip():
            self.add_error("manager_note", _("A rejection reason is required."))
        return cleaned


class CostAdjustmentForm(forms.Form):
    """Manager manual cost adjustment on a WorkOrder.

    Posts a CostTransaction (category=ADJUSTMENT) and a corresponding
    CostAdjustment with a mandatory memo (>= 10 chars).
    """
    amount = forms.DecimalField(
        max_digits=12, decimal_places=2,
        label=_("Amount"),
        widget=forms.NumberInput(attrs={
            **_CTRL, "step": "0.01",
            "placeholder": _("Signed: positive adds, negative reduces"),
        }),
        help_text=_("Signed amount. Positive adds to the WO total, negative reduces it."),
    )
    memo = forms.CharField(
        max_length=300,
        label=_("Memo"),
        widget=forms.Textarea(attrs={
            **_CTRL, "rows": 3,
            "placeholder": _("Why does this adjustment exist? (min 10 chars)"),
        }),
        help_text=_("Required. Min 10 characters. Explains why this adjustment exists."),
    )

    def clean_memo(self):
        memo = (self.cleaned_data.get("memo") or "").strip()
        if len(memo) < 10:
            raise ValidationError({"memo": _("Memo must be at least 10 characters.")})
        return memo

    def clean_amount(self):
        amount = self.cleaned_data.get("amount")
        if amount is None or Decimal(str(amount)) == 0:
            raise ValidationError(_("Amount must be non-zero."))
        return amount


class RepairManagerAcceptForm(forms.Form):
    """P3.2 — manager acceptance of a RETURNED ExternalRepairOrder (UC-20).

    SRS UC-20: every issued part must have cost + supplier + invoice.
    So actual_cost and invoice_ref are mandatory on accept.
    """
    actual_cost = forms.DecimalField(
        max_digits=12, decimal_places=2, min_value=Decimal("0"),
        label=_("Actual cost"),
        widget=forms.NumberInput(attrs={
            **_CTRL, "step": "0.01", "min": "0",
            "placeholder": _("Vendor invoice total"),
        }),
        help_text=_("Required — final vendor invoice amount."),
    )
    invoice_ref = forms.CharField(
        max_length=120,
        label=_("Invoice ref"),
        widget=forms.TextInput(attrs={
            **_CTRL, "placeholder": _("Vendor invoice number"),
        }),
        help_text=_("Required — vendor invoice number (UC-20)."),
    )
    note = forms.CharField(
        required=False,
        label=_("Note"),
        widget=forms.Textarea(attrs={
            **_CTRL, "rows": 2,
            "placeholder": _("Optional verification note (e.g. condition of returned part)"),
        }),
    )

    def clean(self):
        cleaned = super().clean()
        cost = cleaned.get("actual_cost")
        if cost is None or cost <= 0:
            self.add_error("actual_cost", _("Actual cost must be greater than zero."))
        inv = (cleaned.get("invoice_ref") or "").strip()
        if not inv:
            self.add_error("invoice_ref", _("Vendor invoice number is required."))
        else:
            cleaned["invoice_ref"] = inv
        return cleaned
