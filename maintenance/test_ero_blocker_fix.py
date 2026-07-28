"""Regression tests for the ERO VENDOR_REPAIR blocker auto-resolution fix.

Phase 7.10 (July 2026): two complementary fixes
  A) sync_from_external_event is now self-atomic (was failing with
     TransactionManagementError when caller forgot to wrap).
  B) ExternalRepairOrder.save() auto-fires ERO_RETURNED / ERO_ACCEPTED
     on status transitions (was orphaned when view code forgot to call).
"""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from django.test import TestCase
from django.utils import timezone

from maintenance.models import (
    ExternalRepairOrder,
    ExternalRepairRequest,
    Machine,
    WorkOrder,
    WorkOrderBlocker,
    WorkOrderBlockerEvent,
)
from maintenance.services_blocker import WorkOrderBlockerService


User = get_user_model()


def _make_user(username: str, role: str):
    return User.objects.create_user(username=username, password="x", role=role)


def _make_machine(asset_code: str) -> Machine:
    return Machine.objects.create(
        name=f"ERO-fix Machine {asset_code}",
        asset_level=3,
        asset_code=asset_code,
        qr_code=f"qr-ero-fix-{asset_code}",
    )


class SyncFromExternalEventIsSelfAtomic(TestCase):
    """Fix A: sync_from_external_event must work even when the caller
    forgets to wrap in transaction.atomic. select_for_update() requires
    a transaction — the decorator provides one if the caller didn't."""

    def setUp(self):
        self.manager = _make_user("mgr_ero_fix", User.Role.MANAGER)
        self.tech = _make_user("tech_ero_fix", User.Role.TECHNICIAN)
        self.machine = _make_machine("SFEA-1")
        self.wo = WorkOrder.objects.create(
            machine=self.machine,
            created_by=self.manager,
            assigned_technician=self.tech,
            lifecycle_status=WorkOrder.LifecycleStatus.IN_PROGRESS,
        )
        self.ero = ExternalRepairOrder.objects.create(
            work_order=self.wo,
            title="test",
            description="test",
            status=ExternalRepairOrder.Status.RETURNED,
            created_by=self.manager,
            handled_by=self.manager,
            sent_at=timezone.now(),
            returned_at=timezone.now(),
        )
        # Build an ERR so the ERO has an origin to fall back through
        self.err = ExternalRepairRequest.objects.create(
            work_order=self.wo,
            requested_by=self.tech,
            diagnosis_note="fix-A diagnosis",
            part_description="fix-A part",
        )
        self.err.repair_order = self.ero
        self.err.save(update_fields=["repair_order"])

        self.ct = ContentType.objects.get_for_model(self.err)
        self.blocker = WorkOrderBlocker.objects.create(
            work_order=self.wo,
            kind=WorkOrderBlocker.Kind.VENDOR_REPAIR,
            status=WorkOrderBlocker.Status.OPEN,
            content_type=self.ct,
            object_id=self.err.pk,
            related_ero=self.ero,
            external_label="test",
            opened_by=self.manager,
        )

    def test_call_without_atomic_does_not_raise(self):
        """The original bug: this call raised TransactionManagementError
        because select_for_update() was outside a transaction."""
        try:
            result = WorkOrderBlockerService.sync_from_external_event(
                external_obj=self.ero,
                event_type="ERO_ACCEPTED",
                actor=self.manager,
            )
        except Exception as e:
            self.fail(f"sync_from_external_event should not raise: {e}")
        self.assertIsNotNone(result)
        self.blocker.refresh_from_db()
        self.assertEqual(self.blocker.status, WorkOrderBlocker.Status.RESOLVED)

    def test_resolves_blocker_on_ero_accepted(self):
        WorkOrderBlockerService.sync_from_external_event(
            external_obj=self.ero,
            event_type="ERO_ACCEPTED",
            actor=self.manager,
        )
        self.blocker.refresh_from_db()
        self.assertEqual(self.blocker.status, WorkOrderBlocker.Status.RESOLVED)
        self.assertIsNotNone(self.blocker.resolved_at)
        self.assertEqual(self.blocker.resolved_by, self.manager)

    def test_records_resolution_event(self):
        WorkOrderBlockerService.sync_from_external_event(
            external_obj=self.ero,
            event_type="ERO_ACCEPTED",
            actor=self.manager,
        )
        events = WorkOrderBlockerEvent.objects.filter(blocker=self.blocker)
        types = list(events.values_list("event_type", flat=True))
        self.assertIn(
            WorkOrderBlockerEvent.EventType.BLOCKER_RESOLVED, types,
        )

    def test_noop_when_no_open_blocker(self):
        # Close the blocker first
        self.blocker.status = WorkOrderBlocker.Status.RESOLVED
        self.blocker.save()
        # Now call again — should be a no-op
        result = WorkOrderBlockerService.sync_from_external_event(
            external_obj=self.ero,
            event_type="ERO_ACCEPTED",
            actor=self.manager,
        )
        self.assertIsNone(result)


class ExternalRepairOrderSaveFiresEvents(TestCase):
    """Fix B: ExternalRepairOrder.save() auto-fires ERO_RETURNED /
    ERO_ACCEPTED on status transitions. This is the safety net so the
    VENDOR_REPAIR blocker always resolves, even if the view code forgot
    to call sync_from_external_event."""

    def setUp(self):
        self.manager = _make_user("mgr_auto", User.Role.MANAGER)
        self.tech = _make_user("tech_auto", User.Role.TECHNICIAN)
        self.machine = _make_machine("AUTO-1")
        self.wo = WorkOrder.objects.create(
            machine=self.machine,
            created_by=self.manager,
            assigned_technician=self.tech,
            lifecycle_status=WorkOrder.LifecycleStatus.IN_PROGRESS,
        )
        # ERO in SENT state (initial)
        self.ero = ExternalRepairOrder.objects.create(
            work_order=self.wo,
            title="auto-fire test",
            description="test",
            status=ExternalRepairOrder.Status.SENT_TO_VENDOR,
            created_by=self.manager,
            handled_by=self.manager,
            sent_at=timezone.now(),
        )
        # ERR + link so the blocker can be keyed to the ERR (matches real
        # production wiring where the blocker is keyed to the ERR).
        self.err = ExternalRepairRequest.objects.create(
            work_order=self.wo,
            requested_by=self.tech,
            diagnosis_note="fix-B diagnosis",
            part_description="fix-B part",
        )
        self.err.repair_order = self.ero
        self.err.save(update_fields=["repair_order"])

        self.ct = ContentType.objects.get_for_model(self.err)
        self.blocker = WorkOrderBlocker.objects.create(
            work_order=self.wo,
            kind=WorkOrderBlocker.Kind.VENDOR_REPAIR,
            status=WorkOrderBlocker.Status.OPEN,
            content_type=self.ct,
            object_id=self.err.pk,
            related_ero=self.ero,
            external_label="test ERO",
            opened_by=self.manager,
        )

    def test_transition_to_returned_resolves_blocker(self):
        self.ero.status = ExternalRepairOrder.Status.RETURNED
        self.ero.returned_at = timezone.now()
        self.ero.save()
        self.blocker.refresh_from_db()
        self.assertEqual(
            self.blocker.status,
            WorkOrderBlocker.Status.RESOLVED,
        )

    def test_transition_to_closed_resolves_blocker(self):
        self.ero.status = ExternalRepairOrder.Status.CLOSED
        self.ero.closed_at = timezone.now()
        self.ero.actual_cost = Decimal("100.00")
        self.ero.save()
        self.blocker.refresh_from_db()
        self.assertEqual(
            self.blocker.status,
            WorkOrderBlocker.Status.RESOLVED,
        )

    def test_transition_closed_from_returned(self):
        """SENT → RETURNED → CLOSED: both transitions should fire."""
        self.ero.status = ExternalRepairOrder.Status.RETURNED
        self.ero.returned_at = timezone.now()
        self.ero.save()
        # Reopen the blocker to test the second transition
        self.blocker.status = WorkOrderBlocker.Status.OPEN
        self.blocker.resolved_at = None
        self.blocker.resolved_by = None
        self.blocker.save()
        self.ero.status = ExternalRepairOrder.Status.CLOSED
        self.ero.closed_at = timezone.now()
        self.ero.actual_cost = Decimal("100.00")
        self.ero.save()
        self.blocker.refresh_from_db()
        self.assertEqual(
            self.blocker.status,
            WorkOrderBlocker.Status.RESOLVED,
        )

    def test_resave_with_same_status_does_not_fire(self):
        # Already SENT_TO_VENDOR. Re-save with no change.
        self.ero.save()
        self.blocker.refresh_from_db()
        self.assertEqual(
            self.blocker.status,
            WorkOrderBlocker.Status.OPEN,
        )

    def test_save_when_work_order_is_null_does_not_fire(self):
        """An ERO without a work_order (admin-only / standalone) should
        not raise even if status changes. Events would no-op anyway."""
        wo2 = WorkOrder.objects.create(
            machine=self.machine,
            created_by=self.manager,
            assigned_technician=self.tech,
            lifecycle_status=WorkOrder.LifecycleStatus.IN_PROGRESS,
        )
        ero_no_wo = ExternalRepairOrder.objects.create(
            work_order=wo2,
            title="orphan",
            description="test",
            status=ExternalRepairOrder.Status.SENT_TO_VENDOR,
            created_by=self.manager,
            handled_by=self.manager,
            sent_at=timezone.now(),
        )
        # Detach from WO
        ero_no_wo.work_order = None
        ero_no_wo.save()
        # Now transition to RETURNED — should not raise even without a WO
        ero_no_wo.status = ExternalRepairOrder.Status.RETURNED
        ero_no_wo.save()  # should not raise

    def test_save_with_other_status_change_does_not_resolve(self):
        """Saving without changing status should not resolve the blocker."""
        # Re-save with same status
        self.ero.title = "updated title"
        self.ero.save()
        self.blocker.refresh_from_db()
        self.assertEqual(
            self.blocker.status,
            WorkOrderBlocker.Status.OPEN,
        )


class NoRegressionOnExistingTests(TestCase):
    """Sanity check: the ERO_RETURNED event from the view path still works."""

    def setUp(self):
        self.manager = _make_user("mgr_noreg", User.Role.MANAGER)
        self.tech = _make_user("tech_noreg", User.Role.TECHNICIAN)
        self.machine = _make_machine("NR-1")
        self.wo = WorkOrder.objects.create(
            machine=self.machine,
            created_by=self.manager,
            assigned_technician=self.tech,
            lifecycle_status=WorkOrder.LifecycleStatus.IN_PROGRESS,
        )

    def test_explicit_sync_call_still_works(self):
        """Calling sync_from_external_event explicitly (the old API) still works."""
        err = ExternalRepairRequest.objects.create(
            work_order=self.wo,
            requested_by=self.tech,
            diagnosis_note="noreg diagnosis",
            part_description="noreg part",
        )
        ero = ExternalRepairOrder.objects.create(
            work_order=self.wo,
            title="explicit call test",
            description="test",
            status=ExternalRepairOrder.Status.SENT_TO_VENDOR,
            created_by=self.manager,
            handled_by=self.manager,
            sent_at=timezone.now(),
        )
        err.repair_order = ero
        err.save(update_fields=["repair_order"])

        ct = ContentType.objects.get_for_model(err)
        blocker = WorkOrderBlocker.objects.create(
            work_order=self.wo,
            kind=WorkOrderBlocker.Kind.VENDOR_REPAIR,
            status=WorkOrderBlocker.Status.OPEN,
            content_type=ct,
            object_id=err.pk,
            related_ero=ero,
            external_label="test",
            opened_by=self.manager,
        )
        # Transition to RETURNED inside atomic block (old-style path)
        with transaction.atomic():
            ero.status = ExternalRepairOrder.Status.RETURNED
            ero.returned_at = timezone.now()
            # Explicit call (the old way)
            WorkOrderBlockerService.sync_from_external_event(
                external_obj=ero,
                event_type="ERO_RETURNED",
                actor=self.manager,
            )
            ero.save()
        blocker.refresh_from_db()
        self.assertEqual(
            blocker.status,
            WorkOrderBlocker.Status.RESOLVED,
        )