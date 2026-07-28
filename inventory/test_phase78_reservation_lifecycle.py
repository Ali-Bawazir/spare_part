"""
Phase 7.8 — InventoryReservation lifecycle tests.

Validates that the reservation table is the source of truth for what's
"soft-claimed" in inventory, and that execute_warehouse_issue and
cancel_approved_part_request release/cancel the SPECIFIC rows attached
to a line (FIFO by created_at), instead of decrementing the aggregate
quantity_reserved directly (the legacy v4.7 path).
"""
from decimal import Decimal

from django.test import TestCase

from accounts.models import User
from inventory.models import (
    Inventory, InventoryReservation, PartIssueLine, SparePart, StockMovement,
)
from inventory.services import (
    cancel_approved_part_request,
    execute_warehouse_issue,
)
from maintenance.models import AuditEntry, CostTransaction, Machine, Site, WorkOrder


def _make_user(username, role):
    return User.objects.create_user(
        username=username, password="x", role=role,
    )


def _make_part(sku="P78-PART", cost=Decimal("10.00"), on_hand=Decimal("20")):
    site = Site.objects.filter(is_default=True).first() or Site.objects.create(
        name="X", is_default=True, is_active=True,
    )
    p = SparePart.objects.create(
        sku=sku, name=sku, status="active",
        avg_cost=cost, last_purchase_cost=cost,
    )
    Inventory.objects.create(part=p, site=site, quantity_available=on_hand)
    return p, site


def _make_wo(machine, tech, mgr):
    return WorkOrder.objects.create(
        machine=machine, lifecycle_status=WorkOrder.LifecycleStatus.IN_PROGRESS,
        assigned_technician=tech, created_by=mgr,
    )


def _make_line(wo, part, qty, requested_by, approved_by, status="allocated"):
    return PartIssueLine.objects.create(
        work_order=wo, part=part, quantity=qty, requested_qty=qty,
        approved_qty=qty, allocated_qty=qty if status == "allocated" else Decimal("0"),
        issued_qty=Decimal("0"),
        unit_cost=Decimal("10.00"),
        status=status,
        requested_by=requested_by, approved_by=approved_by, issued_by=approved_by,
    )


class WarehouseIssueReservationReleaseTests(TestCase):
    """Phase 7.8: warehouse issue releases specific reservation rows."""

    def setUp(self):
        self.mgr = _make_user("p78_mgr", User.Role.MANAGER)
        self.tech = _make_user("p78_tech", User.Role.TECHNICIAN)
        site, machine = (
            Site.objects.filter(is_default=True).first(),
            Machine.objects.create(
                name="P78Press", qr_code="p78-q", asset_level=3,
                asset_code="P78", is_active=True,
                site=Site.objects.filter(is_default=True).first(),
            ),
        )
        self.part, self.site = _make_part(sku="P78-ISSUE", on_hand=Decimal("20"))
        self.wo = _make_wo(machine, self.tech, self.mgr)
        self.line = _make_line(self.wo, self.part, 5, self.tech, self.mgr)

    def _create_reservation(self, line, qty):
        res = InventoryReservation.objects.create(
            part=line.part, work_order=line.work_order,
            quantity=qty, source_line=line,
            status=InventoryReservation.Status.ACTIVE,
        )
        return res

    def test_full_issue_releases_full_reservation(self):
        """Issue qty == reserved qty: reservation row is fully RELEASED."""
        res = self._create_reservation(self.line, 5)
        inv = self.part.inventory_items.first()
        self.assertEqual(inv.compute_quantity_reserved(), Decimal("5"),
                         "sanity: aggregate should match after create")

        result = execute_warehouse_issue(line=self.line, qty=5, actor=self.mgr)
        res.refresh_from_db()
        self.assertEqual(res.status, InventoryReservation.Status.RELEASED,
                         f"reservation should be RELEASED, got {res.status}")
        self.assertIsNotNone(res.released_at, "released_at should be set")
        inv.refresh_from_db()
        self.assertEqual(inv.compute_quantity_reserved(), Decimal("0"),
                         f"aggregate should be 0 after release, got {inv.compute_quantity_reserved()}")
        self.assertEqual(inv.quantity_available, Decimal("15"))

    def test_partial_issue_splits_reservation(self):
        """Issue qty < reserved qty: original ACTIVE row is shrunk,
        a sibling RELEASED row is created for the consumed portion."""
        res = self._create_reservation(self.line, 5)
        execute_warehouse_issue(line=self.line, qty=2, actor=self.mgr)
        res.refresh_from_db()
        self.assertEqual(res.status, InventoryReservation.Status.ACTIVE,
                         f"shrunk row should stay ACTIVE, got {res.status}")
        self.assertEqual(res.quantity, Decimal("3"),
                         f"shrunk row should be 3, got {res.quantity}")
        # A sibling RELEASED row for 2 should exist
        released = InventoryReservation.objects.filter(
            part=self.part, source_line=self.line,
            status=InventoryReservation.Status.RELEASED,
        )
        self.assertEqual(released.count(), 1,
                         f"expected 1 RELEASED row, got {released.count()}")
        self.assertEqual(released.first().quantity, Decimal("2"))
        inv = self.part.inventory_items.first()
        # Cache: ACTIVE=3, RELEASED=2, so quantity_reserved=3
        self.assertEqual(inv.compute_quantity_reserved(), Decimal("3"),
                         f"cache should be 3, got {inv.compute_quantity_reserved()}")
        self.assertEqual(inv.quantity_available, Decimal("18"))

    def test_issue_falls_back_when_no_specific_reservation(self):
        """If there's no InventoryReservation attached to the line (legacy
        data or pre-allocation path), the issue still succeeds and
        deducts stock. The aggregate quantity_reserved is NOT touched
        because there were no actual ACTIVE reservations to release —
        the previous v4.7 behavior of decrementing the aggregate was
        incorrect and could mis-attribute reservation capacity across
        lines. Phase 7.8 removes that legacy auto-decrement.
        """
        # Don't create a reservation; legacy line path.
        # With the legacy DB field removed, there is no way to pre-set
        # the aggregate (it is computed from ACTIVE reservations). The
        # assertion below verifies that the aggregate stays UNCHANGED
        # (=0) after a warehouse issue that had no specific reservation
        # to release.
        inv = self.part.inventory_items.first()
        result = execute_warehouse_issue(line=self.line, qty=3, actor=self.mgr)
        inv.refresh_from_db()
        self.assertEqual(inv.quantity_available, Decimal("17"),
                         f"stock should be deducted, got {inv.quantity_available}")
        # Aggregate is 0 (no ACTIVE reservations existed to release).
        self.assertEqual(inv.compute_quantity_reserved(), Decimal("0"),
                         f"cache should be 0 (no reservation was attached), got {inv.compute_quantity_reserved()}")

    def test_issue_creates_audit_trail(self):
        """Each release creates a part_reservation_released audit entry."""
        self._create_reservation(self.line, 5)
        execute_warehouse_issue(line=self.line, qty=5, actor=self.mgr)
        audits = AuditEntry.objects.filter(
            action="part_reservation_released",
            object_id=str(self.line.pk),
        )
        self.assertTrue(audits.exists(),
                        f"audit missing for reservation release on line {self.line.pk}")


class CancelApprovedLineReservationTests(TestCase):
    """Phase 7.8: cancel_approved_part_request cancels specific reservations."""

    def setUp(self):
        self.mgr = _make_user("p78_can_mgr", User.Role.MANAGER)
        self.tech = _make_user("p78_can_tech", User.Role.TECHNICIAN)
        site, machine = (
            Site.objects.filter(is_default=True).first(),
            Machine.objects.create(
                name="P78PressCan", qr_code="p78-c", asset_level=3,
                asset_code="P78C", is_active=True,
                site=Site.objects.filter(is_default=True).first(),
            ),
        )
        self.part, self.site = _make_part(sku="P78-CANCEL", on_hand=Decimal("20"))
        self.wo = _make_wo(machine, self.tech, self.mgr)
        self.line = _make_line(self.wo, self.part, 5, self.tech, self.mgr)

    def test_cancel_marks_reservation_cancelled(self):
        res = InventoryReservation.objects.create(
            part=self.part, work_order=self.wo, quantity=5,
            source_line=self.line, status=InventoryReservation.Status.ACTIVE,
        )
        inv = self.part.inventory_items.first()
        self.assertEqual(inv.compute_quantity_reserved(), Decimal("5"))

        cancel_approved_part_request(
            line=self.line, manager=self.mgr,
            reason="Cancelled: wrong part ordered, reorder needed",
        )
        res.refresh_from_db()
        self.assertEqual(res.status, InventoryReservation.Status.CANCELLED,
                         f"reservation should be CANCELLED, got {res.status}")
        self.assertIsNotNone(res.released_at)
        self.assertIn("Cancelled", res.release_reason)
        inv.refresh_from_db()
        self.assertEqual(inv.compute_quantity_reserved(), Decimal("0"),
                         f"cache should be 0 after cancel, got {inv.compute_quantity_reserved()}")

    def test_cancel_only_affects_target_line_reservations(self):
        """Cancelling a line must not affect reservations on OTHER lines."""
        # Reservation on the line being cancelled
        res1 = InventoryReservation.objects.create(
            part=self.part, work_order=self.wo, quantity=5,
            source_line=self.line, status=InventoryReservation.Status.ACTIVE,
        )
        # Create a second line on the same WO with its own reservation
        line2 = _make_line(self.wo, self.part, 3, self.tech, self.mgr)
        res2 = InventoryReservation.objects.create(
            part=self.part, work_order=self.wo, quantity=3,
            source_line=line2, status=InventoryReservation.Status.ACTIVE,
        )
        inv = self.part.inventory_items.first()
        self.assertEqual(inv.compute_quantity_reserved(), Decimal("8"))

        cancel_approved_part_request(
            line=self.line, manager=self.mgr,
            reason="Cancelled: testing isolation across lines",
        )
        res1.refresh_from_db()
        res2.refresh_from_db()
        self.assertEqual(res1.status, InventoryReservation.Status.CANCELLED)
        self.assertEqual(res2.status, InventoryReservation.Status.ACTIVE,
                         "line2 reservation must NOT be affected")
        inv.refresh_from_db()
        self.assertEqual(inv.compute_quantity_reserved(), Decimal("3"),
                         f"cache should be 3 after cancel, got {inv.compute_quantity_reserved()}")


class ReservationAggregateSyncTests(TestCase):
    """Phase 7.8: the aggregate quantity_reserved cache stays in sync
    with the ACTIVE reservation rows after every state transition."""

    def setUp(self):
        self.mgr = _make_user("p78_sync_mgr", User.Role.MANAGER)
        self.tech = _make_user("p78_sync_tech", User.Role.TECHNICIAN)
        site, machine = (
            Site.objects.filter(is_default=True).first(),
            Machine.objects.create(
                name="P78PressSync", qr_code="p78-s", asset_level=3,
                asset_code="P78S", is_active=True,
                site=Site.objects.filter(is_default=True).first(),
            ),
        )
        self.part, self.site = _make_part(sku="P78-SYNC", on_hand=Decimal("30"))
        self.wo = _make_wo(machine, self.tech, self.mgr)

    def test_aggregate_equals_sum_of_active_reservations(self):
        line1 = _make_line(self.wo, self.part, 4, self.tech, self.mgr)
        line2 = _make_line(self.wo, self.part, 6, self.tech, self.mgr)
        InventoryReservation.objects.create(
            part=self.part, work_order=self.wo, quantity=4,
            source_line=line1, status=InventoryReservation.Status.ACTIVE,
        )
        InventoryReservation.objects.create(
            part=self.part, work_order=self.wo, quantity=6,
            source_line=line2, status=InventoryReservation.Status.ACTIVE,
        )
        inv = self.part.inventory_items.first()
        # Trigger a fresh recompute via the signal path: just refresh
        expected = sum(
            r.quantity for r in InventoryReservation.objects.filter(
                part=self.part, status=InventoryReservation.Status.ACTIVE,
            )
        )
        self.assertEqual(inv.compute_quantity_reserved(), expected)

    def test_aggregate_does_not_count_released_reservations(self):
        line = _make_line(self.wo, self.part, 5, self.tech, self.mgr)
        InventoryReservation.objects.create(
            part=self.part, work_order=self.wo, quantity=5,
            source_line=line, status=InventoryReservation.Status.RELEASED,
        )
        InventoryReservation.objects.create(
            part=self.part, work_order=self.wo, quantity=3,
            source_line=line, status=InventoryReservation.Status.ACTIVE,
        )
        inv = self.part.inventory_items.first()
        self.assertEqual(inv.compute_quantity_reserved(), Decimal("3"),
                         f"only ACTIVE should count, got {inv.compute_quantity_reserved()}")

    def test_aggregate_does_not_count_cancelled_reservations(self):
        line = _make_line(self.wo, self.part, 5, self.tech, self.mgr)
        InventoryReservation.objects.create(
            part=self.part, work_order=self.wo, quantity=5,
            source_line=line, status=InventoryReservation.Status.CANCELLED,
        )
        InventoryReservation.objects.create(
            part=self.part, work_order=self.wo, quantity=2,
            source_line=line, status=InventoryReservation.Status.ACTIVE,
        )
        inv = self.part.inventory_items.first()
        self.assertEqual(inv.compute_quantity_reserved(), Decimal("2"))
