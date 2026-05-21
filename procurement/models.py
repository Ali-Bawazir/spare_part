from django.conf import settings
from django.db import models

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
        ORDERED = "ordered", "Ordered"
        RECEIVED = "received", "Received / stock updated"
        CANCELLED = "cancelled", "Cancelled"

    part = models.ForeignKey(SparePart, on_delete=models.PROTECT, related_name="purchase_requests")
    work_order = models.ForeignKey(
        "maintenance.WorkOrder",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="purchase_requests",
    )
    quantity = models.DecimalField(max_digits=14, decimal_places=3)
    urgency = models.CharField(max_length=32, default="normal")
    is_emergency = models.BooleanField(default=False, db_index=True)
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
