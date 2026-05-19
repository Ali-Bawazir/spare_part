from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone


class Machine(models.Model):
    """Factory asset; QR resolves to `qr_code`."""

    name = models.CharField(max_length=255)
    qr_code = models.SlugField(max_length=64, unique=True, db_index=True)
    location = models.CharField(max_length=255, blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return f"{self.name} ({self.qr_code})"


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
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

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


class WorkOrderStateLog(models.Model):
    work_order = models.ForeignKey(WorkOrder, on_delete=models.CASCADE, related_name="state_logs")
    from_status = models.CharField(max_length=32, blank=True)
    to_status = models.CharField(max_length=32)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True)
    note = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]


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
