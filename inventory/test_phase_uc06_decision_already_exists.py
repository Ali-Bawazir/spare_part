"""Phase UC-06 — create_shortage_decision must refuse when a decision
already exists for the PartShortageReport.

Bug: OneToOne(part_shortage_report) lets create_shortage_decision
silently insert a new PSD row when the report was reset to
PENDING_REVIEW, leaving the audit log referring to two different PSD
pks (e.g. part_shortage_decided with decision_id=5 followed by
part_shortage_decision_edited with decision_id=7). The displayed
history then asserts contradictory facts.

Fix: create_shortage_decision now refuses with a ValidationError when
report.decision is already set, forcing the caller to edit instead.
"""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase

from inventory.models import (
    PartShortageDecision,
    PartShortageReport,
    SparePart,
)
from inventory.services import (
    create_shortage_decision,
    request_part_on_wo,
)
from maintenance.models import Machine, Site, WorkOrder


User = get_user_model()


def _make_user(username, role):
    return User.objects.create_user(username=username, password="x", role=role)


def _make_part(sku, on_hand=Decimal("2")):
    site = Site.objects.filter(is_default=True).first() or Site.objects.create(
        name="DecExSite", is_default=True, is_active=True,
    )
    p = SparePart.objects.create(
        sku=sku, name=sku, status="active",
        avg_cost=Decimal("10"), last_purchase_cost=Decimal("10"),
    )
    from inventory.models import Inventory
    Inventory.objects.create(part=p, site=site, quantity_available=on_hand)
    return p


def _make_machine(name="DEC-MACH"):
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


class CreateDecisionRefusesOnExistingTests(TestCase):
    """create_shortage_decision refuses when the PSR already has a
    PartShortageDecision attached."""

    def setUp(self):
        self.mgr = _make_user("decex_mgr", User.Role.MANAGER)
        self.tech = _make_user("decex_tech", User.Role.TECHNICIAN)
        self.machine = _make_machine()
        self.part = _make_part(sku="DECEX-F", on_hand=Decimal("2"))
        self.wo = _make_wo(self.machine, self.tech, self.mgr)

    def test_second_decide_call_raises(self):
        result = request_part_on_wo(
            wo=self.wo, part=self.part,
            quantity=Decimal("5"), technician=self.tech,
        )
        psr = result["shortage_report"]

        # First decide: succeeds, creates PSD row.
        create_shortage_decision(
            report=psr, decision_type="approve",
            approved_issue_qty=Decimal("2"),
            approved_procurement_qty=Decimal("3"),
            rejected_qty=Decimal("0"),
            decided_by=self.mgr,
        )
        self.assertEqual(
            PartShortageDecision.objects.filter(report=psr).count(),
            1,
        )

        # Pretend the report was reset to PENDING_REVIEW (test prep
        # bypass). create_shortage_decision must now refuse instead
        # of silently overwriting PSD.
        psr.status = PartShortageReport.Status.PENDING_REVIEW
        psr.save(update_fields=["status"])

        with self.assertRaises(ValidationError):
            create_shortage_decision(
                report=psr, decision_type="approve",
                approved_issue_qty=Decimal("2"),
                approved_procurement_qty=Decimal("3"),
                rejected_qty=Decimal("0"),
                decided_by=self.mgr,
            )

        # Still only one PSD on the report — no overwrite happened.
        self.assertEqual(
            PartShortageDecision.objects.filter(report=psr).count(),
            1,
        )
