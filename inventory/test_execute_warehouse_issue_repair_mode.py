"""Tests for the skip_approval_check parameter on execute_warehouse_issue.

`skip_approval_check` is a narrowly-scoped flag used ONLY by the
`repair_part_lines` management command to finalize lines where the
manager split the decision (0 issue + N procurement) and stock later
arrived without an auto-issue. The flag bypasses the `approved_qty>0`
check ONLY. Every other precondition, the inventory deduction, the
StockMovement, the PART_ISSUED sync event, and the line state
transition all run normally.

The docs around the gate in services.py make this contract explicit.
These tests pin the contract.
"""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase

from inventory.models import (
    Inventory,
    PartIssueLine,
    PartShortageReport,
    SparePart,
    StockMovement,
)
from inventory.services import execute_warehouse_issue
from maintenance.models import WorkOrder, WorkOrderBlocker


User = get_user_model()


def _bootstrap_part(quantity=2, opening_qty=2):
    """Create a SparePart with default-site inventory of `opening_qty`."""
    from maintenance.models import Site
    site, _ = Site.objects.get_or_create(
        code="MF", defaults={"name": "Main Factory", "is_default": True, "is_active": True},
    )
    part, _ = SparePart.objects.get_or_create(
        sku=f"TEST-{User.objects.count()}-{quantity}",
        defaults={"name": "Test Part", "is_consumable": False},
    )
    inv, _ = Inventory.objects.update_or_create(
        part=part, site=site,
        defaults={"quantity_available": Decimal(str(opening_qty))},
    )
    return part, inv, site


def _make_user(username, role=User.Role.MANAGER):
    u, _ = User.objects.get_or_create(
        username=username,
        defaults={"role": role, "is_active": True},
    )
    u.role = role
    u.is_active = True
    u.save()
    return u


def _make_wo(tech):
    from maintenance.models import Site, Machine
    site, _ = Site.objects.get_or_create(
        code="MAIN-T", defaults={"name": "Main Factory", "is_default": True, "is_active": True},
    )
    machine, _ = Machine.objects.get_or_create(
        qr_code="T-1", defaults={"name": "T-1", "is_active": True},
    )
    return WorkOrder.objects.create(
        number=90000 + WorkOrder.objects.count(),
        category="breakdown",
        lifecycle_status="in_progress",
        operational_status="active",
        assigned_technician=tech,
        created_by=tech,
        machine=machine,
    )


class ExecuteWarehouseIssueRepairModeTests(TestCase):
    """The skip_approval_check flag — only skips the approved_qty check."""

    def setUp(self):
        self.manager = _make_user("mgr")
        self.tech = _make_user("tech", role=User.Role.TECHNICIAN)
        self.part, self.inv, _ = _bootstrap_part(opening_qty=5)
        self.wo = _make_wo(self.tech)

    def _make_approved_line(self, approved_qty=0, quantity=2):
        """Create an approved line. approved_qty=0 simulates split-decision."""
        return PartIssueLine.objects.create(
            work_order=self.wo,
            part=self.part,
            quantity=quantity,
            unit_cost=Decimal("0"),
            invoice_ref="",
            supplier_name="",
            status=PartIssueLine.Status.APPROVED,
            approved_by=self.manager,
            approved_qty=Decimal(str(approved_qty)),
            shortage_qty=Decimal(str(quantity - approved_qty)),
            issued_qty=0,
            issued_by=self.tech,
        )

    def test_normal_mode_requires_approved_qty(self):
        """Default: approved_qty=0 → raises ValueError."""
        line = self._make_approved_line(approved_qty=0, quantity=2)
        with self.assertRaises(ValueError) as ctx:
            execute_warehouse_issue(line=line, qty=2, actor=self.manager)
        self.assertIn("approved_qty", str(ctx.exception))

    def test_repair_mode_skips_approved_qty_check(self):
        """skip_approval_check=True with approved_qty=0 → succeeds."""
        line = self._make_approved_line(approved_qty=0, quantity=2)
        result = execute_warehouse_issue(
            line=line, qty=2, actor=self.manager, skip_approval_check=True,
        )
        self.assertIn("actual_issued", result)
        self.assertEqual(result["actual_issued"], 2)

    def test_repair_mode_still_enforces_status_check(self):
        """A line already in ISSUED status cannot be issued again,
        even with skip_approval_check=True. The status check is
        physical/workflow, not approval."""
        line = self._make_approved_line(approved_qty=2, quantity=2)
        line.status = PartIssueLine.Status.ISSUED
        line.issued_qty = 2
        line.save()
        with self.assertRaises(ValueError) as ctx:
            execute_warehouse_issue(
                line=line, qty=2, actor=self.manager, skip_approval_check=True,
            )
        self.assertIn("cannot issue", str(ctx.exception))

    def test_repair_mode_still_enforces_inventory_check(self):
        """A line whose part has 0 stock cannot be issued, even with
        skip_approval_check=True. Inventory is a physical precondition."""
        self.inv.quantity_available = Decimal("0")
        self.inv.save()
        line = self._make_approved_line(approved_qty=0, quantity=2)
        with self.assertRaises(ValueError) as ctx:
            execute_warehouse_issue(
                line=line, qty=2, actor=self.manager, skip_approval_check=True,
            )
        # The error mentions quantity_available or stock
        self.assertTrue(
            "quantity_available" in str(ctx.exception).lower()
            or "stock" in str(ctx.exception).lower()
        )

    def test_repair_mode_records_qty_and_issued_by(self):
        """The line is fully transitioned: status=ISSUED, issued_qty=qty,
        issued_by=actor. No silent partial state."""
        line = self._make_approved_line(approved_qty=0, quantity=2)
        execute_warehouse_issue(
            line=line, qty=2, actor=self.manager, skip_approval_check=True,
        )
        line.refresh_from_db()
        self.assertEqual(line.status, PartIssueLine.Status.ISSUED)
        self.assertEqual(line.issued_qty, Decimal("2"))
        self.assertEqual(line.issued_by, self.manager)

    def test_repair_mode_fires_part_issued_sync_event(self):
        """The PART_ISSUED sync event is fired by execute_warehouse_issue
        (not by the command). Any open PART blocker for the line should
        resolve via the business-state rule."""
        from django.contrib.contenttypes.models import ContentType
        line = self._make_approved_line(approved_qty=0, quantity=2)
        # Pre-create the blocker (it would normally be created when the
        # shortage was approved, but we're testing in isolation)
        ct = ContentType.objects.get_for_model(line)
        blocker = WorkOrderBlocker.objects.create(
            work_order=self.wo,
            kind=WorkOrderBlocker.Kind.PART,
            status=WorkOrderBlocker.Status.OPEN,
            content_type=ct,
            object_id=line.pk,
            external_label=line.part.sku,
        )

        execute_warehouse_issue(
            line=line, qty=2, actor=self.manager, skip_approval_check=True,
        )

        blocker.refresh_from_db()
        self.assertEqual(blocker.status, WorkOrderBlocker.Status.RESOLVED)

    def test_repair_mode_deducts_inventory(self):
        """Inventory deduction must still happen — that's a physical
        operation, not a workflow check."""
        line = self._make_approved_line(approved_qty=0, quantity=2)
        execute_warehouse_issue(
            line=line, qty=2, actor=self.manager, skip_approval_check=True,
        )
        self.inv.refresh_from_db()
        self.assertEqual(self.inv.quantity_available, Decimal("3"))  # 5 - 2

    def test_repair_mode_writes_stock_movement(self):
        """A StockMovement(ISSUE_TO_WO) is written, just like the normal
        warehouse flow."""
        line = self._make_approved_line(approved_qty=0, quantity=2)
        execute_warehouse_issue(
            line=line, qty=2, actor=self.manager, skip_approval_check=True,
        )
        moves = StockMovement.objects.filter(
            part=self.part, movement_type=StockMovement.MovementType.ISSUE_TO_WO,
            work_order=self.wo,
        )
        self.assertTrue(moves.exists(), "Expected an ISSUE_TO_WO StockMovement")


class ExecuteWarehouseIssueSetsStatusToIssuedTests(TestCase):
    """execute_warehouse_issue must transition the line to ISSUED.

    A pre-existing bug left the line as APPROVED after issue, which
    caused the blocker service's ISSUED-state rule to never fire. The
    fix sets `line.status = ISSUED` so the PART_ISSUED sync event
    properly resolves the corresponding PART blocker.
    """

    def setUp(self):
        from accounts.models import User
        self.manager = _make_user("mgr_status", role=User.Role.MANAGER)
        self.tech = _make_user("tech_status", role=User.Role.TECHNICIAN)
        self.part, self.inv, _ = _bootstrap_part(opening_qty=5)
        from maintenance.models import WorkOrder
        self.wo = WorkOrder.objects.create(
            number=88000 + WorkOrder.objects.count(),
            category="breakdown",
            lifecycle_status="in_progress",
            operational_status="active",
            assigned_technician=self.tech,
            created_by=self.tech,
        )

    def test_status_set_to_issued_after_normal_flow(self):
        """Normal flow: approved_qty=2, qty=2 → status ends as ISSUED."""
        from inventory.models import PartIssueLine
        line = PartIssueLine.objects.create(
            work_order=self.wo, part=self.part,
            quantity=2, unit_cost=0,
            status=PartIssueLine.Status.APPROVED,
            approved_by=self.manager, approved_qty=2,
            issued_qty=0, issued_by=self.tech,
        )
        execute_warehouse_issue(line=line, qty=2, actor=self.manager)
        line.refresh_from_db()
        self.assertEqual(line.status, PartIssueLine.Status.ISSUED)

    def test_status_set_to_issued_after_repair_mode(self):
        """Repair flow: approved_qty=0, qty=2 (skip_approval_check=True) → ISSUED."""
        from inventory.models import PartIssueLine
        line = PartIssueLine.objects.create(
            work_order=self.wo, part=self.part,
            quantity=2, unit_cost=0,
            status=PartIssueLine.Status.APPROVED,
            approved_by=self.manager, approved_qty=0,
            issued_qty=0, issued_by=self.tech,
        )
        execute_warehouse_issue(
            line=line, qty=2, actor=self.manager, skip_approval_check=True,
        )
        line.refresh_from_db()
        self.assertEqual(line.status, PartIssueLine.Status.ISSUED)
