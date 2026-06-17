"""
Phase 1+2 — Cost Ledger (CostTransaction + CostAdjustment + CostLedgerService).

Covers:
  * Service-level tests for post_material, post_vendor_repair,
    post_consumable, post_adjustment
  * Idempotency and reversal-on-change semantics
  * CostTransaction immutability and validation
  * WorkOrderCost cache reconciliation
  * Management commands: backfill + rebuild
  * Atomicity (mocked WO cache failure rolls back the ledger)

Uses the same self-contained setUp pattern as test_phase3a_health_blockers.py.
"""
from __future__ import annotations

from decimal import Decimal
from unittest import mock

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Sum
from django.test import TestCase, TransactionTestCase

from accounts.models import User
from inventory.models import PartIssueLine, SparePart
from maintenance.cost_ledger import CostLedgerService
from maintenance.models import (
    CostAdjustment,
    CostCategory,
    CostTransaction,
    Machine,
    Site,
    WorkOrder,
    WorkOrderCost,
)


# ---------------------------------------------------------------------------
# Helpers (mirror the style of test_phase3a_health_blockers.py)
# ---------------------------------------------------------------------------


def _make_user(username: str, role: str) -> User:
    return User.objects.create_user(username=username, password="x", role=role)


def _make_wo(*, machine: Machine = None, component: Machine = None,
             created_by: User, **kwargs) -> WorkOrder:
    defaults = {
        "machine": machine,
        "component": component,
        "created_by": created_by,
        "lifecycle_status": WorkOrder.LifecycleStatus.ASSIGNED,
    }
    defaults.update(kwargs)
    return WorkOrder.objects.create(**defaults)


def _make_part_line(*, wo: WorkOrder, part: SparePart, qty=Decimal("2"),
                    unit_cost=Decimal("10"), issued_by: User = None) -> PartIssueLine:
    return PartIssueLine.objects.create(
        work_order=wo, part=part, quantity=qty, unit_cost=unit_cost,
        status=PartIssueLine.Status.ISSUED,
        requested_by=issued_by, issued_by=issued_by,
        requested_qty=qty, approved_qty=qty, issued_qty=qty,
    )


def _make_part_for(wo: WorkOrder, sku: str = "ATOM-PART") -> SparePart:
    return SparePart.objects.create(sku=sku, name=f"Part for {wo.number}")


# ---------------------------------------------------------------------------
# 1) Basic posting tests
# ---------------------------------------------------------------------------


class CostLedgerBasicPostTests(TestCase):
    """Each source-specific poster creates a CostTransaction with the
    right category, amount, and target."""

    def setUp(self):
        self.manager = _make_user("manager_ledger", User.Role.MANAGER)
        self.tech = _make_user("tech_ledger", User.Role.TECHNICIAN)
        self.machine = Machine.objects.create(name="Press-Ledger", qr_code="PL-1")
        self.part = SparePart.objects.create(sku="PL-BRG", name="Bearing L1")
        self.wo = _make_wo(machine=self.machine, created_by=self.manager)

    def test_post_material_creates_transaction(self):
        """post_material: qty × unit_cost → one CostTransaction(MATERIAL)."""
        line = _make_part_line(wo=self.wo, part=self.part, qty=Decimal("3"),
                               unit_cost=Decimal("10"), issued_by=self.manager)
        txn = CostLedgerService.post_material(
            part_issue_line=line, actor=self.manager,
            memo="issue test",
        )
        self.assertIsNotNone(txn)
        self.assertEqual(txn.category, CostCategory.MATERIAL)
        self.assertEqual(txn.amount, Decimal("30.00"))
        self.assertEqual(txn.work_order_id, self.wo.pk)
        self.assertEqual(txn.source_type, "part_issue_line")
        self.assertEqual(txn.source_id, line.pk)
        self.assertEqual(txn.quantity, Decimal("3"))
        self.assertEqual(txn.unit_cost, Decimal("10"))
        self.assertEqual(txn.currency, "SAR")
        self.assertFalse(txn.is_reversal)

    def test_post_vendor_repair_creates_transaction(self):
        """post_vendor_repair: ERO.actual_cost → one CostTransaction(VENDOR_REPAIR)."""
        from maintenance.models import ExternalRepairOrder
        ero = ExternalRepairOrder.objects.create(
            work_order=self.wo, machine=self.machine,
            title="Test ERO", description="d",
            actual_cost=Decimal("250.00"),
            invoice_ref="INV-001",
            status=ExternalRepairOrder.Status.CLOSED,
            created_by=self.manager,
        )
        txn = CostLedgerService.post_vendor_repair(
            external_repair_order=ero, actor=self.manager, memo="ero test",
        )
        self.assertIsNotNone(txn)
        self.assertEqual(txn.category, CostCategory.VENDOR_REPAIR)
        self.assertEqual(txn.amount, Decimal("250.00"))
        self.assertEqual(txn.work_order_id, self.wo.pk)
        self.assertEqual(txn.source_type, "external_repair_order")
        self.assertEqual(txn.source_id, ero.pk)

    def test_post_consumable_creates_transaction(self):
        """post_consumable: StockMovement(qty × unit_cost) → one CostTransaction(CONSUMABLE)."""
        from inventory.models import Inventory, StockMovement
        site = Site.objects.create(name="Main", code="main", is_default=True)
        Inventory.objects.create(part=self.part, site=site, quantity_available=Decimal("100"))
        sm = StockMovement.objects.create(
            part=self.part, site=site,
            movement_type=StockMovement.MovementType.CONSUMABLE_USE,
            quantity=Decimal("2"), unit_cost=Decimal("5"),
            performed_by=self.tech, work_order=self.wo,
        )
        txn = CostLedgerService.post_consumable(
            stock_movement=sm, actor=self.tech, memo="cons test",
        )
        self.assertIsNotNone(txn)
        self.assertEqual(txn.category, CostCategory.CONSUMABLE)
        self.assertEqual(txn.amount, Decimal("10.00"))
        self.assertEqual(txn.work_order_id, self.wo.pk)
        self.assertEqual(txn.source_type, "stock_movement")
        self.assertEqual(txn.source_id, sm.pk)

    def test_post_adjustment_creates_cost_adjustment(self):
        """post_adjustment: creates a CostAdjustment AND a linked CostTransaction."""
        txn = CostLedgerService.post_adjustment(
            work_order=self.wo, amount=Decimal("50.00"),
            memo="finance correction for missing line",
            actor=self.manager,
        )
        self.assertIsNotNone(txn)
        self.assertEqual(txn.category, CostCategory.ADJUSTMENT)
        self.assertEqual(txn.amount, Decimal("50.00"))
        self.assertEqual(txn.source_type, "cost_adjustment")
        self.assertIsNotNone(txn.adjustment_id)
        # The CostAdjustment row exists and is linked 1:1 to the transaction
        adj = CostAdjustment.objects.get(pk=txn.adjustment_id)
        self.assertEqual(adj.work_order_id, self.wo.pk)
        self.assertEqual(adj.amount, Decimal("50.00"))
        self.assertEqual(adj.created_by_id, self.manager.id)
        self.assertEqual(adj.memo, "finance correction for missing line")


# ---------------------------------------------------------------------------
# 2) Idempotency and reversal-on-change tests
# ---------------------------------------------------------------------------


class CostLedgerIdempotencyAndReversalTests(TestCase):
    """When the same (source_type, source_id) is posted twice:

      - same amount + same category → no-op, no second row
      - different amount OR different category → reversal row + new row
        (net = new amount via SUM(amount))
    """

    def setUp(self):
        self.manager = _make_user("manager_idem", User.Role.MANAGER)
        self.machine = Machine.objects.create(name="Press-Idem", qr_code="PI-1")
        self.wo = _make_wo(machine=self.machine, created_by=self.manager)

    def test_idempotent_same_amount(self):
        """Post the same material twice → only one row, no reversal."""
        first = CostLedgerService._post(
            amount=Decimal("100.00"), category=CostCategory.MATERIAL,
            quantity=Decimal("10"), unit_cost=Decimal("10"),
            work_order=self.wo, machine=self.machine, component=None,
            source_type="part_issue_line", source_id=42,
            actor=self.manager, memo="first",
        )
        second = CostLedgerService._post(
            amount=Decimal("100.00"), category=CostCategory.MATERIAL,
            quantity=Decimal("10"), unit_cost=Decimal("10"),
            work_order=self.wo, machine=self.machine, component=None,
            source_type="part_issue_line", source_id=42,
            actor=self.manager, memo="second (same)",
        )
        self.assertEqual(first.pk, second.pk)
        rows = CostTransaction.objects.filter(
            source_type="part_issue_line", source_id=42,
        )
        self.assertEqual(rows.count(), 1)

    def test_reversal_on_amount_change(self):
        """Post +100, then post +120 → posts delta (+20), net +120.

        With the delta-based algorithm, posting a different amount creates
        a single delta row (no reversal). The original +100 row stays;
        the delta of +20 brings the running total to +120.
        """
        CostLedgerService._post(
            amount=Decimal("100.00"), category=CostCategory.MATERIAL,
            quantity=None, unit_cost=None,
            work_order=self.wo, machine=self.machine, component=None,
            source_type="part_issue_line", source_id=7,
            actor=self.manager, memo="v1",
        )
        CostLedgerService._post(
            amount=Decimal("120.00"), category=CostCategory.MATERIAL,
            quantity=None, unit_cost=None,
            work_order=self.wo, machine=self.machine, component=None,
            source_type="part_issue_line", source_id=7,
            actor=self.manager, memo="v2",
        )
        rows = list(CostTransaction.objects.filter(
            source_type="part_issue_line", source_id=7,
        ).order_by("pk"))
        # 1: +100, 2: +20 delta (not a reversal)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0].amount, Decimal("100.00"))
        self.assertFalse(rows[0].is_reversal)
        self.assertEqual(rows[1].amount, Decimal("20.00"))
        self.assertFalse(rows[1].is_reversal)
        # Net = +100 + 20 = +120
        net = sum(r.amount for r in rows)
        self.assertEqual(net, Decimal("120.00"))

    def test_reversal_to_zero_then_new(self):
        """Post +100; cannot post 0 (constraint); post +50 → delta(-50), net +50.

        With the delta-based algorithm, posting +50 when current is +100
        creates a delta row of -50. Net: +100 - 50 = +50.
        """
        CostLedgerService._post(
            amount=Decimal("100.00"), category=CostCategory.MATERIAL,
            quantity=None, unit_cost=None,
            work_order=self.wo, machine=self.machine, component=None,
            source_type="part_issue_line", source_id=99,
            actor=self.manager, memo="v1",
        )
        # amount=0 is blocked by the CheckConstraint; verify the model rejects it
        with self.assertRaises(Exception):
            with transaction.atomic():
                CostTransaction.objects.create(
                    amount=Decimal("0"), category=CostCategory.MATERIAL,
                    work_order=self.wo, machine=self.machine,
                    source_type="part_issue_line", source_id=99,
                    actor=self.manager, memo="zero (rejected)",
                )
        CostLedgerService._post(
            amount=Decimal("50.00"), category=CostCategory.MATERIAL,
            quantity=None, unit_cost=None,
            work_order=self.wo, machine=self.machine, component=None,
            source_type="part_issue_line", source_id=99,
            actor=self.manager, memo="v3",
        )
        rows = list(CostTransaction.objects.filter(
            source_type="part_issue_line", source_id=99,
        ).order_by("pk"))
        # 1: +100, 2: -50 delta
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0].amount, Decimal("100.00"))
        self.assertEqual(rows[1].amount, Decimal("-50.00"))
        self.assertFalse(rows[1].is_reversal)
        net = sum(r.amount for r in rows)
        self.assertEqual(net, Decimal("50.00"))

    def test_negative_amount_reverses_active(self):
        """Post +100, then post -50 → delta(-150), net -50.

        With the delta-based algorithm, going from +100 to -50 posts a single
        delta row of -150. Net: +100 - 150 = -50.
        """
        CostLedgerService._post(
            amount=Decimal("100.00"), category=CostCategory.MATERIAL,
            quantity=None, unit_cost=None,
            work_order=self.wo, machine=self.machine, component=None,
            source_type="part_issue_line", source_id=12,
            actor=self.manager, memo="v1",
        )
        CostLedgerService._post(
            amount=Decimal("-50.00"), category=CostCategory.MATERIAL,
            quantity=None, unit_cost=None,
            work_order=self.wo, machine=self.machine, component=None,
            source_type="part_issue_line", source_id=12,
            actor=self.manager, memo="v2 negative",
        )
        rows = list(CostTransaction.objects.filter(
            source_type="part_issue_line", source_id=12,
        ).order_by("pk"))
        # 1: +100, 2: -150 delta
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0].amount, Decimal("100.00"))
        self.assertEqual(rows[1].amount, Decimal("-150.00"))
        self.assertFalse(rows[1].is_reversal)
        net = sum(r.amount for r in rows)
        self.assertEqual(net, Decimal("-50.00"))


# ---------------------------------------------------------------------------
# 3) CostTransaction immutability and validation
# ---------------------------------------------------------------------------


class CostTransactionImmutabilityTests(TestCase):
    def setUp(self):
        self.manager = _make_user("manager_imm", User.Role.MANAGER)
        self.machine = Machine.objects.create(name="Press-Imm", qr_code="PI-2")
        self.wo = _make_wo(machine=self.machine, created_by=self.manager)
        self.txn = CostTransaction.objects.create(
            amount=Decimal("10.00"), category=CostCategory.MATERIAL,
            work_order=self.wo, machine=self.machine,
            source_type="part_issue_line", source_id=1,
            actor=self.manager, memo="first",
        )

    def test_immutability_raises(self):
        """Re-saving an existing CostTransaction row raises ValueError."""
        self.txn.amount = Decimal("99.00")
        with self.assertRaises(ValueError) as cm:
            self.txn.save()
        self.assertIn("immutable", str(cm.exception).lower())

    def test_no_target_raises(self):
        """Saving without WO/machine/component raises ValueError."""
        with self.assertRaises(ValueError) as cm:
            CostTransaction.objects.create(
                amount=Decimal("10.00"), category=CostCategory.MATERIAL,
                source_type="x", source_id=1, actor=self.manager, memo="orphan",
            )
        self.assertIn("must target", str(cm.exception).lower())


# ---------------------------------------------------------------------------
# 4) WorkOrderCost cache update + atomicity
# ---------------------------------------------------------------------------


class WorkOrderCostCacheTests(TestCase):
    def setUp(self):
        self.manager = _make_user("manager_cache", User.Role.MANAGER)
        self.machine = Machine.objects.create(name="Press-Cache", qr_code="PC-1")
        self.wo = _make_wo(machine=self.machine, created_by=self.manager)

    def test_work_order_cost_cache_updated(self):
        """After posting, the WO cost cache is rebuilt from the ledger."""
        CostLedgerService._post(
            amount=Decimal("100.00"), category=CostCategory.MATERIAL,
            quantity=None, unit_cost=None,
            work_order=self.wo, machine=self.machine, component=None,
            source_type="part_issue_line", source_id=1, actor=self.manager, memo="",
        )
        CostLedgerService._post(
            amount=Decimal("200.00"), category=CostCategory.VENDOR_REPAIR,
            quantity=None, unit_cost=None,
            work_order=self.wo, machine=self.machine, component=None,
            source_type="external_repair_order", source_id=2,
            actor=self.manager, memo="",
        )
        CostLedgerService._post(
            amount=Decimal("50.00"), category=CostCategory.CONSUMABLE,
            quantity=None, unit_cost=None,
            work_order=self.wo, machine=self.machine, component=None,
            source_type="stock_movement", source_id=3, actor=self.manager, memo="",
        )
        cache = WorkOrderCost.objects.get(work_order=self.wo)
        self.assertEqual(cache.material_cost, Decimal("100.00"))
        self.assertEqual(cache.vendor_repair_cost, Decimal("200.00"))
        self.assertEqual(cache.consumables_cost, Decimal("50.00"))
        self.assertEqual(cache.ledger_transaction_count, 3)
        self.assertIsNotNone(cache.last_reconciled_at)


class WorkOrderCostAtomicityTests(TransactionTestCase):
    """Atomicity test: must use TransactionTestCase because the failure
    needs to actually roll back the @transaction.atomic block, which
    TestCase's outer test transaction would mask as a savepoint rollback."""

    def setUp(self):
        self.manager = _make_user("manager_atom", User.Role.MANAGER)
        self.machine = Machine.objects.create(name="Press-Atom", qr_code="PA-1")
        self.wo = _make_wo(machine=self.machine, created_by=self.manager)

    def test_atomicity_rollback(self):
        """If the WO cache refresh fails inside post_material, the
        CostTransaction insert (and any reversal row) must roll back.
        """
        line = _make_part_line(wo=self.wo, part=_make_part_for(self.wo),
                               qty=Decimal("1"), unit_cost=Decimal("10"),
                               issued_by=self.manager)
        baseline = CostTransaction.objects.count()
        # Patch the cache refresh to raise; the @transaction.atomic on
        # post_material should roll back the entire flow.
        with mock.patch.object(
            CostLedgerService, "_refresh_wo_cache",
            side_effect=RuntimeError("simulated cache failure"),
        ):
            with self.assertRaises(RuntimeError):
                CostLedgerService.post_material(
                    part_issue_line=line, actor=self.manager, memo="",
                )
        self.assertEqual(CostTransaction.objects.count(), baseline)


# ---------------------------------------------------------------------------
# 5) Supersedes chain scenarios (the 3 named tests)
# ---------------------------------------------------------------------------


class SupersedesChainTests(TestCase):
    """Three named scenarios that exercise the supersedes reversal chain."""

    def setUp(self):
        self.manager = _make_user("manager_chain", User.Role.MANAGER)
        self.machine = Machine.objects.create(name="Press-Chain", qr_code="PC-2")
        self.wo = _make_wo(machine=self.machine, created_by=self.manager)

    def test_chain_scenario_a_positive_only(self):
        """Scenario A: post +100 once. SUM(amount) = +100, no reversals."""
        CostLedgerService._post(
            amount=Decimal("100.00"), category=CostCategory.MATERIAL,
            quantity=None, unit_cost=None,
            work_order=self.wo, machine=self.machine, component=None,
            source_type="part_issue_line", source_id=1,
            actor=self.manager, memo="",
        )
        agg = CostTransaction.objects.filter(
            work_order=self.wo,
        ).aggregate(total=Sum("amount"))["total"]
        self.assertEqual(agg, Decimal("100.00"))
        self.assertEqual(
            CostTransaction.objects.filter(is_reversal=True).count(), 0,
        )

    def test_chain_scenario_b_positive_then_reversal(self):
        """Scenario B: post +100, then explicitly create a reversal row
        with `supersedes=+100`. SUM(amount) = 0.
        """
        original = CostLedgerService._post(
            amount=Decimal("100.00"), category=CostCategory.MATERIAL,
            quantity=None, unit_cost=None,
            work_order=self.wo, machine=self.machine, component=None,
            source_type="part_issue_line", source_id=1,
            actor=self.manager, memo="v1",
        )
        # Direct cancellation: post a reversal row that supersedes the
        # original. This is the "via supersedes" pattern — the caller is
        # explicitly saying "undo this row", not "set a new value".
        CostTransaction.objects.create(
            amount=Decimal("-100.00"),
            category=CostCategory.MATERIAL,
            currency="SAR",
            work_order=self.wo, machine=self.machine, component=None,
            source_type="part_issue_line", source_id=1,
            is_reversal=True,
            supersedes=original,
            actor=self.manager, memo="v2 cancellation",
        )
        agg = CostTransaction.objects.filter(
            work_order=self.wo,
        ).aggregate(total=Sum("amount"))["total"]
        self.assertEqual(agg, Decimal("0.00"))

    def test_chain_scenario_c_positive_reversal_replacement(self):
        """Scenario C: post +100, then explicit cancellation, then post +120.
        SUM(amount) = +120.

        The cancellation marks the +100 as superseded. The follow-up +120
        post finds +100 as the active row and replaces it: -100 reversal
        + +120 = +120. Net = +100 - 100 - 100 + 120 = +120.
        """
        original = CostLedgerService._post(
            amount=Decimal("100.00"), category=CostCategory.MATERIAL,
            quantity=None, unit_cost=None,
            work_order=self.wo, machine=self.machine, component=None,
            source_type="part_issue_line", source_id=1,
            actor=self.manager, memo="v1",
        )
        CostTransaction.objects.create(
            amount=Decimal("-100.00"),
            category=CostCategory.MATERIAL,
            currency="SAR",
            work_order=self.wo, machine=self.machine, component=None,
            source_type="part_issue_line", source_id=1,
            is_reversal=True,
            supersedes=original,
            actor=self.manager, memo="v2 cancellation",
        )
        CostLedgerService._post(
            amount=Decimal("120.00"), category=CostCategory.MATERIAL,
            quantity=None, unit_cost=None,
            work_order=self.wo, machine=self.machine, component=None,
            source_type="part_issue_line", source_id=1,
            actor=self.manager, memo="v3 replacement",
        )
        agg = CostTransaction.objects.filter(
            work_order=self.wo,
        ).aggregate(total=Sum("amount"))["total"]
        self.assertEqual(agg, Decimal("120.00"))


# ---------------------------------------------------------------------------
# 6) Source-specific guard tests
# ---------------------------------------------------------------------------


class SourceSpecificGuardTests(TestCase):
    """Each source-specific poster is a no-op when the source isn't
    in the expected state."""

    def setUp(self):
        self.manager = _make_user("manager_guard", User.Role.MANAGER)
        self.tech = _make_user("tech_guard", User.Role.TECHNICIAN)
        self.machine = Machine.objects.create(name="Press-Guard", qr_code="PG-1")
        self.part = SparePart.objects.create(sku="PG-BRG", name="Bearing G1")
        self.wo = _make_wo(machine=self.machine, created_by=self.manager)

    def test_post_material_only_when_issued(self):
        """post_material is a no-op when the line is PENDING or REJECTED."""
        # PENDING line
        pending = PartIssueLine.objects.create(
            work_order=self.wo, part=self.part, quantity=Decimal("1"),
            unit_cost=Decimal("10"),
            status=PartIssueLine.Status.PENDING,
            requested_by=self.tech, issued_by=self.tech,
            requested_qty=Decimal("1"), approved_qty=Decimal("0"),
            issued_qty=Decimal("0"),
        )
        self.assertIsNone(CostLedgerService.post_material(
            part_issue_line=pending, actor=self.manager, memo="",
        ))
        # REJECTED line
        rejected = PartIssueLine.objects.create(
            work_order=self.wo, part=self.part, quantity=Decimal("1"),
            unit_cost=Decimal("10"),
            status=PartIssueLine.Status.REJECTED,
            requested_by=self.tech, issued_by=self.tech,
            requested_qty=Decimal("1"), approved_qty=Decimal("0"),
            issued_qty=Decimal("0"),
        )
        self.assertIsNone(CostLedgerService.post_material(
            part_issue_line=rejected, actor=self.manager, memo="",
        ))
        self.assertEqual(CostTransaction.objects.count(), 0)

    def test_post_vendor_repair_only_when_closed(self):
        """post_vendor_repair is a no-op for non-CLOSED EROs."""
        from maintenance.models import ExternalRepairOrder
        for status in (
            ExternalRepairOrder.Status.DRAFT,
            ExternalRepairOrder.Status.SENT_TO_VENDOR,
            ExternalRepairOrder.Status.RETURNED,
        ):
            ero = ExternalRepairOrder.objects.create(
                work_order=self.wo, machine=self.machine,
                title="t", description="d",
                actual_cost=Decimal("100"),
                status=status,
                created_by=self.manager,
            )
            self.assertIsNone(CostLedgerService.post_vendor_repair(
                external_repair_order=ero, actor=self.manager, memo="",
            ))
        self.assertEqual(CostTransaction.objects.count(), 0)

    def test_post_consumable_only_for_consumable_use_movement(self):
        """post_consumable is a no-op for non-CONSUMABLE_USE StockMovements."""
        from inventory.models import Inventory, StockMovement
        site = Site.objects.create(name="Main", code="main", is_default=True)
        Inventory.objects.create(part=self.part, site=site, quantity_available=Decimal("100"))
        for mt in (
            StockMovement.MovementType.STOCK_IN,
            StockMovement.MovementType.ISSUE_TO_WO,
            StockMovement.MovementType.STOCK_OUT,
            StockMovement.MovementType.ADJUSTMENT,
        ):
            sm = StockMovement.objects.create(
                part=self.part, site=site,
                movement_type=mt, quantity=Decimal("1"),
                unit_cost=Decimal("5"),
                performed_by=self.tech,
            )
            self.assertIsNone(CostLedgerService.post_consumable(
                stock_movement=sm, actor=self.tech, memo="",
            ))
        self.assertEqual(CostTransaction.objects.count(), 0)

    def test_post_adjustment_memo_too_short_validation(self):
        """CostAdjustment.clean() rejects memo < 10 chars."""
        from maintenance.models import CostAdjustment
        adj = CostAdjustment(
            work_order=self.wo, amount=Decimal("1.00"),
            memo="short", created_by=self.manager,
        )
        with self.assertRaises(ValidationError) as cm:
            adj.clean()
        self.assertIn("memo", cm.exception.message_dict)


# ---------------------------------------------------------------------------
# 7) Management command tests
# ---------------------------------------------------------------------------


class BackfillCommandTests(TestCase):
    def setUp(self):
        self.manager = _make_user("manager_bf", User.Role.MANAGER)
        self.machine = Machine.objects.create(name="Press-BF", qr_code="PB-1")
        self.wo = _make_wo(machine=self.machine, created_by=self.manager)

    def test_backfill_skips_zero_amounts(self):
        """A WO with all-zero cost fields produces no backfill entries."""
        # Create a WorkOrderCost that the auto-calculate on save will
        # zero out (no linked part_issues / external_repairs / movements).
        cost = WorkOrderCost.objects.create(work_order=self.wo)
        # Force a clean state for the test
        cost.material_cost = Decimal("0")
        cost.vendor_repair_cost = Decimal("0")
        cost.consumables_cost = Decimal("0")
        cost.additional_cost = Decimal("0")
        cost.save(update_fields=[
            "material_cost", "vendor_repair_cost", "consumables_cost", "additional_cost",
        ])

        from io import StringIO
        from django.core.management import call_command
        out = StringIO()
        call_command("backfill_cost_ledger", stdout=out)
        self.assertEqual(CostTransaction.objects.count(), 0)

    def test_backfill_dry_run(self):
        """--dry-run prints entries but creates no rows."""
        cost = WorkOrderCost.objects.create(work_order=self.wo)
        cost.material_cost = Decimal("75.00")
        cost.vendor_repair_cost = Decimal("0")
        cost.consumables_cost = Decimal("0")
        cost.additional_cost = Decimal("0")
        cost.save(update_fields=[
            "material_cost", "vendor_repair_cost", "consumables_cost", "additional_cost",
        ])

        from io import StringIO
        from django.core.management import call_command
        out = StringIO()
        call_command("backfill_cost_ledger", "--dry-run", stdout=out)
        self.assertEqual(CostTransaction.objects.count(), 0)
        self.assertIn("Would post 1 backfill", out.getvalue())


class RebuildCommandTests(TestCase):
    def setUp(self):
        self.manager = _make_user("manager_rb", User.Role.MANAGER)
        self.machine = Machine.objects.create(name="Press-RB", qr_code="PR-1")
        self.wo = _make_wo(machine=self.machine, created_by=self.manager)

    def test_rebuild_wo_cost_cache_matches_ledger(self):
        """rebuild_wo_cost_cache sets fields from the ledger."""
        # Post 3 categories directly via the service
        CostLedgerService._post(
            amount=Decimal("100.00"), category=CostCategory.MATERIAL,
            quantity=None, unit_cost=None,
            work_order=self.wo, machine=self.machine, component=None,
            source_type="part_issue_line", source_id=1, actor=self.manager, memo="",
        )
        CostLedgerService._post(
            amount=Decimal("50.00"), category=CostCategory.VENDOR_REPAIR,
            quantity=None, unit_cost=None,
            work_order=self.wo, machine=self.machine, component=None,
            source_type="external_repair_order", source_id=2, actor=self.manager, memo="",
        )
        CostLedgerService._post(
            amount=Decimal("20.00"), category=CostCategory.CONSUMABLE,
            quantity=None, unit_cost=None,
            work_order=self.wo, machine=self.machine, component=None,
            source_type="stock_movement", source_id=3, actor=self.manager, memo="",
        )

        # Wreck the cache
        cost = WorkOrderCost.objects.get(work_order=self.wo)
        cost.material_cost = Decimal("999")
        cost.vendor_repair_cost = Decimal("999")
        cost.consumables_cost = Decimal("999")
        cost.ledger_transaction_count = 0
        cost.save(update_fields=[
            "material_cost", "vendor_repair_cost", "consumables_cost", "ledger_transaction_count",
        ])

        from io import StringIO
        from django.core.management import call_command
        out = StringIO()
        call_command("rebuild_wo_cost_cache", stdout=out)

        cost.refresh_from_db()
        self.assertEqual(cost.material_cost, Decimal("100.00"))
        self.assertEqual(cost.vendor_repair_cost, Decimal("50.00"))
        self.assertEqual(cost.consumables_cost, Decimal("20.00"))
        self.assertEqual(cost.ledger_transaction_count, 3)
