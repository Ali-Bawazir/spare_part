from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models


class SparePart(models.Model):
    sku = models.SlugField(max_length=64, unique=True)
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    is_consumable = models.BooleanField(default=False, db_index=True)
    quantity_on_hand = models.DecimalField(max_digits=14, decimal_places=3, default=Decimal("0"))
    min_stock_level = models.DecimalField(max_digits=14, decimal_places=3, default=Decimal("0"))
    is_repairable = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return f"{self.name} ({self.sku})"

    def is_low_stock(self) -> bool:
        return self.quantity_on_hand <= self.min_stock_level


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
    work_order = models.ForeignKey(
        "maintenance.WorkOrder",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
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
    note = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

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
