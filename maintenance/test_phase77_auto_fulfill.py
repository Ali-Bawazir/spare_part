"""
Phase 7.7 — PO auto-fulfillment tests.

When a PO with linked PR(s) attached to a specific WO is received, the
receive flow auto-calls `execute_warehouse_issue` for matching open
PartIssueLines on that WO. This closes the loop between supplier
delivery and WO consumption so the user no longer has to click
"📤 Issue N from stock" manually after every receive.

Covers:
- Auto-fulfillment is ON by default (settings.PO_AUTO_ISSUE).
- Receiving a PO with a linked PR auto-issues matching stock to the WO.
- Stock-only PRs (work_order_id IS NULL) are NOT auto-issued.
- The settings.PO_AUTO_ISSUE flag can disable the behavior.
- Auto-fulfillment posts the cost to the WO's cost ledger.
"""
from decimal import Decimal

from django.test import TestCase, override_settings
from django.urls import reverse

from accounts.models import User
from inventory.models import (
    Inventory, PartIssueLine, SparePart, StockMovement,
)
from inventory.services import auto_fulfill_wo_lines_from_po
from maintenance.models import (
    CostTransaction, Site, WorkOrder, WorkOrderCost, Machine,
)
from procurement.models import (
    PurchaseOrder, PurchaseOrderItem, PurchaseRequest, Supplier,
)


def _make_user(username, role):
    return User.objects.create_user(
        username=username, password="test1234", role=role,
    )


def _make_machine(name="Test Press"):
    site = Site.objects.filter(is_default=True).first() or Site.objects.create(
        name="Test Site", is_default=True, is_active=True,
    )
    return site, Machine.objects.create(
        name=name, qr_code=f"QR-{name}", asset_level=3,
        asset_code=f"CODE-{name}", is_active=True, site=site,
    )


def _make_part(sku, name="Test Part", cost=Decimal("10.00")):
    p = SparePart.objects.create(
        sku=sku, name=name, status="active",
        avg_cost=cost, last_purchase_cost=cost,
        allow_operator_consumption=False, is_consumable=False,
    )
    Inventory.objects.create(
        part=p, site=Site.objects.get(is_default=True),
        quantity_available=Decimal("0"),
    )
    return p


def _make_wo(machine, technician, manager):
    return WorkOrder.objects.create(
        machine=machine,
        lifecycle_status=WorkOrder.LifecycleStatus.IN_PROGRESS,
        assigned_technician=technician,
        created_by=manager,
    )


def _create_open_line(wo, part, qty, requested_by, approved_by, status="allocated"):
    """Create a PartIssueLine in the chosen status with the given qty.

    approved_qty is set equal to qty; allocated_qty mirrors approved_qty for
    the 'allocated' state (matching the production flow's allocation step)."""
    return PartIssueLine.objects.create(
        work_order=wo, part=part,
        quantity=qty, requested_qty=qty,
        approved_qty=qty if status != "pending" else Decimal("0"),
        allocated_qty=qty if status == "allocated" else Decimal("0"),
        issued_qty=Decimal("0"),
        unit_cost=Decimal("10.00"),
        status=status,
        requested_by=requested_by,
        approved_by=approved_by,
        issued_by=approved_by,
    )


def _create_po(supplier, prs, part, qty, manager, unit_price=Decimal("12.00")):
    po = PurchaseOrder.objects.create(
        supplier=supplier, status=PurchaseOrder.Status.SENT,
        created_by=manager,
    )
    for pr in prs:
        po.purchase_requests.add(pr)
    PurchaseOrderItem.objects.create(
        purchase_order=po, part=part, ordered_qty=qty,
        negotiated_unit_price=unit_price,
    )
    return po


def _set_received(po, good_qty):
    """Simulate the receive-flow side effects without going through the
    full view: mark each line item as received, add stock, transition
    PRs to FULFILLED, and call auto-fulfill."""
    for item in po.items.all():
        item.received_qty = good_qty
        item.save(update_fields=["received_qty"])
        inv = item.part.inventory_items.first()
        inv.quantity_available += good_qty
        inv.save()
    if all(item.received_qty >= item.ordered_qty for item in po.items.all()):
        po.status = PurchaseOrder.Status.RECEIVED
    else:
        po.status = PurchaseOrder.Status.PARTIAL_RECEIVED
    po.save()
    for pr in po.purchase_requests.all():
        pr.status = (
            PurchaseRequest.Status.FULFILLED
            if po.status == PurchaseOrder.Status.RECEIVED
            else PurchaseRequest.Status.PARTIALLY_FULFILLED
        )
        pr.save()
    return po


class AutoFulfillmentTests(TestCase):
    """Tests for the Phase 7.7 PO auto-fulfillment flow."""

    @classmethod
    def setUpTestData(cls):
        cls.tech = _make_user("auto_tech", User.Role.TECHNICIAN)
        cls.mgr = _make_user("auto_mgr", User.Role.MANAGER)

    def setUp(self):
        self.site, self.machine = _make_machine("AutoTestPress")
        self.part = _make_part("AUTO-TEST-01", "Auto Test Part", Decimal("12.00"))
        self.wo = _make_wo(self.machine, self.tech, self.mgr)
        self.supplier = Supplier.objects.create(name="Auto Test Supplier")

    def test_auto_fulfill_fires_on_receive(self):
        """End-to-end: PR linked to WO → PO → receive → line auto-issued."""
        # Match the line's unit_cost to the PO's negotiated_unit_price
        # so the cost ledger math is straightforward.
        line = _create_open_line(self.wo, self.part, 5, self.tech, self.mgr, status="allocated")
        line.unit_cost = Decimal("12.00")
        line.save(update_fields=["unit_cost"])
        pr = PurchaseRequest.objects.create(
            part=self.part, quantity=5, work_order=self.wo,
            machine=self.machine, created_by=self.mgr,
            supplier=self.supplier, status=PurchaseRequest.Status.PENDING,
        )
        self.assertEqual(line.issued_qty, 0)

        po = _create_po(self.supplier, [pr], self.part, 5, self.mgr)
        _set_received(po, good_qty=5)
        # The receive flow then calls auto-fulfill — we replicate that call
        summary = auto_fulfill_wo_lines_from_po(po=po, actor=self.mgr)

        line.refresh_from_db()
        self.assertEqual(line.issued_qty, 5,
                         f"line should be auto-issued 5, got {line.issued_qty}")
        self.assertEqual(line.issued_qty, line.approved_qty)

        inv = self.part.inventory_items.first()
        self.assertEqual(inv.quantity_available, 0,
                         f"inv should be 0 after issue, got {inv.quantity_available}")

        # Cost should be posted to WO ledger (5 × 12 = 60)
        ct_total = sum(
            CostTransaction.objects.filter(
                work_order=self.wo, category="material"
            ).values_list("amount", flat=True)
        )
        self.assertEqual(ct_total, Decimal("60.00"),
                         f"cost ledger should have 60, got {ct_total}")
        # At least one auto_issued action should be in the summary
        issued = [a for a in summary["actions"] if a.get("type") == "auto_issued"]
        self.assertEqual(len(issued), 1, f"expected 1 auto-issued, got {issued}")

    def test_auto_fulfill_skipped_for_stock_only_pr(self):
        """Stock-only PRs (work_order_id IS NULL) are NOT auto-issued."""
        pr = PurchaseRequest.objects.create(
            part=self.part, quantity=10, work_order=None,  # stock-only
            machine=self.machine, created_by=self.mgr,
            supplier=self.supplier, status=PurchaseRequest.Status.PENDING,
        )
        po = _create_po(self.supplier, [pr], self.part, 10, self.mgr)
        _set_received(po, good_qty=10)
        summary = auto_fulfill_wo_lines_from_po(po=po, actor=self.mgr)

        inv = self.part.inventory_items.first()
        self.assertEqual(inv.quantity_available, 10,
                         f"inv should be 10, got {inv.quantity_available}")
        # No auto-issued actions (no WO lines to match)
        self.assertEqual(summary["actions"], [])

    def test_auto_fulfill_partial_qty(self):
        """Partial receive: PO is partial, line is NOT auto-issued.

        The auto-fulfill guard checks `received_qty < ordered_qty` and
        skips lines that aren't fully received."""
        line = _create_open_line(self.wo, self.part, 10, self.tech, self.mgr, status="allocated")
        pr = PurchaseRequest.objects.create(
            part=self.part, quantity=10, work_order=self.wo,
            machine=self.machine, created_by=self.mgr,
            supplier=self.supplier, status=PurchaseRequest.Status.PENDING,
        )
        po = _create_po(self.supplier, [pr], self.part, 10, self.mgr)
        _set_received(po, good_qty=3)  # 3 of 10 → partial
        summary = auto_fulfill_wo_lines_from_po(po=po, actor=self.mgr)

        line.refresh_from_db()
        self.assertEqual(line.issued_qty, 0,
                         f"line should NOT be auto-issued on partial, got {line.issued_qty}")
        issued = [a for a in summary["actions"] if a.get("type") == "auto_issued"]
        self.assertEqual(issued, [])

    def test_auto_fulfill_respects_settings_toggle(self):
        """When settings.PO_AUTO_ISSUE is False, stock is added but
        no auto-issue happens."""
        line = _create_open_line(self.wo, self.part, 5, self.tech, self.mgr, status="allocated")
        pr = PurchaseRequest.objects.create(
            part=self.part, quantity=5, work_order=self.wo,
            machine=self.machine, created_by=self.mgr,
            supplier=self.supplier, status=PurchaseRequest.Status.PENDING,
        )
        po = _create_po(self.supplier, [pr], self.part, 5, self.mgr)
        _set_received(po, good_qty=5)
        with override_settings(PO_AUTO_ISSUE=False):
            summary = auto_fulfill_wo_lines_from_po(po=po, actor=self.mgr)

        line.refresh_from_db()
        self.assertEqual(line.issued_qty, 0,
                         f"line should NOT be auto-issued when toggle off, got {line.issued_qty}")
        inv = self.part.inventory_items.first()
        self.assertEqual(inv.quantity_available, 5,
                         f"stock should still be added, got {inv.quantity_available}")
        self.assertFalse(summary["enabled"], "summary should report enabled=False")

    def test_auto_fulfill_distributes_to_multiple_lines(self):
        """Multiple open lines for the same part on the same WO get
        auto-issued FIFO (oldest first) up to received qty."""
        line1 = _create_open_line(self.wo, self.part, 3, self.tech, self.mgr, status="allocated")
        line2 = _create_open_line(self.wo, self.part, 4, self.tech, self.mgr, status="allocated")
        self.assertGreater(line2.pk, line1.pk, "line2 should have higher pk")

        pr = PurchaseRequest.objects.create(
            part=self.part, quantity=7, work_order=self.wo,
            machine=self.machine, created_by=self.mgr,
            supplier=self.supplier, status=PurchaseRequest.Status.PENDING,
        )
        po = _create_po(self.supplier, [pr], self.part, 7, self.mgr)
        _set_received(po, good_qty=7)
        summary = auto_fulfill_wo_lines_from_po(po=po, actor=self.mgr)

        line1.refresh_from_db()
        line2.refresh_from_db()
        self.assertEqual(line1.issued_qty, 3, f"line1 (older) should be 3, got {line1.issued_qty}")
        self.assertEqual(line2.issued_qty, 4, f"line2 (newer) should be 4, got {line2.issued_qty}")

    def test_auto_fulfill_service_idempotent_with_no_linked_prs(self):
        """A PO with no linked PRs returns the no-op summary."""
        po = PurchaseOrder.objects.create(
            supplier=self.supplier, status=PurchaseOrder.Status.SENT,
            created_by=self.mgr,
        )
        PurchaseOrderItem.objects.create(
            purchase_order=po, part=self.part, ordered_qty=5,
            negotiated_unit_price=Decimal("10.00"),
        )
        summary = auto_fulfill_wo_lines_from_po(po=po, actor=self.mgr)
        self.assertEqual(summary["actions"], [])

    def test_auto_fulfill_creates_audit_trail(self):
        """Each auto-issue creates a part_auto_issued_from_po audit entry."""
        from maintenance.models import AuditEntry
        line = _create_open_line(self.wo, self.part, 3, self.tech, self.mgr, status="allocated")
        pr = PurchaseRequest.objects.create(
            part=self.part, quantity=3, work_order=self.wo,
            machine=self.machine, created_by=self.mgr,
            supplier=self.supplier, status=PurchaseRequest.Status.PENDING,
        )
        po = _create_po(self.supplier, [pr], self.part, 3, self.mgr)
        _set_received(po, good_qty=3)
        auto_fulfill_wo_lines_from_po(po=po, actor=self.mgr)

        audits = AuditEntry.objects.filter(
            action="part_auto_issued_from_po",
            object_id=str(line.pk),
        )
        self.assertTrue(audits.exists(),
                        f"audit trail missing for auto-issue of line {line.pk}")
        audit = audits.first()
        self.assertEqual(audit.payload["po_number"], po.po_number)
        self.assertEqual(audit.payload["qty"], "3.000")

    def test_auto_fulfill_resolves_part_blocker(self):
        """After auto-fulfillment, the PART WO Blocker for the line
        is auto-resolved (keystone rule: issued_qty >= approved_qty)."""
        from maintenance.models import WorkOrderBlocker
        from maintenance.services_blocker import WorkOrderBlockerService
        line = _create_open_line(self.wo, self.part, 5, self.tech, self.mgr, status="allocated")
        # Open a PART blocker for the line
        WorkOrderBlockerService.open_blocker(
            work_order=self.wo,
            kind=WorkOrderBlocker.Kind.PART,
            external_obj=line, opened_by=self.tech,
            note="waiting for part", external_label=f"{self.part.sku} x 5",
        )
        blocker = WorkOrderBlocker.objects.get(
            work_order=self.wo, content_type__isnull=False, object_id=line.pk,
            status=WorkOrderBlocker.Status.OPEN,
        )
        self.assertEqual(blocker.status, WorkOrderBlocker.Status.OPEN)

        pr = PurchaseRequest.objects.create(
            part=self.part, quantity=5, work_order=self.wo,
            machine=self.machine, created_by=self.mgr,
            supplier=self.supplier, status=PurchaseRequest.Status.PENDING,
        )
        po = _create_po(self.supplier, [pr], self.part, 5, self.mgr)
        _set_received(po, good_qty=5)
        auto_fulfill_wo_lines_from_po(po=po, actor=self.mgr)

        blocker.refresh_from_db()
        self.assertEqual(blocker.status, WorkOrderBlocker.Status.RESOLVED,
                         f"PART blocker should auto-resolve after full issue, got {blocker.status}")
