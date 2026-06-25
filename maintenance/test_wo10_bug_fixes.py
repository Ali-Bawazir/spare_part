"""
Bug fixes for the WO-10 "Awaiting Spare Part + cost is 0" issue.

Reported state on WO-10 (real production data):
- 5 PartIssueLines: 1 ALLOCATED (Electrical Tape x 2), 2 APPROVED with
  issued=2 (Drive belt x 2), 2 APPROVED with issued=0 (Air filter x 10,
  Grease 5L x 34).
- 1 open PART blocker (Electrical Tape is approved+allocated but
  not yet warehouse-issued).
- 3 StockMovements (ISSUE_TO_WO): 1 with unit_cost=122 (Grease) and
  2 with unit_cost=0 (Drive belt x 2).
- WorkOrderCost row does NOT exist for WO-10.
- CostTransaction ledger has 0 entries for WO-10.

Two real bugs uncovered by the investigation:

Bug 1: `_deduct_and_record_issue` did not fall back to
`part.last_purchase_cost` or `part.avg_cost` when the manager direct-
issue form passed `unit_cost=0` (e.g. user left the field blank or
the supplier invoice didn't have a cost). The line stored 0, and
even though post_material was called, the amount was 0.

Bug 2: Same — the result was material cost never posted to the
ledger when the form's unit_cost was 0. Fixed by the same fallback
in _deduct_and_record_issue.
"""
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import User
from inventory.models import (
    Inventory, PartIssueLine, PartShortageReport, SparePart, StockMovement,
)
from inventory.services import issue_part_to_work_order
from maintenance.models import (
    CostCategory, CostTransaction, MaintenanceIssue, Site, WorkOrder,
    WorkOrderBlocker, WorkOrderCost, Machine,
)


def _make_user(username, role):
    return User.objects.create_user(username=username, role=role)


def _make_site():
    s, _ = Site.objects.get_or_create(
        is_default=True, defaults={"name": "TestSite"},
    )
    return s


def _make_machine(code="M-1"):
    return Machine.objects.create(
        name=f"Test Machine {code}",
        asset_level=3, asset_code=code,
        qr_code=f"qr-{code}-{timezone.now().timestamp()}",
    )


def _make_part(sku="P-1", name="Test Part", avg_cost=None):
    return SparePart.objects.create(
        sku=sku, name=name, is_consumable=False, avg_cost=avg_cost,
    )


def _stock_in(part, site, qty, unit_cost=Decimal("10")):
    inv, _ = Inventory.objects.get_or_create(part=part, site=site)
    inv.quantity_available = qty
    inv.save()
    return inv


# -------- Bug 1: issue_part_to_work_order posts to the cost ledger --------

class IssuePartToWorkOrderPostsLedgerTests(TestCase):
    """Manager direct-issue path must post a CostTransaction to the ledger."""

    def setUp(self):
        self.manager = _make_user("mgr", User.Role.MANAGER)
        self.site = _make_site()
        self.machine = _make_machine("BL-1")
        self.part = _make_part(sku="BL-1", name="BL Part", avg_cost=Decimal("25.00"))
        _stock_in(self.part, self.site, Decimal("50"))
        issue = MaintenanceIssue.objects.create(
            description="x", machine=self.machine, reported_by=self.manager,
        )
        issue.validated_by = self.manager
        issue.save()
        self.wo = WorkOrder.objects.create(
            machine=self.machine, assigned_technician=self.manager,
            created_by=self.manager, issue=issue,
            lifecycle_status=WorkOrder.LifecycleStatus.IN_PROGRESS,
        )

    def test_direct_issue_creates_cost_transaction(self):
        ok, msg = issue_part_to_work_order(
            wo=self.wo, part=self.part, quantity=Decimal("3"),
            unit_cost=Decimal("25.00"), invoice_ref="INV-1",
            supplier_name="Acme", issued_by=self.manager,
        )
        self.assertTrue(ok, msg)
        txns = CostTransaction.objects.filter(
            work_order=self.wo, category=CostCategory.MATERIAL,
        )
        self.assertEqual(txns.count(), 1)
        txn = txns.first()
        self.assertEqual(txn.amount, Decimal("75.00"))  # 3 * 25
        self.assertEqual(txn.quantity, Decimal("3"))
        self.assertEqual(txn.unit_cost, Decimal("25.00"))

    def test_direct_issue_creates_work_order_cost_cache(self):
        issue_part_to_work_order(
            wo=self.wo, part=self.part, quantity=Decimal("2"),
            unit_cost=Decimal("20.00"), invoice_ref="INV-2",
            supplier_name="Acme", issued_by=self.manager,
        )
        cost = WorkOrderCost.objects.get(work_order=self.wo)
        cost.recalculate_from_ledger()
        cost.refresh_from_db()
        self.assertEqual(cost.material_cost, Decimal("40.00"))
        self.assertEqual(cost.total_cost, Decimal("40.00"))


# -------- Bug 2: unit_cost falls back to part cost when 0 --------

class UnitCostFallbackTests(TestCase):
    """When the manager direct-issue form has unit_cost=0 (e.g. user
    didn't enter a cost), the line should fall back to
    part.last_purchase_cost or part.avg_cost so the ledger captures
    material cost."""

    def setUp(self):
        self.manager = _make_user("mgr", User.Role.MANAGER)
        self.site = _make_site()
        self.machine = _make_machine("UC-1")
        self.part = _make_part(sku="UC-1", name="UC Part", avg_cost=Decimal("15.00"))
        _stock_in(self.part, self.site, Decimal("50"))
        issue = MaintenanceIssue.objects.create(
            description="x", machine=self.machine, reported_by=self.manager,
        )
        issue.validated_by = self.manager
        issue.save()
        self.wo = WorkOrder.objects.create(
            machine=self.machine, assigned_technician=self.manager,
            created_by=self.manager, issue=issue,
            lifecycle_status=WorkOrder.LifecycleStatus.IN_PROGRESS,
        )

    def test_zero_unit_cost_falls_back_to_part_cost(self):
        ok, msg = issue_part_to_work_order(
            wo=self.wo, part=self.part, quantity=Decimal("4"),
            unit_cost=Decimal("0"), invoice_ref="INV-UC",
            supplier_name="Acme", issued_by=self.manager,
        )
        self.assertTrue(ok, msg)
        pil = PartIssueLine.objects.filter(work_order=self.wo).first()
        self.assertEqual(pil.unit_cost, Decimal("15.0000"))
        txn = CostTransaction.objects.filter(
            work_order=self.wo, category=CostCategory.MATERIAL,
        ).first()
        self.assertIsNotNone(txn)
        self.assertEqual(txn.amount, Decimal("60.00"))
