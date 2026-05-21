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


class Machine(models.Model):
    """Factory asset; QR resolves to `qr_code`."""

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
        choices=[(1, "Plant"), (2, "Line"), (3, "Machine"), (4, "Component")],
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


class WorkOrder(models.Model):
    class Category(models.TextChoices):
        BREAKDOWN = "breakdown", "Breakdown / corrective"
        PREVENTIVE = "preventive", "Preventive"
        EMERGENCY = "emergency", "Emergency"

    class Status(models.TextChoices):
        APPROVED = "approved", "Approved"
        ASSIGNED = "assigned", "Assigned"
        IN_PROGRESS = "in_progress", "In progress"
        PAUSED = "paused", "Paused"
        WAITING_FOR_VENDOR = "waiting_vendor", "Waiting for vendor"
        PENDING_PARTS = "pending_parts", "Pending parts"
        PENDING_REVIEW = "pending_review", "Pending manager review"
        CLOSED = "closed", "Closed"

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
    machine = models.ForeignKey(Machine, on_delete=models.PROTECT, related_name="work_orders")
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
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def save(self, *args, **kwargs):
        if not self.number:
            last = WorkOrder.objects.order_by("-number").values_list("number", flat=True).first()
            self.number = (last or 0) + 1
        super().save(*args, **kwargs)

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


class Notification(models.Model):
    """In-app alerts (PDF: low stock, emergency, PM due, pending WO, etc.)."""

    class Kind(models.TextChoices):
        ISSUE_NEW = "issue_new", "New issue"
        ISSUE_VALIDATED = "issue_validated", "Issue validated"
        WO_PENDING_REVIEW = "wo_review", "Work order pending review"
        WO_ASSIGNED = "wo_assigned", "Work order assigned"
        WO_EMERGENCY = "wo_emergency", "Emergency work order"
        LOW_STOCK = "low_stock", "Low stock"
        PROCUREMENT = "procurement", "Procurement"
        PM_OVERDUE = "pm_overdue", "PM overdue"
        REPAIR_RETURNED = "repair_returned", "Repair returned from vendor"

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
    title = models.CharField(max_length=255)
    description = models.TextField()
    vendor_name = models.CharField(max_length=255, blank=True)
    estimated_cost = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    actual_cost = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
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
    sent_at = models.DateTimeField(null=True, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]


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
    """Store uploads at media/attachments/{entity_type}/{entity_id}/{uuid}.{ext}"""
    import os
    from uuid import uuid4
    ext = filename.split(".")[-1] if "." in filename else ""
    new_name = f"{uuid4().hex}.{ext}" if ext else uuid4().hex
    return f"attachments/{instance.entity_type}/{instance.entity_id}/{new_name}"


class Attachment(models.Model):
    """File attachments for any entity: work_order, machine, spare_part, purchase_request, repair_order."""

    class EntityType(models.TextChoices):
        WORK_ORDER = "work_order"
        MACHINE = "machine"
        SPARE_PART = "spare_part"
        PURCHASE_REQUEST = "purchase_request"
        REPAIR_ORDER = "repair_order"

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

    class Meta:
        ordering = ["-uploaded_at"]
        indexes = [models.Index(fields=["entity_type", "entity_id"])]

    def save(self, *args, **kwargs):
        if self.file and not self.filename:
            self.filename = self.file.name
        if self.file and not self.size_bytes:
            self.size_bytes = self.file.size or 0
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f"{self.filename} ({self.entity_type}:{self.entity_id})"
