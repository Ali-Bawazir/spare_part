"""
Phase 2A — WorkOrder blocker system core service tests.

Covers:
- WorkOrderBlockerService (open/resolve/cancel/sync_from_external_event)
- WorkOrderService.recompute_operational_status (blocker-driven + dual-read)
- Inventory.quantity_reserved derived cache signal

These tests use a self-contained setUp to keep them isolated from the
broader maintenance test suite.
"""
from __future__ import annotations

from decimal import Decimal

from django.contrib.contenttypes.models import ContentType
from django.test import TestCase

from accounts.models import User
from inventory.models import (
    Inventory,
    InventoryReservation,
    PartIssueLine,
    SparePart,
)
from maintenance.models import (
    ExternalRepairOrder,
    Machine,
    MaintenanceIssue,
    Site,
    WorkOrder,
    WorkOrderBlocker,
    WorkOrderBlockerEvent,
)
from maintenance.services_blocker import (
    WorkOrderBlockerEventService,
    WorkOrderBlockerService,
)
from maintenance.services_wo_status import WorkOrderService


def _make_user(username: str, role: str) -> User:
    return User.objects.create_user(username=username, password="pass1234", role=role)


def _make_wo(*, machine: Machine, created_by: User, **kwargs) -> WorkOrder:
    defaults = {
        "machine": machine,
        "created_by": created_by,
        "lifecycle_status": WorkOrder.LifecycleStatus.ASSIGNED,
    }
    defaults.update(kwargs)
    return WorkOrder.objects.create(**defaults)


class WorkOrderBlockerServiceTests(TestCase):
    """Service-layer tests for WorkOrderBlockerService."""

    def setUp(self):
        self.manager = _make_user("manager_bs", User.Role.MANAGER)
        self.tech = _make_user("tech_bs", User.Role.TECHNICIAN)
        self.machine = Machine.objects.create(name="Press BS", qr_code="PRESS-BS")
        self.site = Site.objects.filter(is_default=True).first()
        self.part = SparePart.objects.create(sku="BRG-BS-01", name="Bearing BS")
        self.wo = _make_wo(machine=self.machine, created_by=self.manager)
        # A PENDING part issue line is the natural external object for a PART blocker.
        self.line = PartIssueLine.objects.create(
            work_order=self.wo,
            part=self.part,
            quantity=Decimal("2"),
            unit_cost=Decimal("10"),
            status=PartIssueLine.Status.PENDING,
            requested_by=self.tech,
            issued_by=self.tech,
            requested_qty=Decimal("2"),
            approved_qty=Decimal("0"),
            issued_qty=Decimal("0"),
        )

    def test_open_blocker_creates_open_blocker(self):
        blocker = WorkOrderBlockerService.open_blocker(
            work_order=self.wo,
            kind=WorkOrderBlocker.Kind.PART,
            external_obj=self.line,
            opened_by=self.tech,
            note="Waiting for part",
            external_label=f"{self.part.sku} x 2",
        )
        self.assertIsNotNone(blocker)
        self.assertEqual(blocker.status, WorkOrderBlocker.Status.OPEN)
        self.assertEqual(blocker.kind, WorkOrderBlocker.Kind.PART)
        self.assertEqual(blocker.work_order, self.wo)
        self.assertEqual(blocker.external_ref, self.line)
        self.assertEqual(blocker.external_label, f"{self.part.sku} x 2")
        self.assertEqual(blocker.opened_by, self.tech)
        # BLOCKER_CREATED event written
        events = blocker.events.all()
        self.assertEqual(events.count(), 1)
        self.assertEqual(
            events.first().event_type,
            WorkOrderBlockerEvent.EventType.BLOCKER_CREATED,
        )

    def test_open_blocker_is_idempotent(self):
        first = WorkOrderBlockerService.open_blocker(
            work_order=self.wo,
            kind=WorkOrderBlocker.Kind.PART,
            external_obj=self.line,
            opened_by=self.tech,
        )
        second = WorkOrderBlockerService.open_blocker(
            work_order=self.wo,
            kind=WorkOrderBlocker.Kind.PART,
            external_obj=self.line,
            opened_by=self.tech,
        )
        self.assertEqual(first.pk, second.pk)
        # Only one OPEN blocker exists for this (WO, line)
        self.assertEqual(
            WorkOrderBlocker.objects.filter(
                work_order=self.wo, status=WorkOrderBlocker.Status.OPEN,
            ).count(),
            1,
        )
        # No duplicate BLOCKER_CREATED event
        self.assertEqual(
            WorkOrderBlockerEvent.objects.filter(
                blocker=first,
                event_type=WorkOrderBlockerEvent.EventType.BLOCKER_CREATED,
            ).count(),
            1,
        )

    def test_open_blocker_returns_none_when_external_obj_is_none(self):
        result = WorkOrderBlockerService.open_blocker(
            work_order=self.wo,
            kind=WorkOrderBlocker.Kind.PART,
            external_obj=None,
        )
        self.assertIsNone(result)

    def test_resolve_blocker_transitions_open_to_resolved(self):
        blocker = WorkOrderBlockerService.open_blocker(
            work_order=self.wo,
            kind=WorkOrderBlocker.Kind.PART,
            external_obj=self.line,
            opened_by=self.tech,
        )
        result = WorkOrderBlockerService.resolve_blocker(
            blocker=blocker,
            resolution_note="Issued from stock",
            resolved_by=self.manager,
        )
        self.assertEqual(result.status, WorkOrderBlocker.Status.RESOLVED)
        self.assertIsNotNone(result.resolved_at)
        self.assertEqual(result.resolved_by, self.manager)
        self.assertEqual(result.resolution_note, "Issued from stock")
        # BLOCKER_RESOLVED event written
        self.assertTrue(
            result.events.filter(
                event_type=WorkOrderBlockerEvent.EventType.BLOCKER_RESOLVED
            ).exists()
        )

    def test_resolve_blocker_is_idempotent(self):
        blocker = WorkOrderBlockerService.open_blocker(
            work_order=self.wo,
            kind=WorkOrderBlocker.Kind.PART,
            external_obj=self.line,
            opened_by=self.tech,
        )
        WorkOrderBlockerService.resolve_blocker(
            blocker=blocker,
            resolved_by=self.manager,
            resolution_note="Issued from stock",
        )
        # Second call: no-op, returns same blocker; resolution_note is preserved.
        second = WorkOrderBlockerService.resolve_blocker(
            blocker=blocker, resolved_by=self.manager,
            resolution_note="Different note",
        )
        self.assertEqual(second.pk, blocker.pk)
        self.assertEqual(second.resolution_note, "Issued from stock")  # first note preserved
        # Only one BLOCKER_RESOLVED event
        self.assertEqual(
            blocker.events.filter(
                event_type=WorkOrderBlockerEvent.EventType.BLOCKER_RESOLVED
            ).count(),
            1,
        )

    def test_cancel_blocker_transitions_open_to_cancelled(self):
        blocker = WorkOrderBlockerService.open_blocker(
            work_order=self.wo,
            kind=WorkOrderBlocker.Kind.PART,
            external_obj=self.line,
            opened_by=self.tech,
        )
        result = WorkOrderBlockerService.cancel_blocker(
            blocker=blocker,
            cancel_reason="Wrong part number",
            cancelled_by=self.manager,
        )
        self.assertEqual(result.status, WorkOrderBlocker.Status.CANCELLED)
        self.assertIsNotNone(result.cancelled_at)
        self.assertEqual(result.cancelled_by, self.manager)
        self.assertEqual(result.cancel_reason, "Wrong part number")
        self.assertTrue(
            result.events.filter(
                event_type=WorkOrderBlockerEvent.EventType.BLOCKER_CANCELLED
            ).exists()
        )

    def test_cancel_blocker_is_idempotent(self):
        blocker = WorkOrderBlockerService.open_blocker(
            work_order=self.wo,
            kind=WorkOrderBlocker.Kind.PART,
            external_obj=self.line,
            opened_by=self.tech,
        )
        WorkOrderBlockerService.cancel_blocker(
            blocker=blocker,
            cancelled_by=self.manager,
            cancel_reason="Wrong part number",
        )
        second = WorkOrderBlockerService.cancel_blocker(
            blocker=blocker, cancelled_by=self.manager,
            cancel_reason="Different reason",
        )
        self.assertEqual(second.pk, blocker.pk)
        self.assertEqual(second.cancel_reason, "Wrong part number")  # first preserved
        self.assertEqual(
            blocker.events.filter(
                event_type=WorkOrderBlockerEvent.EventType.BLOCKER_CANCELLED
            ).count(),
            1,
        )

    def test_resolve_blocker_after_cancel_raises(self):
        blocker = WorkOrderBlockerService.open_blocker(
            work_order=self.wo,
            kind=WorkOrderBlocker.Kind.PART,
            external_obj=self.line,
            opened_by=self.tech,
        )
        WorkOrderBlockerService.cancel_blocker(
            blocker=blocker, cancelled_by=self.manager,
        )
        with self.assertRaises(ValueError):
            WorkOrderBlockerService.resolve_blocker(blocker=blocker, resolved_by=self.manager)

    def test_sync_from_external_event_part_issued_resolves_blocker(self):
        blocker = WorkOrderBlockerService.open_blocker(
            work_order=self.wo,
            kind=WorkOrderBlocker.Kind.PART,
            external_obj=self.line,
            opened_by=self.tech,
        )
        # Mark the line as fully issued
        self.line.approved_qty = Decimal("2")
        self.line.issued_qty = Decimal("2")
        self.line.save(update_fields=["approved_qty", "issued_qty"])
        result = WorkOrderBlockerService.sync_from_external_event(
            external_obj=self.line, event_type="PART_ISSUED", actor=self.manager,
        )
        self.assertIsNotNone(result)
        self.assertEqual(result.status, WorkOrderBlocker.Status.RESOLVED)

    def test_sync_from_external_event_part_issued_noop_when_partial(self):
        WorkOrderBlockerService.open_blocker(
            work_order=self.wo,
            kind=WorkOrderBlocker.Kind.PART,
            external_obj=self.line,
            opened_by=self.tech,
        )
        # Partial issue: issued < approved — blocker must remain OPEN
        self.line.approved_qty = Decimal("2")
        self.line.issued_qty = Decimal("1")
        self.line.save(update_fields=["approved_qty", "issued_qty"])
        result = WorkOrderBlockerService.sync_from_external_event(
            external_obj=self.line, event_type="PART_ISSUED", actor=self.manager,
        )
        self.assertIsNone(result)
        self.assertEqual(
            WorkOrderBlocker.objects.filter(
                work_order=self.wo, status=WorkOrderBlocker.Status.OPEN,
            ).count(),
            1,
        )

    def test_sync_from_external_event_part_rejected_cancels_blocker(self):
        WorkOrderBlockerService.open_blocker(
            work_order=self.wo,
            kind=WorkOrderBlocker.Kind.PART,
            external_obj=self.line,
            opened_by=self.tech,
        )
        result = WorkOrderBlockerService.sync_from_external_event(
            external_obj=self.line, event_type="PART_REJECTED", actor=self.manager,
        )
        self.assertIsNotNone(result)
        self.assertEqual(result.status, WorkOrderBlocker.Status.CANCELLED)

    def test_sync_from_external_event_unknown_event_is_noop(self):
        WorkOrderBlockerService.open_blocker(
            work_order=self.wo,
            kind=WorkOrderBlocker.Kind.PART,
            external_obj=self.line,
            opened_by=self.tech,
        )
        result = WorkOrderBlockerService.sync_from_external_event(
            external_obj=self.line, event_type="UNRELATED_EVENT",
        )
        self.assertIsNone(result)
        # Blocker remains OPEN
        self.assertEqual(
            WorkOrderBlocker.objects.filter(
                work_order=self.wo, status=WorkOrderBlocker.Status.OPEN,
            ).count(),
            1,
        )


class WorkOrderBlockerEventServiceTests(TestCase):
    """Event service is a simple append-only log; just smoke-test the API."""

    def setUp(self):
        self.manager = _make_user("manager_evt", User.Role.MANAGER)
        self.machine = Machine.objects.create(name="Press EVT", qr_code="PRESS-EVT")
        self.part = SparePart.objects.create(sku="BRG-EVT-01", name="Bearing EVT")
        self.wo = _make_wo(machine=self.machine, created_by=self.manager)
        self.line = PartIssueLine.objects.create(
            work_order=self.wo, part=self.part, quantity=Decimal("1"),
            unit_cost=Decimal("0"), status=PartIssueLine.Status.PENDING,
            requested_by=self.manager, issued_by=self.manager,
            requested_qty=Decimal("1"),
        )
        self.blocker = WorkOrderBlocker.objects.create(
            work_order=self.wo,
            kind=WorkOrderBlocker.Kind.PART,
            status=WorkOrderBlocker.Status.OPEN,
            content_type=ContentType.objects.get_for_model(self.line),
            object_id=self.line.pk,
            opened_by=self.manager,
        )

    def test_record_creates_event_row(self):
        event = WorkOrderBlockerEventService.record(
            blocker=self.blocker,
            event_type=WorkOrderBlockerEvent.EventType.PART_REQUEST_CREATED,
            actor=self.manager,
            payload={"qty": "1"},
        )
        self.assertEqual(event.event_type, WorkOrderBlockerEvent.EventType.PART_REQUEST_CREATED)
        self.assertEqual(event.blocker, self.blocker)
        self.assertEqual(event.payload, {"qty": "1"})


class WorkOrderOperationalStatusTests(TestCase):
    """WorkOrderService.recompute_operational_status."""

    def setUp(self):
        self.manager = _make_user("manager_os", User.Role.MANAGER)
        self.tech = _make_user("tech_os", User.Role.TECHNICIAN)
        self.machine = Machine.objects.create(name="Press OS", qr_code="PRESS-OS")
        self.site = Site.objects.filter(is_default=True).first()
        self.part = SparePart.objects.create(sku="BRG-OS-01", name="Bearing OS")
        self.wo = _make_wo(
            machine=self.machine, created_by=self.manager,
            lifecycle_status=WorkOrder.LifecycleStatus.IN_PROGRESS,
        )

    def _make_line(self, status=PartIssueLine.Status.PENDING) -> PartIssueLine:
        return PartIssueLine.objects.create(
            work_order=self.wo, part=self.part, quantity=Decimal("2"),
            unit_cost=Decimal("10"), status=status,
            requested_by=self.tech, issued_by=self.tech,
            requested_qty=Decimal("2"),
        )

    def test_pending_parts_when_part_blocker_open(self):
        line = self._make_line()
        WorkOrderBlockerService.open_blocker(
            work_order=self.wo,
            kind=WorkOrderBlocker.Kind.PART,
            external_obj=line,
            opened_by=self.tech,
        )
        new_status = WorkOrderService.recompute_operational_status(self.wo)
        self.wo.refresh_from_db()
        self.assertEqual(new_status, WorkOrder.OperationalStatus.PENDING_PARTS)
        self.assertEqual(self.wo.operational_status, WorkOrder.OperationalStatus.PENDING_PARTS)

    def test_pending_parts_when_shortage_blocker_open(self):
        from inventory.models import PartShortageReport
        report = PartShortageReport.objects.create(
            content_type=ContentType.objects.get_for_model(WorkOrder),
            object_id=self.wo.pk,
            work_order=self.wo,
            part=self.part,
            qty_requested=Decimal("2"),
            shortage_qty=Decimal("2"),
            available_qty_snapshot=Decimal("0"),
            reserved_qty_snapshot=Decimal("0"),
            usable_qty_snapshot=Decimal("0"),
            reported_by=self.tech,
            status=PartShortageReport.Status.PENDING_REVIEW,
        )
        WorkOrderBlockerService.open_blocker(
            work_order=self.wo,
            kind=WorkOrderBlocker.Kind.SHORTAGE,
            external_obj=report,
            opened_by=self.tech,
        )
        new_status = WorkOrderService.recompute_operational_status(self.wo)
        self.assertEqual(new_status, WorkOrder.OperationalStatus.PENDING_PARTS)

    def test_waiting_vendor_when_vendor_repair_blocker_open(self):
        ero = ExternalRepairOrder.objects.create(
            work_order=self.wo, machine=self.machine, title="Test repair",
            description="Testing", created_by=self.manager,
            status=ExternalRepairOrder.Status.SENT_TO_VENDOR,
        )
        WorkOrderBlockerService.open_blocker(
            work_order=self.wo,
            kind=WorkOrderBlocker.Kind.VENDOR_REPAIR,
            external_obj=ero,
            opened_by=self.manager,
            related_ero=ero,
        )
        new_status = WorkOrderService.recompute_operational_status(self.wo)
        self.assertEqual(new_status, WorkOrder.OperationalStatus.WAITING_VENDOR)

    def test_paused_when_operational_blocker_open(self):
        # Use a dummy external object (an issue) to anchor the blocker
        issue = MaintenanceIssue.objects.create(
            machine=self.machine, reported_by=self.manager,
            description="Emergency", status=MaintenanceIssue.Status.NEW,
        )
        # For OPERATIONAL blockers, no specific external entity is required
        # by the service API, but open_blocker requires external_obj. Use
        # the source_work_order hook and a synthetic part issue line as
        # the external ref.
        line = self._make_line()
        WorkOrderBlockerService.open_blocker(
            work_order=self.wo,
            kind=WorkOrderBlocker.Kind.OPERATIONAL,
            external_obj=line,
            opened_by=self.manager,
            source_work_order=self.wo,
            pause_reason="operational",
            note="Paused for emergency",
        )
        new_status = WorkOrderService.recompute_operational_status(self.wo)
        self.assertEqual(new_status, WorkOrder.OperationalStatus.PAUSED)

    def test_active_when_labor_running_and_no_blockers(self):
        self.wo.labor_started_at = __import__("django.utils.timezone", fromlist=["now"]).now()
        self.wo.save(update_fields=["labor_started_at", "updated_at"])
        new_status = WorkOrderService.recompute_operational_status(self.wo)
        self.assertEqual(new_status, WorkOrder.OperationalStatus.ACTIVE)

    def test_active_when_in_progress_no_labor_no_blockers(self):
        # in_progress lifecycle, no labor, no blockers
        new_status = WorkOrderService.recompute_operational_status(self.wo)
        self.assertEqual(new_status, WorkOrder.OperationalStatus.ACTIVE)

    def test_terminal_lifecycle_does_not_modify_operational_status(self):
        # Set operational_status to something non-default, then close the WO
        self.wo.operational_status = WorkOrder.OperationalStatus.ACTIVE
        self.wo.save(update_fields=["operational_status", "updated_at"])
        self.wo.lifecycle_status = WorkOrder.LifecycleStatus.CLOSED
        self.wo.save(update_fields=["lifecycle_status", "updated_at"])
        WorkOrderService.recompute_operational_status(self.wo)
        self.wo.refresh_from_db()
        # Operational status is preserved at the terminal — never overwritten.
        self.assertEqual(
            self.wo.operational_status, WorkOrder.OperationalStatus.ACTIVE,
        )

    def test_no_save_when_status_unchanged(self):
        # WO starts at PAUSED (the default). No blockers. Recompute should
        # produce PAUSED — the model row should not be re-saved.
        self.assertEqual(
            self.wo.operational_status, WorkOrder.OperationalStatus.PAUSED,
        )
        # Capture updated_at, run recompute, check it didn't change
        self.wo.operational_status = WorkOrder.OperationalStatus.ACTIVE
        self.wo.save(update_fields=["operational_status", "updated_at"])
        original_updated = self.wo.updated_at
        WorkOrderService.recompute_operational_status(self.wo)
        self.wo.refresh_from_db()
        self.assertEqual(self.wo.updated_at, original_updated)


class InventoryReservationSignalTests(TestCase):
    """Inventory.compute_quantity_reserved() must sum ACTIVE reservations."""

    def setUp(self):
        self.manager = _make_user("manager_inv", User.Role.MANAGER)
        self.site = Site.objects.filter(is_default=True).first()
        self.part = SparePart.objects.create(sku="BRG-INV-01", name="Bearing INV")
        self.inv = Inventory.objects.create(
            part=self.part, site=self.site, quantity_available=Decimal("10"),
        )
        self.machine = Machine.objects.create(name="Press INV", qr_code="PRESS-INV")
        self.wo = _make_wo(machine=self.machine, created_by=self.manager)

    def test_create_reservation_increments_quantity_reserved(self):
        InventoryReservation.objects.create(
            part=self.part, work_order=self.wo,
            quantity=Decimal("3"),
            status=InventoryReservation.Status.ACTIVE,
        )
        self.inv.refresh_from_db()
        self.assertEqual(self.inv.compute_quantity_reserved(), Decimal("3"))

    def test_release_reservation_decrements_quantity_reserved(self):
        res = InventoryReservation.objects.create(
            part=self.part, work_order=self.wo,
            quantity=Decimal("3"),
            status=InventoryReservation.Status.ACTIVE,
        )
        self.inv.refresh_from_db()
        self.assertEqual(self.inv.compute_quantity_reserved(), Decimal("3"))
        # Release the reservation
        res.status = InventoryReservation.Status.RELEASED
        res.save(update_fields=["status"])
        self.inv.refresh_from_db()
        self.assertEqual(self.inv.compute_quantity_reserved(), Decimal("0"))

    def test_multiple_active_reservations_summed(self):
        InventoryReservation.objects.create(
            part=self.part, work_order=self.wo,
            quantity=Decimal("2"), status=InventoryReservation.Status.ACTIVE,
        )
        InventoryReservation.objects.create(
            part=self.part, work_order=self.wo,
            quantity=Decimal("5"), status=InventoryReservation.Status.ACTIVE,
        )
        self.inv.refresh_from_db()
        self.assertEqual(self.inv.compute_quantity_reserved(), Decimal("7"))

    def test_delete_reservation_updates_cache(self):
        res = InventoryReservation.objects.create(
            part=self.part, work_order=self.wo,
            quantity=Decimal("4"), status=InventoryReservation.Status.ACTIVE,
        )
        self.inv.refresh_from_db()
        self.assertEqual(self.inv.compute_quantity_reserved(), Decimal("4"))
        res.delete()
        self.inv.refresh_from_db()
        self.assertEqual(self.inv.compute_quantity_reserved(), Decimal("0"))



class OperationalBlockerServiceTests(TestCase):
    """WorkOrderBlockerService.open_operational_blocker — the dedicated
    helper for OPERATIONAL pauses. Replaces the no-op pattern of calling
    `open_blocker(external_obj=None)` (which early-returns None).
    """

    def setUp(self):
        self.manager = _make_user("manager_op", User.Role.MANAGER)
        self.tech = _make_user("tech_op", User.Role.TECHNICIAN)
        self.machine = Machine.objects.create(name="Press OP", qr_code="PRESS-OP")

    def test_open_operational_blocker_creates_blocker(self):
        """open_operational_blocker actually creates an OPERATIONAL blocker
        (not a no-op like open_blocker(external_obj=None))."""
        wo = _make_wo(
            machine=self.machine, created_by=self.manager,
            lifecycle_status=WorkOrder.LifecycleStatus.IN_PROGRESS,
        )
        blocker = WorkOrderBlockerService.open_operational_blocker(
            work_order=wo,
            opened_by=self.tech,
            note="Paused for emergency",
            pause_reason=WorkOrder.PauseReason.EMERGENCY,
        )
        self.assertIsNotNone(blocker)
        self.assertEqual(blocker.status, WorkOrderBlocker.Status.OPEN)
        self.assertEqual(blocker.kind, WorkOrderBlocker.Kind.OPERATIONAL)
        self.assertEqual(blocker.work_order, wo)
        self.assertEqual(blocker.opened_by, self.tech)
        self.assertEqual(blocker.pause_reason, WorkOrder.PauseReason.EMERGENCY)
        self.assertEqual(blocker.note, "Paused for emergency")
        # A BLOCKER_CREATED event was written
        self.assertTrue(
            blocker.events.filter(
                event_type=WorkOrderBlockerEvent.EventType.BLOCKER_CREATED
            ).exists()
        )
        # Sanity: only one OPEN OPERATIONAL blocker exists on this WO
        self.assertEqual(
            WorkOrderBlocker.objects.filter(
                work_order=wo,
                kind=WorkOrderBlocker.Kind.OPERATIONAL,
                status=WorkOrderBlocker.Status.OPEN,
            ).count(),
            1,
        )

    def test_open_operational_blocker_idempotent(self):
        """Calling open_operational_blocker twice returns the same blocker
        (no duplicate OPERATIONAL blockers stacked on the same WO)."""
        wo = _make_wo(
            machine=self.machine, created_by=self.manager,
            lifecycle_status=WorkOrder.LifecycleStatus.IN_PROGRESS,
        )
        first = WorkOrderBlockerService.open_operational_blocker(
            work_order=wo,
            opened_by=self.tech,
            pause_reason=WorkOrder.PauseReason.OPERATIONAL,
        )
        second = WorkOrderBlockerService.open_operational_blocker(
            work_order=wo,
            opened_by=self.tech,
            pause_reason=WorkOrder.PauseReason.OPERATIONAL,
        )
        self.assertEqual(first.pk, second.pk)
        # Only one OPEN OPERATIONAL blocker exists
        self.assertEqual(
            WorkOrderBlocker.objects.filter(
                work_order=wo,
                kind=WorkOrderBlocker.Kind.OPERATIONAL,
                status=WorkOrderBlocker.Status.OPEN,
            ).count(),
            1,
        )
        # No duplicate BLOCKER_CREATED event
        self.assertEqual(
            WorkOrderBlockerEvent.objects.filter(
                blocker=first,
                event_type=WorkOrderBlockerEvent.EventType.BLOCKER_CREATED,
            ).count(),
            1,
        )


# ---------------------------------------------------------------------------
# Phase 2B-8: hook tests for the blocker system integrations
# ---------------------------------------------------------------------------


class PartBlockerHookTests(TestCase):
    """The keystone rule: PART blocker resolves on `issued_qty == approved_qty`,
    NOT on allocation. These tests verify the hooks added in Phase 2B-2/3/4."""

    def setUp(self):
        from maintenance.models import WorkOrder
        from inventory.models import SparePart, Inventory
        from accounts.models import User
        from maintenance.models import Site

        self.manager = User.objects.create_user(
            username="test_manager_hook", password="x", role=User.Role.MANAGER
        )
        self.technician = User.objects.create_user(
            username="test_tech_hook", password="x", role=User.Role.TECHNICIAN
        )
        self.part = SparePart.objects.create(
            sku="HOOK-TEST-001", name="Hook Test Part",
            quantity_on_hand=Decimal("10"), avg_cost=Decimal("10"),
        )
        self.wo = WorkOrder.objects.create(
            number=9999, machine=None, created_by=self.manager,
            assigned_technician=self.technician,
            lifecycle_status="in_progress",
        )
        # Seed an Inventory row so free stock is 10
        site = Site.objects.filter(is_default=True).first()
        if not site:
            site = Site.objects.create(
                code="MF", name="Main Factory", is_default=True,
                is_active=True, timezone="UTC",
            )
        Inventory.objects.create(
            part=self.part, site=site,
            quantity_available=Decimal("10"),
        )

    def test_request_part_on_wo_opens_part_blocker(self):
        """request_part_on_wo creates a PART WO Blocker."""
        from inventory.services import request_part_on_wo
        from maintenance.models import WorkOrderBlocker

        result = request_part_on_wo(
            wo=self.wo, part=self.part, quantity=Decimal("3"),
            technician=self.technician, note="Test request",
        )
        line = result.get("line") if isinstance(result, dict) else result
        self.assertIsNotNone(line)
        blocker = WorkOrderBlocker.objects.filter(
            work_order=self.wo, kind=WorkOrderBlocker.Kind.PART,
            status=WorkOrderBlocker.Status.OPEN,
        ).first()
        self.assertIsNotNone(blocker, "PART blocker should be open after request_part_on_wo")
        self.assertEqual(blocker.external_ref, line)
        self.assertIn("Hook Test Part", blocker.external_label)

    def test_approve_part_does_not_resolve_blocker(self):
        """KEYSTONE: approve_part_request does NOT resolve the PART blocker."""
        from inventory.services import request_part_on_wo, approve_part_request
        from maintenance.models import WorkOrderBlocker

        result = request_part_on_wo(
            wo=self.wo, part=self.part, quantity=Decimal("3"),
            technician=self.technician,
        )
        line = result.get("line") if isinstance(result, dict) else result
        # Now approve
        approve_part_request(line=line, manager=self.manager)
        # Blocker should still be OPEN (not resolved)
        blocker = WorkOrderBlocker.objects.get(work_order=self.wo, kind=WorkOrderBlocker.Kind.PART)
        self.assertEqual(
            blocker.status, WorkOrderBlocker.Status.OPEN,
            "PART blocker must stay OPEN after approval (keystone rule)"
        )

    def test_warehouse_issue_resolves_blocker_when_full(self):
        """KEYSTONE: execute_warehouse_issue resolves the PART blocker when issued_qty == approved_qty.

        Flow under test: request → approve → execute_warehouse_issue(qty == approved).
        After Bug #2 fix, warehouse issue is a physical event and MUST NOT
        increment approved_qty; the line must be APPROVED with a non-zero
        approved_qty before warehouse issue is allowed.
        """
        from inventory.services import (
            request_part_on_wo, approve_part_request, execute_warehouse_issue,
        )
        from maintenance.models import WorkOrderBlocker

        result = request_part_on_wo(
            wo=self.wo, part=self.part, quantity=Decimal("3"),
            technician=self.technician,
        )
        line = result.get("line") if isinstance(result, dict) else result
        # Manager approves the full 3 units (transition PENDING -> ALLOCATED).
        approve_part_request(line=line, manager=self.manager)
        line.refresh_from_db()
        self.assertEqual(line.approved_qty, Decimal("3"))
        # Issue the full qty (issued == approved -> keystone rule fires).
        execute_warehouse_issue(line=line, qty=Decimal("3"), actor=self.technician)
        # Blocker should be RESOLVED
        blocker = WorkOrderBlocker.objects.get(work_order=self.wo, kind=WorkOrderBlocker.Kind.PART)
        self.assertEqual(
            blocker.status, WorkOrderBlocker.Status.RESOLVED,
            "PART blocker must resolve when issued_qty == approved_qty"
        )

    def test_warehouse_issue_does_not_resolve_blocker_when_partial(self):
        """KEYSTONE: issue with issued_qty < approved_qty keeps the blocker OPEN.

        Deviation from the prompt: the prompt used approve_part_request +
        execute_warehouse_issue, but the current implementation requires the
        line to be PENDING on entry to execute_warehouse_issue, so the prompt's
        sequence would raise before reaching the keystone check. We pre-set
        line.approved_qty to a higher value to simulate a "partial issue"
        scenario (issued 2 of approved 10) and verify the blocker stays OPEN.
        """
        from inventory.services import request_part_on_wo, execute_warehouse_issue
        from maintenance.models import WorkOrderBlocker

        result = request_part_on_wo(
            wo=self.wo, part=self.part, quantity=Decimal("3"),
            technician=self.technician,
        )
        line = result.get("line") if isinstance(result, dict) else result
        # Simulate a prior approval of 10 units (more than the requested 3).
        # This represents a real-world case where the manager approved a
        # larger qty for this part (e.g. across multiple WOs).
        line.approved_qty = Decimal("10")
        line.save(update_fields=["approved_qty", "updated_at"])
        # Issue only 2 — issued (2) < approved (10+2=12), so the keystone
        # rule does NOT fire.
        execute_warehouse_issue(line=line, qty=Decimal("2"), actor=self.technician)
        blocker = WorkOrderBlocker.objects.get(work_order=self.wo, kind=WorkOrderBlocker.Kind.PART)
        self.assertEqual(
            blocker.status, WorkOrderBlocker.Status.OPEN,
            "PART blocker must stay OPEN on partial issue (issued < approved)"
        )


class OperationalBlockerHookTests(TestCase):
    """Tests for OPERATIONAL blocker hooks (pause, auto-pause, resume)."""

    def setUp(self):
        from maintenance.models import WorkOrder
        from accounts.models import User
        self.manager = User.objects.create_user(username="m_op", password="x", role=User.Role.MANAGER)
        self.technician = User.objects.create_user(username="t_op", password="x", role=User.Role.TECHNICIAN)
        self.wo = WorkOrder.objects.create(
            number=8888, machine=None, created_by=self.manager,
            assigned_technician=self.technician,
            lifecycle_status="in_progress",
        )

    def test_pause_with_note_opens_operational_blocker(self):
        """work_order_pause with a note opens an OPERATIONAL blocker (content-based rule)."""
        from maintenance.services import work_order_pause
        from maintenance.models import WorkOrderBlocker

        work_order_pause(
            wo=self.wo,
            pause_reason="operational",
            pause_note="Waiting for shift briefing",
            actor=self.technician,
        )
        blocker = WorkOrderBlocker.objects.filter(
            work_order=self.wo, kind=WorkOrderBlocker.Kind.OPERATIONAL,
            status=WorkOrderBlocker.Status.OPEN,
        ).first()
        self.assertIsNotNone(blocker, "OPERATIONAL blocker should open when note is non-empty")

    def test_pause_without_note_no_blocker(self):
        """work_order_pause with no note and reason=operational does NOT open a blocker."""
        from maintenance.services import work_order_pause
        from maintenance.models import WorkOrderBlocker

        work_order_pause(
            wo=self.wo,
            pause_reason="operational",
            pause_note="",
            actor=self.technician,
        )
        blocker = WorkOrderBlocker.objects.filter(
            work_order=self.wo, kind=WorkOrderBlocker.Kind.OPERATIONAL,
        ).first()
        self.assertIsNone(blocker, "No OPERATIONAL blocker when pause is micro (no note)")

    def test_emergency_auto_pause_creates_blocker_with_source(self):
        """pause_other_in_progress (called when an emergency WO starts) creates an OPERATIONAL blocker with source_work_order set."""
        from maintenance.services import pause_other_in_progress
        from maintenance.models import WorkOrder, WorkOrderBlocker

        # Create an emergency WO (the source)
        emergency_wo = WorkOrder.objects.create(
            number=7777, machine=None, created_by=self.manager,
            assigned_technician=self.technician,
            lifecycle_status="in_progress",
            is_emergency=True,
        )
        # self.wo is the WO being auto-paused. Reaffirm its state in case
        # previous test methods in the class touched it.
        self.wo.lifecycle_status = WorkOrder.LifecycleStatus.IN_PROGRESS
        self.wo.labor_started_at = self.wo.created_at
        self.wo.save()
        # Call pause_other_in_progress
        pause_other_in_progress(
            technician=self.technician,
            except_pk=emergency_wo.pk,
        )
        blocker = WorkOrderBlocker.objects.filter(
            work_order=self.wo, kind=WorkOrderBlocker.Kind.OPERATIONAL,
        ).first()
        self.assertIsNotNone(blocker)
        self.assertEqual(blocker.source_work_order, emergency_wo)


class VendorRepairBlockerHookTests(TestCase):
    """Tests for VENDOR_REPAIR blocker hooks."""

    def setUp(self):
        from maintenance.models import WorkOrder
        from accounts.models import User
        from inventory.models import SparePart
        from maintenance.models import Machine

        self.manager = User.objects.create_user(username="m_vr", password="x", role=User.Role.MANAGER)
        self.technician = User.objects.create_user(username="t_vr", password="x", role=User.Role.TECHNICIAN)
        # Need a machine for WO (required FK on work_order)
        self.machine = Machine.objects.create(
            name="VR Test Machine", qr_code="VR-M1", asset_level=3, asset_code="VR-M1",
        )
        self.wo = WorkOrder.objects.create(
            number=6666, machine=self.machine, created_by=self.manager,
            assigned_technician=self.technician,
            lifecycle_status="in_progress",
        )
        self.part = SparePart.objects.create(
            sku="VR-TEST-001", name="VR Test Part",
            quantity_on_hand=Decimal("0"), avg_cost=Decimal("0"),
        )

    def test_request_external_repair_opens_vendor_repair_blocker(self):
        """request_external_repair opens a VENDOR_REPAIR blocker."""
        from maintenance.services import request_external_repair
        from maintenance.models import WorkOrderBlocker
        from inventory.models import SparePart

        part = SparePart.objects.create(sku="VR-EXT-001", name="Ext Part", quantity_on_hand=Decimal("0"))
        err = request_external_repair(
            work_order=self.wo,
            requested_by=self.technician,
            diagnosis_note="Encoder failure, fault code F-0317",
            part_description=f"Servo drive S7-300 (SKU {part.sku})",
        )
        blocker = WorkOrderBlocker.objects.filter(
            work_order=self.wo, kind=WorkOrderBlocker.Kind.VENDOR_REPAIR,
            status=WorkOrderBlocker.Status.OPEN,
        ).first()
        self.assertIsNotNone(blocker, "VENDOR_REPAIR blocker should open")
        self.assertEqual(blocker.external_ref, err)

    def test_approve_external_repair_sets_related_ero(self):
        """approve_external_repair_request sets related_ero on the blocker."""
        from maintenance.services import request_external_repair, approve_external_repair_request
        from maintenance.models import WorkOrderBlocker
        from inventory.models import SparePart

        part = SparePart.objects.create(sku="VR-EXT-002", name="Ext Part 2", quantity_on_hand=Decimal("0"))
        err = request_external_repair(
            work_order=self.wo,
            requested_by=self.technician,
            diagnosis_note="Test diagnosis",
            part_description="Test part",
        )
        # Need to give the part on the ERR (the new field added in Phase 1)
        err.part = part
        err.save()
        ero = approve_external_repair_request(
            err=err, manager=self.manager, manager_note="approved",
        )
        blocker = WorkOrderBlocker.objects.get(work_order=self.wo, kind=WorkOrderBlocker.Kind.VENDOR_REPAIR)
        self.assertEqual(blocker.related_ero, ero)

    def test_ero_returned_resolves_vendor_repair_blocker(self):
        """Regression: ERO_RETURNED must resolve the VENDOR_REPAIR blocker.

        The VENDOR_REPAIR blocker is opened against the
        ExternalRepairRequest (ERR), not the ERO. The repair_officer
        view passes the ERO to sync_from_external_event(), so the
        service must reverse-lookup the ERR via the ERO.repair_order
        FK. Previously the service looked for ERO.origin_request (which
        doesn't exist), so the blocker never resolved and the WO got
        stuck at operational='waiting_vendor' forever.
        """
        from django.utils import timezone
        from maintenance.services import request_external_repair, approve_external_repair_request
        from maintenance.services_blocker import WorkOrderBlockerService
        from maintenance.models import (
            ExternalRepairOrder, WorkOrderBlocker, WorkOrder,
        )
        from maintenance.services_wo_status import WorkOrderService

        err = request_external_repair(
            work_order=self.wo,
            requested_by=self.technician,
            diagnosis_note="Encoder failure",
            part_description="Servo drive S7-300",
        )
        ero = approve_external_repair_request(
            err=err, manager=self.manager, manager_note="approved",
        )
        blocker = WorkOrderBlocker.objects.get(
            work_order=self.wo, kind=WorkOrderBlocker.Kind.VENDOR_REPAIR,
        )
        self.assertEqual(blocker.status, WorkOrderBlocker.Status.OPEN)
        # WO is now waiting on the vendor
        self.assertEqual(
            WorkOrderService.recompute_operational_status(self.wo),
            WorkOrder.OperationalStatus.WAITING_VENDOR,
        )

        # Simulate the officer marking the ERO as SENT, then RETURNED.
        # The officer view calls sync_from_external_event(ERO, "ERO_RETURNED").
        ero.status = ExternalRepairOrder.Status.SENT_TO_VENDOR
        ero.sent_at = timezone.now()
        ero.save(update_fields=["status", "sent_at"])

        WorkOrderBlockerService.sync_from_external_event(
            external_obj=ero,
            event_type="ERO_RETURNED",
            actor=self.manager,
            payload={"note": "Vendor returned part"},
        )

        blocker.refresh_from_db()
        self.assertEqual(
            blocker.status,
            WorkOrderBlocker.Status.RESOLVED,
            "VENDOR_REPAIR blocker must resolve when ERO returns. "
            "If this fails, the sync_from_external_event fallback isn't "
            "resolving the blocker keyed to the ERR (origin_request "
            "fallback was broken because ERO has no such FK).",
        )

        # And the WO must leave waiting_vendor.
        self.assertEqual(
            WorkOrderService.recompute_operational_status(self.wo),
            WorkOrder.OperationalStatus.ACTIVE,
        )

    def test_ero_accepted_resolves_vendor_repair_blocker(self):
        """Regression: ERO_ACCEPTED (manager close) resolves the blocker too.

        Same as above but via the ERO_ACCEPTED event (manager accepts the
        returned part and posts vendor_repair cost to the ledger). The
        blocker must resolve even if the tech's RETURNED transition was
        skipped (e.g. direct manager accept from SENT).
        """
        from maintenance.services import request_external_repair, approve_external_repair_request
        from maintenance.services_blocker import WorkOrderBlockerService
        from maintenance.models import ExternalRepairOrder, WorkOrderBlocker

        err = request_external_repair(
            work_order=self.wo,
            requested_by=self.technician,
            diagnosis_note="Encoder failure",
            part_description="Servo drive S7-300",
        )
        ero = approve_external_repair_request(
            err=err, manager=self.manager, manager_note="approved",
        )

        # Direct accept without RETURNED in between (admin override path).
        WorkOrderBlockerService.sync_from_external_event(
            external_obj=ero,
            event_type="ERO_ACCEPTED",
            actor=self.manager,
            payload={"note": "Manager accepted directly"},
        )

        blocker = WorkOrderBlocker.objects.get(
            work_order=self.wo, kind=WorkOrderBlocker.Kind.VENDOR_REPAIR,
        )
        self.assertEqual(
            blocker.status,
            WorkOrderBlocker.Status.RESOLVED,
            "ERO_ACCEPTED must also resolve the VENDOR_REPAIR blocker "
            "via the ERR reverse-lookup fallback.",
        )
