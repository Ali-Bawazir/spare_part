from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone


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
        verbose_name = "Failure Category"
        verbose_name_plural = "Failure Categories"

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
        verbose_name = "Failure Mode"
        verbose_name_plural = "Failure Modes"

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
        choices=[(1, "Area"), (2, "Production Line"), (3, "Machine"), (4, "Subassembly"), (5, "Component")],
    )
    asset_type = models.CharField(
        max_length=32, blank=True,
        choices=[
            ("production", "Production"), ("utility", "Utility"),
            ("safety", "Safety"), ("hvac", "HVAC"), ("other", "Other"),
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
    serial_number = models.CharField(max_length=128, blank=True, help_text="Serial number (level-5 Component)")
    manufacturer = models.CharField(max_length=255, blank=True, help_text="Manufacturer (level-5 Component)")
    model_number = models.CharField(max_length=128, blank=True, help_text="Model number (level-5 Component)")
    install_date = models.DateField(null=True, blank=True, help_text="Date installed (level-5 Component)")
    expected_life_days = models.PositiveIntegerField(null=True, blank=True, help_text="Expected service life in days (level-5 Component)")
    criticality = models.CharField(
        max_length=20, blank=True,
        choices=[("LOW", "Low"), ("MEDIUM", "Medium"), ("HIGH", "High"), ("CRITICAL", "Critical")],
        help_text="Criticality rating (level-5 Component)",
    )
    status = models.CharField(
        max_length=20, blank=True, default="active",
        choices=[("active", "Active"), ("inactive", "Inactive"), ("retired", "Retired"), ("awaiting_repair", "Awaiting Repair")],
        help_text="Component status",
    )
    asset_code = models.CharField(
        max_length=128, blank=True, default="",
        help_text="Hierarchical asset code (e.g. FM-01-CONV-BRG-001)",
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
        NEW = "new", "New"
        VALIDATED = "validated", "Validated"
        CONVERTED = "converted", "Converted to work order"

    class Priority(models.TextChoices):
        CRITICAL = "critical", "Critical"
        HIGH = "high", "High"
        MEDIUM = "medium", "Medium"
        LOW = "low", "Low"

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
        verbose_name="Issue Type",
        help_text="Classified failure category (Phase 2: FailureMode sub-classification)",
    )
    failure_mode = models.ForeignKey(
        "FailureMode",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="issues",
        verbose_name="Failure Mode",
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
        help_text=(
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
        help_text="P3.3: user who escalated this issue to emergency status.",
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
        BREAKDOWN = "breakdown", "Breakdown / corrective"
        PREVENTIVE = "preventive", "Preventive"
        EMERGENCY = "emergency", "Emergency"
        REPAIR = "repair", "Repair"

    class Status(models.TextChoices):
        APPROVED = "approved", "Approved"
        ASSIGNED = "assigned", "Assigned"
        IN_PROGRESS = "in_progress", "In progress"
        PAUSED = "paused", "Paused"
        WAITING_FOR_VENDOR = "waiting_vendor", "Waiting for vendor"
        PENDING_PARTS = "pending_parts", "Pending parts"
        PENDING_REVIEW = "pending_review", "Pending manager review"
        CLOSED = "closed", "Closed"

    class PauseReason(models.TextChoices):
        """Categorized reason for pausing a work order.

        AWAITING_PARTS / AWAITING_VENDOR were removed in P3.5 (Phase 2.10
        Q6 grill). A technician who is blocked on parts/vendor should
        transition the WO to WAITING_FOR_PARTS / WAITING_FOR_VENDOR
        (those are statuses with their own dedicated workflow).
        """
        EMERGENCY = "emergency", "Emergency override (auto-paused)"
        OPERATIONAL = "operational", "Operational interruption"
        OTHER = "other", "Other (note required)"

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
        help_text="Level-5 Component this WO targets (optional)",
    )
    status = models.CharField(
        max_length=32,
        choices=Status.choices,
        default=Status.APPROVED,
        db_index=True,
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
    pause_reason = models.CharField(
        max_length=20,
        choices=PauseReason.choices,
        blank=True,
        help_text="Why the work order was paused (set on every IN_PROGRESS→PAUSED transition).",
    )
    pause_note = models.TextField(
        blank=True,
        help_text="Free-text note for pause. Required when pause_reason='other'.",
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
        if not self.number:
            last = WorkOrder.objects.order_by("-number").values_list("number", flat=True).first()
            self.number = (last or 0) + 1
        super().save(*args, **kwargs)

    def clean(self):
        super().clean()
        from .validators import validate_component_belongs_to_machine
        if self.machine_id and self.component_id:
            validate_component_belongs_to_machine(self.component, self.machine)

    def __str__(self) -> str:
        return f"WO-{self.number} ({self.get_status_display()})"

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
        ASSIGNED = "assigned", "Assigned"
        RELEASED = "released", "Released"

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
        BREAKDOWN = "breakdown", "Breakdown"
        EMERGENCY = "emergency", "Emergency"
        SCHEDULED = "scheduled", "Scheduled"
        IDLE = "idle", "Idle"

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
    """Quick log without full work order (operator/technician)."""

    machine = models.ForeignKey(Machine, on_delete=models.CASCADE, related_name="quick_logs")
    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    summary = models.CharField(max_length=500)
    details = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]


class PMSchedule(models.Model):
    machine = models.ForeignKey(Machine, on_delete=models.CASCADE, related_name="pm_schedules")
    component = models.ForeignKey(
        Machine, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="pm_component_schedules",
        help_text="Level-5 Component this PM targets (optional)",
    )
    title = models.CharField(max_length=255)
    frequency_days = models.PositiveIntegerField(default=30)
    checklist = models.TextField(blank=True, help_text="One line per checklist item")
    next_due_at = models.DateTimeField()
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["next_due_at"]

    def __str__(self) -> str:
        return f"PM: {self.title} ({self.machine})"

    def clean(self):
        super().clean()
        from .validators import validate_component_belongs_to_machine
        if self.machine_id and self.component_id:
            validate_component_belongs_to_machine(self.component, self.machine)


class Tool(models.Model):
    class Status(models.TextChoices):
        AVAILABLE = "available", "Available"
        IN_USE = "in_use", "In use"
        OUT_OF_SERVICE = "out_of_service", "Out of service"

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
        GOOD = "good", "Good"
        DAMAGED = "damaged", "Damaged"
        LOST = "lost", "Lost"

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
            raise ValidationError("Return condition is required when returning a tool.")


class Incident(models.Model):
    """Incident report for lost/damaged tools or safety issues."""

    class Status(models.TextChoices):
        OPEN = "open", "Open"
        INVESTIGATING = "investigating", "Investigating"
        CLOSED = "closed", "Closed"

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
        ISSUE_NEW = "issue_new", "New issue"
        ISSUE_VALIDATED = "issue_validated", "Issue validated"
        ISSUE_STALE = "issue_stale", "Stale issue (not validated)"
        WO_CREATED = "wo_created", "Work order created from issue"
        WO_PENDING_REVIEW = "wo_review", "Work order pending review"
        WO_ASSIGNED = "wo_assigned", "Work order assigned"
        WO_STARTED = "wo_started", "Work order started"
        WO_PAUSED = "wo_paused", "Work order paused / waiting"
        WO_CLOSED = "wo_closed", "Work order closed"
        WO_EMERGENCY = "wo_emergency", "Emergency work order"
        LOW_STOCK = "low_stock", "Low stock"
        PART_SHORTAGE_REPORTED = "part_shortage", "Part shortage reported"
        PROCUREMENT = "procurement", "Procurement"
        PM_OVERDUE = "pm_overdue", "PM overdue"
        REPAIR_RETURNED = "repair_returned", "Repair returned from vendor"
        REPAIR_REQUESTED = "repair_requested", "External repair requested"
        REPAIR_DRAFT = "repair_draft", "External repair order created (needs vendor)"
        REPAIR_SENT = "repair_sent", "External repair sent to vendor"
        # v4.9 B4: New notification kinds for richer procurement/return visibility
        PART_RECEIVED = "part_received", "Part received against PO"
        VENDOR_RETURN = "vendor_return", "Vendor returned spare part"
        SHORTAGE_FOLLOWUP = "shortage_followup", "Shortage follow-up"
        # v4.9.3: WO flow notifications requested by user
        WO_PART_RECEIVED = "wo_part_received", "Part received from supplier (linked to WO)"
        WO_PART_RETURNED = "wo_part_returned", "Part returned from vendor (linked to WO)"
        WO_PART_REJECTED = "wo_part_rejected", "Part request rejected (linked to WO)"

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

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.title} → {self.recipient}"


class ExternalRepairOrder(models.Model):
    """Repair work order sent to vendor (UC-19/20)."""

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        SENT_TO_VENDOR = "sent", "Sent to vendor"
        RETURNED = "returned", "Returned"
        CLOSED = "closed", "Closed / accepted"
        REJECTED = "rejected", "Rejected / re-repair"

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
    vendor_name = models.CharField(max_length=255, blank=True)
    estimated_cost = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    actual_cost = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True,
        help_text=(
            "Final vendor invoice amount. Set by manager on UC-20 acceptance. "
            "Required when status moves to CLOSED."
        ),
    )
    invoice_ref = models.CharField(
        max_length=120, blank=True,
        help_text="Vendor invoice number. Required on UC-20 acceptance.",
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
    closed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

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
        PENDING = "pending", "Pending manager review"
        APPROVED = "approved", "Approved (ERO created)"
        REJECTED = "rejected", "Rejected"

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
        help_text="Technician's diagnosis: what's wrong with the part"
    )
    part_description = models.TextField(
        help_text="Description of the part being sent out (name, part#, qty)"
    )
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING,
        db_index=True,
    )
    manager_note = models.TextField(
        blank=True,
        help_text="Manager's reason on approve/reject",
    )
    repair_order = models.OneToOneField(
        "ExternalRepairOrder",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="origin_request",
        help_text="Set when manager approves — links to the created ERO",
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
    parts_cost = models.DecimalField(
        max_digits=14,
        decimal_places=4,
        default=Decimal("0"),
        help_text="Sum of PartIssueLine unit_cost × qty",
    )
    vendor_cost = models.DecimalField(
        max_digits=14,
        decimal_places=4,
        default=Decimal("0"),
        help_text="Sum of ExternalRepairOrder.actual_cost",
    )
    consumables_cost = models.DecimalField(
        max_digits=14,
        decimal_places=4,
        default=Decimal("0"),
        help_text="StockMovement CONSUMABLE_USE for linked machine",
    )
    additional_cost = models.DecimalField(
        max_digits=14,
        decimal_places=4,
        default=Decimal("0"),
    )
    additional_cost_note = models.CharField(max_length=500, blank=True)

    class Meta:
        verbose_name = "Work Order Cost"
        verbose_name_plural = "Work Order Costs"

    def __str__(self) -> str:
        return f"Cost for WO-{self.work_order.number}: {self.total_cost}"

    @property
    def total_cost(self) -> Decimal:
        return (
            (self.parts_cost or Decimal("0"))
            + (self.vendor_cost or Decimal("0"))
            + (self.consumables_cost or Decimal("0"))
            + (self.additional_cost or Decimal("0"))
        )

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
            "parts_cost", "vendor_cost", "consumables_cost", "updated_at"
        ] if hasattr(self, "updated_at") else [
            "parts_cost", "vendor_cost", "consumables_cost",
        ])
        return self

    def _auto_calculate(self):
        from inventory.models import StockMovement
        from django.db.models import F, Sum

        wo = self.work_order

        parts_total = wo.part_issues.aggregate(
            total=Sum(F('quantity') * F('unit_cost'))
        )['total'] or Decimal("0")
        self.parts_cost = parts_total

        vendor_total = wo.external_repairs.aggregate(
            total=Sum('actual_cost')
        )['total'] or Decimal("0")
        self.vendor_cost = vendor_total

        consumables_total = StockMovement.objects.filter(
            work_order=wo,
            movement_type=StockMovement.MovementType.CONSUMABLE_USE,
        ).aggregate(
            total=Sum(F('quantity') * F('unit_cost'))
        )['total'] or Decimal("0")
        self.consumables_cost = consumables_total


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
        help_text="Auto-generated 300px thumbnail"
    )
    width = models.PositiveIntegerField(default=0, help_text="Original image width in px")
    height = models.PositiveIntegerField(default=0, help_text="Original image height in px")
    is_primary = models.BooleanField(default=False, help_text="Primary/default image for this entity")
    category = models.CharField(
        max_length=20,
        choices=[
            ("PRODUCT", "Product"),
            ("LABEL", "Label"),
            ("PACKAGING", "Packaging"),
            ("INSTALLED", "Installed"),
            ("DOCUMENT", "Document"),
        ],
        default="PRODUCT",
        help_text="Category of attachment image",
    )

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
