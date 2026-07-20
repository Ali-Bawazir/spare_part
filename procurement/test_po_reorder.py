"""
Phase 8 — Purchase Order reorder regression tests.
"""
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse

from accounts.models import User
from inventory.models import SparePart
from maintenance.models import AuditEntry, Tool
from procurement.models import PurchaseOrder, PurchaseOrderItem, Supplier
from procurement.services import PurchaseOrderService


class PurchaseOrderReorderTests(TestCase):
    """Reorder service + view: clones supplier + lines, generates new number,
    writes audit, blocks reorder of active POs."""

    def setUp(self):
        self.supplier = Supplier.objects.create(name="Old Steel Co.", is_active=True, code="OSC")
        self.part1 = SparePart.objects.create(sku="BRG-001", name="Bearing 6201")
        self.part2 = SparePart.objects.create(sku="BLT-001", name="Belt drive")
        self.manager = User.objects.create_user(
            username="mgr", password="pass1234", role=User.Role.MANAGER,
        )
        self.procurement = User.objects.create_user(
            username="proc", password="pass1234", role=User.Role.PROCUREMENT,
        )

    def _make_po(self, status):
        po = PurchaseOrder(
            supplier=self.supplier,
            status=status,
            created_by=self.manager,
        )
        po.save()  # auto po_number
        PurchaseOrderItem.objects.create(
            purchase_order=po, part=self.part1, ordered_qty=10,
            negotiated_unit_price=Decimal("12.50"),
        )
        PurchaseOrderItem.objects.create(
            purchase_order=po, part=self.part2, ordered_qty=5,
            negotiated_unit_price=Decimal("20.00"),
        )
        return po

    def test_reorder_copies_lines_and_supplier_and_generates_new_number(self):
        source = self._make_po(PurchaseOrder.Status.RECEIVED)
        # Set actual_unit_price on one line to test the cloning logic.
        source.items.filter(part=self.part1).update(actual_unit_price=Decimal("13.25"))
        new_po = PurchaseOrderService.reorder(source_po=source, created_by=self.manager)
        self.assertNotEqual(new_po.po_number, source.po_number)
        self.assertEqual(new_po.supplier, source.supplier)
        self.assertEqual(new_po.reorder_source, source)
        self.assertEqual(new_po.status, PurchaseOrder.Status.DRAFT)
        self.assertEqual(new_po.items.count(), 2)
        line = new_po.items.get(part=self.part1)
        self.assertEqual(line.negotiated_unit_price, Decimal("13.25"))
        self.assertEqual(line.ordered_qty, 10)

    def test_reorder_writes_audit_log(self):
        source = self._make_po(PurchaseOrder.Status.RECEIVED)
        AuditEntry.objects.all().delete()
        new_po = PurchaseOrderService.reorder(source_po=source, created_by=self.manager)
        self.assertTrue(
            AuditEntry.objects.filter(action="po_reorder_created").exists(),
            "Reorder must write an AuditEntry with action='po_reorder_created'.",
        )

    def test_reorder_active_po_blocked(self):
        source = self._make_po(PurchaseOrder.Status.SENT)
        with self.assertRaises(ValueError):
            PurchaseOrderService.reorder(source_po=source, created_by=self.manager)

    def test_reorder_cancelled_po_allowed(self):
        source = self._make_po(PurchaseOrder.Status.CANCELLED)
        new_po = PurchaseOrderService.reorder(source_po=source, created_by=self.manager)
        self.assertEqual(new_po.supplier, self.supplier)
        self.assertEqual(new_po.reorder_source, source)

    def test_reorder_closed_short_po_allowed(self):
        source = self._make_po(PurchaseOrder.Status.CLOSED_SHORT)
        new_po = PurchaseOrderService.reorder(source_po=source, created_by=self.manager)
        self.assertEqual(new_po.items.count(), 2)

    def test_reorder_view_creates_new_po(self):
        """POST /procurement/purchase-orders/<id>/reorder/ -> new PO detail redirect."""
        source = self._make_po(PurchaseOrder.Status.RECEIVED)
        self.client.force_login(self.procurement)
        resp = self.client.post(reverse("purchase_order_reorder", kwargs={"pk": source.pk}))
        self.assertEqual(resp.status_code, 302)
        # New PO exists, number differs.
        self.assertEqual(PurchaseOrder.objects.count(), 2)
        new_po = PurchaseOrder.objects.exclude(pk=source.pk).first()
        self.assertEqual(new_po.reorder_source, source)

    def test_reorder_view_get_only_redirects(self):
        source = self._make_po(PurchaseOrder.Status.RECEIVED)
        self.client.force_login(self.procurement)
        resp = self.client.get(reverse("purchase_order_reorder", kwargs={"pk": source.pk}))
        self.assertEqual(resp.status_code, 302)
        # No new PO created.
        self.assertEqual(PurchaseOrder.objects.count(), 1)

    def test_reorder_view_blocks_active_po(self):
        source = self._make_po(PurchaseOrder.Status.SENT)
        self.client.force_login(self.procurement)
        resp = self.client.post(reverse("purchase_order_reorder", kwargs={"pk": source.pk}))
        self.assertEqual(resp.status_code, 302)
        # No new PO created (rejected).
        self.assertEqual(PurchaseOrder.objects.count(), 1)


class PurchaseOrderCreatePrefillSupplierTests(TestCase):
    """/procurement/purchase-orders/new/?supplier=<id> pre-selects the dropdown."""

    def setUp(self):
        self.supplier = Supplier.objects.create(name="Acme", is_active=True)
        self.procurement = User.objects.create_user(
            username="proc", password="pass1234", role=User.Role.PROCUREMENT,
        )

    def test_supplier_query_param_prefills(self):
        self.client.force_login(self.procurement)
        resp = self.client.get(reverse("purchase_order_create") + f"?supplier={self.supplier.pk}")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["form"].initial.get("supplier"), self.supplier.pk)


class PurchaseOrderCreateToolPrefillTests(TestCase):
    """/procurement/purchase-orders/new/?tool_id=<id> pre-populates one PO line
    with the tool, qty=1, the tool's purchase_cost as unit price, and a
    Replacement line_note. POSTing the form saves a PO whose only line
    references that tool."""

    def setUp(self):
        self.supplier = Supplier.objects.create(name="Tools Co.", is_active=True, code="TOOLCO")
        self.procurement = User.objects.create_user(
            username="proc", password="pass1234", role=User.Role.PROCUREMENT,
        )
        self.tool = Tool.objects.create(
            code="WR-99", name="Torque Wrench", supplier=self.supplier,
            purchase_cost=Decimal("450.00"),
        )

    def test_reorder_damaged_tool_prepopulates_line(self):
        """GET pre-populates the first formset row with the tool."""
        self.client.force_login(self.procurement)
        url = (
            reverse("purchase_order_create")
            + f"?supplier={self.supplier.pk}&tool_id={self.tool.pk}"
        )
        # GET: confirm the pre-fill is visible in the formset's initial.
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        formset = resp.context["formset"]
        first_form = formset.forms[0]
        self.assertEqual(first_form.initial.get("tool"), self.tool.pk)
        self.assertEqual(first_form.initial.get("ordered_qty"), "1")
        self.assertEqual(first_form.initial.get("negotiated_unit_price"), "450.00")
        self.assertIn(self.tool.name, first_form.initial.get("line_note", ""))
        self.assertIn(self.tool.code, first_form.initial.get("line_note", ""))

        # POST: submit the form with the pre-filled row and verify the
        # saved PurchaseOrderItem references the tool (not a part).
        post_data = {
            "supplier": self.supplier.pk,
            "status": "draft",
            "notes": "",
            "items-TOTAL_FORMS": "1",
            "items-INITIAL_FORMS": "0",
            "items-MIN_NUM_FORMS": "0",
            "items-MAX_NUM_FORMS": "1000",
            "items-0-tool": str(self.tool.pk),
            "items-0-part": "",
            "items-0-ordered_qty": "1",
            "items-0-negotiated_unit_price": "450.00",
            "items-0-line_note": f"Replacement for damaged tool: {self.tool.name} ({self.tool.code})",
        }
        resp = self.client.post(url, data=post_data)
        self.assertEqual(resp.status_code, 302)
        po = PurchaseOrder.objects.latest("created_at")
        self.assertEqual(po.supplier, self.supplier)
        self.assertEqual(po.items.count(), 1)
        line = po.items.first()
        self.assertEqual(line.tool_id, self.tool.pk)
        self.assertIsNone(line.part_id)
        self.assertIn(self.tool.name, line.line_note)
