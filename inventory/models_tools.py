"""Tool-pool models: ToolAssignment, ToolDamageReport, ToolMovement.

These are kept in a separate module to avoid a circular import with
``inventory.models.ReusableToolInstance``, which references the active
assignment via a property.
"""
from django.conf import settings
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.db import models
from django.utils.translation import gettext_lazy as _


class ToolAssignment(models.Model):
    """One row per checkout of a reusable tool.

    Operational record of "who has which tool right now, on which machine,
    in what condition, with what notes". An assignment is open while
    ``return_at`` is null and closed once the operator returns the tool.
    """

    class Condition(models.TextChoices):
        GOOD    = "good",    _("Good")
        FAIR    = "fair",    _("Fair")
        DAMAGED = "damaged", _("Damaged")

    instance = models.ForeignKey(
        "inventory.ReusableToolInstance",
        on_delete=models.PROTECT,
        related_name="assignments",
    )
    operator = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="tool_assignments",
    )
    machine = models.ForeignKey(
        "maintenance.Machine",
        on_delete=models.PROTECT,
        related_name="tool_assignments",
    )
    checkout_at = models.DateTimeField()
    return_at = models.DateTimeField(null=True, blank=True, db_index=True)
    condition_out = models.CharField(
        max_length=10, choices=Condition.choices, default=Condition.GOOD,
    )
    condition_in = models.CharField(
        max_length=10, choices=Condition.choices, blank=True,
    )
    notes = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-checkout_at"]
        indexes = [
            models.Index(fields=["operator", "return_at"]),
            models.Index(fields=["machine", "return_at"]),
            models.Index(fields=["instance", "return_at"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["instance"],
                condition=models.Q(return_at__isnull=True),
                name="one_open_assignment_per_instance",
            ),
        ]

    def __str__(self) -> str:
        state = "open" if self.return_at is None else "closed"
        return f"{self.instance} → {self.operator} ({state})"

    def clean(self):
        if self.condition_in and not self.return_at:
            raise ValidationError(_("Cannot set condition_in without return_at."))
        if self.return_at and self.return_at < self.checkout_at:
            raise ValidationError(_("Return time cannot be before checkout time."))


class ToolDamageReport(models.Model):
    """Report of damage to a reusable tool.

    Created automatically when an operator returns a tool with
    condition_in=damaged. Manually created only via /tools/damage/.
    Distinct from ToolAssignment because damage is a separate workflow
    that may outlast the assignment (tool remains out of service until
    repaired or written off).
    """

    class Status(models.TextChoices):
        OPEN        = "open",        _("Open")
        REPAIRED    = "repaired",    _("Repaired")
        WRITTEN_OFF = "written_off", _("Written Off")

    instance = models.ForeignKey(
        "inventory.ReusableToolInstance",
        on_delete=models.PROTECT,
        related_name="damage_reports",
    )
    reported_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="tool_damage_reported",
    )
    machine = models.ForeignKey(
        "maintenance.Machine",
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="tool_damage_reports",
    )
    assignment = models.ForeignKey(
        "ToolAssignment",
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="damage_reports",
        help_text=_("The return that triggered this report, if any."),
    )
    damage_date = models.DateTimeField()
    reason = models.TextField()
    repair_cost = models.DecimalField(
        max_digits=12, decimal_places=4,
        null=True, blank=True,
        help_text=_("Set when the manager marks the report as repaired."),
    )
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.OPEN, db_index=True,
    )
    resolved_at = models.DateTimeField(null=True, blank=True)
    resolved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True, blank=True,
        related_name="tool_damage_resolved",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-damage_date"]
        indexes = [
            models.Index(fields=["instance", "status"]),
            models.Index(fields=["status", "damage_date"]),
        ]

    def __str__(self) -> str:
        return f"Damage #{self.pk} ({self.instance} · {self.status})"

    def clean(self):
        if self.status == self.Status.REPAIRED and self.repair_cost is None:
            raise ValidationError(_("Repair cost is required when marking as repaired."))


class ToolMovement(models.Model):
    """Append-only audit log of every reusable-tool state change.

    One row per state transition. Created by the small services in
    ``inventory.services_tools``. Read by the dashboard, search, and
    history views.
    """

    class MovementType(models.TextChoices):
        ISSUED       = "issued",       _("Issued to Tool Pool")
        ASSIGNED     = "assigned",     _("Assigned")
        RETURNED     = "returned",     _("Returned")
        DAMAGED      = "damaged",      _("Damaged")
        REPAIRED     = "repaired",     _("Repaired")
        WRITTEN_OFF  = "written_off",  _("Written Off")

    instance = models.ForeignKey(
        "inventory.ReusableToolInstance",
        on_delete=models.PROTECT,
        related_name="movements",
    )
    movement_type = models.CharField(
        max_length=20, choices=MovementType.choices, db_index=True,
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="tool_movements",
    )
    machine = models.ForeignKey(
        "maintenance.Machine",
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="tool_movements",
    )
    assignment = models.ForeignKey(
        "ToolAssignment",
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="movements",
    )
    damage_report = models.ForeignKey(
        "ToolDamageReport",
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="movements",
    )
    note = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["instance", "-created_at"]),
            models.Index(fields=["actor", "-created_at"]),
            models.Index(fields=["movement_type", "-created_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.instance} {self.movement_type} @ {self.created_at:%Y-%m-%d %H:%M}"
