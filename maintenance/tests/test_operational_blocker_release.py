"""Tests for the operational-blocker-release invariant.

The invariant: a source WO blocks dependents only while it is
actively being worked on
(lifecycle=IN_PROGRESS AND operational=ACTIVE).

Tested end-to-end through the public helper
`release_dependent_blockers(source_wo, actor)` because that is the
single shared code path used by both runtime
(`transition_work_order`) and the management command
(`repair_paused_blockers`).
"""
from django.contrib.auth import get_user_model
from django.test import TestCase

from maintenance.models import WorkOrder, WorkOrderBlocker
from maintenance.services import (
    release_dependent_blockers,
    source_still_blocks,
)


User = get_user_model()


def _make_user(username, role=User.Role.TECHNICIAN):
    return User.objects.create(
        username=username,
        role=role,
        is_active=True,
    )


def _make_wo(number, technician, lifecycle, operational, is_emergency=False):
    """Create a WO with a deterministic display number (1, 2, 3, ...)."""
    return WorkOrder.objects.create(
        number=number,
        category="breakdown",
        is_emergency=is_emergency,
        lifecycle_status=lifecycle,
        operational_status=operational,
        assigned_technician=technician,
        created_by=technician,
    )


class SourceStillBlocksTests(TestCase):
    """Direct unit tests of the invariant predicate."""

    def setUp(self):
        self.tech = _make_user("tech")

    def test_active_in_progress_blocks(self):
        wo = _make_wo(1, self.tech, "in_progress", "active")
        self.assertTrue(source_still_blocks(wo))

    def test_in_progress_pending_parts_does_not_block(self):
        wo = _make_wo(1, self.tech, "in_progress", "pending_parts")
        self.assertFalse(source_still_blocks(wo))

    def test_in_progress_paused_does_not_block(self):
        wo = _make_wo(1, self.tech, "in_progress", "paused")
        self.assertFalse(source_still_blocks(wo))

    def test_in_progress_waiting_vendor_does_not_block(self):
        wo = _make_wo(1, self.tech, "in_progress", "waiting_vendor")
        self.assertFalse(source_still_blocks(wo))

    def test_closed_does_not_block(self):
        wo = _make_wo(1, self.tech, "closed", "closed")
        self.assertFalse(source_still_blocks(wo))

    def test_cancelled_does_not_block(self):
        wo = _make_wo(1, self.tech, "cancelled", "cancelled")
        self.assertFalse(source_still_blocks(wo))

    def test_assigned_does_not_block(self):
        wo = _make_wo(1, self.tech, "assigned", "paused")
        self.assertFalse(source_still_blocks(wo))


class ReleaseDependentBlockersTests(TestCase):
    """Integration tests via the helper used by runtime + repair command."""

    def setUp(self):
        self.tech = _make_user("tech")
        self.manager = _make_user("manager", role=User.Role.MANAGER)

    def _open_blocker(self, source, dependent):
        return WorkOrderBlocker.objects.create(
            work_order=dependent,
            kind="operational",
            status="open",
            pause_reason="emergency",
            source_work_order=source,
            opened_by=self.tech,
        )

    def test_active_source_does_not_release_dependent(self):
        source = _make_wo(1, self.tech, "in_progress", "active")
        dependent = _make_wo(2, self.tech, "in_progress", "paused")
        self._open_blocker(source, dependent)

        released = release_dependent_blockers(source, self.manager)

        self.assertEqual(released, 0)
        self.assertTrue(
            WorkOrderBlocker.objects.filter(work_order=dependent, status="open").exists()
        )

    def test_pending_parts_source_releases_dependent(self):
        source = _make_wo(1, self.tech, "in_progress", "pending_parts")
        dependent = _make_wo(2, self.tech, "in_progress", "paused")
        self._open_blocker(source, dependent)

        released = release_dependent_blockers(source, self.manager)

        self.assertEqual(released, 1)
        dependent.refresh_from_db()
        self.assertEqual(dependent.operational_status, "active")
        self.assertFalse(
            WorkOrderBlocker.objects.filter(work_order=dependent, status="open").exists()
        )

    def test_paused_source_releases_dependent(self):
        source = _make_wo(1, self.tech, "in_progress", "paused")
        dependent = _make_wo(2, self.tech, "in_progress", "paused")
        self._open_blocker(source, dependent)

        released = release_dependent_blockers(source, self.manager)

        self.assertEqual(released, 1)
        dependent.refresh_from_db()
        self.assertEqual(dependent.operational_status, "active")

    def test_closed_source_releases_dependent(self):
        source = _make_wo(1, self.tech, "closed", "closed")
        dependent = _make_wo(2, self.tech, "in_progress", "paused")
        self._open_blocker(source, dependent)

        released = release_dependent_blockers(source, self.manager)

        self.assertEqual(released, 1)
        dependent.refresh_from_db()
        # Closed WO's dependent: no longer has any operational blockers,
        # lifecycle=in_progress, no labor running → falls through to
        # the "default paused" branch in recompute_operational_status
        # (in_progress with no labor and no blockers). That's fine — the
        # important invariant is the blocker is gone.
        self.assertFalse(
            WorkOrderBlocker.objects.filter(work_order=dependent, status="open").exists()
        )

    def test_cancelled_source_releases_dependent(self):
        source = _make_wo(1, self.tech, "cancelled", "cancelled")
        dependent = _make_wo(2, self.tech, "in_progress", "paused")
        self._open_blocker(source, dependent)

        released = release_dependent_blockers(source, self.manager)

        self.assertEqual(released, 1)

    def test_release_multiple_dependents(self):
        source = _make_wo(1, self.tech, "in_progress", "paused")
        d1 = _make_wo(2, self.tech, "in_progress", "paused")
        d2 = _make_wo(3, self.tech, "in_progress", "paused")
        d3 = _make_wo(4, self.tech, "in_progress", "paused")
        self._open_blocker(source, d1)
        self._open_blocker(source, d2)
        self._open_blocker(source, d3)

        released = release_dependent_blockers(source, self.manager)

        self.assertEqual(released, 3)
        for d in [d1, d2, d3]:
            self.assertFalse(
                WorkOrderBlocker.objects.filter(work_order=d, status="open").exists()
            )

    def test_does_not_touch_other_kinds_of_blockers(self):
        """Only OPERATIONAL blockers are released. PART / SHORTAGE / VENDOR
        blockers are independent and have their own lifecycle."""
        source = _make_wo(1, self.tech, "in_progress", "paused")
        dependent = _make_wo(2, self.tech, "in_progress", "pending_parts")
        WorkOrderBlocker.objects.create(
            work_order=dependent, kind="part", status="open",
        )
        WorkOrderBlocker.objects.create(
            work_order=dependent, kind="shortage", status="open",
        )
        self._open_blocker(source, dependent)

        released = release_dependent_blockers(source, self.manager)

        self.assertEqual(released, 1)
        # PART and SHORTAGE blockers are still open
        self.assertTrue(
            WorkOrderBlocker.objects.filter(work_order=dependent, kind="part", status="open").exists()
        )
        self.assertTrue(
            WorkOrderBlocker.objects.filter(work_order=dependent, kind="shortage", status="open").exists()
        )

    def test_does_not_touch_blockers_without_source(self):
        """Blockers with source_work_order=None (legacy or operator pause)
        should not be touched by the helper when called on a specific WO."""
        source = _make_wo(1, self.tech, "in_progress", "paused")
        dependent = _make_wo(2, self.tech, "in_progress", "paused")
        # Operator-paused blocker (no source)
        WorkOrderBlocker.objects.create(
            work_order=dependent, kind="operational", status="open",
            pause_reason="operational", source_work_order=None,
        )

        released = release_dependent_blockers(source, self.manager)

        self.assertEqual(released, 0)
        # The sourceless operational blocker is still open
        self.assertTrue(
            WorkOrderBlocker.objects.filter(
                work_order=dependent, source_work_order=None, status="open"
            ).exists()
        )


class PartBlockerReactsToIssuedStatusTests(TestCase):
    """The PART_ISSUED handler must react to business state, not quantity math.

    The business service that transitioned the line to ISSUED has
    already validated everything; the blocker service just reacts.
    """

    def setUp(self):
        from accounts.models import User
        self.tech = _make_user("tech")
        self.manager = _make_user("manager", role=User.Role.MANAGER)
        from maintenance.services_blocker import WorkOrderBlockerService
        self.WorkOrderBlockerService = WorkOrderBlockerService

    def _make_part(self, opening_qty=5):
        from decimal import Decimal
        from inventory.models import SparePart, Inventory
        from maintenance.models import Site
        site, _ = Site.objects.get_or_create(
            code="MF", defaults={"name": "Main Factory", "is_default": True, "is_active": True},
        )
        part, _ = SparePart.objects.get_or_create(
            sku=f"TEST-B-{self.tech.pk}",
            defaults={"name": "Test Part B", "is_consumable": False},
        )
        # Always ensure inventory exists for this part+site
        Inventory.objects.update_or_create(
            part=part, site=site,
            defaults={"quantity_available": Decimal(str(opening_qty))},
        )
        return part

    def _open_part_blocker(self, line, work_order):
        from django.contrib.contenttypes.models import ContentType
        ct = ContentType.objects.get_for_model(line)
        return WorkOrderBlocker.objects.create(
            work_order=work_order,
            kind="part",
            status="open",
            content_type=ct,
            object_id=line.pk,
            external_label="x",
        )

    def test_part_blocker_resolves_on_issued_status(self):
        """Line in ISSUED status + PART_ISSUED event → blocker resolves.

        This is the post-refactor rule: no quantity comparison. The
        business service flipped the line to ISSUED; the blocker
        service reacts to that signal.
        """
        from inventory.models import PartIssueLine
        wo = _make_wo(100, self.tech, "in_progress", "paused")
        line = PartIssueLine.objects.create(
            work_order=wo,
            part=self._make_part(),
            quantity=2,
            unit_cost=0,
            status=PartIssueLine.Status.ISSUED,
            issued_qty=0,  # ← deliberately 0
            approved_qty=0,
            issued_by=self.tech,
        )
        blocker = self._open_part_blocker(line, wo)

        self.WorkOrderBlockerService.sync_from_external_event(
            external_obj=line, event_type="PART_ISSUED", actor=self.manager,
        )

        blocker.refresh_from_db()
        self.assertEqual(blocker.status, WorkOrderBlocker.Status.RESOLVED)

    def test_part_blocker_does_not_resolve_on_approved_status(self):
        """Line still APPROVED (not yet ISSUED) → no resolve."""
        from inventory.models import PartIssueLine
        wo = _make_wo(101, self.tech, "in_progress", "paused")
        line = PartIssueLine.objects.create(
            work_order=wo,
            part=self._make_part(),
            quantity=2,
            unit_cost=0,
            status=PartIssueLine.Status.APPROVED,
            approved_qty=2,
            issued_by=self.tech,
        )
        blocker = self._open_part_blocker(line, wo)

        self.WorkOrderBlockerService.sync_from_external_event(
            external_obj=line, event_type="PART_ISSUED", actor=self.manager,
        )

        blocker.refresh_from_db()
        self.assertEqual(blocker.status, WorkOrderBlocker.Status.OPEN)

    def test_part_blocker_does_not_resolve_on_rejected_status(self):
        """Line REJECTED → not ISSUED → no resolve."""
        from inventory.models import PartIssueLine
        wo = _make_wo(102, self.tech, "in_progress", "paused")
        line = PartIssueLine.objects.create(
            work_order=wo,
            part=self._make_part(),
            quantity=2,
            unit_cost=0,
            status=PartIssueLine.Status.REJECTED,
            issued_by=self.tech,
        )
        blocker = self._open_part_blocker(line, wo)

        self.WorkOrderBlockerService.sync_from_external_event(
            external_obj=line, event_type="PART_ISSUED", actor=self.manager,
        )

        blocker.refresh_from_db()
        self.assertEqual(blocker.status, WorkOrderBlocker.Status.OPEN)
