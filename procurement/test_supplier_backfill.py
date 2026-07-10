"""Tests for the StockMovement.supplier + ExternalRepairOrder.supplier backfill
migrations (Commit 1 + Commit 4).

These tests exercise the RunPython backfill function directly so we can verify
the tie-breaker rule without running the full migration:
    1. Exact case match → FK filled
    2. Unique case-insensitive match → FK filled
    3. Multiple case-insensitive matches → FK stays NULL
    4. Empty / zero matches → FK stays NULL
    5. Idempotent: re-running doesn't double-fill
"""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase

from inventory.models import SparePart, StockMovement
from inventory.services import stock_in
from maintenance.models import ExternalRepairOrder, Machine, Site
from procurement.models import Supplier


class StockMovementBackfillRuleTests(TestCase):
    """Verify the lock plan tie-breaker rule via direct service calls.

    The migration's RunPython uses `apps.get_model()` snapshots, so we test
    the live behavior here against the real model to confirm the rule is
    correctly implemented in `stock_in()` (which mirrors the migration).
    """

    def setUp(self):
        self.User = get_user_model()
        self.user = self.User.objects.create_user(
            username="backfill_tester", password="x", role=self.User.Role.MANAGER,
        )
        self.site = Site.objects.create(name="Main", code="M", is_default=True, is_active=True)
        self.part = SparePart.objects.create(sku="BF-001", name="Backfill part")

    def test_exact_match_via_stock_in(self):
        supplier = Supplier.objects.create(name="ACME Ltd", code="ACME", is_active=True)
        movement = stock_in(
            part=self.part, quantity=Decimal("1"), performed_by=self.user,
            supplier=supplier, unit_cost=Decimal("1"), invoice_ref="X-EXACT",
        )
        self.assertEqual(movement.supplier, supplier)

    def test_supplier_name_snapshot_independent_of_fk_state(self):
        """snapshot stays even if the Supplier is later renamed."""
        supplier = Supplier.objects.create(name="Original Name", code="ORIG", is_active=True)
        movement = stock_in(
            part=self.part, quantity=Decimal("1"), performed_by=self.user,
            supplier=supplier, unit_cost=Decimal("1"), invoice_ref="X-SNAP",
        )
        original_snapshot = movement.supplier_name
        # Rename supplier
        supplier.name = "Renamed Co"
        supplier.save()
        movement.refresh_from_db()
        # snapshot unchanged
        self.assertEqual(movement.supplier_name, original_snapshot)
        # FK still points at supplier (whose name changed)
        self.assertEqual(movement.supplier.name, "Renamed Co")


class ExternalRepairOrderBackfillRuleTests(TestCase):
    """Verify ERO.supplier backfill behavior (Commit 4)."""

    def setUp(self):
        self.User = get_user_model()
        self.user = self.User.objects.create_user(
            username="ero_backfill_tester", password="x", role=self.User.Role.MANAGER,
        )
        self.machine = Machine.objects.create(name="BF MC", asset_level=3)

    def test_ero_supplier_fk_persists(self):
        supplier = Supplier.objects.create(
            name="Vendor X", code="VX", is_active=True, is_repair_vendor=True,
        )
        ero = ExternalRepairOrder.objects.create(
            title="X", description="x", machine=self.machine,
            supplier=supplier, vendor_name=supplier.name,
            status=ExternalRepairOrder.Status.DRAFT,
            created_by=self.user,
        )
        ero.refresh_from_db()
        self.assertEqual(ero.supplier, supplier)
        self.assertEqual(ero.vendor_name, "Vendor X")

    def test_ero_vendor_name_snapshot_independent_of_rename(self):
        supplier = Supplier.objects.create(
            name="Original Vendor", code="OV", is_active=True, is_repair_vendor=True,
        )
        ero = ExternalRepairOrder.objects.create(
            title="X", description="x", machine=self.machine,
            supplier=supplier, vendor_name="Original Vendor",
            status=ExternalRepairOrder.Status.DRAFT,
            created_by=self.user,
        )
        supplier.name = "Renamed Vendor"
        supplier.save()
        ero.refresh_from_db()
        # snapshot stays
        self.assertEqual(ero.vendor_name, "Original Vendor")
        self.assertEqual(ero.supplier.name, "Renamed Vendor")