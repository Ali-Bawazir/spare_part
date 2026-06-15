from decimal import Decimal

from django.conf import settings
from django.db import models
from django.utils import timezone

from inventory.models import SparePart


class Supplier(models.Model):
    name = models.CharField(max_length=255, unique=True)
    contact = models.CharField(max_length=255, blank=True)
    notes = models.TextField(blank=True)
    is_repair_vendor = models.BooleanField(default=False, help_text="Also handles external repairs")
    code = models.SlugField(
        max_length=64,
        unique=True,
        db_index=True,
        blank=True,
        null=True,
        help_text="Unique supplier code e.g. SUP-001. Used in QR format SUPPLIER:{code}.",
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
        PENDING = "pending", "Pending officer"
        CONVERTED_TO_PO = "converted_to_po", "Converted to PO"
        PARTIALLY_FULFILLED = "partially_fulfilled", "Partially fulfilled"
        FULFILLED = "fulfilled", "Fulfilled"
        CANCELLED = "cancelled", "Cancelled"

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
        help_text=(
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
        DRAFT = "draft", "Draft"
        SENT = "sent", "Sent to supplier"
        PARTIAL_RECEIVED = "partial", "Partial received"
        RECEIVED = "received", "Fully received"
        CLOSED_SHORT = "closed_short", "Closed short"
        CANCELLED = "cancelled", "Cancelled"

    po_number = models.CharField(max_length=12, unique=True, editable=False)
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
    notes = models.TextField(blank=True)
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
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"PO-{self.po_number}"

    def save(self, *args, **kwargs):
        if not self.po_number:
            last = PurchaseOrder.objects.order_by("-po_number").values_list("po_number", flat=True).first()
            if last:
                parts = last.split("-")
                year = timezone.now().year
                if len(parts) == 3 and parts[0] == "PO" and parts[1] == str(year):
                    next_num = int(parts[2]) + 1
                else:
                    next_num = 1
            else:
                next_num = 1
            self.po_number = f"PO-{timezone.now().year}-{next_num:04d}"
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
            total += item.received_qty * item.unit_price
        return total


class PurchaseOrderItem(models.Model):
    """Line item on a purchase order tracking ordered vs received."""

    purchase_order = models.ForeignKey(
        PurchaseOrder,
        on_delete=models.CASCADE,
        related_name="items",
    )
    part = models.ForeignKey(
        SparePart,
        on_delete=models.PROTECT,
        related_name="po_items",
    )
    ordered_qty = models.DecimalField(max_digits=14, decimal_places=3)
    received_qty = models.DecimalField(max_digits=14, decimal_places=3, default=Decimal("0"))
    unit_price = models.DecimalField(max_digits=12, decimal_places=4, default=Decimal("0"))
    total_price = models.DecimalField(max_digits=14, decimal_places=4, default=Decimal("0"))

    class Meta:
        verbose_name_plural = "purchase order items"

    def __str__(self) -> str:
        return f"{self.part.sku} x{self.ordered_qty} (recv {self.received_qty})"

    @property
    def remaining_qty(self) -> Decimal:
        return self.ordered_qty - self.received_qty

    def save(self, *args, **kwargs):
        if self.ordered_qty and self.unit_price:
            self.total_price = self.ordered_qty * self.unit_price
        super().save(*args, **kwargs)
