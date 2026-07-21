"""Phase UC-06 — PR/PO cancellation paths must close SHORTAGE blockers.

Bug: purchase_order_cancel and purchase_order_close_short both left
the SHORTAGE WO Blocker keyed to the linked PR's source PSR stuck
OPEN. The "Awaiting Procurement" panel and the
operational_status=pending_parts flag stuck on even after the
procurement round was over.

Fix: a shared helper sync_shortage_blocker_after_pr_change() (in
procurement/services.py) wraps WorkOrderBlockerService.sync_from_external_event
and fires PR_CANCELLED / PO_CANCELLED / PO_CLOSED_SHORT events. The new
handlers in services_blocker.py resolve the SHORTAGE blocker with a
clear resolution_note.
"""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase

from inventory.models import (
    PartIssueLine,
    PartShortageReport,
    SparePart,
)
from inventory.services import (
    create_shortage_decision,
    request_part_on_wo,
)
from maintenance.models import Machine, Site, WorkOrder, WorkOrderBlocker
from procurement.models import PurchaseOrder, PurchaseOrderItem, PurchaseRequest, Supplier
from procurement.services import sync_shortage_blocker_after_pr_change


User = get_user_model()


def _make_user(username, role):
    return User.objects.create_user(username=username, password="x", role=role)


def _make_part(sku, on_hand=Decimal("2")):
    site = Site.objects.filter(is_default=True).first() or Site.objects.create(
        name="BlkSyncSite", is_default=True, is_active=True,
    )
    p = SparePart.objects.create(
        sku=sku, name=sku, status="active",
        avg_cost=Decimal("10"), last_purchase_cost=Decimal("10"),
    )
    from inventory.models import Inventory
    Inventory.objects.create(part=p, site=site, quantity_available=on_hand)
    return p


def _make_machine(name="BLK-MACH"):
    site = Site.objects.filter(is_default=True).first()
    return Machine.objects.create(
        name=name, qr_code=name[:6].lower(), asset_level=3,
        asset_code=name[:8], is_active=True, site=site,
    )


def _make_wo(machine, tech, mgr):
    return WorkOrder.objects.create(
        machine=machine,
        lifecycle_status=WorkOrder.LifecycleStatus.ASSIGNED,
        assigned_technician=tech, created_by=mgr,
    )


def _open_shortage_blocker(wo, psr):
    """Direct construction of an open SHORTAGE blocker keyed to a PSR.
    Mirrors procurement/services.py:auto_create_pr_for_shortage without
    dragging in the auto-PR side effects."""
    from django.contrib.contenttypes.models import ContentType
    from maintenance.services_blocker import WorkOrderBlockerService
    ct = ContentType.objects.get_for_model(PartShortageReport)
    return WorkOrderBlockerService.open_blocker(
        work_order=wo, kind=WorkOrderBlocker.Kind.SHORTAGE,
        external_obj=psr, opened_by=_make_user("opener", User.Role.MANAGER),
        external_label=f"{(psr.part.name)} × 5",
    )


class CancelEventsResolveShortageBlockerTests(TestCase):
    """PR_CANCELLED / PO_CANCELLED / PO_CLOSED_SHORT resolve the SHORTAGE blocker."""

    def setUp(self):
        self.mgr = _make_user("blks_mgr", User.Role.MANAGER)
        self.proc = _make_user("blks_proc", User.Role.PROCUREMENT)
        self.tech = _make_user("blks_tech", User.Role.TECHNICIAN)
        self.machine = _make_machine()
        self.part = _make_part(sku="BLKS-F", on_hand=Decimal("2"))
        self.wo = _make_wo(self.machine, self.tech, self.mgr)

    def _build_shortage_with_blocker(self):
        result = request_part_on_wo(
            wo=self.wo, part=self.part,
            quantity=Decimal("5"), technician=self.tech,
        )
        psr = result["shortage_report"]
        create_shortage_decision(
            report=psr, decision_type="approve",
            approved_issue_qty=Decimal("0"),
            approved_procurement_qty=Decimal("5"),
            rejected_qty=Decimal("0"),
            decided_by=self.mgr,
        )
        return psr

    def test_po_cancel_resolves_shortage_blocker(self):
        psr = self._build_shortage_with_blocker()
        pr = PurchaseRequest.objects.get(source_shortage_report=psr)
        # Sanity: SHORTAGE blocker is OPEN.
        self.assertTrue(
            WorkOrderBlocker.objects.filter(
                work_order=self.wo,
                kind=WorkOrderBlocker.Kind.SHORTAGE,
                status=WorkOrderBlocker.Status.OPEN,
            ).exists()
        )
        sync_shortage_blocker_after_pr_change(
            pr=pr, event_type="PO_CANCELLED", actor=self.proc,
        )
        self.assertFalse(
            WorkOrderBlocker.objects.filter(
                work_order=self.wo,
                kind=WorkOrderBlocker.Kind.SHORTAGE,
                status=WorkOrderBlocker.Status.OPEN,
            ).exists()
        )

    def test_pr_cancelled_resolves_shortage_blocker(self):
        psr = self._build_shortage_with_blocker()
        pr = PurchaseRequest.objects.get(source_shortage_report=psr)
        sync_shortage_blocker_after_pr_change(
            pr=pr, event_type="PR_CANCELLED", actor=self.proc,
        )
        self.assertFalse(
            WorkOrderBlocker.objects.filter(
                work_order=self.wo,
                kind=WorkOrderBlocker.Kind.SHORTAGE,
                status=WorkOrderBlocker.Status.OPEN,
            ).exists()
        )

    def test_po_closed_short_resolves_shortage_blocker(self):
        psr = self._build_shortage_with_blocker()
        pr = PurchaseRequest.objects.get(source_shortage_report=psr)
        sync_shortage_blocker_after_pr_change(
            pr=pr, event_type="PO_CLOSED_SHORT", actor=self.proc,
        )
        self.assertFalse(
            WorkOrderBlocker.objects.filter(
                work_order=self.wo,
                kind=WorkOrderBlocker.Kind.SHORTAGE,
                status=WorkOrderBlocker.Status.OPEN,
            ).exists()
        )
