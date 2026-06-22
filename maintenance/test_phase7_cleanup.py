"""
Phase 7 — Final cleanup tests.

Covers:
- WorkOrderCost._auto_calculate uses issued_qty (not quantity).
- Over-allocated parts show "0 (over-allocated)" in the part request
  dropdown instead of a negative number.
"""
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import User
from inventory.models import (
    Inventory, PartIssueLine, SparePart, StockMovement,
)
from maintenance.models import (
    MaintenanceIssue, Site, WorkOrder, WorkOrderCost, Machine,
)


def _make_user(username, role):
    return User.objects.create_user(username=username, role=role)


def _make_site():
    return Site.objects.create(name="Phase7Site", code="P7S", is_default=True)


def _make_machine(code="P7M-1"):
    return Machine.objects.create(
        name=f"Test Machine {code}",
        asset_level=3, asset_code=code,
        qr_code=f"qr-{code}-{timezone.now().timestamp()}",
    )


def _make_wo(machine, technician, manager):
    return WorkOrder.objects.create(
        machine=machine,
        lifecycle_status=WorkOrder.LifecycleStatus.IN_PROGRESS,
        assigned_technician=technician,
        created_by=manager,
    )


def _make_part(sku="P7-P1", name="P7 Part"):
    return SparePart.objects.create(
        sku=sku, name=name, is_consumable=False,
    )


# -------- _auto_calculate uses issued_qty --------

class AutoCalculateUsesIssuedQtyTests(TestCase):
    """WorkOrderCost._auto_calculate should aggregate issued_qty, not
    quantity. The old code summed the REQUESTED amount which overstated
    the cost by the unfulfilled shortage."""

    def setUp(self):
        self.manager = _make_user("mgr", User.Role.MANAGER)
        self.tech = _make_user("tech", User.Role.TECHNICIAN)
        self.machine = _make_machine("AC-1")
        self.part = _make_part(sku="AC-1", name="AC Part")
        self.wo = _make_wo(self.machine, self.tech, self.manager)

    def test_auto_calculate_uses_issued_qty(self):
        # PartIssueLine with quantity=10, issued_qty=3, unit_cost=20
        # Old (buggy) sum would be 10 * 20 = 200
        # New (correct) sum is 3 * 20 = 60
        PartIssueLine.objects.create(
            work_order=self.wo, part=self.part,
            quantity=Decimal("10"),
            issued_qty=Decimal("3"),
            unit_cost=Decimal("20"),
            status=PartIssueLine.Status.APPROVED,
            issued_by=self.manager,
        )
        cost = WorkOrderCost.objects.create(work_order=self.wo)
        self.assertEqual(cost.material_cost, Decimal("60"))

    def test_auto_calculate_zero_when_issued_qty_is_zero(self):
        # A line with quantity=5 but issued_qty=0 (legacy / pending
        # data) contributes 0 to material cost. This is a deliberate
        # change: pre-Phase 7, _auto_calculate summed `quantity` and
        # overstated the cost by the unfulfilled amount. We now sum
        # `issued_qty` only, which means a legacy line with no
        # tracking produces 0 until it's backfilled. The ledger is
        # the source of truth (recalculate_from_ledger).
        PartIssueLine.objects.create(
            work_order=self.wo, part=self.part,
            quantity=Decimal("5"),
            issued_qty=Decimal("0"),
            unit_cost=Decimal("10"),
            status=PartIssueLine.Status.PENDING,
            issued_by=self.manager,
        )
        cost = WorkOrderCost.objects.create(work_order=self.wo)
        self.assertEqual(cost.material_cost, Decimal("0"))

    def test_auto_calculate_zero_with_no_lines(self):
        cost = WorkOrderCost.objects.create(work_order=self.wo)
        self.assertEqual(cost.material_cost, Decimal("0"))


# -------- Inventory badge UX: over-allocated --------

class InventoryBadgeOverAllocatedTests(TestCase):
    """When reservations exceed available stock, free_stock goes
    negative. The part request dropdown should display
    "0 (over-allocated)" instead of a raw negative number."""

    def setUp(self):
        self.manager = _make_user("mgr", User.Role.MANAGER)
        self.tech = _make_user("tech", User.Role.TECHNICIAN)
        self.machine = _make_machine("OB-1")
        self.part = _make_part(sku="OB-1", name="Over-allocated Part")
        # Use the existing default site (the view annotates free_stock
        # only for the default site). 5 in stock, 10 reserved → -5.
        self.site = Site.objects.filter(is_default=True).first()
        if not self.site:
            self.site = Site.objects.create(
                name="OB-Site", code="OBS", is_default=True,
            )
        # Mark any pre-existing default site as non-default so we are unique
        Site.objects.filter(is_default=True).exclude(pk=self.site.pk).update(
            is_default=False,
        )
        Inventory.objects.create(
            part=self.part, site=self.site,
            quantity_available=Decimal("5"),
            quantity_reserved=Decimal("10"),
        )
        issue = MaintenanceIssue.objects.create(
            description="x", machine=self.machine, reported_by=self.tech,
        )
        issue.validated_by = self.manager
        issue.save()
        self.wo = WorkOrder.objects.create(
            machine=self.machine,
            lifecycle_status=WorkOrder.LifecycleStatus.IN_PROGRESS,
            assigned_technician=self.tech,
            created_by=self.manager,
            issue=issue,
        )

    def test_part_dropdown_shows_over_allocated_text(self):
        self.client.force_login(self.tech)
        response = self.client.get(reverse("work_order_detail", args=[self.wo.pk]))
        self.assertEqual(response.status_code, 200)
        # The part's option text should say "0 (over-allocated)" instead
        # of the raw -5 value.
        body = response.content.decode()
        self.assertIn("0 (over-allocated)", body)
        self.assertNotIn("— In stock: -5", body)
        self.assertNotIn("— In stock: -10", body)

    def test_part_dropdown_shows_positive_stock_normally(self):
        # Add a second part with positive stock and verify it shows normally
        positive = _make_part(sku="POS-1", name="Positive Part")
        Inventory.objects.create(
            part=positive, site=self.site,
            quantity_available=Decimal("15"),
            quantity_reserved=Decimal("3"),
        )
        # free_stock = 15 - 3 = 12
        self.client.force_login(self.tech)
        response = self.client.get(reverse("work_order_detail", args=[self.wo.pk]))
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        # free_stock = 15 - 3 = 12 → shown as "12"
        self.assertIn("Positive Part (POS-1) — In stock: 12", body)
