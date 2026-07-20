from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models.expressions import RawSQL
from django.db.models import Max
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from inventory.models import SparePart


class Supplier(models.Model):
    class Type(models.TextChoices):
        PARTS_SUPPLIER = "parts_supplier", _("Parts supplier")
        REPAIR_VENDOR = "repair_vendor", _("Repair vendor")

    name = models.CharField(max_length=255, unique=True)
    contact = models.CharField(max_length=255, blank=True)
    notes = models.TextField(blank=True)
    supplier_type = models.CharField(
        max_length=20,
        choices=Type.choices,
        default=Type.PARTS_SUPPLIER,
        db_index=True,
        help_text=_(
            "What this supplier does for us: a parts supplier sells spare parts, "
            "a repair vendor fixes damaged parts. These are mutually exclusive — "
            "if a supplier does both, create two separate Supplier records."
        ),
    )
    is_repair_vendor = models.BooleanField(
        default=False,
        db_index=True,
        help_text=_(
            "DEPRECATED: derived from `supplier_type`. Kept for back-compat in "
            "queries and admin filters. Will be removed once callers are migrated."
        ),
    )
    code = models.CharField(
        max_length=64,
        unique=True,
        db_index=True,
        blank=True,
        null=True,
        # Bug fix: was SlugField, which only accepts ASCII. CharField allows
        # Arabic and other non-ASCII codes. DB column type is unchanged
        # (VARCHAR(64)); only form-validator behavior changes.
        help_text=_("Unique supplier code. Use letters, digits, and dashes (e.g. SUP-001 or MWRD-001). "
                  "Used in QR format SUPPLIER:{code}."),
    )
    contact_person = models.CharField(max_length=128, blank=True)
    phone = models.CharField(max_length=64, blank=True)
    email = models.EmailField(blank=True)
    address = models.TextField(blank=True)
    is_active = models.BooleanField(default=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        if self.code:
            return f"{self.code} — {self.name}"
        return self.name

    def save(self, *args, **kwargs):
        # Keep the deprecated is_repair_vendor boolean in sync with the
        # canonical supplier_type field. This lets existing queries that
        # filter on is_repair_vendor keep working while we migrate callers.
        self.is_repair_vendor = self.supplier_type == self.Type.REPAIR_VENDOR

        is_new = self.pk is None
        old_code = None
        if not is_new and self.__class__.objects.filter(pk=self.pk).exists():
            old_code = self.__class__.objects.get(pk=self.pk).code
        super().save(*args, **kwargs)
        if is_new or (old_code != self.code and self.code):
            try:
                from inventory.qr_utils import save_supplier_qr
                save_supplier_qr(self.code)
            except Exception:
                pass


class PurchaseRequest(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", _("Pending officer")
        CONVERTED_TO_PO = "converted_to_po", _("Converted to PO")
        PARTIALLY_FULFILLED = "partially_fulfilled", _("Partially fulfilled")
        FULFILLED = "fulfilled", _("Fulfilled")
        CANCELLED = "cancelled", _("Cancelled")

    part = models.ForeignKey(SparePart, on_delete=models.PROTECT, related_name="purchase_requests")
    work_order = models.ForeignKey(
        "maintenance.WorkOrder",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="purchase_requests",
    )
    machine = models.ForeignKey(
        "maintenance.Machine",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="purchase_requests",
    )
    component = models.ForeignKey(
        "maintenance.Machine",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="component_purchase_requests",
        limit_choices_to={"asset_level": 5},
    )
    quantity = models.DecimalField(max_digits=14, decimal_places=3)
    notes = models.TextField(blank=True)
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING,
        db_index=True,
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="purchase_requests_created",
    )
    supplier = models.ForeignKey(
        Supplier,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="purchase_requests",
    )
    purchase_order = models.ForeignKey(
        "PurchaseOrder",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="purchase_requests",
    )
    source_shortage_report = models.ForeignKey(
        "inventory.PartShortageReport",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="purchase_requests",
        help_text=_(
            "Set when this PR was auto-created from a shortage decision. "
            "Null for manual PRs. SET_NULL on delete (closing the shortage "
            "does not delete the PR)."
        ),
    )
    unit_price = models.DecimalField(max_digits=12, decimal_places=4, null=True, blank=True)
    handled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="purchase_requests_handled",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"PR #{self.pk} — {self.part.sku} x{self.quantity}"

    def clean(self):
        super().clean()
        from maintenance.validators import validate_component_belongs_to_machine
        if self.machine_id and self.component_id:
            validate_component_belongs_to_machine(self.component, self.machine)


class PurchaseOrder(models.Model):
    """Actual procurement order sent to a supplier."""

    class Status(models.TextChoices):
        DRAFT = "draft", _("Draft")
        SENT = "sent", _("Sent to supplier")
        PARTIAL_RECEIVED = "partial", _("Partial received")
        RECEIVED = "received", _("Fully received")
        CLOSED_SHORT = "closed_short", _("Closed short")
        CANCELLED = "cancelled", _("Cancelled")

    po_number = models.CharField(max_length=20, unique=True, editable=False)
    supplier = models.ForeignKey(
        Supplier,
        on_delete=models.PROTECT,
        related_name="purchase_orders",
    )
    invoice_ref = models.CharField(max_length=120, blank=True)
    expected_delivery = models.DateField(null=True, blank=True)
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.DRAFT,
        db_index=True,
    )
    cancellation_reason = models.TextField(blank=True, default="")
    notes = models.TextField(blank=True)

    # Supplier invoice (captured at receive time, distinct from PO-level invoice_ref)
    supplier_invoice_number = models.CharField(max_length=120, blank=True)
    supplier_invoice_date   = models.DateField(null=True, blank=True)
    supplier_invoice_total  = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    currency                = models.CharField(max_length=3, default="USD")
    exchange_rate           = models.DecimalField(max_digits=12, decimal_places=6, default=1,
        help_text=_("PO currency → site currency at receive time"))
    invoice_attachment      = models.ForeignKey("maintenance.Attachment", on_delete=models.SET_NULL,
                                                null=True, blank=True, related_name="po_invoice_for")

    # Delivery metadata
    received_by       = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                           null=True, blank=True, related_name="pos_received")
    inspected_by      = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                           null=True, blank=True, related_name="pos_inspected")
    received_at       = models.DateTimeField(null=True, blank=True)
    carrier           = models.CharField(max_length=120, blank=True)
    tracking_number   = models.CharField(max_length=120, blank=True)
    delivery_note_ref = models.CharField(max_length=120, blank=True)
    delivery_date     = models.DateField(null=True, blank=True)
    condition_notes   = models.TextField(blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="purchase_orders_created",
    )
    handled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="purchase_orders_handled",
    )
    reorder_source = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reorders",
        db_index=True,
        help_text=_("If this PO was created by reordering an earlier PO, the source PO's id. "
                  "Lets the manager trace the lineage and view the original pricing."),
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"PO-{self.po_number}"

    def save(self, *args, **kwargs):
        if not self.po_number:
            year = timezone.now().year
            prefix = f"PO-{year}-"
            last_suffix = PurchaseOrder.objects.filter(
                po_number__startswith=prefix
            ).annotate(
                suffix=RawSQL(
                    "CAST(SUBSTRING(po_number FROM %s) AS INTEGER)",
                    (len(prefix) + 1,),
                )
            ).aggregate(m=Max("suffix"))["m"]
            next_num = (last_suffix or 0) + 1
            self.po_number = f"PO-{year}-{next_num:04d}"
        super().save(*args, **kwargs)

    @property
    def is_locked(self) -> bool:
        return self.status in (
            self.Status.RECEIVED,
            self.Status.CLOSED_SHORT,
            self.Status.CANCELLED,
        )

    @property
    def total_ordered_value(self) -> Decimal:
        return sum(item.total_price or Decimal("0") for item in self.items.all())

    @property
    def total_received_value(self) -> Decimal:
        total = Decimal("0")
        for item in self.items.all():
            price = item.actual_unit_price if item.actual_unit_price is not None else item.negotiated_unit_price
            total += item.received_qty * price
        return total


class PurchaseOrderItem(models.Model):
    """Line item on a purchase order tracking ordered vs received."""

    class Condition(models.TextChoices):
        GOOD     = "good",     _("Good")
        DAMAGED  = "damaged",  _("Damaged (quarantine)")
        REJECTED = "rejected", _("Rejected at inspection")

    purchase_order = models.ForeignKey(
        PurchaseOrder,
        on_delete=models.CASCADE,
        related_name="items",
    )
    part = models.ForeignKey(
        SparePart,
        on_delete=models.PROTECT,
        related_name="po_items",
        null=True,
        blank=True,
    )
    tool = models.ForeignKey(
        "maintenance.Tool",
        on_delete=models.PROTECT,
        related_name="po_items",
        null=True,
        blank=True,
        help_text=_(
            "Set when this PO line is for a tool (e.g. a damaged tool being "
            "reordered). Mutually exclusive with `part` — exactly one of "
            "`part` or `tool` must be set."
        ),
    )
    ordered_qty = models.DecimalField(max_digits=14, decimal_places=3)
    received_qty = models.DecimalField(max_digits=14, decimal_places=3, default=Decimal("0"))
    negotiated_unit_price = models.DecimalField(max_digits=12, decimal_places=4, default=Decimal("0"),
        help_text=_("Price agreed on the PO. Renamed from unit_price."))
    actual_unit_price = models.DecimalField(max_digits=12, decimal_places=4, null=True, blank=True,
        help_text=_("What was actually invoiced by the supplier. Used for weighted-avg cost recompute."))
    total_price = models.DecimalField(max_digits=14, decimal_places=4, default=Decimal("0"))
    damaged_qty       = models.DecimalField(max_digits=14, decimal_places=3, default=Decimal("0"))
    rejected_qty      = models.DecimalField(max_digits=14, decimal_places=3, default=Decimal("0"))
    backordered_qty   = models.DecimalField(max_digits=14, decimal_places=3, default=Decimal("0"),
        help_text=_(
            "Units the supplier owes but hasn't delivered. "
            "Phase 4 invariant: ordered_qty = received_qty + backordered_qty "
            "(maintained on every receive + on close_short)."
        ))
    condition         = models.CharField(max_length=16, choices=Condition.choices, default=Condition.GOOD)
    line_note         = models.CharField(max_length=500, blank=True)

    class Meta:
        verbose_name_plural = _("purchase order items")

    def __str__(self) -> str:
        label = self.part.sku if self.part_id else (self.tool.code if self.tool_id else "?")
        return f"{label} x{self.ordered_qty} (recv {self.received_qty})"

    def clean(self):
        super().clean()
        has_part = bool(self.part_id)
        has_tool = bool(self.tool_id)
        if has_part and has_tool:
            raise ValidationError(
                _("A PO line item must reference either a part or a tool, not both.")
            )
        if not has_part and not has_tool:
            raise ValidationError(
                _("A PO line item must reference a part or a tool.")
            )

    @property
    def remaining_qty(self) -> Decimal:
        return self.ordered_qty - self.received_qty

    def save(self, *args, **kwargs):
        price = self.actual_unit_price if self.actual_unit_price is not None else self.negotiated_unit_price
        if self.ordered_qty and price:
            self.total_price = self.ordered_qty * price
        super().save(*args, **kwargs)
