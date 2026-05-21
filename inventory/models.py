from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models


class Inventory(models.Model):
    """Per-site stock truth. Unique per (part, site)."""
    part = models.ForeignKey("SparePart", on_delete=models.PROTECT, related_name="inventory_items")
    site = models.ForeignKey("maintenance.Site", on_delete=models.PROTECT, related_name="inventory_items")
    quantity_available = models.DecimalField(max_digits=14, decimal_places=3, default=Decimal("0"))
    quantity_reserved = models.DecimalField(max_digits=14, decimal_places=3, default=Decimal("0"))
    rack_location = models.CharField(max_length=64, blank=True)
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
    quantity_on_hand = models.DecimalField(max_digits=14, decimal_places=3, default=Decimal("0"))
    min_stock_level = models.DecimalField(max_digits=14, decimal_places=3, default=Decimal("0"))
    max_stock_level = models.DecimalField(max_digits=14, decimal_places=3, null=True, blank=True)
    is_repairable = models.BooleanField(default=False)
    avg_cost = models.DecimalField(max_digits=12, decimal_places=4, null=True, blank=True)
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
    """Parts issued against a work order (manager)."""

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
    issued_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="part_issues_issued",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def clean(self):
        if self.quantity <= 0:
            raise ValidationError("Quantity must be positive.")
