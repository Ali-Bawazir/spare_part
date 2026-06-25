"""
Phase 7.8 — WorkOrder.blocker_system_version tests.

Covers:
- New WOs default to v1 (created under the blocker system).
- Legacy WOs start at v0 when explicitly seeded (e.g. via fixture or
  pre-migration data).
- The first WO Blocker on a v0 WO bumps it to v1 (idempotent on further
  blockers).
- The Legacy Reconciliation Dashboard view renders for managers and is
  hidden from technicians.
"""
from __future__ import annotations

from django.test import TestCase
from django.urls import reverse

from accounts.models import User
from maintenance.models import (
    Machine,
    WorkOrder,
    WorkOrderBlocker,
)


def _make_user(username: str, role: str) -> User:
    return User.objects.create_user(username=username, password="x", role=role)


def _make_wo(*, created_by: User, **kwargs) -> WorkOrder:
    defaults = {
        "created_by": created_by,
        "lifecycle_status": WorkOrder.LifecycleStatus.ASSIGNED,
    }
    defaults.update(kwargs)
    return WorkOrder.objects.create(**defaults)


class BlockerSystemVersionTests(TestCase):
    """Auto-management of WorkOrder.blocker_system_version."""

    def setUp(self):
        self.manager = _make_user("manager_bsv", User.Role.MANAGER)
        self.tech = _make_user("tech_bsv", User.Role.TECHNICIAN)
        self.machine = Machine.objects.create(name="Press BSV", qr_code="BSV-M1")

    def test_new_wo_defaults_to_v1(self):
        wo = _make_wo(machine=self.machine, created_by=self.manager)
        wo.refresh_from_db()
        self.assertEqual(wo.blocker_system_version, 1)

    def test_legacy_wo_starts_at_v0(self):
        # New WOs are always created at v1 via WorkOrder.save().
        # Legacy v0 WOs exist via migration backfill or back-date update().
        wo = WorkOrder.objects.create(
            machine=self.machine,
            created_by=self.manager,
            lifecycle_status=WorkOrder.LifecycleStatus.ASSIGNED,
        )
        self.assertEqual(wo.blocker_system_version, 1)
        WorkOrder.objects.filter(pk=wo.pk).update(blocker_system_version=0)
        wo.refresh_from_db()
        self.assertEqual(wo.blocker_system_version, 0)

    def test_first_blocker_bumps_to_v1(self):
        wo = WorkOrder.objects.create(
            machine=self.machine,
            created_by=self.manager,
            lifecycle_status=WorkOrder.LifecycleStatus.ASSIGNED,
        )
        # Simulate legacy backfill.
        WorkOrder.objects.filter(pk=wo.pk).update(blocker_system_version=0)
        WorkOrderBlocker.objects.create(
            work_order=wo,
            kind=WorkOrderBlocker.Kind.OPERATIONAL,
            status=WorkOrderBlocker.Status.OPEN,
            opened_by=self.tech,
        )
        wo.refresh_from_db()
        self.assertEqual(wo.blocker_system_version, 1)

    def test_blocker_bump_is_idempotent(self):
        wo = WorkOrder.objects.create(
            machine=self.machine,
            created_by=self.manager,
            lifecycle_status=WorkOrder.LifecycleStatus.ASSIGNED,
        )
        WorkOrder.objects.filter(pk=wo.pk).update(blocker_system_version=0)
        WorkOrderBlocker.objects.create(
            work_order=wo,
            kind=WorkOrderBlocker.Kind.OPERATIONAL,
            status=WorkOrderBlocker.Status.OPEN,
            opened_by=self.tech,
        )
        WorkOrderBlocker.objects.create(
            work_order=wo,
            kind=WorkOrderBlocker.Kind.PART,
            status=WorkOrderBlocker.Status.OPEN,
            opened_by=self.tech,
        )
        wo.refresh_from_db()
        self.assertEqual(wo.blocker_system_version, 1)
        self.assertEqual(
            WorkOrderBlocker.objects.filter(work_order=wo).count(), 2
        )


class LegacyReconciliationDashboardTests(TestCase):
    """The /work-orders/legacy-reconciliation/ dashboard."""

    def setUp(self):
        self.manager = _make_user("manager_lrd", User.Role.MANAGER)
        self.tech = _make_user("tech_lrd", User.Role.TECHNICIAN)
        self.machine = Machine.objects.create(name="Press LRD", qr_code="LRD-M1")

    def _legacy_wo(self):
        wo = WorkOrder.objects.create(
            machine=self.machine,
            created_by=self.manager,
            lifecycle_status=WorkOrder.LifecycleStatus.ASSIGNED,
        )
        WorkOrder.objects.filter(pk=wo.pk).update(blocker_system_version=0)
        wo.refresh_from_db()
        return wo

    def test_legacy_dashboard_renders(self):
        wo = self._legacy_wo()
        self.client.force_login(self.manager)
        resp = self.client.get(reverse("legacy_reconciliation"))
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode()
        self.assertIn("Legacy Work Order Reconciliation", body)
        self.assertIn(f"WO-{wo.number}", body)
        # KPI shows at least 1 legacy WO.
        self.assertContains(resp, "Legacy (v0)")

    def test_legacy_dashboard_excludes_technicians(self):
        self._legacy_wo()
        self.client.force_login(self.tech)
        resp = self.client.get(reverse("legacy_reconciliation"))
        self.assertNotEqual(resp.status_code, 200)