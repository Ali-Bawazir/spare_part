"""Phase 3 tests: Machine + Component cost dashboards.

Tests the MachineCost and ComponentCost dataclasses which aggregate
from the CostTransaction ledger live (no snapshot tables).
"""
from __future__ import annotations

from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from inventory.models import PartIssueLine
from maintenance.models import (
    CostCategory, CostTransaction, MaintenanceIssue, Machine, WorkOrder,
)
from maintenance.cost_views import (
    MachineCost, ComponentCost,
    machine_costs_for_periods, component_costs_for_periods,
)


# ----- helpers -----

def _make_user(username, role):
    from accounts.models import User
    return User.objects.create_user(username=username, password="x", role=role)


def _make_machine(name="Press-Test", qr="PT-1", asset_level=3, parent=None):
    return Machine.objects.create(
        name=name, qr_code=qr, asset_level=asset_level, parent=parent,
    )


def _make_wo(machine, component=None, created_by=None, lifecycle="closed"):
    from accounts.models import User
    if created_by is None:
        created_by = _make_user("wo-creator", User.Role.MANAGER)
    return WorkOrder.objects.create(
        machine=machine, component=component,
        created_by=created_by,
        lifecycle_status=lifecycle,
    )


def _post_cost(wo, machine, component, amount, category, days_ago=0, source_id=1):
    return CostTransaction.objects.create(
        amount=amount,
        category=category,
        currency="SAR",
        quantity=None, unit_cost=None,
        work_order=wo, machine=machine, component=component,
        source_type="part_issue_line", source_id=source_id,
        is_reversal=False, supersedes=None, actor=None,
        occurred_at=timezone.now() - timezone.timedelta(days=days_ago),
        memo=f"test post {amount}",
    )


_sku_counter = 0
def _post_pil(wo, status=PartIssueLine.Status.ISSUED, days_ago=0, qty=1, actor=None):
    global _sku_counter
    from inventory.models import SparePart
    from accounts.models import User
    _sku_counter += 1
    if actor is None:
        actor, _ = User.objects.get_or_create(
            username="pil-actor", defaults={"role": User.Role.MANAGER},
        )
        if not actor.has_usable_password():
            actor.set_password("x")
            actor.save()
    sku = f"TEST-{_sku_counter}"
    part = SparePart.objects.create(
        sku=sku, name=f"Test Part {qty}",
    )
    return PartIssueLine.objects.create(
        work_order=wo, part=part, quantity=qty, unit_cost=10,
        requested_qty=qty, status=status, issued_by=actor,
        created_at=timezone.now() - timezone.timedelta(days=days_ago),
    )


# ----- tests -----

class MachineCostAggregationTests(TestCase):
    """MachineCost aggregates ledger transactions by category, in a period."""

    def setUp(self):
        from accounts.models import User
        self.manager = _make_user("m_cf_mgr", User.Role.MANAGER)
        self.machine = _make_machine()
        self.wo = _make_wo(self.machine, created_by=self.manager)

    def test_empty_machine_returns_zeros(self):
        # setUp creates a WO so wo_count=1 even with no cost ledger entries
        cost = MachineCost.for_machine(self.machine, period_days=30)
        self.assertEqual(cost.total, Decimal("0"))
        self.assertEqual(cost.material, Decimal("0"))
        self.assertEqual(cost.wo_count, 1)
        self.assertEqual(cost.failure_count, 0)
        self.assertIsNone(cost.cost_per_failure)

    def test_single_category_aggregated(self):
        _post_cost(self.wo, self.machine, None, Decimal("100"), CostCategory.MATERIAL)
        cost = MachineCost.for_machine(self.machine, period_days=30)
        self.assertEqual(cost.material, Decimal("100"))
        self.assertEqual(cost.total, Decimal("100"))

    def test_all_four_categories_aggregated(self):
        _post_cost(self.wo, self.machine, None, Decimal("10"), CostCategory.MATERIAL, source_id=1)
        _post_cost(self.wo, self.machine, None, Decimal("20"), CostCategory.VENDOR_REPAIR, source_id=2)
        _post_cost(self.wo, self.machine, None, Decimal("5"), CostCategory.CONSUMABLE, source_id=3)
        _post_cost(self.wo, self.machine, None, Decimal("15"), CostCategory.ADJUSTMENT, source_id=4)
        cost = MachineCost.for_machine(self.machine, period_days=30)
        self.assertEqual(cost.material, Decimal("10"))
        self.assertEqual(cost.vendor_repair, Decimal("20"))
        self.assertEqual(cost.consumable, Decimal("5"))
        self.assertEqual(cost.adjustment, Decimal("15"))
        self.assertEqual(cost.total, Decimal("50"))

    def test_period_filter_excludes_old(self):
        # Old: 40 days ago, should be excluded from 30-day window
        _post_cost(self.wo, self.machine, None, Decimal("100"), CostCategory.MATERIAL, days_ago=40)
        # Recent: today, included
        _post_cost(self.wo, self.machine, None, Decimal("30"), CostCategory.MATERIAL, days_ago=0)
        cost = MachineCost.for_machine(self.machine, period_days=30)
        self.assertEqual(cost.material, Decimal("30"))

    def test_reversal_subtracts_from_total(self):
        _post_cost(self.wo, self.machine, None, Decimal("100"), CostCategory.MATERIAL, source_id=1)
        CostTransaction.objects.create(
            amount=Decimal("-100"),
            category=CostCategory.MATERIAL, currency="SAR",
            work_order=self.wo, machine=self.machine, component=None,
            source_type="part_issue_line", source_id=1,
            is_reversal=True, supersedes=None, actor=None,
        )
        cost = MachineCost.for_machine(self.machine, period_days=30)
        self.assertEqual(cost.material, Decimal("0"))
        self.assertEqual(cost.total, Decimal("0"))

    def test_wo_count_from_updated_at(self):
        # Two WOs both in the period
        wo2 = _make_wo(self.machine, created_by=self.manager)
        # One WO in the past, outside the 30-day window (raw update to
        # bypass auto_now on updated_at)
        from django.db import connection
        old_wo = _make_wo(self.machine, created_by=self.manager, lifecycle="closed")
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE maintenance_workorder SET updated_at = %s WHERE id = %s",
                [timezone.now() - timezone.timedelta(days=60), old_wo.pk],
            )
        cost = MachineCost.for_machine(self.machine, period_days=30)
        self.assertEqual(cost.wo_count, 2)  # self.wo and wo2 (old_wo excluded)

    def test_failure_count_from_issued_pils(self):
        # One ISSUED PIL → 1 failure
        _post_pil(self.wo, status=PartIssueLine.Status.ISSUED)
        # One APPROVED PIL → not counted
        _post_pil(self.wo, status=PartIssueLine.Status.APPROVED)
        cost = MachineCost.for_machine(self.machine, period_days=30)
        self.assertEqual(cost.failure_count, 1)

    def test_cost_per_failure(self):
        _post_cost(self.wo, self.machine, None, Decimal("100"), CostCategory.MATERIAL)
        _post_pil(self.wo, status=PartIssueLine.Status.ISSUED)
        _post_pil(self.wo, status=PartIssueLine.Status.ISSUED)
        cost = MachineCost.for_machine(self.machine, period_days=30)
        self.assertEqual(cost.cost_per_failure, Decimal("50.00"))  # 100/2

    def test_descendant_machines_roll_up(self):
        """Costs on a descendant subassembly/component roll up to the parent machine."""
        from accounts.models import User
        # Create a hierarchy: machine → subassembly → component
        sub = _make_machine(name="Sub-1", qr="PT-SUB-1", asset_level=4, parent=self.machine)
        comp = _make_machine(name="Comp-1", qr="PT-COMP-1", asset_level=5, parent=sub)
        # Post a cost directly to the subassembly
        wo_sub = _make_wo(self.machine, component=sub, created_by=self.manager)
        _post_cost(wo_sub, self.machine, sub, Decimal("50"), CostCategory.MATERIAL, source_id=10)
        # Post a cost directly to the component
        wo_comp = _make_wo(self.machine, component=comp, created_by=self.manager)
        _post_cost(wo_comp, self.machine, comp, Decimal("25"), CostCategory.MATERIAL, source_id=11)
        # Aggregate at the parent machine
        cost = MachineCost.for_machine(self.machine, period_days=30)
        self.assertEqual(cost.material, Decimal("75"))  # 50 + 25

    def test_periods_helper(self):
        costs = machine_costs_for_periods(self.machine)
        self.assertEqual(set(costs.keys()), {30, 90, 365})
        for days, c in costs.items():
            self.assertIsInstance(c, MachineCost)
            self.assertEqual(c.period_days, days)


class ComponentCostAggregationTests(TestCase):
    """ComponentCost aggregates ledger transactions for a specific component."""

    def setUp(self):
        from accounts.models import User
        self.manager = _make_user("c_cf_mgr", User.Role.MANAGER)
        self.machine = _make_machine()
        self.component = _make_machine(name="Comp-A", qr="PT-C-A", asset_level=5, parent=self.machine)
        self.wo = _make_wo(self.machine, component=self.component, created_by=self.manager)

    def test_empty_component_returns_zeros(self):
        cost = ComponentCost.for_component(self.component, period_days=30)
        self.assertEqual(cost.total, Decimal("0"))

    def test_cost_on_wo_targeting_component_aggregated(self):
        # Cost attached to the WO (work_order FK is set, component FK is the target)
        _post_cost(self.wo, self.machine, self.component, Decimal("75"), CostCategory.MATERIAL)
        cost = ComponentCost.for_component(self.component, period_days=30)
        self.assertEqual(cost.material, Decimal("75"))

    def test_cost_on_other_component_not_aggregated(self):
        other_comp = _make_machine(name="Comp-B", qr="PT-C-B", asset_level=5, parent=self.machine)
        # Post a cost where BOTH the ledger's component AND the WO's
        # component are the OTHER component. This cost should not be
        # included in the self.component aggregation.
        other_wo = _make_wo(self.machine, component=other_comp, created_by=self.manager)
        _post_cost(other_wo, self.machine, other_comp, Decimal("99"), CostCategory.MATERIAL)
        cost = ComponentCost.for_component(self.component, period_days=30)
        self.assertEqual(cost.material, Decimal("0"))

    def test_periods_helper(self):
        costs = component_costs_for_periods(self.component)
        self.assertEqual(set(costs.keys()), {30, 90, 365})
