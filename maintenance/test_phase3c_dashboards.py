"""
Phase 3C — WorkOrder Blocker System: Reconciliation and Active Blockers dashboards.

Covers:
- reconciliation_dashboard: lists legacy WOs (blocker_system_version=0)
- active_blockers_dashboard: lists open blockers sorted by impact
"""
from __future__ import annotations

from datetime import datetime, timedelta
from unittest import mock

from django.contrib.contenttypes.models import ContentType
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import User
from inventory.models import SparePart
from maintenance.models import (
    Machine,
    WorkOrder,
    WorkOrderBlocker,
)


def _make_user(username: str, role: str) -> User:
    return User.objects.create_user(username=username, password="x", role=role)


def _make_wo(
    *,
    machine: Machine = None,
    created_by: User,
    assigned_technician: User = None,
    status: str = WorkOrder.Status.ASSIGNED,
    lifecycle_status: str = WorkOrder.LifecycleStatus.ASSIGNED,
    blocker_system_version: int = 0,
    created_at=None,
    **kwargs,
) -> WorkOrder:
    defaults = {
        "machine": machine,
        "created_by": created_by,
        "status": status,
        "lifecycle_status": lifecycle_status,
        "assigned_technician": assigned_technician,
    }
    if created_at:
        defaults["created_at"] = created_at
    defaults.update(kwargs)
    # Create the WO, then set blocker_system_version via update() to
    # bypass WorkOrder.save() which always bumps 0→1 for new records.
    wo = WorkOrder.objects.create(**defaults)
    if blocker_system_version == 0:
        WorkOrder.objects.filter(pk=wo.pk).update(blocker_system_version=0)
    return wo


# ---------------------------------------------------------------------------
# Reconciliation Dashboard Tests
# ---------------------------------------------------------------------------

class ReconciliationDashboardTests(TestCase):
    """Tests for the reconciliation_dashboard view."""

    def setUp(self):
        self.manager = _make_user("manager_rec", User.Role.MANAGER)
        self.machine = Machine.objects.create(
            name="CNC-1000",
            qr_code="CNC-1000",
        )
        self.wo_legacy = _make_wo(
            machine=self.machine,
            created_by=self.manager,
            assigned_technician=self.manager,
            blocker_system_version=0,
        )
        self.wo_migrated = _make_wo(
            machine=self.machine,
            created_by=self.manager,
            blocker_system_version=1,
        )
        self.url = reverse("reconciliation_dashboard")

    def test_legacy_wos_visible(self):
        """GET as manager returns legacy WOs in response."""
        self.client.force_login(self.manager)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertIn(self.wo_legacy, response.context["legacy_wos"])
        self.assertNotIn(self.wo_migrated, response.context["legacy_wos"])

    def test_filter_by_lifecycle_status(self):
        """GET with lifecycle_status=assigned filters correctly."""
        wo_closed = _make_wo(
            machine=self.machine,
            created_by=self.manager,
            blocker_system_version=0,
            lifecycle_status=WorkOrder.LifecycleStatus.CLOSED,
            status=WorkOrder.Status.CLOSED,
        )
        self.client.force_login(self.manager)
        response = self.client.get(self.url, {"lifecycle_status": "assigned"})
        self.assertEqual(response.status_code, 200)
        self.assertIn(self.wo_legacy, response.context["legacy_wos"])
        self.assertNotIn(wo_closed, response.context["legacy_wos"])

    def test_search_by_number(self):
        """GET with q=WO- filters by number."""
        wo2 = _make_wo(
            machine=self.machine,
            created_by=self.manager,
            blocker_system_version=0,
        )
        self.client.force_login(self.manager)
        response = self.client.get(self.url, {"q": str(self.wo_legacy.number)})
        self.assertEqual(response.status_code, 200)
        self.assertIn(self.wo_legacy, response.context["legacy_wos"])
        self.assertNotIn(wo2, response.context["legacy_wos"])

    def test_search_by_machine_name(self):
        """GET with q=machine name filters correctly."""
        machine2 = Machine.objects.create(name="Press-200", qr_code="PRESS-200")
        wo2 = _make_wo(
            machine=machine2,
            created_by=self.manager,
            blocker_system_version=0,
        )
        self.client.force_login(self.manager)
        response = self.client.get(self.url, {"q": "CNC"})
        self.assertEqual(response.status_code, 200)
        self.assertIn(self.wo_legacy, response.context["legacy_wos"])
        self.assertNotIn(wo2, response.context["legacy_wos"])

    def test_date_range(self):
        """GET with created_after and created_before filters correctly."""
        old_wo = _make_wo(
            machine=self.machine,
            created_by=self.manager,
            blocker_system_version=0,
            created_at=timezone.now() - timedelta(days=30),
        )
        # Force the created_at for old_wo since auto_now_add overrides
        WorkOrder.objects.filter(pk=old_wo.pk).update(
            created_at=timezone.now() - timedelta(days=30)
        )
        self.client.force_login(self.manager)
        now = timezone.now()
        after = (now - timedelta(days=5)).strftime("%Y-%m-%d")
        before = (now + timedelta(days=1)).strftime("%Y-%m-%d")
        response = self.client.get(self.url, {
            "created_after": after,
            "created_before": before,
        })
        self.assertEqual(response.status_code, 200)
        self.assertIn(self.wo_legacy, response.context["legacy_wos"])
        self.assertNotIn(old_wo, response.context["legacy_wos"])

    def test_all_migrated_banner(self):
        """If no legacy WOs exist, green banner is shown."""
        self.wo_legacy.delete()
        self.client.force_login(self.manager)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        body = response.content.decode("utf-8")
        self.assertIn("All WorkOrders are on the blocker system", body)

    def test_total_count_in_context(self):
        """total_count context variable reflects count of legacy WOs."""
        self.client.force_login(self.manager)
        response = self.client.get(self.url)
        self.assertEqual(response.context["total_count"], 1)

    def test_filters_in_context(self):
        """Active filters are passed to context."""
        self.client.force_login(self.manager)
        response = self.client.get(self.url, {"lifecycle_status": "assigned", "q": "test"})
        self.assertEqual(response.context["filters"]["lifecycle_status"], "assigned")
        self.assertEqual(response.context["filters"]["q"], "test")

    def test_status_choices_in_context(self):
        """LifecycleStatus choices are passed to context."""
        self.client.force_login(self.manager)
        response = self.client.get(self.url)
        self.assertEqual(
            response.context["status_choices"],
            WorkOrder.LifecycleStatus.choices,
        )

    def test_pagination(self):
        """Create 30 legacy WOs, assert only 25 per page."""
        for i in range(28):
            _make_wo(
                machine=self.machine,
                created_by=self.manager,
                blocker_system_version=0,
            )
        self.client.force_login(self.manager)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["legacy_wos"]), 25)
        response_page2 = self.client.get(self.url, {"page": 2})
        self.assertEqual(response_page2.status_code, 200)
        self.assertEqual(len(response_page2.context["legacy_wos"]), 4)


# ---------------------------------------------------------------------------
# Active Blockers Dashboard Tests
# ---------------------------------------------------------------------------

class ActiveBlockersDashboardTests(TestCase):
    """Tests for the active_blockers_dashboard view."""

    def setUp(self):
        self.manager = _make_user("manager_abd", User.Role.MANAGER)
        self.tech = _make_user("tech_abd", User.Role.TECHNICIAN)
        self.machine = Machine.objects.create(
            name="CNC-1000",
            qr_code="CNC-1000",
        )
        self.wo = _make_wo(
            machine=self.machine,
            created_by=self.manager,
            assigned_technician=self.tech,
            blocker_system_version=1,
        )
        self.url = reverse("active_blockers_dashboard")

    def _make_blocker(self, kind=WorkOrderBlocker.Kind.PART, **kwargs):
        defaults = {
            "work_order": self.wo,
            "kind": kind,
            "opened_by": self.manager,
        }
        defaults.update(kwargs)
        return WorkOrderBlocker.objects.create(**defaults)

    def test_open_blockers_visible(self):
        """GET as manager shows open blockers."""
        blocker = self._make_blocker()
        self.client.force_login(self.manager)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        items = response.context["blockers"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["blocker"], blocker)

    def test_resolved_blockers_not_visible(self):
        """Only OPEN blockers are shown."""
        self._make_blocker(status=WorkOrderBlocker.Status.RESOLVED)
        self.client.force_login(self.manager)
        response = self.client.get(self.url)
        self.assertEqual(response.context["total_open"], 0)

    def test_filter_by_kind(self):
        """GET with kind=operational filters correctly."""
        part_blocker = self._make_blocker(kind=WorkOrderBlocker.Kind.PART)
        op_blocker = self._make_blocker(
            kind=WorkOrderBlocker.Kind.OPERATIONAL,
            note="Paused",
            external_label="Operational pause",
        )
        self.client.force_login(self.manager)
        response = self.client.get(self.url, {"kind": "operational"})
        self.assertEqual(response.status_code, 200)
        items = response.context["blockers"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["blocker"], op_blocker)

    def test_search_by_wo_number(self):
        """GET with q=WO number filters."""
        wo2 = _make_wo(
            machine=self.machine,
            created_by=self.manager,
            blocker_system_version=1,
        )
        blocker1 = self._make_blocker()
        blocker2 = WorkOrderBlocker.objects.create(
            work_order=wo2, kind=WorkOrderBlocker.Kind.PART,
            opened_by=self.manager,
        )
        self.client.force_login(self.manager)
        response = self.client.get(self.url, {"q": str(self.wo.number)})
        self.assertEqual(response.status_code, 200)
        items = response.context["blockers"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["blocker"], blocker1)

    def test_search_by_machine_name(self):
        """GET with q=machine name filters."""
        machine2 = Machine.objects.create(name="Press-200", qr_code="PRESS-200")
        wo2 = _make_wo(
            machine=machine2, created_by=self.manager,
            blocker_system_version=1,
        )
        blocker1 = self._make_blocker()
        blocker2 = WorkOrderBlocker.objects.create(
            work_order=wo2, kind=WorkOrderBlocker.Kind.PART,
            opened_by=self.manager,
        )
        self.client.force_login(self.manager)
        response = self.client.get(self.url, {"q": "Press"})
        self.assertEqual(response.status_code, 200)
        items = response.context["blockers"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["blocker"], blocker2)

    def test_impact_score_computed(self):
        """PART blocker with GFK to SparePart has impact score computed."""
        part = SparePart.objects.create(sku="BRG-6006", name="Bearing 6006")
        part_ct = ContentType.objects.get_for_model(SparePart)
        blocker = self._make_blocker(
            kind=WorkOrderBlocker.Kind.PART,
            content_type=part_ct,
            object_id=part.pk,
        )
        self.client.force_login(self.manager)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        items = response.context["blockers"]
        self.assertEqual(len(items), 1)
        impact = items[0]["impact"]
        self.assertIsNotNone(impact)
        self.assertTrue(0 <= impact.score <= 100)
        self.assertIn(impact.level, ("LOW", "MEDIUM", "HIGH"))

    def test_impact_level_filter(self):
        """GET with impact_level=HIGH filters correctly."""
        part = SparePart.objects.create(sku="BRG-6006", name="Bearing 6006")
        part_ct = ContentType.objects.get_for_model(SparePart)
        self._make_blocker(
            kind=WorkOrderBlocker.Kind.PART,
            content_type=part_ct,
            object_id=part.pk,
        )
        op_blocker = self._make_blocker(
            kind=WorkOrderBlocker.Kind.OPERATIONAL,
            note="Paused",
            external_label="Operational pause",
        )
        self.client.force_login(self.manager)
        # With no part data the impact is LOW, so filtering by HIGH should exclude it
        response = self.client.get(self.url, {"impact_level": "HIGH"})
        self.assertEqual(response.status_code, 200)
        items = response.context["blockers"]
        self.assertEqual(len(items), 0)

    def test_no_blockers_banner(self):
        """If no open blockers, green banner is shown."""
        self.client.force_login(self.manager)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        body = response.content.decode("utf-8")
        self.assertIn("No active blockers. All clear!", body)

    def test_pagination(self):
        """Create 30 blockers, assert only 25 per page."""
        for i in range(30):
            self._make_blocker(
                kind=WorkOrderBlocker.Kind.OPERATIONAL,
                note=f"Blocker {i}",
                external_label=f"Blocker {i}",
            )
        self.client.force_login(self.manager)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["blockers"]), 25)
        response_page2 = self.client.get(self.url, {"page": 2})
        self.assertEqual(response_page2.status_code, 200)
        self.assertEqual(len(response_page2.context["blockers"]), 5)

    def test_total_open_in_context(self):
        """total_open reflects count of open blockers."""
        self._make_blocker()
        self._make_blocker(kind=WorkOrderBlocker.Kind.OPERATIONAL, note="x", external_label="x")
        self.client.force_login(self.manager)
        response = self.client.get(self.url)
        self.assertEqual(response.context["total_open"], 2)

    def test_impact_level_choices_in_context(self):
        """impact_level_choices is passed to context."""
        self.client.force_login(self.manager)
        response = self.client.get(self.url)
        self.assertEqual(
            response.context["impact_level_choices"],
            [("LOW", "Low Impact"), ("MEDIUM", "Medium Impact"), ("HIGH", "High Impact")],
        )

    def test_kind_choices_in_context(self):
        """kind_choices is passed to context."""
        self.client.force_login(self.manager)
        response = self.client.get(self.url)
        self.assertEqual(
            response.context["kind_choices"],
            WorkOrderBlocker.Kind.choices,
        )

    def test_non_part_blocker_impact_is_none(self):
        """OPERATIONAL blocker has no impact score."""
        self._make_blocker(
            kind=WorkOrderBlocker.Kind.OPERATIONAL,
            note="Paused",
            external_label="Operational pause",
        )
        self.client.force_login(self.manager)
        response = self.client.get(self.url)
        items = response.context["blockers"]
        self.assertEqual(len(items), 1)
        self.assertIsNone(items[0]["impact"])
