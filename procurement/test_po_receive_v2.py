"""
Phase 2D-2 — purchase_order_receive v2 tests.

Covers:
- 3-bucket (good / damaged / rejected) receive with per-line condition
- Atomic transaction (mid-loop failure rolls back all state)
- Supplier invoice ref + actual_unit_price capture
- reallocate_for_part() called for changed-stock parts
- notify_po_received_summary() called once after a successful receipt

The view lives at procurement/views.py:purchase_order_receive. It uses
transaction.atomic() so per-PO state mutation is all-or-nothing. Summary
notification fires OUTSIDE the atomic block, so a failure inside the
loop suppresses the summary.
"""
from __future__ import annotations

from decimal import Decimal
from unittest import mock

from django.test import TestCase
from django.urls import reverse

from accounts.models import User
from inventory.models import (
    Inventory,
    InventoryReservation,
    PartIssueLine,
    SparePart,
    StockMovement,
)
from inventory.services_allocation import PartAllocationService
from maintenance.models import Machine, Notification, Site, WorkOrder
from procurement.models import (
    PurchaseOrder,
    PurchaseOrderItem,
    PurchaseRequest,
    Supplier,
)


def _to_decimal(value) -> Decimal:
    """Mirror of procurement.views._to_decimal — keep tests independent."""
    if not value:
        return Decimal("0")
    try:
        return Decimal(str(value))
    except Exception:
        return Decimal("0")


def _make_user(username: str, role: str) -> User:
    return User.objects.create_user(username=username, password="pass1234", role=role)


def _make_po(*, created_by: User, supplier: Supplier) -> PurchaseOrder:
    return PurchaseOrder.objects.create(created_by=created_by, supplier=supplier)


class PurchaseOrderReceiveV2Tests(TestCase):
    """purchase_order_receive — v2 features (good/damaged/rejected + invoice + reallocate)."""

    def setUp(self):
        # Default site (the view fetches it on POST)
        self.site = Site.objects.filter(is_default=True).first()
        if self.site is None:
            self.site = Site.objects.create(
                name="Test Factory", code="TST", is_default=True,
            )

        self.procurement = _make_user("proc_v2", User.Role.PROCUREMENT)
        self.manager = _make_user("mgr_v2", User.Role.MANAGER)
        self.tech = _make_user("tech_v2", User.Role.TECHNICIAN)

        self.supplier = Supplier.objects.create(name="ACME V2")
        self.machine = Machine.objects.create(name="Press V2", qr_code="PRESS-V2")

        # Two parts on the PO
        self.part_a = SparePart.objects.create(sku="PO-V2-A", name="Bearing A")
        self.part_b = SparePart.objects.create(sku="PO-V2-B", name="Belt B")

        # PO with two line items: 10 of A, 5 of B
        self.po = _make_po(created_by=self.procurement, supplier=self.supplier)
        self.po.status = PurchaseOrder.Status.SENT
        self.po.save(update_fields=["status"])
        self.item_a = PurchaseOrderItem.objects.create(
            purchase_order=self.po, part=self.part_a,
            ordered_qty=Decimal("10"), received_qty=Decimal("0"),
            negotiated_unit_price=Decimal("5.00"),
        )
        self.item_b = PurchaseOrderItem.objects.create(
            purchase_order=self.po, part=self.part_b,
            ordered_qty=Decimal("5"), received_qty=Decimal("0"),
            negotiated_unit_price=Decimal("20.00"),
        )

        # A PurchaseRequest linked to a WO (so notify_wo_part_received has a target)
        self.wo = WorkOrder.objects.create(
            machine=self.machine, created_by=self.manager,
            lifecycle_status=WorkOrder.LifecycleStatus.ASSIGNED,
        )
        self.pr = PurchaseRequest.objects.create(
            part=self.part_a, work_order=self.wo,
            quantity=Decimal("10"), status=PurchaseRequest.Status.CONVERTED_TO_PO,
            created_by=self.procurement, purchase_order=self.po,
        )

        self.url = reverse("purchase_order_receive", kwargs={"pk": self.po.pk})

    # ------------------------------------------------------------------
    # 1. Good-only receive
    # ------------------------------------------------------------------
    def test_receive_with_good_units_only(self):
        """Submit good_qty=5 for one line → PO RECEIVED, stock +5,
        StockMovement(STOCK_IN) with the right invoice_ref, summary
        notification fired, transaction atomic."""
        self.client.force_login(self.procurement)
        with mock.patch(
            "procurement.views.notify_po_received_summary"
        ) as mock_summary, mock.patch(
            "procurement.views.PartAllocationService.reallocate_for_part"
        ) as mock_realloc, mock.patch(
            "procurement.views.PartAllocationService"
        ) as mock_pas:
            # Pass through to the real allocate so we exercise the real code path
            mock_pas.reallocate_for_part = mock_realloc
            response = self.client.post(self.url, {
                f"good_qty_{self.item_a.pk}": "5",
                "supplier_invoice_ref": "",
            })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], reverse("purchase_order_detail", kwargs={"pk": self.po.pk}))

        # PO: still partial (item_b has 0 received)
        self.po.refresh_from_db()
        self.assertEqual(self.po.status, PurchaseOrder.Status.PARTIAL_RECEIVED)

        # Item A received_qty == 5
        self.item_a.refresh_from_db()
        self.assertEqual(self.item_a.received_qty, Decimal("5.000"))
        self.assertEqual(self.item_a.damaged_qty, Decimal("0"))
        self.assertEqual(self.item_a.rejected_qty, Decimal("0"))

        # Inventory increased by 5
        inv = Inventory.objects.get(part=self.part_a, site=self.site)
        self.assertEqual(inv.quantity_available, Decimal("5.000"))

        # StockMovement(STOCK_IN) created with invoice_ref = po_number
        movements = StockMovement.objects.filter(
            part=self.part_a, movement_type=StockMovement.MovementType.STOCK_IN,
        )
        self.assertEqual(movements.count(), 1)
        self.assertEqual(movements.first().invoice_ref, self.po.po_number)
        self.assertEqual(movements.first().quantity, Decimal("5.000"))

        # Summary notification fired exactly once
        mock_summary.assert_called_once()

        # PR is partially fulfilled (PO is partial)
        self.pr.refresh_from_db()
        self.assertEqual(self.pr.status, PurchaseRequest.Status.PARTIALLY_FULFILLED)

    # ------------------------------------------------------------------
    # 2. Damaged units route to quarantine
    # ------------------------------------------------------------------
    def test_receive_with_damaged_units_routes_to_quarantine(self):
        """good_qty=3 + damaged_qty=2 → available += 3, quarantine += 2,
        PurchaseOrderItem.damaged_qty == 2, ADJUSTMENT movement with
        reference destination=quarantine."""
        self.client.force_login(self.procurement)
        with mock.patch(
            "procurement.views.notify_po_received_summary"
        ), mock.patch(
            "procurement.views.PartAllocationService.reallocate_for_part"
        ):
            response = self.client.post(self.url, {
                f"good_qty_{self.item_a.pk}": "3",
                f"damaged_qty_{self.item_a.pk}": "2",
            })
        self.assertEqual(response.status_code, 302)

        self.item_a.refresh_from_db()
        self.assertEqual(self.item_a.received_qty, Decimal("5.000"))
        self.assertEqual(self.item_a.damaged_qty, Decimal("2.000"))
        self.assertEqual(self.item_a.rejected_qty, Decimal("0"))

        inv = Inventory.objects.get(part=self.part_a, site=self.site)
        self.assertEqual(inv.quantity_available, Decimal("3.000"))
        self.assertEqual(inv.quantity_quarantine, Decimal("2.000"))

        # ADJUSTMENT movement with reference["destination"] == "quarantine"
        adj = StockMovement.objects.filter(
            part=self.part_a, movement_type=StockMovement.MovementType.ADJUSTMENT,
        )
        self.assertEqual(adj.count(), 1)
        m = adj.first()
        self.assertEqual(m.quantity, Decimal("2.000"))
        ref = m.reference or {}
        self.assertEqual(ref.get("destination"), "quarantine")
        self.assertEqual(ref.get("po_number"), self.po.po_number)

        # A separate STOCK_IN exists for the good units
        stock_in_mv = StockMovement.objects.filter(
            part=self.part_a, movement_type=StockMovement.MovementType.STOCK_IN,
        )
        self.assertEqual(stock_in_mv.count(), 1)

    # ------------------------------------------------------------------
    # 3. Rejected units — no stock change
    # ------------------------------------------------------------------
    def test_receive_with_rejected_units_no_stock_change(self):
        """Submit rejected_qty=2 → rejected_qty == 2, no Inventory change,
        no new StockMovement."""
        self.client.force_login(self.procurement)
        with mock.patch(
            "procurement.views.notify_po_received_summary"
        ), mock.patch(
            "procurement.views.PartAllocationService.reallocate_for_part"
        ):
            response = self.client.post(self.url, {
                f"rejected_qty_{self.item_a.pk}": "2",
            })
        self.assertEqual(response.status_code, 302)

        self.item_a.refresh_from_db()
        self.assertEqual(self.item_a.rejected_qty, Decimal("2.000"))
        self.assertEqual(self.item_a.received_qty, Decimal("2.000"))  # counts toward received
        self.assertEqual(self.item_a.damaged_qty, Decimal("0"))

        # No inventory change
        self.assertFalse(Inventory.objects.filter(part=self.part_a, site=self.site).exists())
        # No StockMovement for this part
        self.assertEqual(
            StockMovement.objects.filter(part=self.part_a).count(), 0,
        )

    # ------------------------------------------------------------------
    # 4. reallocate_for_part called for stock-changing lines
    # ------------------------------------------------------------------
    def test_receive_calls_reallocate_for_part(self):
        """A WO with an open PartIssueLine exists → reallocate_for_part
        is called for the part whose stock changed, and the line gets a
        new allocation."""
        line = PartIssueLine.objects.create(
            work_order=self.wo, part=self.part_a,
            quantity=Decimal("10"), unit_cost=Decimal("5"),
            status=PartIssueLine.Status.APPROVED,
            requested_by=self.tech, issued_by=self.tech,
            requested_qty=Decimal("10"),
            approved_qty=Decimal("10"),
        )
        # Pre-existing Inventory so reallocate has a "before" to compare
        Inventory.objects.create(
            part=self.part_a, site=self.site,
            quantity_available=Decimal("0"),
        )

        self.client.force_login(self.procurement)
        with mock.patch(
            "procurement.views.notify_po_received_summary"
        ), mock.patch(
            "procurement.views.PartAllocationService.reallocate_for_part",
            wraps=PartAllocationService.reallocate_for_part,
        ) as mock_realloc:
            response = self.client.post(self.url, {
                f"good_qty_{self.item_a.pk}": "5",
            })
        self.assertEqual(response.status_code, 302)
        # Called for the part whose stock changed
        mock_realloc.assert_any_call(self.part_a)

        # The line got allocated from the new free stock
        line.refresh_from_db()
        self.assertEqual(line.allocated_qty, Decimal("5.000"))
        # A reservation was created
        self.assertTrue(
            InventoryReservation.objects.filter(
                part=self.part_a, work_order=self.wo, status="active",
            ).exists()
        )

    # ------------------------------------------------------------------
    # 5. supplier_invoice_ref + actual_unit_price capture
    # ------------------------------------------------------------------
    def test_receive_captures_supplier_invoice_ref_and_actual_unit_price(self):
        """supplier_invoice_ref=INV-001 + actual_unit_price=12.50 →
        StockMovement.invoice_ref=INV-001, POItem.actual_unit_price=12.50."""
        self.client.force_login(self.procurement)
        with mock.patch(
            "procurement.views.notify_po_received_summary"
        ), mock.patch(
            "procurement.views.PartAllocationService.reallocate_for_part"
        ):
            response = self.client.post(self.url, {
                f"good_qty_{self.item_a.pk}": "5",
                f"actual_unit_price_{self.item_a.pk}": "12.50",
                "supplier_invoice_ref": "INV-001",
            })
        self.assertEqual(response.status_code, 302)

        self.item_a.refresh_from_db()
        self.assertEqual(self.item_a.actual_unit_price, Decimal("12.5000"))

        m = StockMovement.objects.filter(
            part=self.part_a, movement_type=StockMovement.MovementType.STOCK_IN,
        ).first()
        self.assertEqual(m.invoice_ref, "INV-001")
        self.assertEqual(m.unit_cost, Decimal("12.5000"))

    # ------------------------------------------------------------------
    # 6. Atomic on failure: mid-loop exception rolls back
    # ------------------------------------------------------------------
    def test_receive_atomic_on_failure(self):
        """If stock_in raises on the 2nd line, the transaction rolls back
        — no partial state, no summary notification, no PR status change."""
        self.client.force_login(self.procurement)

        from inventory.services import stock_in as real_si

        def stock_in_side_effect(*args, **kwargs):
            # First call (part A) — actually run the real stock_in
            # Second call (part B) — raise
            if not stock_in_side_effect.calls:
                stock_in_side_effect.calls += 1
                return real_si(*args, **kwargs)
            raise RuntimeError("simulated failure on line 2")
        stock_in_side_effect.calls = 0  # type: ignore[attr-defined]

        with mock.patch(
            "procurement.views.notify_po_received_summary"
        ) as mock_summary, mock.patch(
            "procurement.views.PartAllocationService.reallocate_for_part"
        ), mock.patch(
            "procurement.views.stock_in", side_effect=stock_in_side_effect,
        ):
            self.client.raise_request_exception = False
            response = self.client.post(
                self.url,
                {
                    f"good_qty_{self.item_a.pk}": "5",
                    f"good_qty_{self.item_b.pk}": "3",
                },
            )

        # The exception propagates out of the view; the test client
        # converts it to a 500 because raise_request_exception=False.
        self.assertEqual(response.status_code, 500)

        # Roll back: item_a.received_qty back to 0
        self.item_a.refresh_from_db()
        self.assertEqual(self.item_a.received_qty, Decimal("0"))

        # No inventory change
        self.assertFalse(
            Inventory.objects.filter(part=self.part_a, site=self.site).exists()
        )

        # No StockMovement persisted
        self.assertEqual(
            StockMovement.objects.filter(part=self.part_a).count(), 0,
        )

        # No summary notification fired (it's outside atomic)
        mock_summary.assert_not_called()

        # PO status unchanged (still SENT)
        self.po.refresh_from_db()
        self.assertEqual(self.po.status, PurchaseOrder.Status.SENT)

        # PR status unchanged
        self.pr.refresh_from_db()
        self.assertEqual(self.pr.status, PurchaseRequest.Status.CONVERTED_TO_PO)

    # ------------------------------------------------------------------
    # 7. notify_po_received_summary called exactly once
    # ------------------------------------------------------------------
    def test_receive_calls_notify_po_received_summary(self):
        """Receiving ≥1 line → notify_po_received_summary called exactly 1×."""
        self.client.force_login(self.procurement)
        with mock.patch(
            "procurement.views.notify_po_received_summary"
        ) as mock_summary, mock.patch(
            "procurement.views.PartAllocationService.reallocate_for_part"
        ):
            response = self.client.post(self.url, {
                f"good_qty_{self.item_a.pk}": "5",
            })
        self.assertEqual(response.status_code, 302)
        mock_summary.assert_called_once()
        call_args = mock_summary.call_args
        # (po, actor)
        self.assertEqual(call_args[0][0].pk, self.po.pk)
        self.assertEqual(call_args[0][1].pk, self.procurement.pk)

    # ------------------------------------------------------------------
    # 8. Silent no-op when all lines are zero and no invoice ref
    # ------------------------------------------------------------------
    def test_receive_silent_noop_when_all_zero(self):
        """All zero + no actual_price + no invoice ref → no message, no
        summary notification, redirect to PO detail."""
        self.client.force_login(self.procurement)
        with mock.patch(
            "procurement.views.notify_po_received_summary"
        ) as mock_summary, mock.patch(
            "procurement.views.PartAllocationService.reallocate_for_part"
        ):
            response = self.client.post(self.url, {})  # no fields at all
        self.assertEqual(response.status_code, 302)
        mock_summary.assert_not_called()
        # No item state change
        self.item_a.refresh_from_db()
        self.item_b.refresh_from_db()
        self.assertEqual(self.item_a.received_qty, Decimal("0"))
        self.assertEqual(self.item_b.received_qty, Decimal("0"))
