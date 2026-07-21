"""
Phase UC-06 — Shortage-blocker auto-close on PSR terminal transition.

Bug: When a PartShortageReport reached CLOSED (manager-verified closure
terminal), reservations were released and the auto-PR was cancelled, but
the SHORTAGE WO Blocker keyed to the report stayed OPEN. As a result the
WO page kept showing "Awaiting Procurement × N · age Nh" and
WorkOrderService.operational_status remained "pending_parts" even after
the manager had verified closure.

Fix: transition_shortage_status() now closes the SHORTAGE WO Blocker when
the new status is in {FULFILLED, CLOSED, REJECTED}. REJECTED cancels;
FULFILLED/CLOSED resolve.
"""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase

from inventory.models import (
    Inventory,
    PartShortageDecision,
    PartShortageReport,
    SparePart,
)
from inventory.services import (
    create_shortage_decision,
    request_part_on_wo,
    transition_shortage_status,
)
from maintenance.models import Machine, Site, WorkOrder, WorkOrderBlocker


User = get_user_model()


def _make_user(username, role):
    return User.objects.create_user(username=username, password="x", role=role)


def _make_part(sku, on_hand=Decimal("2")):
    site = Site.objects.filter(is_default=True).first() or Site.objects.create(
        name="DefaultSiteShortBlk", is_default=True, is_active=True,
    )
    p = SparePart.objects.create(
        sku=sku, name=sku, status="active",
        avg_cost=Decimal("10"), last_purchase_cost=Decimal("10"),
    )
    Inventory.objects.create(part=p, site=site, quantity_available=on_hand)
    return p


def _make_machine(name="SBR-PRESS"):
    site = Site.objects.filter(is_default=True).first()
    return Machine.objects.create(
        name=name, qr_code=name[:6].lower(), asset_level=3,
        asset_code=name[:8], is_active=True, site=site,
    )


def _make_wo(machine, tech, mgr):
    return WorkOrder.objects.create(
        machine=machine, lifecycle_status=WorkOrder.LifecycleStatus.ASSIGNED,
        assigned_technician=tech, created_by=mgr,
    )


class TerminalShortageClosesBlockerTests(TestCase):
    """PSR.terminal transition must close the SHORTAGE WO Blocker."""

    def setUp(self):
        self.mgr = _make_user("sbr_mgr", User.Role.MANAGER)
        self.tech = _make_user("sbr_tech", User.Role.TECHNICIAN)
        self.machine = _make_machine()
        self.part = _make_part(sku="SBR-FILTER-1", on_hand=Decimal("2"))
        self.wo = _make_wo(self.machine, self.tech, self.mgr)

    def test_psr_close_resolves_open_shortage_blocker(self):
        # 1. Tech requests 5 of a part with 2 on hand -> creates a
        #    PENDING PartIssueLine AND a PENDING_REVIEW PartShortageReport.
        result = request_part_on_wo(
            wo=self.wo, part=self.part,
            quantity=Decimal("5"),
            technician=self.tech,
        )
        psr = result["shortage_report"]
        self.assertIsNotNone(psr, "expected a shortage report on under-stock request")
        self.assertEqual(psr.status, PartShortageReport.Status.PENDING_REVIEW)

        # 2. Manager approves with zero issue and full procurement — this
        #    path goes through auto_create_pr_for_shortage which opens the
        #    SHORTAGE WO Blocker.
        decision = create_shortage_decision(
            report=psr, decision_type="approve",
            approved_issue_qty=Decimal("0"),
            approved_procurement_qty=Decimal("5"),
            rejected_qty=Decimal("0"),
            decided_by=self.mgr,
        )
        self.assertIsInstance(decision, PartShortageDecision)
        psr.refresh_from_db()
        self.assertEqual(psr.status, PartShortageReport.Status.APPROVED)

        blocker = WorkOrderBlocker.objects.filter(
            work_order=self.wo,
            kind=WorkOrderBlocker.Kind.SHORTAGE,
            status=WorkOrderBlocker.Status.OPEN,
        ).first()
        self.assertIsNotNone(
            blocker, "expected the SHORTAGE blocker to be open after auto-PR",
        )

        # 3. Manager terminal-closes the PSR (e.g. auto-PR cancelled and
        #    they verify the closure externally).
        transition_shortage_status(
            psr, PartShortageReport.Status.CLOSED,
            actor=self.mgr, note="test: closure after PR cancelled",
        )

        # 4. The SHORTAGE blocker must now be RESOLVED — exactly one
        #    closed-on-this-PSR blocker exists in the WO history.
        blocker.refresh_from_db()
        self.assertEqual(
            blocker.status,
            WorkOrderBlocker.Status.RESOLVED,
            "SHORTAGE blocker should resolve on PSR terminal close",
        )
        self.assertEqual(
            WorkOrderBlocker.objects.filter(
                work_order=self.wo,
                kind=WorkOrderBlocker.Kind.SHORTAGE,
                status=WorkOrderBlocker.Status.OPEN,
            ).count(),
            0,
        )
