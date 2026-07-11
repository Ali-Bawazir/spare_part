from decimal import Decimal

from django.conf import settings
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.utils import timezone
from django.utils.translation import gettext_lazy as _


class Site(models.Model):
    """Factory/site location. Single default 'Main Factory' in Phase 1."""
    name = models.CharField(max_length=255)
    code = models.SlugField(max_length=32, unique=True)
    address = models.TextField(blank=True)
    timezone = models.CharField(max_length=64, default="UTC")
    downtime_rate = models.DecimalField(
        max_digits=10, decimal_places=2, default=0
    )
    is_active = models.BooleanField(default=True)
    is_default = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class FailureCategory(models.Model):
    """Top-level classification of equipment failures (Mechanical, Electrical, Hydraulic, etc.)."""
    code = models.SlugField(max_length=32, unique=True)
    name = models.CharField(max_length=128)
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]
        verbose_name = _("Failure Category")
        verbose_name_plural = _("Failure Categories")

    def __str__(self) -> str:
        return f"{self.name} ({self.code})"


class FailureMode(models.Model):
    """Specific failure pattern within a FailureCategory (e.g. Bearing Failure under Mechanical). Globally shared. Auto-assigned code like MECH-BRG-001."""
    category = models.ForeignKey(
        "FailureCategory",
        on_delete=models.CASCADE,
        related_name="failure_modes",
    )
    code = models.CharField(max_length=32, unique=True)
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["code"]
        verbose_name = _("Failure Mode")
        verbose_name_plural = _("Failure Modes")

    def __str__(self) -> str:
        return f"{self.code} — {self.name}"


class Machine(models.Model):
    """Asset node in the hierarchy (Area > Line > Machine > Subassembly > Component)."""

    name = models.CharField(max_length=255)
    qr_code = models.SlugField(max_length=64, unique=True, db_index=True)
    location = models.CharField(max_length=255, blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    parent = models.ForeignKey(
        "self", on_delete=models.SET_NULL,
        null=True, blank=True, related_name="children"
    )
    asset_level = models.PositiveIntegerField(
        default=3,
        choices=[
            (1, _("Area")), (2, _("Production Line")), (3, _("Machine")),
            (4, _("Subassembly")), (5, _("Component")),
        ],
    )
    asset_type = models.CharField(
        max_length=32, blank=True,
        choices=[
            ("production", _("Production")), ("utility", _("Utility")),
            ("safety", _("Safety")), ("hvac", _("HVAC")), ("other", _("Other")),
        ],
    )
    site = models.ForeignKey(
        "Site", on_delete=models.PROTECT,
        null=True, blank=True, related_name="machines"
    )
    failure_category = models.ForeignKey(
        "FailureCategory", on_delete=models.SET_NULL,
        null=True, blank=True, related_name="machines"
    )
    serial_number = models.CharField(max_length=128, blank=True, help_text=_("Serial number (level-5 Component)"))
    manufacturer = models.CharField(max_length=255, blank=True, help_text=_("Manufacturer (level-5 Component)"))
    model_number = models.CharField(max_length=128, blank=True, help_text=_("Model number (level-5 Component)"))
    install_date = models.DateField(null=True, blank=True, help_text=_("Date installed (level-5 Component)"))
    expected_life_days = models.PositiveIntegerField(null=True, blank=True, help_text=_("Expected service life in days (level-5 Component)"))
    criticality = models.CharField(
        max_length=20, blank=True,
        choices=[("LOW", _("Low")), ("MEDIUM", _("Medium")), ("HIGH", _("High")), ("CRITICAL", _("Critical"))],
        help_text=_("Criticality rating (level-5 Component)"),
    )
    status = models.CharField(
        max_length=20, blank=True, default="active",
        choices=[
            ("active", _("Active")), ("inactive", _("Inactive")),
            ("retired", _("Retired")), ("awaiting_repair", _("Awaiting Repair")),
        ],
        help_text=_("Component status"),
    )
    asset_code = models.CharField(
        max_length=128, blank=True, default="",
        help_text=_("Hierarchical asset code (e.g. FM-01-CONV-BRG-001)"),
    )

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return f"{self.name} ({self.qr_code})"

    def full_path(self) -> str:
        """e.g. 'Factory A > Line 1 > Press 1'"""
        ancestors = []
        current = self
        while current.parent:
            current = current.parent
            ancestors.insert(0, current)
        return " > ".join([m.name for m in ancestors] + [self.name])

    def get_descendants(self):
        all_descendants = []
        for child in self.children.all():
            all_descendants.append(child)
            all_descendants.extend(child.get_descendants())
        return all_descendants

    def get_root(self):
        current = self
        while current.parent:
            current = current.parent
        return current

    def get_ancestor_machines(self):
        """Walk the parent chain and return all level-3 (Machine) ancestors in order closest-first."""
        machines = []
        current = self.parent
        while current is not None:
            if current.asset_level == 3:
                machines.append(current)
            current = current.parent
        return machines

    def get_descendant_components(self):
        """Return all level-5 Machines (components) whose ancestor chain ends at this machine."""
        components = []
        for child in self.children.all():
            if child.asset_level == 5:
                components.append(child)
            components.extend(child.get_descendant_components())
        return components

    def _generate_asset_code(self) -> str:
        if self.parent and self.parent.asset_code:
            return f"{self.parent.asset_code}-{self.qr_code}"
        return self.qr_code or self.name.upper().replace(" ", "_")

    def save(self, *args, **kwargs):
        if not self.asset_code:
            self.asset_code = self._generate_asset_code()
        super().save(*args, **kwargs)

class ActiveManager(models.Manager):
    def get_queryset(self):
        return super().get_queryset().filter(is_archived=False)


class ArchivedManager(models.Manager):
    def get_queryset(self):
        return super().get_queryset().filter(is_archived=True)


class MaintenanceIssue(models.Model):
    class Status(models.TextChoices):
        NEW = "new", _("New")
        VALIDATED = "validated", _("Validated")
        CONVERTED = "converted", _("Converted to work order")

    class Priority(models.TextChoices):
        CRITICAL = "critical", _("Critical")
        HIGH = "high", _("High")
        MEDIUM = "medium", _("Medium")
        LOW = "low", _("Low")

    machine = models.ForeignKey(Machine, on_delete=models.PROTECT, related_name="issues")
    component = models.ForeignKey(
        Machine,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="component_issues",
        limit_choices_to={"asset_level": 5},
    )
    reported_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="reported_issues",
    )
    issue_type = models.ForeignKey(
        "FailureCategory",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="issues",
        verbose_name=_("Issue Type"),
        help_text=_("Classified failure category (Phase 2: FailureMode sub-classification)"),
    )
    failure_mode = models.ForeignKey(
        "FailureMode",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="issues",
        verbose_name=_("Failure Mode"),
    )
    description = models.TextField()
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.NEW,
        db_index=True,
    )
    priority = models.CharField(
        max_length=20,
        choices=Priority.choices,
        blank=True,
        default="",
    )
    validated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="validated_issues",
    )
    validated_at = models.DateTimeField(null=True, blank=True)
    is_archived = models.BooleanField(default=False, db_index=True)
    is_emergency = models.BooleanField(
        default=False, db_index=True,
        help_text=_(
            "P3.3: operator can flag an issue as emergency on creation, "
            "or a supervisor/manager can escalate during validation. "
            "When True, priority auto-sets to CRITICAL and any WO "
            "created from this issue inherits is_emergency=True."
        ),
    )
    escalated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL, null=True, blank=True,
        related_name="escalated_issues",
        help_text=_("P3.3: user who escalated this issue to emergency status."),
    )
    escalated_at = models.DateTimeField(null=True, blank=True)
    archived_at = models.DateTimeField(null=True, blank=True)
    archived_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="archived_issues"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    objects = ActiveManager()
    all_objects = models.Manager()
    archived = ArchivedManager()

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["issue_type"])]

    def __str__(self) -> str:
        return f"Issue #{self.pk} — {self.machine.name} ({self.status})"

    def clean(self):
        super().clean()
        from .validators import validate_component_belongs_to_machine
        if self.machine_id and self.component_id:
            validate_component_belongs_to_machine(self.component, self.machine)


class WorkOrder(models.Model):
    class Category(models.TextChoices):
        BREAKDOWN = "breakdown", _("Breakdown / corrective")
        PREVENTIVE = "preventive", _("Preventive")
        EMERGENCY = "emergency", _("Emergency")
        REPAIR = "repair", _("Repair")

    class LifecycleStatus(models.TextChoices):
        DRAFT          = "draft",          _("Draft")
        ASSIGNED       = "assigned",       _("Assigned")
        IN_PROGRESS    = "in_progress",    _("In progress")
        PENDING_REVIEW = "pending_review", _("Pending review")
        CLOSED         = "closed",         _("Closed")
        CANCELLED      = "cancelled",      _("Cancelled")

    class OperationalStatus(models.TextChoices):
        ACTIVE         = "active",         _("Active")
        PENDING_PARTS  = "pending_parts",  _("Pending parts")
        WAITING_VENDOR = "waiting_vendor", _("Waiting vendor")
        PAUSED         = "paused",         _("Paused")

    class Status(models.TextChoices):
        APPROVED = "approved", _("Approved")
        ASSIGNED = "assigned", _("Assigned")
        IN_PROGRESS = "in_progress", _("In progress")
        PAUSED = "paused", _("Paused")
        WAITING_FOR_VENDOR = "waiting_vendor", _("Waiting for vendor")
        PENDING_PARTS = "pending_parts", _("Pending parts")
        PENDING_REVIEW = "pending_review", _("Pending manager review")
        CLOSED = "closed", _("Closed")

    class PauseReason(models.TextChoices):
        """Categorized reason for pausing a work order.

        AWAITING_PARTS / AWAITING_VENDOR were removed in P3.5 (Phase 2.10
        Q6 grill). A technician who is blocked on parts/vendor should
        transition the WO to WAITING_FOR_PARTS / WAITING_FOR_VENDOR
        (those are statuses with their own dedicated workflow).
        """
        EMERGENCY = "emergency", _("Emergency override (auto-paused)")
        OPERATIONAL = "operational", _("Operational interruption")
        OTHER = "other", _("Other (note required)")

    number = models.PositiveIntegerField(unique=True, editable=False)
    category = models.CharField(
        max_length=20,
        choices=Category.choices,
        default=Category.BREAKDOWN,
        db_index=True,
    )
    is_emergency = models.BooleanField(default=False, db_index=True)
    issue = models.OneToOneField(
        MaintenanceIssue,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="work_order",
    )
    machine = models.ForeignKey(Machine, on_delete=models.PROTECT, related_name="work_orders", null=True, blank=True)
    component = models.ForeignKey(
        Machine, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="component_work_orders",
        help_text=_("Level-5 Component this WO targets (optional)"),
    )
    lifecycle_status = models.CharField(
        max_length=20, choices=LifecycleStatus.choices,
        default=LifecycleStatus.ASSIGNED, db_index=True,
        help_text=_("Explicit, user-driven state.")
    )
    operational_status = models.CharField(
        max_length=20, choices=OperationalStatus.choices,
        default=OperationalStatus.PAUSED, db_index=True,
        help_text=_("Derived from open blockers + labor state. Always computed; do not write directly.")
    )
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancelled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="wos_cancelled"
    )
    assigned_technician = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="assigned_work_orders",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="created_work_orders",
    )
    root_cause = models.TextField(blank=True)
    action_taken = models.TextField(blank=True)
    notes = models.TextField(blank=True)
    labor_started_at = models.DateTimeField(null=True, blank=True)
    labor_stopped_at = models.DateTimeField(null=True, blank=True)
    downtime_started_at = models.DateTimeField(null=True, blank=True)
    downtime_ended_at = models.DateTimeField(null=True, blank=True)
    is_archived = models.BooleanField(default=False, db_index=True)
    archived_at = models.DateTimeField(null=True, blank=True)
    photo_count = models.PositiveSmallIntegerField(
        default=0,
        help_text=_("Denormalized count of attached photos — used for fast Complete gating"),
    )
    archived_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="archived_work_orders"
    )
    rejected_at = models.DateTimeField(null=True, blank=True)
    rejected_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="rejected_work_orders",
    )
    rejection_reason = models.CharField(max_length=500, blank=True)
    rejection_count = models.PositiveIntegerField(default=0)
    blocker_system_version = models.PositiveIntegerField(
        default=0,
        db_index=True,
        help_text=_(
            "0 = created under legacy single-status model. "
            "1 = created under the new lifecycle/operational/blocker model. "
            "Auto-bumped to 1 on the first domain event (part request, pause, etc.) "
            "that creates a WorkOrderBlocker row."
        ),
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    tool = models.ForeignKey(
        "Tool",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="work_orders",
    )

    class Meta:
        ordering = ["-created_at"]

    def save(self, *args, **kwargs):
        is_create = self.pk is None
        if not self.number:
            last = WorkOrder.objects.order_by("-number").values_list("number", flat=True).first()
            self.number = (last or 0) + 1
        if is_create:
            self.blocker_system_version = 1
        super().save(*args, **kwargs)

    def clean(self):
        super().clean()
        from .validators import validate_component_belongs_to_machine
        if self.machine_id and self.component_id:
            validate_component_belongs_to_machine(self.component, self.machine)

    def __str__(self) -> str:
        return f"WO-{self.number} ({self.get_lifecycle_status_display()})"

    @property
    def total_downtime_minutes(self) -> int:
        """Sum of all downtime record minutes for this WO."""
        return sum(dt.total_minutes or 0 for dt in self.downtime_records.all())

    objects = ActiveManager()
    all_objects = models.Manager()
    archived = ArchivedManager()


class WorkOrderStateLog(models.Model):
    work_order = models.ForeignKey(WorkOrder, on_delete=models.CASCADE, related_name="state_logs")
    from_status = models.CharField(max_length=32, blank=True)
    to_status = models.CharField(max_length=32)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True)
    note = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]


class WorkOrderAssignmentHistory(models.Model):
    """
    Immutable append-only record of every technician assignment period on a Work Order.
    Each record tracks who was assigned, when, and when they were released.
    Records are NEVER deleted or modified — only new ones added.
    """

    class Action(models.TextChoices):
        ASSIGNED = "assigned", _("Assigned")
        RELEASED = "released", _("Released")

    work_order = models.ForeignKey(
        "WorkOrder", on_delete=models.CASCADE,
        related_name="assignment_history"
    )
    technician = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
        related_name="assignment_records"
    )
    action = models.CharField(max_length=20, choices=Action.choices)
    assigned_at = models.DateTimeField(auto_now_add=True)
    unassigned_at = models.DateTimeField(null=True, blank=True)
    reason = models.CharField(max_length=500, blank=True)
    assigned_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="assignments_made"
    )

    class Meta:
        ordering = ["assigned_at"]
        unique_together = [["work_order", "technician", "assigned_at"]]
        verbose_name_plural = "work order assignment histories"

    def __str__(self) -> str:
        status = "active" if not self.unassigned_at else f"released {self.unassigned_at}"
        return f"WO-{self.work_order.number}: {self.technician} ({status})"


class Downtime(models.Model):
    """
    Tracks machine downtime periods per work order.
    Multiple records per WO support interrupted repairs (parts wait, vendor wait, emergency overrides).
    Downtime clock runs from first WO start until manager closes — NOT paused during labor pauses.
    """

    class DowntimeType(models.TextChoices):
        BREAKDOWN = "breakdown", _("Breakdown")
        EMERGENCY = "emergency", _("Emergency")
        SCHEDULED = "scheduled", _("Scheduled")
        IDLE = "idle", _("Idle")

    work_order = models.ForeignKey(
        "WorkOrder", on_delete=models.PROTECT,
        related_name="downtime_records"
    )
    downtime_type = models.CharField(
        max_length=20, choices=DowntimeType.choices,
        default=DowntimeType.BREAKDOWN
    )
    start_time = models.DateTimeField()
    end_time = models.DateTimeField(null=True, blank=True)
    total_minutes = models.PositiveIntegerField(null=True, blank=True)
    reason = models.CharField(max_length=500, blank=True)

    class Meta:
        ordering = ["-start_time"]
        verbose_name_plural = "downtime_records"

    def save(self, *args, **kwargs):
        if self.start_time and self.end_time:
            delta = self.end_time - self.start_time
            self.total_minutes = int(delta.total_seconds() / 60)
        super().save(*args, **kwargs)

    @property
    def is_open(self):
        return self.end_time is None

    def __str__(self) -> str:
        status = "OPEN" if self.is_open else f"{self.total_minutes}min"
        return f"Downtime on WO-{self.work_order.number} ({status})"


class QuickMaintenanceLog(models.Model):
    """Lightweight machine history entry — a single observation, maintenance
    action, or operational note attached to a machine.

    Logs are immutable (no edit, no delete). They never escalate into
    Issues, Work Orders, PM tasks, or External Repairs — those are
    separate modules. The History tab on the machine page is the
    primary consumer; the model is intentionally minimal so it can be
    extended later (e.g. adding log kinds like Repair, PM Completed) by
    just adding enum values.
    """

    class Type(models.TextChoices):
        OBSERVATION      = "observation",     _("Observation")
        MAINTENANCE_NOTE = "maintenance_note", _("Maintenance")
        OPERATION_NOTE   = "operation_note",   _("Operation")

    machine    = models.ForeignKey(Machine, on_delete=models.CASCADE, related_name="quick_logs")
    author     = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    type       = models.CharField(
        max_length=20, choices=Type.choices,
        default=Type.OBSERVATION, db_index=True,
    )
    summary    = models.CharField(max_length=500)
    details    = models.TextField(blank=True)
    attachment = models.ForeignKey(
        "Attachment",
        on_delete=models.SET_NULL, null=True, blank=True,
        related_name="quick_log",
        help_text=_("Optional single attachment (image, video, audio, or PDF)."),
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["machine", "-created_at"],
                         name="qmlog_machine_recent_idx"),
        ]


class PMTemplate(models.Model):
    """Reusable PM procedure — used across many PMSchedules."""

    class Priority(models.TextChoices):
        LOW = "low", _("Low")
        MEDIUM = "medium", _("Medium")
        HIGH = "high", _("High")
        CRITICAL = "critical", _("Critical")

    code = models.SlugField(max_length=64, unique=True, help_text=_("e.g. PM-HYD-001"))
    title = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    estimated_duration_minutes = models.PositiveIntegerField(default=30)
    priority = models.CharField(
        max_length=16, choices=Priority.choices, default=Priority.MEDIUM
    )
    requires_manager_review = models.BooleanField(default=True)
    requires_photo_min_count = models.PositiveSmallIntegerField(
        default=0,
        help_text=_("Minimum number of photos the technician must attach when completing this PM"),
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["code"]

    def __str__(self) -> str:
        return f"{self.code} — {self.title}"


class PMChecklistItem(models.Model):
    """One line item in a PMTemplate's inspection checklist."""

    template = models.ForeignKey(
        PMTemplate, on_delete=models.CASCADE, related_name="checklist_items"
    )
    order = models.PositiveIntegerField(default=0)
    text = models.CharField(max_length=500)
    is_required = models.BooleanField(default=True)

    class Meta:
        ordering = ["order", "pk"]

    def __str__(self) -> str:
        return f"{self.template.code} #{self.order}: {self.text}"


class PMExecution(models.Model):
    """Dedicated compliance record per PM cycle, independent of WorkOrder."""

    class Status(models.TextChoices):
        SUBMITTED = "submitted", _("Submitted")
        APPROVED = "approved", _("Approved")
        REJECTED = "rejected", _("Rejected")
        MISSED = "missed", _("Missed")

    pm_schedule = models.ForeignKey(
        "PMSchedule", on_delete=models.CASCADE, related_name="executions"
    )
    work_order = models.OneToOneField(
        "WorkOrder",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="pm_execution",
    )
    scheduled_due_at = models.DateTimeField(
        help_text=_("Locks this execution to a specific due occurrence")
    )
    execution_sequence = models.PositiveIntegerField(
        default=1, help_text=_("Cycle counter per PMSchedule")
    )
    template_snapshot_json = models.JSONField(
        default=dict, blank=True,
        help_text=_("Immutable snapshot of template state at WO spawn time")
    )
    completed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL, null=True, blank=True,
        related_name="pm_executions_completed",
    )
    completed_at = models.DateTimeField(null=True, blank=True)
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL, null=True, blank=True,
        related_name="pm_executions_legacy_approved",
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.SUBMITTED, db_index=True
    )
    notes = models.TextField(blank=True)
    assigned_technician = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="pm_executions_assigned",
    )
    assigned_at = models.DateTimeField(null=True, blank=True)
    reassignment_count = models.PositiveIntegerField(default=0)
    last_reassignment_reason = models.CharField(max_length=500, blank=True)
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="pm_executions_approved",
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    rejected_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="pm_executions_rejected",
    )
    rejected_at = models.DateTimeField(null=True, blank=True)
    rejection_reason = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-scheduled_due_at", "-execution_sequence"]
        constraints = [
            models.UniqueConstraint(
                fields=["pm_schedule", "scheduled_due_at"],
                name="unique_pm_execution_per_occurrence",
            ),
        ]

    def __str__(self) -> str:
        return (
            f"PMExecution #{self.pk} ({self.pm_schedule_id} "
            f"seq={self.execution_sequence} {self.status})"
        )


class PMSchedule(models.Model):
    """Assignment of a PMTemplate to an asset."""

    class FrequencyType(models.TextChoices):
        DAILY = "daily", _("Daily")
        WEEKLY = "weekly", _("Weekly")
        MONTHLY = "monthly", _("Monthly")
        YEARLY = "yearly", _("Yearly")

    class TriggerType(models.TextChoices):
        TIME = "time", _("Time-based")
        METER = "meter", _("Meter-based")

    template = models.ForeignKey(
        PMTemplate, on_delete=models.PROTECT, related_name="schedules",
        help_text=_("Reusable procedure applied to this asset"),
    )
    machine = models.ForeignKey(Machine, on_delete=models.CASCADE, related_name="pm_schedules")
    component = models.ForeignKey(
        Machine, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="pm_component_schedules",
        help_text=_("Level-5 Component this PM targets (optional)"),
    )
    frequency_type = models.CharField(
        max_length=16, choices=FrequencyType.choices, default=FrequencyType.MONTHLY,
    )
    interval = models.PositiveIntegerField(default=1, help_text=_("e.g. MONTHLY × 3 = every 3 months"))
    start_date = models.DateField(default=timezone.now)
    next_due_at = models.DateTimeField()
    last_completed_at = models.DateTimeField(null=True, blank=True)
    trigger_type = models.CharField(
        max_length=16, choices=TriggerType.choices, default=TriggerType.TIME,
    )
    priority_override = models.CharField(
        max_length=16, choices=PMTemplate.Priority.choices, null=True, blank=True,
        help_text=_("If null, fall back to template.priority"),
    )
    estimated_duration_override = models.PositiveIntegerField(
        null=True, blank=True,
        help_text=_("If null, fall back to template.estimated_duration_minutes"),
    )
    grace_days = models.PositiveIntegerField(default=7)
    reminder_days_before = models.PositiveIntegerField(default=7)
    auto_generate_wo = models.BooleanField(default=False)
    due_time = models.TimeField(
        default="08:00",
        help_text=_("Time-of-day for scheduled occurrences (used for grouping Today's Schedule)"),
    )
    ends_at = models.DateField(
        null=True, blank=True,
        help_text=_("Schedule stops generating occurrences after this date"),
    )
    is_active = models.BooleanField(default=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL, null=True, blank=True,
        related_name="pm_schedules_created",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["next_due_at"]

    def __str__(self) -> str:
        return f"PM: {self.template.title} ({self.machine})"

    @property
    def effective_priority(self) -> str:
        return self.priority_override or self.template.priority

    @property
    def effective_duration_minutes(self) -> int:
        return self.estimated_duration_override or self.template.estimated_duration_minutes

    def clean(self):
        super().clean()
        from .validators import validate_component_belongs_to_machine
        if self.machine_id and self.component_id:
            validate_component_belongs_to_machine(self.component, self.machine)


class Tool(models.Model):
    class Status(models.TextChoices):
        AVAILABLE = "available", _("Available")
        IN_USE = "in_use", _("In use")
        OUT_OF_SERVICE = "out_of_service", _("Out of service")

    code = models.SlugField(max_length=64, unique=True)
    name = models.CharField(max_length=255)
    status = models.CharField(
        max_length=32,
        choices=Status.choices,
        default=Status.AVAILABLE,
        db_index=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return f"{self.name} ({self.code})"


class ToolAssignment(models.Model):
    class ReturnCondition(models.TextChoices):
        GOOD = "good", _("Good")
        DAMAGED = "damaged", _("Damaged")
        LOST = "lost", _("Lost")

    tool = models.ForeignKey(Tool, on_delete=models.CASCADE, related_name="assignments")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="tool_assignments",
    )
    assigned_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="tool_assignments_given",
    )
    assigned_at = models.DateTimeField(auto_now_add=True)
    returned_at = models.DateTimeField(null=True, blank=True)
    return_condition = models.CharField(
        max_length=20,
        choices=ReturnCondition.choices,
        blank=True,
        default="",
    )

    class Meta:
        ordering = ["-assigned_at"]

    def clean(self):
        if self.returned_at and not self.return_condition:
            raise ValidationError(_("Return condition is required when returning a tool."))


class Incident(models.Model):
    """Incident report for lost/damaged tools or safety issues."""

    class Status(models.TextChoices):
        OPEN = "open", _("Open")
        INVESTIGATING = "investigating", _("Investigating")
        CLOSED = "closed", _("Closed")

    title = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    reported_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="reported_incidents",
    )
    tool = models.ForeignKey(
        "Tool",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="incidents",
    )
    work_order = models.ForeignKey(
        "WorkOrder",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="incidents",
    )
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.OPEN,
        db_index=True,
    )
    resolved_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return self.title


class Notification(models.Model):
    """In-app alerts (PDF: low stock, emergency, PM due, pending WO, etc.)."""

    class Kind(models.TextChoices):
        ISSUE_NEW = "issue_new", _("New issue")
        ISSUE_VALIDATED = "issue_validated", _("Issue validated")
        ISSUE_STALE = "issue_stale", _("Stale issue (not validated)")
        WO_CREATED = "wo_created", _("Work order created from issue")
        WO_PENDING_REVIEW = "wo_review", _("Work order pending review")
        WO_ASSIGNED = "wo_assigned", _("Work order assigned")
        WO_STARTED = "wo_started", _("Work order started")
        WO_PAUSED = "wo_paused", _("Work order paused / waiting")
        WO_CLOSED = "wo_closed", _("Work order closed")
        WO_EMERGENCY = "wo_emergency", _("Emergency work order")
        LOW_STOCK = "low_stock", _("Low stock")
        PART_SHORTAGE_REPORTED = "part_shortage", _("Part shortage reported")
        PROCUREMENT = "procurement", _("Procurement")
        PM_OVERDUE = "pm_overdue", _("PM overdue")
        PM_UPCOMING_7D = "pm_upcoming_7d", _("PM due in 7 days")
        PM_UPCOMING_3D = "pm_upcoming_3d", _("PM due in 3 days")
        PM_UPCOMING_1D = "pm_upcoming_1d", _("PM due tomorrow")
        PM_DUE_TODAY = "pm_due_today", _("PM due today")
        REPAIR_RETURNED = "repair_returned", _("Repair returned from vendor")
        REPAIR_REQUESTED = "repair_requested", _("External repair requested")
        REPAIR_DRAFT = "repair_draft", _("External repair order created (needs vendor)")
        REPAIR_SENT = "repair_sent", _("External repair sent to vendor")
        # v4.9 B4: New notification kinds for richer procurement/return visibility
        PART_RECEIVED = "part_received", _("Part received against PO")
        VENDOR_RETURN = "vendor_return", _("Vendor returned spare part")
        SHORTAGE_FOLLOWUP = "shortage_followup", _("Shortage follow-up")
        # v4.9.3: WO flow notifications requested by user
        WO_PART_RECEIVED = "wo_part_received", _("Part received from supplier (linked to WO)")
        WO_PART_RETURNED = "wo_part_returned", _("Part returned from vendor (linked to WO)")
        WO_PART_REJECTED = "wo_part_rejected", _("Part request rejected (linked to WO)")
        # Phase 2C: WorkOrder Blocker System notifications
        WO_BLOCKER_OPENED = "wo_blocker_opened", _("WO blocker opened")
        WO_BLOCKER_RESOLVED = "wo_blocker_resolved", _("WO blocker resolved")
        WO_BLOCKER_CANCELLED = "wo_blocker_cancelled", _("WO blocker cancelled")
        EMERGENCY_INTERRUPTED = "emergency_interrupted", _("Emergency WO interrupted another WO")
        LABOR_RESUMED = "labor_resumed", _("Labor resumed on WO")
        PO_RECEIVED_SUMMARY = "po_received_summary", _("PO received (summary)")
        # Phase 8: PM workflow notifications (workflow-first CMMS)
        PM_MORNING_SUMMARY = "pm_morning_summary", _("PM morning summary (technician)")
        PM_MANAGER_MORNING = "pm_manager_morning", _("PM manager morning summary")
        PM_NEW_ASSIGNMENT = "pm_new_assignment", _("PM assigned to you")
        PM_RETURNED = "pm_returned", _("PM submission returned")
        PM_OVERDUE_TECH = "pm_overdue_tech", _("PM is overdue")
        PM_OVERDUE_MANAGER = "pm_overdue_manager", _("PM overdue (manager alert)")
        PM_UNASSIGNED = "pm_unassigned", _("PM has no technician")
        PM_WAITING_REVIEW = "pm_waiting_review", _("PM awaiting manager review")
        PM_PLAN_PAUSED = "pm_plan_paused", _("PM plan paused")

    recipient = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="notifications",
    )
    kind = models.CharField(max_length=32, choices=Kind.choices, db_index=True)
    title = models.CharField(max_length=255)
    body = models.TextField(blank=True)
    link = models.CharField(max_length=500, blank=True)
    read_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    is_critical = models.BooleanField(default=False, db_index=True)
    priority = models.CharField(
        max_length=10,
        choices=[
            ("low", _("Low")), ("normal", _("Normal")),
            ("high", _("High")), ("critical", _("Critical")),
        ],
        default="normal", db_index=True
    )
    dedup_key = models.CharField(max_length=200, blank=True, db_index=True)
    work_order_blocker = models.ForeignKey(
        "maintenance.WorkOrderBlocker", on_delete=models.SET_NULL,
        null=True, blank=True, related_name="notifications"
    )

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.title} → {self.recipient}"


class ExternalRepairOrder(models.Model):
    """Repair work order sent to vendor (UC-19/20)."""

    class Status(models.TextChoices):
        DRAFT = "draft", _("Draft")
        SENT_TO_VENDOR = "sent", _("Sent to vendor")
        RETURNED = "returned", _("Returned")
        CLOSED = "closed", _("Closed / accepted")
        REJECTED = "rejected", _("Rejected / re-repair")

    work_order = models.ForeignKey(
        WorkOrder,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="external_repairs",
    )
    machine = models.ForeignKey(
        Machine,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="external_repair_orders",
    )
    component = models.ForeignKey(
        Machine,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="component_external_repair_orders",
        limit_choices_to={"asset_level": 5},
    )
    title = models.CharField(max_length=255)
    description = models.TextField()
    vendor_name = models.CharField(
        max_length=255, blank=True,
        help_text=_(
            "Immutable audit snapshot of the vendor name at the time the "
            "ERO was sent. Kept even when `supplier` is renamed or deleted."
        ),
    )
    supplier = models.ForeignKey(
        "procurement.Supplier",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="external_repair_orders",
        help_text=_(
            "Supplier (vendor) handling the repair. Nullable for legacy EROs "
            "created before this field was added — see `vendor_name` for the "
            "immutable snapshot. New EROs should always set this FK."
        ),
    )
    estimated_cost = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    actual_cost = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True,
        help_text=_(
            "Final vendor invoice amount. Set by manager on UC-20 acceptance. "
            "Required when status moves to CLOSED."
        ),
    )
    invoice_ref = models.CharField(
        max_length=120, blank=True,
        help_text=_("Vendor invoice number. Required on UC-20 acceptance."),
    )
    invoice_date = models.DateField(
        null=True, blank=True,
        help_text=_("Date on the vendor invoice. Mirrors PurchaseOrder.supplier_invoice_date."),
    )
    invoice_attachment = models.ForeignKey(
        "Attachment",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="ero_invoice_for",
        help_text=_(
            "Uploaded scan/photo of the vendor invoice PDF/image. "
            "Mirrors PurchaseOrder.invoice_attachment."
        ),
    )
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.DRAFT,
        db_index=True,
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="repair_orders_created",
    )
    handled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="repair_orders_handled",
    )
    closed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="repair_orders_closed",
    )
    sent_at = models.DateTimeField(null=True, blank=True)
    diagnosed_at = models.DateTimeField(
        null=True, blank=True,
        help_text=_(
            "Timestamp the vendor reported a diagnosis back (after sending, "
            "before actual repair). Drives the ERO repair timeline."
        ),
    )
    returned_at = models.DateTimeField(
        null=True, blank=True,
        help_text=_(
            "Timestamp the part physically came back from the vendor. "
            "Drives the ERO repair timeline; status is RETURNED at this point."
        ),
    )
    closed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["supplier", "status"], name="ero_supplier_status_idx"),
            models.Index(fields=["status", "returned_at"], name="ero_status_returned_idx"),
        ]

    def clean(self):
        super().clean()
        from .validators import validate_component_belongs_to_machine
        if self.machine_id and self.component_id:
            validate_component_belongs_to_machine(self.component, self.machine)


class ExternalRepairRequest(models.Model):
    """Technician's request to send a part to an external vendor for repair.

    Created by the assigned technician when they diagnose that a part
    needs off-site repair. The Maintenance Manager reviews and either:
      - APPROVES — an ExternalRepairOrder (DRAFT) is created on the WO
      - REJECTS  — the request is closed with a reason

    The technician cannot create an ExternalRepairOrder directly because
    EROs create vendor engagement, cost, invoice, and financial
    obligation — those are management decisions.
    """

    class Status(models.TextChoices):
        PENDING = "pending", _("Pending manager review")
        APPROVED = "approved", _("Approved (ERO created)")
        REJECTED = "rejected", _("Rejected")

    work_order = models.ForeignKey(
        WorkOrder,
        on_delete=models.CASCADE,
        related_name="external_repair_requests",
    )
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="external_repair_requests_made",
    )
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="external_repair_requests_reviewed",
    )
    diagnosis_note = models.TextField(
        help_text=_("Technician's diagnosis: what's wrong with the part")
    )
    part_description = models.TextField(
        help_text=_("Description of the part being sent out (name, part#, qty)")
    )
    part = models.ForeignKey(
        "inventory.SparePart", on_delete=models.SET_NULL,
        null=True, blank=True, related_name="external_repair_requests"
    )
    asset = models.ForeignKey(
        "maintenance.Machine", on_delete=models.SET_NULL,
        null=True, blank=True, related_name="external_repair_requests",
        help_text=_("The level-3 machine whose part is being sent for repair")
    )
    component = models.ForeignKey(
        "maintenance.Machine", on_delete=models.SET_NULL,
        null=True, blank=True, related_name="component_external_repair_requests",
        help_text=_("The level-5 component where the part is located")
    )
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING,
        db_index=True,
    )
    manager_note = models.TextField(
        blank=True,
        help_text=_("Manager's reason on approve/reject"),
    )
    repair_order = models.OneToOneField(
        "ExternalRepairOrder",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="origin_request",
        help_text=_("Set when manager approves — links to the created ERO"),
    )
    created_at = models.DateTimeField(auto_now_add=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return (
            f"ERR-{self.pk} ({self.get_status_display()}) "
            f"on WO-{self.work_order.number}"
        )


class WorkOrderCost(models.Model):
    work_order = models.OneToOneField(
        "WorkOrder",
        on_delete=models.CASCADE,
        related_name="cost_record",
    )
    material_cost = models.DecimalField(
        max_digits=14, decimal_places=2, default=0,
        help_text=_("Sum of StockMovement.unit_cost × qty for ISSUE_TO_WO movements on this WO. Renamed from parts_cost.")
    )
    committed_material_cost = models.DecimalField(
        max_digits=14, decimal_places=2, default=Decimal("0"),
        help_text=_(
            "Sum of (PartIssueLine.approved_qty × unit_cost) for approved / "
            "allocated / issued lines. Set at approval time. Distinct from "
            "material_cost (which is the actual cost posted to the ledger at "
            "warehouse-issue time). Allows the dashboard to show committed vs "
            "actual side-by-side. Excludes pending/rejected lines. Falls back to "
            "SparePart.last_purchase_cost or avg_cost if line.unit_cost is 0."
        ),
    )
    vendor_repair_cost = models.DecimalField(
        max_digits=14, decimal_places=2, default=0,
        help_text=_("Sum of ERO.actual_cost for EROs linked via PR/ExternalRepairRequest to this WO. Renamed from vendor_cost.")
    )
    consumables_cost = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    additional_cost = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    # Phase 1+2 Cost Ledger: cache layer for the CostTransaction ledger.
    last_reconciled_at = models.DateTimeField(auto_now=True)
    ledger_transaction_count = models.PositiveIntegerField(
        default=0,
        help_text=_("Cached count of CostTransaction rows for this WO. Updated by WorkOrderCost.recalculate_from_ledger()."),
    )

    class Meta:
        verbose_name = _("Work Order Cost")
        verbose_name_plural = _("Work Order Costs")

    def __str__(self) -> str:
        return f"Cost for WO-{self.work_order.number}: {self.total_cost}"

    @property
    def total_cost(self) -> Decimal:
        return (self.material_cost + self.vendor_repair_cost
                + self.consumables_cost + self.additional_cost)

    def save(self, *args, **kwargs):
        if not self.pk:
            self._auto_calculate()
        super().save(*args, **kwargs)

    def recalculate(self):
        """Recompute cost fields from linked records. Call this when
        child records (PartIssueLine, ExternalRepairOrder, StockMovement)
        change after the WorkOrderCost row already exists. Saves and
        returns self.
        """
        self._auto_calculate()
        super().save(update_fields=[
            "material_cost", "vendor_repair_cost",
            "consumables_cost", "updated_at"
        ] if hasattr(self, "updated_at") else [
            "material_cost", "vendor_repair_cost",
            "consumables_cost",
        ])
        return self

    def _auto_calculate(self):
        """Legacy cache backfill from the source records.

        Phase 7: this used to sum `quantity` (the REQUESTED amount),
        which overstates material cost when a request is partially
        fulfilled. Switched to `issued_qty` so the cache matches what
        the cost ledger records.

        For legacy WOs that predate the `issued_qty` field, fall back
        to `quantity` via Coalesce — this preserves the old behavior
        for the small set of WOs that have requests but no issues.
        """
        from inventory.models import StockMovement
        from django.db.models import F, Sum, Value
        from django.db.models.functions import Coalesce

        wo = self.work_order

        # Phase 7: Coalesce(issued_qty, quantity). New lines always
        # have issued_qty set; legacy lines without it fall back to
        # the requested amount (same as the pre-Phase 7 behavior).
        issued_or_requested = Coalesce(F("issued_qty"), F("quantity"))
        parts_total = wo.part_issues.aggregate(
            total=Sum(issued_or_requested * F("unit_cost"))
        )['total'] or Decimal("0")
        self.material_cost = parts_total

        vendor_total = wo.external_repairs.aggregate(
            total=Sum('actual_cost')
        )['total'] or Decimal("0")
        self.vendor_repair_cost = vendor_total

        consumables_total = StockMovement.objects.filter(
            work_order=wo,
            movement_type=StockMovement.MovementType.CONSUMABLE_USE,
        ).aggregate(
            total=Sum(F('quantity') * F('unit_cost'))
        )['total'] or Decimal("0")
        self.consumables_cost = consumables_total

    def recalculate_from_ledger(self):
        """Refresh this cache from the CostTransaction ledger.

        Financial totals = SUM(amount) over ALL ledger rows for this WO
        (including reversals, which are negative).
        """
        from django.db.models import Sum
        from django.db.models.functions import Coalesce
        from .models import CostTransaction, CostCategory
        sums = (
            CostTransaction.objects
            .filter(work_order=self.work_order)
            .values("category")
            .annotate(total=Coalesce(Sum("amount"), Decimal("0")))
        )
        by_cat = {row["category"]: row["total"] for row in sums}
        self.material_cost      = by_cat.get(CostCategory.MATERIAL, Decimal("0"))
        self.vendor_repair_cost = by_cat.get(CostCategory.VENDOR_REPAIR, Decimal("0"))
        self.consumables_cost   = by_cat.get(CostCategory.CONSUMABLE, Decimal("0"))
        self.additional_cost    = by_cat.get(CostCategory.ADJUSTMENT, Decimal("0"))
        self.ledger_transaction_count = CostTransaction.objects.filter(
            work_order=self.work_order,
        ).count()

        # Phase 7.6: committed_material_cost = sum of approved × unit_cost
        # (with last_purchase_cost / avg_cost fallback) for non-rejected lines.
        # Distinct from material_cost which is the actual issued cost.
        from inventory.models import PartIssueLine
        committed_total = Decimal("0")
        for line in PartIssueLine.objects.filter(
            work_order=self.work_order,
            status__in=[
                PartIssueLine.Status.APPROVED,
                PartIssueLine.Status.ALLOCATED,
                PartIssueLine.Status.ISSUED,
            ],
        ).select_related("part"):
            eff_uc = line.unit_cost if (line.unit_cost and line.unit_cost > 0) else (
                line.part.last_purchase_cost or line.part.avg_cost or Decimal("0")
            )
            committed_total += (line.approved_qty or Decimal("0")) * eff_uc
        self.committed_material_cost = committed_total

        self.save(update_fields=[
            "material_cost", "vendor_repair_cost", "consumables_cost",
            "additional_cost", "committed_material_cost",
            "ledger_transaction_count", "last_reconciled_at",
        ])


class CostCategory(models.TextChoices):
    MATERIAL       = "material"        # Posted when part physically issued to WO
    VENDOR_REPAIR  = "vendor_repair"   # Posted when ERO closed with actual_cost
    CONSUMABLE     = "consumable"      # Posted on operator consumable use
    ADJUSTMENT     = "adjustment"      # Posted on manager manual entry


# Source types for CostTransaction. Plain CharField with choices.
# New source types are added to this list as the system grows.
COST_SOURCE_TYPE_CHOICES = [
    ("part_issue_line",       _("Part Issue Line")),
    ("external_repair_order", _("External Repair Order")),
    ("stock_movement",        _("Stock Movement")),
    ("cost_adjustment",       _("Cost Adjustment")),
    ("calibration",           _("Calibration")),
    ("fuel",                  _("Fuel")),
    ("tool_issue",            _("Tool Issue")),
]


class CostTransaction(models.Model):
    """Append-only cost journal. Single source of truth for all WO costs.

    Financial totals for a (source_type, source_id) come from SUM(amount)
    over ALL rows including reversals. The is_reversal flag is for UI
    display and audit only — never filter financial totals by it.
    """
    amount   = models.DecimalField(max_digits=12, decimal_places=2)
    category = models.CharField(max_length=20, choices=CostCategory.choices, db_index=True)
    currency = models.CharField(
        max_length=3, default="SAR", db_index=True,
        help_text=_("ISO 4217 currency code. SAR default for this deployment."),
    )

    # Material-only snapshot (null for other categories)
    quantity  = models.DecimalField(max_digits=14, decimal_places=3, null=True, blank=True)
    unit_cost = models.DecimalField(max_digits=12, decimal_places=4, null=True, blank=True)

    # Targets — three FKs, denormalized at creation
    work_order = models.ForeignKey(
        "WorkOrder", null=True, on_delete=models.PROTECT, related_name="cost_transactions"
    )
    machine    = models.ForeignKey(
        "Machine", null=True, on_delete=models.PROTECT, related_name="+",
        limit_choices_to={"asset_level__lte": 4},
    )
    component  = models.ForeignKey(
        "Machine", null=True, on_delete=models.PROTECT, related_name="+",
        limit_choices_to={"asset_level": 5},
    )

    # Provenance — plain CharField with choices (no GFK)
    source_type = models.CharField(max_length=50, choices=COST_SOURCE_TYPE_CHOICES, db_index=True)
    source_id   = models.PositiveIntegerField(null=True, blank=True)

    # Special FK for adjustments (not generic)
    adjustment = models.ForeignKey(
        "CostAdjustment", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )

    # Reversal support
    is_reversal = models.BooleanField(
        default=False, db_index=True,
        help_text=_(
            "True if this transaction reverses a previous one. Net amount for "
            "(source_type, source_id) = SUM(amount) over ALL rows including reversals."
        ),
    )
    supersedes = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="superseded_by",
        help_text=_("If is_reversal=True, points to the row being reversed. Audit trail."),
    )

    # Audit
    actor       = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT,
        related_name="posted_cost_transactions",
    )
    occurred_at = models.DateTimeField(default=timezone.now, db_index=True)
    memo        = models.CharField(max_length=300, blank=True)
    created_at  = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                check=~Q(amount=0),
                name="cost_transaction_amount_not_zero",
            ),
        ]
        indexes = [
            models.Index(fields=["work_order", "category"]),
            models.Index(fields=["machine", "-occurred_at"]),
            models.Index(fields=["component", "-occurred_at"]),
            models.Index(fields=["source_type", "source_id"]),
        ]

    def save(self, *args, **kwargs):
        if self.pk is not None:
            raise ValueError(
                "CostTransaction is immutable. To reverse, post a new transaction "
                "with negative amount, is_reversal=True, and supersedes=this row."
            )
        if not (self.work_order_id or self.machine_id or self.component_id):
            raise ValueError("CostTransaction must target a WO, machine, or component.")
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        target = (
            f"WO-{self.work_order.number}" if self.work_order_id
            else (self.machine.name if self.machine_id else (self.component.name if self.component_id else "?"))
        )
        sign = "-" if self.amount < 0 else ""
        return f"{sign}{abs(self.amount):.2f} {self.currency} {self.category} → {target}"


class CostAdjustment(models.Model):
    """Manager manual cost adjustment. Immutable. Linked 1:1 to a CostTransaction."""
    work_order = models.ForeignKey(
        "WorkOrder", on_delete=models.PROTECT, related_name="cost_adjustments"
    )
    amount     = models.DecimalField(
        max_digits=12, decimal_places=2,
        help_text=_("Signed: positive adds, negative reduces."),
    )
    memo       = models.CharField(
        max_length=300,
        help_text=_("Required. Min 10 chars. Explains why this adjustment exists."),
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="cost_adjustments_created"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                check=~Q(amount=0),
                name="cost_adjustment_amount_not_zero",
            ),
            models.CheckConstraint(
                check=Q(memo__regex=r".{10,}"),
                name="cost_adjustment_memo_min_10_chars",
            ),
        ]

    def clean(self):
        super().clean()
        if self.memo and len(self.memo.strip()) < 10:
            from django.core.exceptions import ValidationError
            raise ValidationError({"memo": _("Memo must be at least 10 characters.")})

    def save(self, *args, **kwargs):
        if self.pk is not None:
            raise ValueError("CostAdjustment is immutable. Post a reversing adjustment.")
        self.full_clean()
        super().save(*args, **kwargs)


class AuditEntry(models.Model):
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True)
    action = models.CharField(max_length=120)
    entity = models.CharField(max_length=120, blank=True)
    object_id = models.CharField(max_length=64, blank=True)
    payload = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]


def attachment_upload_path(instance, filename):
    """Store uploads at media/attachments/originals/{entity_type}/{entity_id}/{uuid}.{ext}"""
    import os
    from uuid import uuid4
    ext = filename.split(".")[-1] if "." in filename else ""
    new_name = f"{uuid4().hex}.{ext}" if ext else uuid4().hex
    return f"attachments/originals/{instance.entity_type}/{instance.entity_id}/{new_name}"

def attachment_thumbnail_path(instance, filename):
    """Thumbnail path: media/attachments/thumbs/{entity_type}/{entity_id}/{uuid}_300.{ext}"""
    import os
    from uuid import uuid4
    ext = filename.split(".")[-1] if "." in filename else ""
    base = os.path.splitext(filename)[0] if '.' in filename else filename
    return f"attachments/thumbs/{instance.entity_type}/{instance.entity_id}/{base}_300.{ext}"


class Attachment(models.Model):
    """File attachments for any entity: work_order, machine, spare_part, purchase_request, repair_order."""

    class EntityType(models.TextChoices):
        WORK_ORDER = "work_order"
        MACHINE = "machine"
        SPARE_PART = "spare_part"
        PURCHASE_REQUEST = "purchase_request"
        PURCHASE_ORDER = "purchase_order"
        MAINTENANCE_ISSUE = "maintenance_issue"
        STOCK_MOVEMENT = "stock_movement"
        REPAIR_ORDER = "repair_order"
        CONSUMABLE_ASSIGNMENT = "consumable_assignment"
        PM_SCHEDULE = "pm_schedule"
        MACHINE_LOG = "machine_log"

    entity_type = models.CharField(max_length=32, choices=EntityType.choices)
    entity_id = models.PositiveIntegerField()
    file = models.FileField(upload_to=attachment_upload_path, max_length=500)
    filename = models.CharField(max_length=255)
    size_bytes = models.PositiveIntegerField(default=0)
    mime_type = models.CharField(max_length=128, blank=True)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, related_name="attachments"
    )
    uploaded_at = models.DateTimeField(auto_now_add=True)
    note = models.CharField(max_length=500, blank=True)
    thumbnail = models.ImageField(
        upload_to=attachment_thumbnail_path,
        blank=True,
        help_text=_("Auto-generated 300px thumbnail")
    )
    width = models.PositiveIntegerField(default=0, help_text=_("Original image width in px"))
    height = models.PositiveIntegerField(default=0, help_text=_("Original image height in px"))
    is_primary = models.BooleanField(default=False, help_text=_("Primary/default image for this entity"))
    category = models.CharField(
        max_length=20,
        choices=[
            ("PRODUCT", _("Product")),
            ("LABEL", _("Label")),
            ("PACKAGING", _("Packaging")),
            ("INSTALLED", _("Installed")),
            ("DOCUMENT", _("Document")),
        ],
        default="PRODUCT",
        help_text=_("Category of attachment image"),
    )
    is_video = models.BooleanField(default=False, db_index=True)
    compressed_path = models.CharField(max_length=500, blank=True,
        help_text=_("Path to the compressed video file (mp4) — populated by VideoCompressionService"))
    thumbnail_path = models.CharField(max_length=500, blank=True,
        help_text=_("Path to a 1-second thumbnail of the video"))

    class Meta:
        ordering = ["-uploaded_at"]
        indexes = [models.Index(fields=["entity_type", "entity_id"])]

    def save(self, *args, **kwargs):
        if self.file and not self.filename:
            self.filename = self.file.name
        if self.file and not self.size_bytes:
            self.size_bytes = self.file.size or 0
        if self.file and not self.mime_type:
            self.mime_type = getattr(self.file, 'content_type', '') or ''
        super().save(*args, **kwargs)
        if self.file and not self.thumbnail:
            self._generate_thumbnail()

    def _generate_thumbnail(self):
        """Generate 300px max thumbnail using Pillow."""
        import os
        from django.conf import settings
        from PIL import Image

        try:
            file_path = self.file.path
            img = Image.open(file_path)

            self.width = img.width
            self.height = img.height

            img.thumbnail((300, 300), Image.LANCZOS)

            dir_name = os.path.dirname(file_path)
            base_name = os.path.splitext(os.path.basename(file_path))[0]
            thumb_name = f"{base_name}_300.jpg"
            thumb_dir = file_path.replace('/originals/', '/thumbs/').rsplit('/', 1)[0]
            os.makedirs(thumb_dir, exist_ok=True)
            thumb_path = os.path.join(thumb_dir, thumb_name)

            thumb_img = img.convert('RGB')
            thumb_img.save(thumb_path, 'JPEG', quality=85, optimize=True)

            self.thumbnail = thumb_path.replace(settings.MEDIA_ROOT, '').lstrip('/')
            self.width = img.width
            self.height = img.height

            super().save(update_fields=['thumbnail', 'width', 'height'])
        except Exception as e:
            import logging
            logger = logging.getLogger(__name__)
            logger.warning(f"Thumbnail generation failed for attachment {self.pk}: {e}")

    def __str__(self) -> str:
        return f"{self.filename} ({self.entity_type}:{self.entity_id})"


class WorkOrderBlocker(models.Model):
    class Kind(models.TextChoices):
        PART          = "part",           _("Awaiting Spare Part")
        SHORTAGE      = "shortage",       _("Awaiting Procurement")
        VENDOR_REPAIR = "vendor_repair",  _("Awaiting Vendor Repair")
        OPERATIONAL   = "operational",    _("Operational Pause")

    class Status(models.TextChoices):
        OPEN      = "open",      _("Open")
        RESOLVED  = "resolved",  _("Resolved")
        CANCELLED = "cancelled", _("Cancelled")

    work_order       = models.ForeignKey("WorkOrder", on_delete=models.CASCADE, related_name="blockers")
    kind             = models.CharField(max_length=20, choices=Kind.choices, db_index=True)
    status           = models.CharField(max_length=16, choices=Status.choices, default=Status.OPEN, db_index=True)
    content_type     = models.ForeignKey(ContentType, on_delete=models.CASCADE, null=True, blank=True)
    object_id        = models.PositiveIntegerField(null=True, blank=True)
    external_ref     = GenericForeignKey("content_type", "object_id")
    external_label   = models.CharField(max_length=300, blank=True,
        help_text=_("Cached human-readable summary, e.g. 'BRG-6006 × 2' or 'Servo S7-300'"))
    related_ero      = models.ForeignKey("maintenance.ExternalRepairOrder", on_delete=models.SET_NULL,
                                         null=True, blank=True, related_name="blockers")
    source_work_order = models.ForeignKey("maintenance.WorkOrder", on_delete=models.SET_NULL,
                                          null=True, blank=True, related_name="interruptions_caused")
    note             = models.TextField(blank=True)
    pause_reason     = models.CharField(max_length=20, blank=True,
        help_text=_("'operational' | 'other' | 'emergency' (system-set)"))
    opened_at        = models.DateTimeField(auto_now_add=True)
    opened_by        = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
                                         null=True, related_name="blockers_opened")
    resolved_at      = models.DateTimeField(null=True, blank=True)
    resolved_by      = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                         null=True, blank=True, related_name="blockers_resolved")
    resolution_note  = models.TextField(blank=True)
    cancelled_at     = models.DateTimeField(null=True, blank=True)
    cancelled_by     = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                         null=True, blank=True, related_name="blockers_cancelled")
    cancel_reason    = models.TextField(blank=True)
    migrated_from_legacy = models.BooleanField(default=False)

    class Meta:
        indexes = [
            models.Index(fields=["work_order", "status"]),
            models.Index(fields=["status", "kind"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["work_order", "content_type", "object_id"],
                condition=models.Q(status="open"),
                name="uniq_open_blocker_per_wo_ref",
            ),
        ]


class WorkOrderBlockerEvent(models.Model):
    class EventType(models.TextChoices):
        BLOCKER_CREATED       = "blocker_created"
        BLOCKER_RESOLVED      = "blocker_resolved"
        BLOCKER_CANCELLED     = "blocker_cancelled"
        PART_REQUEST_CREATED  = "part_request_created"
        PART_APPROVED         = "part_approved"
        PART_REJECTED         = "part_rejected"
        PART_ISSUED           = "part_issued"
        PART_RECEIVED         = "part_received"
        SHORTAGE_RAISED       = "shortage_raised"
        SHORTAGE_DECIDED      = "shortage_decided"
        SHORTAGE_FULFILLED    = "shortage_fulfilled"
        ERO_CREATED           = "ero_created"
        ERO_SENT              = "ero_sent"
        ERO_RETURNED          = "ero_returned"
        ERO_ACCEPTED          = "ero_accepted"
        EMERGENCY_INTERRUPTED = "emergency_interrupted"
        LABOR_RESUMED         = "labor_resumed"

    blocker      = models.ForeignKey(WorkOrderBlocker, on_delete=models.CASCADE, related_name="events")
    event_type   = models.CharField(max_length=32, choices=EventType.choices, db_index=True)
    actor        = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                     null=True, blank=True, related_name="blocker_events")
    payload      = models.JSONField(default=dict, blank=True)
    created_at   = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        indexes = [
            models.Index(fields=["blocker", "created_at"]),
            models.Index(fields=["event_type", "created_at"]),
        ]


class MaintenanceSettings(models.Model):
    """Singleton row tracking cron state for daily maintenance generation."""

    last_generate_run = models.DateTimeField(null=True, blank=True)
    morning_summary_sent_date = models.DateField(null=True, blank=True)

    class Meta:
        verbose_name_plural = _("Maintenance settings")

    def save(self, *args, **kwargs):
        self.pk = 1  # singleton
        super().save(*args, **kwargs)

    @classmethod
    def get_solo(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    def __str__(self):
        return _('Maintenance settings')

