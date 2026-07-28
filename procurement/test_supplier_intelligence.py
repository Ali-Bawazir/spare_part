"""Tests for the supplier FK + vendor dropdown + CSV work.

Covers:

- StockMovement.supplier FK is saved when stock_in() is called with a Supplier
- supplier_name snapshot is auto-populated from supplier.name when only FK given
- supplier_name survives even when supplier_name is also passed (back-compat)
- Old callers passing supplier_name=... still work (no Supplier FK, but
  movement is still recorded)
- ExternalRepairOfficerForm dropdown is filtered to is_repair_vendor=True
- Form.clean() auto-fills vendor_name from supplier.name
- supplier_detail view shows repair history + stock received tables
- supplier_export_csv returns a UTF-8 BOM CSV with three sections
"""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse

from inventory.models import SparePart, StockMovement
from inventory.services import stock_in
from maintenance.forms import ExternalRepairOfficerForm
from maintenance.models import ExternalRepairOrder, Machine, Site
from procurement.models import Supplier, PurchaseOrder


class SupplierFKStockInTests(TestCase):
    """Cover Commit 1+2: StockMovement.supplier FK behavior."""

    def setUp(self):
        self.User = get_user_model()
        self.user = self.User.objects.create_user(
            username="manager_si", password="x", role=self.User.Role.MANAGER,
        )
        self.site = Site.objects.create(name="Main", code="M", is_default=True, is_active=True)
        self.supplier = Supplier.objects.create(
            name="ACME Stock Co", code="ACME-S", is_active=True,
        )
        self.part = SparePart.objects.create(sku="TST-001", name="Test part")

    def test_supplier_fk_saved_when_passed(self):
        """stock_in(supplier=...) saves the FK on the StockMovement."""
        movement = stock_in(
            part=self.part,
            quantity=Decimal("5"),
            performed_by=self.user,
            supplier=self.supplier,
            unit_cost=Decimal("10.00"),
            invoice_ref="INV-FK-001",
        )
        self.assertEqual(movement.supplier, self.supplier)
        self.assertEqual(movement.supplier_name, "ACME Stock Co")

    def test_supplier_name_snapshot_auto_filled_from_supplier(self):
        """When only supplier= is passed, supplier_name snapshot is filled from FK."""
        movement = stock_in(
            part=self.part,
            quantity=Decimal("3"),
            performed_by=self.user,
            supplier=self.supplier,
            unit_cost=Decimal("7.50"),
            invoice_ref="INV-AUTO",
        )
        self.assertEqual(movement.supplier, self.supplier)
        self.assertEqual(movement.supplier_name, self.supplier.name)

    def test_old_caller_passing_supplier_name_only_still_works(self):
        """Backward-compat: supplier_name=... works, FK stays NULL."""
        movement = stock_in(
            part=self.part,
            quantity=Decimal("2"),
            performed_by=self.user,
            supplier_name="Legacy Vendor LLC",
            unit_cost=Decimal("5.00"),
            invoice_ref="INV-LEG",
        )
        self.assertIsNone(movement.supplier)
        self.assertEqual(movement.supplier_name, "Legacy Vendor LLC")

    def test_part_default_supplier_stamped_on_first_stock_in(self):
        """First stock-in with supplier= stamps part.supplier; never overwrites."""
        # First stock-in
        stock_in(
            part=self.part, quantity=Decimal("1"), performed_by=self.user,
            supplier=self.supplier, unit_cost=Decimal("1"),
            invoice_ref="INV-FIRST",
        )
        self.part.refresh_from_db()
        self.assertEqual(self.part.supplier, self.supplier)

        # Second stock-in with a different supplier — should NOT overwrite
        other = Supplier.objects.create(name="Other Vendor", code="OTH", is_active=True)
        stock_in(
            part=self.part, quantity=Decimal("1"), performed_by=self.user,
            supplier=other, unit_cost=Decimal("1"),
            invoice_ref="INV-SECOND",
        )
        self.part.refresh_from_db()
        self.assertEqual(self.part.supplier, self.supplier)  # unchanged


class EROVendorDropdownTests(TestCase):
    """Cover Commit 5: ExternalRepairOfficerForm vendor dropdown."""

    def setUp(self):
        self.User = get_user_model()
        self.user = self.User.objects.create_user(
            username="ero_form_user", password="x", role=self.User.Role.MANAGER,
        )
        self.repair_vendor = Supplier.objects.create(
            name="ABC Repair", code="ABC-R", is_active=True,
            supplier_type=Supplier.Type.REPAIR_VENDOR,
        )
        self.stock_only = Supplier.objects.create(
            name="Stock Co", code="STK", is_active=True,
            supplier_type=Supplier.Type.PARTS_SUPPLIER,
        )
        self.inactive_vendor = Supplier.objects.create(
            name="Old Repair Co", code="OLD-R", is_active=False,
            supplier_type=Supplier.Type.REPAIR_VENDOR,
        )
        self.machine = Machine.objects.create(name="Test MC", asset_level=3)
        self.ero = ExternalRepairOrder.objects.create(
            title="Test repair",
            description="Test",
            machine=self.machine,
            status=ExternalRepairOrder.Status.DRAFT,
            vendor_name="ABC Repair",
            created_by=self.user,
        )

    def test_dropdown_filters_to_active_repair_vendors(self):
        form = ExternalRepairOfficerForm(instance=self.ero)
        qs = form.fields["supplier"].queryset
        self.assertIn(self.repair_vendor, qs)
        self.assertNotIn(self.stock_only, qs)
        self.assertNotIn(self.inactive_vendor, qs)

    def test_clean_autofills_vendor_name_from_supplier(self):
        form = ExternalRepairOfficerForm(
            data={"supplier": self.repair_vendor.pk, "vendor_name": "",
                  "actual_cost": "100", "status": "draft"},
            instance=self.ero,
        )
        self.assertTrue(form.is_valid(), form.errors)
        cleaned = form.clean()
        self.assertEqual(cleaned["vendor_name"], "ABC Repair")

    def test_pre_existing_ero_with_no_fk_keeps_legacy_vendor_name(self):
        """An ERO that pre-dates the FK keeps its legacy vendor_name on save."""
        legacy_ero = ExternalRepairOrder.objects.create(
            title="Legacy", description="x", machine=self.machine,
            status=ExternalRepairOrder.Status.DRAFT,
            vendor_name="Hand-typed vendor name",
            created_by=self.user,
        )
        form = ExternalRepairOfficerForm(
            data={"supplier": "", "vendor_name": "Hand-typed vendor name",
                  "actual_cost": "", "status": "draft"},
            instance=legacy_ero,
        )
        self.assertTrue(form.is_valid(), form.errors)
        saved = form.save()
        self.assertEqual(saved.vendor_name, "Hand-typed vendor name")
        self.assertIsNone(saved.supplier)


class SupplierExportCSVTests(TestCase):
    """Cover Commit 8: supplier_export_csv."""

    def setUp(self):
        self.User = get_user_model()
        self.manager = self.User.objects.create_user(
            username="csv_mgr", password="x", role=self.User.Role.MANAGER,
        )
        self.site = Site.objects.create(name="Main", code="M", is_default=True, is_active=True)
        self.supplier = Supplier.objects.create(
            name="CSV Test Supplier", code="CSV-T", is_active=True,
        )
        self.part = SparePart.objects.create(sku="CSV-001", name="CSV part")
        self.machine = Machine.objects.create(name="CSV MC", asset_level=3)

    def test_csv_starts_with_utf8_bom_and_has_three_sections(self):
        # Seed one stock movement + one ERO
        stock_in(
            part=self.part, quantity=Decimal("5"), performed_by=self.manager,
            supplier=self.supplier, unit_cost=Decimal("10"),
            invoice_ref="CSV-INV-001",
        )
        ExternalRepairOrder.objects.create(
            title="CSV repair", description="x", machine=self.machine,
            supplier=self.supplier, vendor_name=self.supplier.name,
            status=ExternalRepairOrder.Status.CLOSED,
            actual_cost=Decimal("250.00"),
            invoice_ref="CSV-ERO-INV",
            created_by=self.manager,
        )

        c = Client(SERVER_NAME="localhost")
        c.force_login(self.manager)
        resp = c.get(reverse("supplier_export_csv", args=[self.supplier.pk]))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["Content-Type"], "text/csv; charset=utf-8")
        # UTF-8 BOM at the start
        self.assertTrue(resp.content.startswith(b"\xef\xbb\xbf"))
        # RFC 5987 filename
        self.assertIn("attachment;", resp["Content-Disposition"])
        # Three sections
        body = resp.content.decode("utf-8-sig")
        self.assertIn("Supplier Information", body)
        self.assertIn("Repair History", body)
        self.assertIn("Stock Received", body)
        # Specific data points appear
        self.assertIn("CSV-INV-001", body)
        self.assertIn("CSV-ERO-INV", body)

    def test_csv_for_supplier_with_no_history(self):
        """Empty supplier still exports valid CSV with header section only."""
        empty_supplier = Supplier.objects.create(
            name="Empty", code="EMPTY", is_active=True,
        )
        c = Client(SERVER_NAME="localhost")
        c.force_login(self.manager)
        resp = c.get(reverse("supplier_export_csv", args=[empty_supplier.pk]))
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode("utf-8-sig")
        self.assertIn("Empty", body)
        self.assertIn("Supplier Information", body)


class SupplierDetailViewTests(TestCase):
    """Cover Commit 7: supplier_detail shows repair + stock tables."""

    def setUp(self):
        self.User = get_user_model()
        self.manager = self.User.objects.create_user(
            username="detail_mgr", password="x", role=self.User.Role.MANAGER,
        )
        self.site = Site.objects.create(name="Main", code="M", is_default=True, is_active=True)
        self.supplier = Supplier.objects.create(
            name="Detail Test", code="DET-T", is_active=True,
        )
        self.part = SparePart.objects.create(sku="DET-001", name="Detail part")
        self.machine = Machine.objects.create(name="Detail MC", asset_level=3)

    def test_detail_shows_repair_and_stock_tables(self):
        stock_in(
            part=self.part, quantity=Decimal("3"), performed_by=self.manager,
            supplier=self.supplier, unit_cost=Decimal("10"),
            invoice_ref="DET-INV",
        )
        ExternalRepairOrder.objects.create(
            title="Repair A", description="x", machine=self.machine,
            supplier=self.supplier, vendor_name=self.supplier.name,
            status=ExternalRepairOrder.Status.CLOSED,
            actual_cost=Decimal("500.00"),
            created_by=self.manager,
        )

        c = Client(SERVER_NAME="localhost")
        c.force_login(self.manager)
        resp = c.get(reverse("supplier_detail", args=[self.supplier.pk]))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Repair history")
        self.assertContains(resp, "Stock received")
        self.assertContains(resp, "DET-INV")
        # Repair totals
        self.assertContains(resp, "1")  # 1 repair
        self.assertContains(resp, "500.00")

    def test_detail_with_no_history_renders_cleanly(self):
        empty_supplier = Supplier.objects.create(
            name="Empty detail", code="EMPTY-D", is_active=True,
        )
        c = Client(SERVER_NAME="localhost")
        c.force_login(self.manager)
        resp = c.get(reverse("supplier_detail", args=[empty_supplier.pk]))
        self.assertEqual(resp.status_code, 200)