from decimal import Decimal

from django.conf import settings
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q


class Inventory(models.Model):
    """Per-site stock truth. Unique per (part, site)."""
    part = models.ForeignKey("SparePart", on_delete=models.PROTECT, related_name="inventory_items")
    site = models.ForeignKey("maintenance.Site", on_delete=models.PROTECT, related_name="inventory_items")
    quantity_available = models.DecimalField(max_digits=14, decimal_places=3, default=Decimal("0"))
    quantity_reserved = models.DecimalField(
        max_digits=14, decimal_places=3, default=Decimal("0"),
        help_text=(
            "Aggregate reserved quantity, used for planning/reporting purposes. "
            "Reservations are soft claims: warehouse issue operations validate "
            "against quantity_available only, not against quantity_available minus "
            "quantity_reserved. Do NOT add a hard physical-lock check here."
        ),
    )
    rack_location = models.CharField(max_length=64, blank=True)
    last_counted_at = models.DateTimeField(null=True, blank=True)
    last_counted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="inventory_counts",
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ["part", "site"]
        verbose_name_plural = "inventories"

    def __str__(self) -> str:
        return f"{self.part.name} @ {self.site.code} ({self.quantity_available})"


class SparePart(models.Model):
    sku = models.SlugField(max_length=64, unique=True)
    qr_code = models.CharField(
        max_length=128,
        blank=True,
        help_text="Auto-generated from SKU. Used in PART QR format.",
    )
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    category = models.CharField(max_length=64, blank=True)
    unit = models.CharField(max_length=32, blank=True)
    is_consumable = models.BooleanField(default=False, db_index=True)
    allow_operator_consumption = models.BooleanField(
        default=False,
        db_index=True,
        help_text="When True, operators can self-log this item via /consumables/",
    )
    quantity_on_hand = models.DecimalField(max_digits=14, decimal_places=3, default=Decimal("0"))
    min_stock_level = models.DecimalField(max_digits=14, decimal_places=3, default=Decimal("0"))
    max_stock_level = models.DecimalField(max_digits=14, decimal_places=3, null=True, blank=True)
    is_repairable = models.BooleanField(default=False)
    avg_cost = models.DecimalField(max_digits=12, decimal_places=4, null=True, blank=True)
    supplier = models.ForeignKey(
        "procurement.Supplier",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="parts",
    )
    last_purchase_cost = models.DecimalField(
        max_digits=12,
        decimal_places=4,
        null=True,
        blank=True,
        help_text="Most recent unit purchase price.",
    )
    status = models.CharField(
        max_length=20,
        choices=[
            ("active", "Active"),
            ("obsolete", "Obsolete"),
            ("discontinued", "Discontinued"),
        ],
        default="active",
        db_index=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]
        indexes = [
            models.Index(fields=["qr_code"]),
        ]

    def __str__(self) -> str:
        return f"{self.name} ({self.sku})"

    def is_low_stock(self, site=None) -> bool:
        from maintenance.models import Site
        default_site = Site.objects.filter(is_default=True).first()
        target_site = site or default_site
        if target_site:
            inv = self.inventory_items.filter(site=target_site).first()
            if inv:
                return (inv.quantity_available - inv.quantity_reserved) <= self.min_stock_level
        total = sum(
            inv.quantity_available - inv.quantity_reserved
            for inv in self.inventory_items.all()
        )
        return total <= self.min_stock_level

    @property
    def image_status(self):
        """Return 'COMPLETE' if a primary image is attached, else 'MISSING'."""
        from maintenance.models import Attachment
        has_primary = Attachment.objects.filter(
            entity_type="spare_part",
            entity_id=self.pk,
            is_primary=True,
        ).exists()
        return "COMPLETE" if has_primary else "MISSING"

    @property
    def has_primary_image(self):
        """True if a primary image is attached."""
        return self.image_status == "COMPLETE"

    @property
    def image_url(self):
        """Full-size URL of the primary image attachment, or None.

        Returns the original-size file URL (NOT the thumbnail). The
        primary thumbnail should be displayed via the
        ``{% part_primary_image %}`` template tag; this property is
        intended for "open full size" links.
        """
        from maintenance.models import Attachment
        att = Attachment.objects.filter(
            entity_type="spare_part",
            entity_id=self.pk,
            is_primary=True,
        ).first()
        if att and att.file:
            return att.file.url
        return None

    def save(self, *args, **kwargs):
        is_new = self.pk is None
        old_sku = None
        if not is_new and self.__class__.objects.filter(pk=self.pk).exists():
            old_sku = self.__class__.objects.get(pk=self.pk).sku
        if is_new or (old_sku and old_sku != self.sku):
            self.qr_code = f"PART:{self.sku}"
        super().save(*args, **kwargs)
        if is_new or (old_sku and old_sku != self.sku):
            try:
                from .qr_utils import save_part_qr
                save_part_qr(self.sku)
            except Exception:
                pass


class StockMovement(models.Model):
    class MovementType(models.TextChoices):
        STOCK_IN = "stock_in", "Stock in"
        STOCK_OUT = "stock_out", "Stock out"
        ISSUE_TO_WO = "issue_wo", "Issue to work order"
        CONSUMABLE_USE = "consumable", "Consumable use"
        ADJUSTMENT = "adjustment", "Adjustment"

    part = models.ForeignKey(SparePart, on_delete=models.PROTECT, related_name="movements")
    movement_type = models.CharField(max_length=20, choices=MovementType.choices, db_index=True)
    quantity = models.DecimalField(max_digits=14, decimal_places=3)
    quantity_before = models.DecimalField(max_digits=14, decimal_places=3, null=True, blank=True)
    quantity_after = models.DecimalField(max_digits=14, decimal_places=3, null=True, blank=True)
    work_order = models.ForeignKey(
        "maintenance.WorkOrder",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="stock_movements",
    )
    site = models.ForeignKey(
        "maintenance.Site",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="stock_movements",
    )
    performed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="stock_movements",
    )
    supplier_name = models.CharField(max_length=255, blank=True)
    unit_cost = models.DecimalField(max_digits=12, decimal_places=4, null=True, blank=True)
    invoice_ref = models.CharField(max_length=120, blank=True)
    reference = models.JSONField(default=dict, blank=True)
    note = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    is_archived = models.BooleanField(default=False, db_index=True)
    archived_at = models.DateTimeField(null=True, blank=True)

    class ActiveManager(models.Manager):
        def get_queryset(self):
            return super().get_queryset().filter(is_archived=False)

    class ArchivedManager(models.Manager):
        def get_queryset(self):
            return super().get_queryset().filter(is_archived=True)

    objects = ActiveManager()
    all_objects = models.Manager()
    archived = ArchivedManager()

    class Meta:
        ordering = ["-created_at"]


class PartIssueLine(models.Model):
    """Parts requested/issued against a work order.

    Hybrid approval workflow (Phase 2.1):
    - A technician on the assigned WO creates a PENDING request (no
      inventory change).
    - A manager approves (deducts stock + StockMovement) or rejects
      (no inventory change). Managers can also edit qty before approval.
    - Manager can also create a directly-APPROVED line via the legacy
      "issue part" flow.
    - Emergency exception: when the WO is_emergency, the technician's
      request auto-deducts stock and is flagged for manager post-review.
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"

    work_order = models.ForeignKey(
        "maintenance.WorkOrder",
        on_delete=models.CASCADE,
        related_name="part_issues",
    )
    part = models.ForeignKey(SparePart, on_delete=models.PROTECT)
    quantity = models.DecimalField(max_digits=14, decimal_places=3)
    unit_cost = models.DecimalField(max_digits=12, decimal_places=4)
    invoice_ref = models.CharField(max_length=120, blank=True)
    supplier_name = models.CharField(max_length=255, blank=True)
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.PENDING,
        db_index=True,
    )
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True, blank=True,
        related_name="part_issues_requested",
        help_text="Technician who added the request (null when created by manager).",
    )
    issued_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="part_issues_issued",
        help_text=(
            "The actor who caused the stock deduction. For manager direct-issue, "
            "this is the manager. For technician emergency auto-approve, this is the "
            "technician. Set at approval time for non-emergency approvals."
        ),
    )
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True, blank=True,
        related_name="part_issues_approved",
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    rejection_reason = models.TextField(blank=True)
    manager_note = models.TextField(
        blank=True,
        help_text="Manager's free-text comment attached to this part line.",
    )
    related_shortage_report = models.ForeignKey(
        "PartShortageReport",
        null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name="issue_lines",
        help_text=(
            "Set when this line is the execution record for a shortage report. "
            "Set atomically in request_part_on_wo. Used by execute_warehouse_issue "
            "to find the report via explicit FK link, not by (wo, part) lookup."
        ),
    )
    is_emergency_auto_approved = models.BooleanField(
        default=False,
        help_text=(
            "True when the line was auto-approved due to the parent WO being an "
            "emergency. Manager should review the cost and edit the qty if needed."
        ),
    )
    requested_qty = models.DecimalField(
        max_digits=14, decimal_places=3, default=0,
        help_text=(
            "What the technician originally requested. Mirrors `quantity` at "
            "request time. Preserved through edits so audit trail is intact."
        ),
    )
    approved_qty = models.DecimalField(
        max_digits=14, decimal_places=3, default=0,
        help_text=(
            "Quantity the manager approved (may differ from requested_qty). "
            "Set on approval. 0 while PENDING."
        ),
    )
    issued_qty = models.DecimalField(
        max_digits=14, decimal_places=3, default=0,
        help_text=(
            "Quantity actually deducted from stock on approval. May be less "
            "than approved_qty if stock ran out between request and approval."
        ),
    )
    shortage_qty = models.DecimalField(
        max_digits=14, decimal_places=3, default=0,
        help_text=(
            "Quantity covered by an auto-created PurchaseRequest. Computed as "
            "max(0, requested_qty - approved_qty). Independent of whether the "
            "manager edits approved_qty — PR is a separate procurement doc."
        ),
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    previous_attempt = models.ForeignKey(
        "self",
        null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name="re_reviews",
        help_text=(
            "v4.9 A5: If this line is a re-review request, points to the refused "
            "line it replaces. Preserves the audit trail of the original decision."
        ),
    )

    class Meta:
        ordering = ["-created_at"]

    def clean(self):
        if self.quantity <= 0:
            raise ValidationError("Quantity must be positive.")
        if self.status == self.Status.REJECTED and not self.rejection_reason.strip():
            raise ValidationError("Rejection reason is required when status is REJECTED.")


class ConsumableAssignment(models.Model):
    """Business accountability record when an operator self-logs an approved consumable."""

    class Source(models.TextChoices):
        SELF_SERVICE = "SELF_SERVICE", "Self Service"
        SUPERVISOR_ISSUE = "SUPERVISOR_ISSUE", "Supervisor Issue"
        WO_CONSUMPTION = "WO_CONSUMPTION", "Work Order Consumption"

    part = models.ForeignKey(
        "SparePart",
        on_delete=models.PROTECT,
        related_name="consumable_assignments",
    )
    consumed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="consumed_assignments",
    )
    issued_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="issued_consumables",
    )
    quantity = models.DecimalField(max_digits=14, decimal_places=3)
    source = models.CharField(
        max_length=32,
        choices=Source.choices,
        default=Source.SELF_SERVICE,
        db_index=True,
    )
    approved = models.BooleanField(default=True)
    site = models.ForeignKey(
        "maintenance.Site",
        on_delete=models.PROTECT,
        related_name="consumable_assignments",
    )
    machine = models.ForeignKey(
        "maintenance.Machine",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )
    note = models.CharField(max_length=500, blank=True)
    stock_movement = models.OneToOneField(
        "StockMovement",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Consumable Assignment"
        verbose_name_plural = "Consumable Assignments"

    def __str__(self) -> str:
        return f"{self.consumed_by.username} consumed {self.quantity} x {self.part.name}"


class PartShortageReport(models.Model):
    """Tech-reported (or explicit) shortage of a part on a work order.

    Distinct from PartIssueLine (which tracks the request/approval
    flow) and from StockMovement (which only logs actual inventory
    deductions).

    The report is created ONLY when the technician presses the
    "📦 Raise Shortage Request" button — not on the initial part
    request. Two layers of uniqueness protection:
      1. App-level get_or_create() in raise_shortage_request() service
      2. DB-level UniqueConstraint (partial, status='pending') below
    """

    class Status(models.TextChoices):
        PENDING_REVIEW = "pending",         "Pending manager decision"
        APPROVED       = "approved",        "Manager decision recorded (editable, no execution yet)"
        IN_FULFILLMENT = "in_fulfillment",  "Execution started (warehouse issue or PR in progress)"
        FULFILLED      = "fulfilled",       "All committed units issued and/or procured (manager-verified)"
        BLOCKED        = "blocked",         "Operational problem (PR cancelled, reservation conflict, etc.)"
        CLOSED         = "closed",          "Manager-verified closure (terminal)"
        REJECTED       = "rejected",        "Manager rejected (terminal)"

    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.PENDING_REVIEW,
        db_index=True,
    )

    # Generic source — WO today, PM/Inspection/Direct tomorrow
    content_type = models.ForeignKey(
        ContentType, on_delete=models.PROTECT, related_name="part_shortage_reports"
    )
    object_id = models.PositiveIntegerField()
    source    = GenericForeignKey("content_type", "object_id")

    # Convenience FK for WO-sourced reports (null for future sources)
    work_order = models.ForeignKey(
        "maintenance.WorkOrder", on_delete=models.PROTECT,
        null=True, blank=True, related_name="shortage_reports",
        help_text="Set when source is a WO. Null for other sources.",
    )
    part = models.ForeignKey(SparePart, on_delete=models.PROTECT, related_name="shortage_reports")
    qty_requested = models.DecimalField(max_digits=14, decimal_places=3)
    qty_issued    = models.DecimalField(max_digits=14, decimal_places=3, default=0)

    # Inventory snapshots — state at report time
    available_qty_snapshot = models.DecimalField(max_digits=14, decimal_places=3)
    reserved_qty_snapshot  = models.DecimalField(max_digits=14, decimal_places=3)
    usable_qty_snapshot    = models.DecimalField(max_digits=14, decimal_places=3)

    # Operational context snapshots
    machine_criticality_snapshot = models.CharField(max_length=20, blank=True,
        help_text="Snapshot of the source machine's criticality at report time.")
    part_criticality_snapshot     = models.CharField(max_length=20, blank=True,
        help_text="Snapshot of the part's criticality. Empty until part-criticality is added in Sprint 4+.")
    wo_priority_snapshot          = models.CharField(max_length=20, blank=True,
        help_text="Snapshot of the source WO's priority at report time. Managers approve based on urgency.")

    shortage_qty              = models.DecimalField(max_digits=14, decimal_places=3)
    expected_availability_date = models.DateField(
        null=True, blank=True,
        help_text="Manager's estimate of when the part will be back. Visible to the technician as '📦 Awaiting Procurement'.",
    )
    reason       = models.TextField(blank=True, help_text="Tech's note when raising the report.")
    reported_by  = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
                                     related_name="shortage_reports_made")
    reviewed_by  = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                     on_delete=models.SET_NULL,
                                     related_name="shortage_reports_reviewed")
    reviewed_at  = models.DateTimeField(null=True, blank=True)
    rejection_reason = models.TextField(
        blank=True,
        help_text="Manager's reason for rejection. Required when status=REJECTED. Shown to the technician.",
    )
    decision_note = models.TextField(
        blank=True,
        help_text="Manager's free-text decision note (used for both approve and reject).",
    )
    is_emergency = models.BooleanField(default=False)
    created_at   = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["content_type", "object_id"]),
            models.Index(fields=["part", "status"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["content_type", "object_id", "part"],
                condition=Q(status="pending"),
                name="unique_pending_shortage_per_source_part",
            ),
        ]

    @property
    def is_decision_locked(self) -> bool:
        """True when the manager's decision can no longer be edited.

        Rule (v4.8): once execution has started (status moved past APPROVED),
        the decision is locked. Edits require manual closure + a new report.
        """
        return self.status in {
            self.Status.IN_FULFILLMENT,
            self.Status.FULFILLED,
            self.Status.BLOCKED,
            self.Status.CLOSED,
            self.Status.REJECTED,
        }

    def __str__(self) -> str:
        try:
            wo_or_id = f"WO-{self.work_order.number}" if self.work_order_id else f"#{self.object_id}"
        except Exception:
            wo_or_id = f"#{self.object_id}"
        return f"PSR-{self.pk} ({wo_or_id} · {self.part.sku} · {self.status})"


class PartShortageDecision(models.Model):
    """Manager's decision on a PartShortageReport (v4.8).

    Editable while the parent report is in APPROVED state. Once the report
    moves to IN_FULFILLMENT (or any terminal state), the decision is locked
    -- edits are refused. The manager must close the report and create a new
    shortage if fulfillment needs to change after execution begins.

    One-to-one with the report. Once recorded, the decision's three
    qty fields are immutable from the report's perspective. The manager
    can edit them only while the report is in APPROVED state, in which
    case reservation is adjusted atomically.
    """

    class DecisionType(models.TextChoices):
        APPROVE = "approve", "Approve (issue and/or procure committed)"
        REJECT  = "reject",  "Reject (nothing committed, full qty rejected)"

    report = models.OneToOneField(
        PartShortageReport, on_delete=models.PROTECT, related_name="decision",
    )

    decision_type = models.CharField(max_length=16, choices=DecisionType.choices)

    # Books-must-balance numbers
    approved_issue_qty       = models.DecimalField(max_digits=14, decimal_places=3, default=0)
    approved_procurement_qty = models.DecimalField(max_digits=14, decimal_places=3, default=0)
    rejected_qty             = models.DecimalField(max_digits=14, decimal_places=3, default=0)

    expected_availability_date = models.DateField(null=True, blank=True)

    decided_by  = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
        related_name="shortage_decisions_made",
    )
    decided_at  = models.DateTimeField(auto_now_add=True)
    last_edited_at = models.DateTimeField(auto_now=True)
    last_edited_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True,
        related_name="shortage_decisions_edited",
    )

    rejection_reason = models.TextField(blank=True)
    decision_note    = models.TextField(blank=True)

    def clean(self):
        if any(v < 0 for v in (self.approved_issue_qty, self.approved_procurement_qty, self.rejected_qty)):
            raise ValidationError("Quantities cannot be negative.")
        total = self.approved_issue_qty + self.approved_procurement_qty + self.rejected_qty
        if total != self.report.qty_requested:
            raise ValidationError(
                f"Books must balance: issue({self.approved_issue_qty}) + "
                f"procure({self.approved_procurement_qty}) + "
                f"reject({self.rejected_qty}) = {total} "
                f"!= requested({self.report.qty_requested})."
            )
        if self.decision_type == self.DecisionType.APPROVE:
            if self.approved_issue_qty == 0 and self.approved_procurement_qty == 0:
                raise ValidationError(
                    "Approve decision must commit at least one unit to issue or procure. "
                    "Use REJECT instead."
                )
        elif self.decision_type == self.DecisionType.REJECT:
            if self.approved_issue_qty != 0 or self.approved_procurement_qty != 0:
                raise ValidationError("Reject decision must have issue_qty=0 and procurement_qty=0.")
            if self.rejected_qty != self.report.qty_requested:
                raise ValidationError(
                    f"Reject decision must reject all requested units "
                    f"({self.rejected_qty} != {self.report.qty_requested})."
                )

    def __str__(self) -> str:
        return f"PSD-{self.pk} (report={self.report_id} · {self.decision_type})"
