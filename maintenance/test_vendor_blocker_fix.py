"""
Phase 7.8.2 — Vendor-flow bug fix tests.

Covers the two root-cause bugs in repair_manager_accept:

1. `sync_from_external_event(external_obj=ero, event_type="ERO_ACCEPTED")`
   must resolve the VENDOR_REPAIR blocker even when that blocker is keyed
   to the ExternalRepairRequest (origin_request), not the ERO.

2. `reconcile_orphan_vendor_blockers` management command must resolve
   orphan VENDOR_REPAIR blockers (ERO already CLOSED but blocker OPEN)
   and backfill the missing CostTransaction rows.

Plus a regression test for the direct-lookup path (blocker keyed to the
ERO directly) which must still work.
"""
from decimal import Decimal

from django.contrib.contenttypes.models import ContentType
from django.core.management import call_command
from django.test import TestCase
from io import StringIO

from accounts.models import User
from maintenance.models import (
    CostTransaction,
    ExternalRepairOrder,
    ExternalRepairRequest,
    Machine,
    WorkOrder,
    WorkOrderBlocker,
    WorkOrderBlockerEvent,
)
from maintenance.services_blocker import WorkOrderBlockerService


def _make_user(username: str, role: str) -> User:
    return User.objects.create_user(username=username, password="x", role=role)


def _make_machine(asset_code: str) -> Machine:
    return Machine.objects.create(
        name=f"Vendor-fix Machine {asset_code}",
        asset_level=3,
        asset_code=asset_code,
        qr_code=f"qr-{asset_code}",
    )


def _make_wo(*, machine: Machine, created_by: User,
             assigned_technician: User) -> WorkOrder:
    return WorkOrder.objects.create(
        machine=machine,
        created_by=created_by,
        assigned_technician=assigned_technician,
        lifecycle_status=WorkOrder.LifecycleStatus.IN_PROGRESS,
    )


def _make_err(*, work_order: WorkOrder, requested_by: User) -> ExternalRepairRequest:
    return ExternalRepairRequest.objects.create(
        work_order=work_order,
        requested_by=requested_by,
        diagnosis_note="Vendor-fix test diagnosis",
        part_description="Vendor-fix test part",
    )


def _make_ero(*, work_order: WorkOrder, created_by: User,
               origin_request: ExternalRepairRequest,
               status: str = ExternalRepairOrder.Status.SENT_TO_VENDOR,
               actual_cost: Decimal = None) -> ExternalRepairOrder:
    return ExternalRepairOrder.objects.create(
        work_order=work_order,
        title=f"ERO for ERR-{origin_request.pk}",
        description="Vendor-fix ERO",
        created_by=created_by,
        handled_by=created_by,
        status=status,
        actual_cost=actual_cost,
        origin_request=origin_request,
    )


class EroAcceptedFallbackToOriginRequestTests(TestCase):
    """ERO_ACCEPTED must resolve a blocker keyed to the ERR (origin_request)."""

    def setUp(self):
        self.manager = _make_user("mgr_vf", User.Role.MANAGER)
        self.tech = _make_user("tech_vf", User.Role.TECHNICIAN)
        self.machine = _make_machine("VF-1")
        self.wo = _make_wo(machine=self.machine, created_by=self.manager,
                           assigned_technician=self.tech)
        self.err = _make_err(work_order=self.wo, requested_by=self.tech)
        self.ero = _make_ero(work_order=self.wo, created_by=self.manager,
                              origin_request=self.err)

        ct_err = ContentType.objects.get_for_model(ExternalRepairRequest)
        self.blocker = WorkOrderBlocker.objects.create(
            work_order=self.wo,
            kind=WorkOrderBlocker.Kind.VENDOR_REPAIR,
            status=WorkOrderBlocker.Status.OPEN,
            content_type=ct_err,
            object_id=self.err.pk,
            related_ero=self.ero,
            opened_by=self.tech,
        )

    def test_ero_accepted_falls_back_to_origin_request(self):
        """ERO_ACCEPTED with external_obj=ERO resolves the ERR-keyed blocker."""
        self.ero.status = ExternalRepairOrder.Status.CLOSED
        self.ero.actual_cost = Decimal("250.00")
        self.ero.save(update_fields=["status", "actual_cost"])

        result = WorkOrderBlockerService.sync_from_external_event(
            external_obj=self.ero,
            event_type="ERO_ACCEPTED",
            actor=self.manager,
            payload={"ero_id": self.ero.pk},
        )

        self.assertIsNotNone(result,
            "ERO_ACCEPTED with ERO obj should resolve the ERR-keyed blocker")
        self.blocker.refresh_from_db()
        self.assertEqual(
            self.blocker.status,
            WorkOrderBlocker.Status.RESOLVED,
        )
        self.assertEqual(self.blocker.resolved_by, self.manager)

    def test_ero_returned_falls_back_to_origin_request(self):
        """Regression: ERO_RETURNED fallback still works after the fix."""
        self.ero.status = ExternalRepairOrder.Status.RETURNED
        self.ero.save(update_fields=["status"])

        result = WorkOrderBlockerService.sync_from_external_event(
            external_obj=self.ero,
            event_type="ERO_RETURNED",
            actor=self.manager,
            payload={"ero_id": self.ero.pk},
        )

        self.assertIsNotNone(result)
        self.blocker.refresh_from_db()
        self.assertEqual(
            self.blocker.status,
            WorkOrderBlocker.Status.RESOLVED,
        )

    def test_ero_accepted_with_direct_keyed_blocker(self):
        """Regression: ERO_ACCEPTED still resolves a blocker when the ERO
        has no origin_request (fallback is skipped, direct ERO lookup
        applies)."""
        self.err.repair_order = None
        self.err.save(update_fields=["repair_order"])

        ct_ero = ContentType.objects.get_for_model(ExternalRepairOrder)
        self.blocker.content_type = ct_ero
        self.blocker.object_id = self.ero.pk
        self.blocker.save(update_fields=["content_type", "object_id"])

        self.ero.status = ExternalRepairOrder.Status.CLOSED
        self.ero.actual_cost = Decimal("100.00")
        self.ero.save(update_fields=["status", "actual_cost"])

        result = WorkOrderBlockerService.sync_from_external_event(
            external_obj=self.ero,
            event_type="ERO_ACCEPTED",
            actor=self.manager,
            payload={"ero_id": self.ero.pk},
        )

        self.assertIsNotNone(result)
        self.blocker.refresh_from_db()
        self.assertEqual(
            self.blocker.status,
            WorkOrderBlocker.Status.RESOLVED,
        )

    def test_ero_accepted_with_explicit_err_object(self):
        """ERO_ACCEPTED fired directly against the ERR still works."""
        result = WorkOrderBlockerService.sync_from_external_event(
            external_obj=self.err,
            event_type="ERO_ACCEPTED",
            actor=self.manager,
            payload={"err_id": self.err.pk},
        )

        self.assertIsNotNone(result)
        self.blocker.refresh_from_db()
        self.assertEqual(
            self.blocker.status,
            WorkOrderBlocker.Status.RESOLVED,
        )


class ReconcileOrphanVendorBlockersTests(TestCase):
    """The reconcile_orphan_vendor_blockers management command must:

    - resolve orphan VENDOR_REPAIR blockers whose ERO is already CLOSED
    - backfill the missing CostTransaction row from ERO.actual_cost
    - refresh the WO cost cache and operational status
    - be a no-op when no orphans exist
    """

    def setUp(self):
        self.manager = _make_user("mgr_rec", User.Role.MANAGER)
        self.tech = _make_user("tech_rec", User.Role.TECHNICIAN)
        self.machine = _make_machine("REC-1")
        self.wo = _make_wo(machine=self.machine, created_by=self.manager,
                           assigned_technician=self.tech)
        self.err = _make_err(work_order=self.wo, requested_by=self.tech)
        self.ero = _make_ero(
            work_order=self.wo,
            created_by=self.manager,
            origin_request=self.err,
            status=ExternalRepairOrder.Status.CLOSED,
            actual_cost=Decimal("300.00"),
        )
        self.ero.closed_at = self.ero.created_at
        self.ero.save(update_fields=["closed_at"])

        ct_err = ContentType.objects.get_for_model(ExternalRepairRequest)
        self.blocker = WorkOrderBlocker.objects.create(
            work_order=self.wo,
            kind=WorkOrderBlocker.Kind.VENDOR_REPAIR,
            status=WorkOrderBlocker.Status.OPEN,
            content_type=ct_err,
            object_id=self.err.pk,
            related_ero=self.ero,
            opened_by=self.tech,
        )

    def test_command_resolves_blocker_and_backfills_cost(self):
        out = StringIO()
        call_command("reconcile_orphan_vendor_blockers", stdout=out)

        self.blocker.refresh_from_db()
        self.assertEqual(
            self.blocker.status,
            WorkOrderBlocker.Status.RESOLVED,
        )
        self.assertEqual(self.blocker.resolved_by, self.manager)
        self.assertIn("Backfilled", self.blocker.resolution_note)

        ct = CostTransaction.objects.filter(
            source_type="external_repair_order",
            source_id=self.ero.pk,
        ).first()
        self.assertIsNotNone(ct,
            "CostTransaction for ERO must be backfilled by the command")
        self.assertEqual(ct.amount, Decimal("300.00"))
        self.assertEqual(ct.category, "vendor_repair")
        self.assertIn("Backfill", ct.memo)

        from maintenance.models import WorkOrderCost
        woc = WorkOrderCost.objects.get(work_order=self.wo)
        self.assertEqual(woc.vendor_repair_cost, Decimal("300.00"))

        self.wo.refresh_from_db()
        self.assertEqual(
            self.wo.operational_status,
            WorkOrder.OperationalStatus.ACTIVE,
        )

        self.assertTrue(
            self.blocker.events.filter(
                event_type=WorkOrderBlockerEvent.EventType.BLOCKER_RESOLVED
            ).exists()
        )

        self.assertIn(
            f"B-{self.blocker.id}: resolved",
            out.getvalue(),
        )

    def test_command_no_op_when_no_orphans(self):
        self.blocker.status = WorkOrderBlocker.Status.RESOLVED
        self.blocker.save(update_fields=["status"])

        out = StringIO()
        call_command("reconcile_orphan_vendor_blockers", stdout=out)
        self.assertIn("No orphan VENDOR_REPAIR blockers found.", out.getvalue())

    def test_command_dry_run_does_not_mutate(self):
        out = StringIO()
        call_command(
            "reconcile_orphan_vendor_blockers",
            "--dry-run",
            stdout=out,
        )
        self.assertIn("--dry-run", out.getvalue())

        self.blocker.refresh_from_db()
        self.assertEqual(
            self.blocker.status,
            WorkOrderBlocker.Status.OPEN,
        )
        self.assertFalse(
            CostTransaction.objects.filter(
                source_type="external_repair_order",
                source_id=self.ero.pk,
            ).exists()
        )

    def test_command_does_not_double_post_if_ledger_exists(self):
        CostTransaction.objects.create(
            work_order=self.wo,
            machine=self.wo.machine,
            component=self.wo.component,
            amount=Decimal("300.00"),
            category="vendor_repair",
            source_type="external_repair_order",
            source_id=self.ero.pk,
            actor=self.manager,
        )

        out = StringIO()
        call_command("reconcile_orphan_vendor_blockers", stdout=out)

        rows = CostTransaction.objects.filter(
            source_type="external_repair_order",
            source_id=self.ero.pk,
        )
        self.assertEqual(rows.count(), 1,
            "Command must not create a duplicate CostTransaction when one "
            "already exists for the ERO")
        self.assertEqual(rows.first().amount, Decimal("300.00"))

        self.blocker.refresh_from_db()
        self.assertEqual(
            self.blocker.status,
            WorkOrderBlocker.Status.RESOLVED,
        )

    def test_command_ignores_blockers_with_non_closed_ero(self):
        self.ero.status = ExternalRepairOrder.Status.RETURNED
        self.ero.save(update_fields=["status"])

        out = StringIO()
        call_command("reconcile_orphan_vendor_blockers", stdout=out)
        self.assertIn("No orphan VENDOR_REPAIR blockers found.", out.getvalue())

        self.blocker.refresh_from_db()
        self.assertEqual(
            self.blocker.status,
            WorkOrderBlocker.Status.OPEN,
        )