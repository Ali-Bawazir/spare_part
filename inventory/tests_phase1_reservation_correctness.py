"""
Phase 1 regression tests — free-stock correctness fixes.

Locks down:
  BUG-1: create_shortage_decision reserves exactly once (single reservation source)
  BUG-2: transition_shortage_status CLOSED releases BOTH line-linked and
         legacy (source_line=None) reservations
  BUG-3: cancel_approved_part_request releases BOTH line-linked and legacy
         reservations on the same (part, work_order)
  reconcile_legacy_reservations command: idempotent, --dry-run, audit log
"""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase

from inventory.models import (
    Inventory,
    InventoryReservation,
    PartIssueLine,
    PartShortageDecision,
    PartShortageReport,
    SparePart,
)
from inventory.services import (
    cancel_approved_part_request,
    create_shortage_decision,
    request_part_on_wo,
    transition_shortage_status,
)
from maintenance.models import Machine, Site, WorkOrder


User = get_user_model()


def _user(username, role):
    return User.objects.create_user(username=username, password="x", role=role)


def _part(sku, on_hand=Decimal("20")):
    site = Site.objects.filter(is_default=True).first() or Site.objects.create(
        name="Default", is_default=True, is_active=True,
    )
    p = SparePart.objects.create(
        sku=sku, name=sku, status="active",
        avg_cost=Decimal("10"), last_purchase_cost=Decimal("10"),
    )
    Inventory.objects.create(part=p, site=site, quantity_available=on_hand)
    return p, site


def _wo(machine, tech, mgr):
    return WorkOrder.objects.create(
        machine=machine, lifecycle_status=WorkOrder.LifecycleStatus.IN_PROGRESS,
        assigned_technician=tech, created_by=mgr,
    )


def _line(wo, part, qty, requested_by, approved_by, status="approved"):
    return PartIssueLine.objects.create(
        work_order=wo, part=part, quantity=qty, requested_qty=qty,
        approved_qty=qty, allocated_qty=qty if status == "approved" else Decimal("0"),
        issued_qty=Decimal("0"),
        unit_cost=Decimal("10"),
        status=status,
        requested_by=requested_by, approved_by=approved_by, issued_by=approved_by,
    )


def _machine(name="Press-1", qr="press1"):
    site = Site.objects.filter(is_default=True).first() or Site.objects.create(
        name="Default", is_default=True, is_active=True,
    )
    return Machine.objects.create(
        name=name, qr_code=qr, asset_level=3,
        asset_code=name[:8], is_active=True, site=site,
    )


class CreateShortageDecisionSingleReservationTests(TestCase):
    """BUG-1: create_shortage_decision must create exactly one reservation,
    not two, when free stock is greater than approved_issue_qty."""

    def setUp(self):
        self.mgr = _user("p1m", User.Role.MANAGER)
        self.tech = _user("p1t", User.Role.TECHNICIAN)
        self.machine = _machine()
        self.part, _ = _part("P1-DUP", on_hand=Decimal("0"))
        self.wo = _wo(self.machine, self.tech, self.mgr)

    def test_no_double_reservation_when_free_stock_exceeds_approved_qty(self):
        # Tech requests 5 of zero-stock part → shortage created
        request_part_on_wo(
            wo=self.wo, part=self.part, quantity=Decimal("5"),
            technician=self.tech,
        )
        report = PartShortageReport.objects.get(work_order=self.wo, part=self.part)
        line = PartIssueLine.objects.get(work_order=self.wo, part=self.part)

        # Stock arrives BEFORE manager decides (e.g. PO received overnight)
        inv = Inventory.objects.get(part=self.part)
        inv.quantity_available = Decimal("20")
        inv.save()

        # Manager approves: issue 5, reject 0. Books-balance: 5+0+0 = 5 requested.
        create_shortage_decision(
            report=report,
            decision_type=PartShortageDecision.DecisionType.APPROVE,
            approved_issue_qty=Decimal("5"),
            approved_procurement_qty=Decimal("0"),
            rejected_qty=Decimal("0"),
            decided_by=self.mgr,
        )

        # BUG-1 fix: exactly ONE active reservation, not two.
        active = InventoryReservation.objects.filter(
            part=self.part, work_order=self.wo,
            status=InventoryReservation.Status.ACTIVE,
        )
        self.assertEqual(active.count(), 1)
        self.assertEqual(active.first().quantity, Decimal("5"))
        # Keystone: the single reservation is linked to the originating line.
        self.assertEqual(active.first().source_line, line)


class TransitionShortageClosedReleasesAllReservationsTests(TestCase):
    """BUG-2: transition_shortage_status CLOSED must release BOTH line-linked
    AND legacy (source_line=None) reservations on the same (part, work_order)."""

    def setUp(self):
        self.mgr = _user("p2m", User.Role.MANAGER)
        self.tech = _user("p2t", User.Role.TECHNICIAN)
        self.machine = _machine(name="P2", qr="p2")
        # Start with zero stock so a request creates a shortage.
        self.part, _ = _part("P2-LEG", on_hand=Decimal("0"))
        self.wo = _wo(self.machine, self.tech, self.mgr)

    def test_closed_releases_legacy_reservation(self):
        # Tech requests a part on a zero-stock WO → shortage report created
        request_part_on_wo(
            wo=self.wo, part=self.part, quantity=Decimal("5"),
            technician=self.tech,
        )
        report = PartShortageReport.objects.get(work_order=self.wo, part=self.part)

        # Add a legacy reservation (source_line=None) AND a line-linked one.
        line = PartIssueLine.objects.get(work_order=self.wo, part=self.part)
        InventoryReservation.objects.create(
            part=self.part, work_order=self.wo, quantity=Decimal("2"),
            status=InventoryReservation.Status.ACTIVE, source_line=None,
        )
        InventoryReservation.objects.create(
            part=self.part, work_order=self.wo, quantity=Decimal("3"),
            status=InventoryReservation.Status.ACTIVE, source_line=line,
        )
        self.assertEqual(
            InventoryReservation.objects.filter(
                part=self.part, work_order=self.wo,
                status=InventoryReservation.Status.ACTIVE,
            ).count(), 2,
        )

        # Transition to APPROVED then CLOSED
        transition_shortage_status(
            report, PartShortageReport.Status.APPROVED, actor=self.mgr,
        )
        transition_shortage_status(
            report, PartShortageReport.Status.CLOSED, actor=self.mgr,
        )

        # BUG-2 fix: BOTH legacy and line-linked are released.
        self.assertEqual(
            InventoryReservation.objects.filter(
                part=self.part, work_order=self.wo,
                status=InventoryReservation.Status.ACTIVE,
            ).count(), 0,
        )


class CancelApprovedPartRequestReleasesAllReservationsTests(TestCase):
    """BUG-3: cancel_approved_part_request must release BOTH line-linked
    AND legacy (source_line=None) reservations on the same (part, work_order)."""

    def setUp(self):
        self.mgr = _user("p3m", User.Role.MANAGER)
        self.tech = _user("p3t", User.Role.TECHNICIAN)
        self.machine = _machine(name="P3", qr="p3")
        self.part, _ = _part("P3-CAN", on_hand=Decimal("10"))
        self.wo = _wo(self.machine, self.tech, self.mgr)
        self.line = _line(self.wo, self.part, Decimal("3"), self.tech, self.mgr)

    def test_cancel_releases_legacy_reservation(self):
        # Line-linked reservation
        InventoryReservation.objects.create(
            part=self.part, work_order=self.wo, quantity=Decimal("3"),
            status=InventoryReservation.Status.ACTIVE, source_line=self.line,
        )
        # Legacy reservation on the same (part, work_order)
        InventoryReservation.objects.create(
            part=self.part, work_order=self.wo, quantity=Decimal("2"),
            status=InventoryReservation.Status.ACTIVE, source_line=None,
        )
        self.assertEqual(
            InventoryReservation.objects.filter(
                part=self.part, work_order=self.wo,
                status=InventoryReservation.Status.ACTIVE,
            ).count(), 2,
        )

        # Cancel the line (min 15-char reason)
        cancel_approved_part_request(
            line=self.line, manager=self.mgr,
            reason="scope changed, no longer needed",
        )

        # BUG-3 fix: BOTH reservations are released.
        self.assertEqual(
            InventoryReservation.objects.filter(
                part=self.part, work_order=self.wo,
                status=InventoryReservation.Status.ACTIVE,
            ).count(), 0,
        )


class ReconcileLegacyReservationsCommandTests(TestCase):
    """The reconcile_legacy_reservations management command cancels legacy
    source_line=None reservations. Idempotent. Supports --dry-run. Logs
    audit entries."""

    def setUp(self):
        self.mgr = _user("p4m", User.Role.MANAGER)
        self.tech = _user("p4t", User.Role.TECHNICIAN)
        self.machine = _machine(name="P4", qr="p4")
        self.part, _ = _part("P4-REC", on_hand=Decimal("100"))
        self.wo = _wo(self.machine, self.tech, self.mgr)
        self.line = _line(self.wo, self.part, Decimal("5"), self.tech, self.mgr)

    def _make_legacy(self, qty=Decimal("1")):
        return InventoryReservation.objects.create(
            part=self.part, work_order=self.wo, quantity=qty,
            status=InventoryReservation.Status.ACTIVE, source_line=None,
        )

    def _make_line_linked(self, qty=Decimal("1")):
        return InventoryReservation.objects.create(
            part=self.part, work_order=self.wo, quantity=qty,
            status=InventoryReservation.Status.ACTIVE, source_line=self.line,
        )

    def test_dry_run_does_not_cancel(self):
        self._make_legacy()
        call_command("reconcile_legacy_reservations", "--dry-run")
        self.assertEqual(
            InventoryReservation.objects.filter(
                status=InventoryReservation.Status.ACTIVE, source_line__isnull=True,
            ).count(), 1,
        )

    def test_live_run_cancels_legacy_only(self):
        self._make_legacy(qty=Decimal("3"))
        self._make_legacy(qty=Decimal("2"))
        self._make_line_linked(qty=Decimal("5"))
        call_command("reconcile_legacy_reservations")
        # Legacy ones cancelled
        self.assertEqual(
            InventoryReservation.objects.filter(
                status=InventoryReservation.Status.CANCELLED, source_line__isnull=True,
            ).count(), 2,
        )
        # Line-linked one untouched
        self.assertEqual(
            InventoryReservation.objects.filter(
                source_line__isnull=False, status=InventoryReservation.Status.ACTIVE,
            ).count(), 1,
        )

    def test_is_idempotent(self):
        self._make_legacy()
        call_command("reconcile_legacy_reservations")
        # Second run finds nothing to do
        call_command("reconcile_legacy_reservations")
        self.assertEqual(
            InventoryReservation.objects.filter(
                status=InventoryReservation.Status.CANCELLED, source_line__isnull=True,
            ).count(), 1,
        )
