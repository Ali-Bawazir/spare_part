"""Phase UC-06 — edit_shortage_decision must sync PR.quantity.

Bug: v4.8 procurement lock refused to let the manager change
approved_procurement_qty once an auto-PR existed (`ValidationError`),
so the PR.quantity was forever stuck at its initial value while the
decision row was edited freely. Surface result: "Decisions Recorded"
shows a different qty than "Linked procurement requests".

Fix: instead of raising, edit_shortage_decision now syncs the PR's
quantity to the new approved_procurement_qty and writes a
purchase_request_qty_synced audit entry. Same transaction, no extra
refactor.
"""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase

from inventory.models import (
    PartShortageDecision,
    PartShortageReport,
    SparePart,
)
from inventory.services import (
    create_shortage_decision,
    edit_shortage_decision,
    request_part_on_wo,
)
from maintenance.models import Machine, Site, WorkOrder
from procurement.models import PurchaseRequest


User = get_user_model()


def _make_user(username, role):
    return User.objects.create_user(username=username, password="x", role=role)


def _make_part(sku, on_hand=Decimal("2")):
    site = Site.objects.filter(is_default=True).first() or Site.objects.create(
        name="PrSyncSite", is_default=True, is_active=True,
    )
    p = SparePart.objects.create(
        sku=sku, name=sku, status="active",
        avg_cost=Decimal("10"), last_purchase_cost=Decimal("10"),
    )
    from inventory.models import Inventory
    Inventory.objects.create(part=p, site=site, quantity_available=on_hand)
    return p


def _make_machine(name="PS-MACH"):
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


class EditDecisionProcurementSyncsPRTests(TestCase):
    """When the manager edits a decision and changes
    approved_procurement_qty, the linked PR's quantity must follow."""

    def setUp(self):
        self.mgr = _make_user("prsync_mgr", User.Role.MANAGER)
        self.tech = _make_user("prsync_tech", User.Role.TECHNICIAN)
        self.machine = _make_machine()
        self.part = _make_part(sku="PRSYNC-F", on_hand=Decimal("2"))
        self.wo = _make_wo(self.machine, self.tech, self.mgr)

    def test_edit_decreases_procurement_qty_updates_pr(self):
        # 1. Request 5 of 2 on hand -> PSR PENDING_REVIEW + line PENDING.
        result = request_part_on_wo(
            wo=self.wo, part=self.part,
            quantity=Decimal("5"), technician=self.tech,
        )
        psr = result["shortage_report"]
        # 2. Decide issue=2, proc=3 -> PR created with qty=3.
        create_shortage_decision(
            report=psr, decision_type="approve",
            approved_issue_qty=Decimal("2"),
            approved_procurement_qty=Decimal("3"),
            rejected_qty=Decimal("0"),
            decided_by=self.mgr,
        )
        pr = PurchaseRequest.objects.get(source_shortage_report=psr)
        self.assertEqual(pr.quantity, Decimal("3.000"))
        # 3. Manager edits decision: proc from 3 to 2.
        edit_shortage_decision(
            report=psr,
            approved_issue_qty=Decimal("3"),
            approved_procurement_qty=Decimal("2"),
            rejected_qty=Decimal("0"),
            edited_by=self.mgr,
        )
        psr.refresh_from_db()
        pr.refresh_from_db()
        # 4. Decision updated AND PR.quantity follows.
        self.assertEqual(
            psr.decision.approved_procurement_qty,
            Decimal("2.000"),
        )
        self.assertEqual(pr.quantity, Decimal("2.000"))

    def test_same_procurement_qty_is_a_noop(self):
        result = request_part_on_wo(
            wo=self.wo, part=self.part,
            quantity=Decimal("5"), technician=self.tech,
        )
        psr = result["shortage_report"]
        create_shortage_decision(
            report=psr, decision_type="approve",
            approved_issue_qty=Decimal("2"),
            approved_procurement_qty=Decimal("3"),
            rejected_qty=Decimal("0"),
            decided_by=self.mgr,
        )
        pr = PurchaseRequest.objects.get(source_shortage_report=psr)
        # Editing with the SAME procurement_qty must not raise.
        edit_shortage_decision(
            report=psr,
            approved_issue_qty=Decimal("2"),
            approved_procurement_qty=Decimal("3"),
            rejected_qty=Decimal("0"),
            edited_by=self.mgr,
        )
        pr.refresh_from_db()
        self.assertEqual(pr.quantity, Decimal("3.000"))
