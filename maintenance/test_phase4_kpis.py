import uuid
"""
Phase 4 — Reliability + Failure Mode + Reports Counters.

Tests for the kpi_dashboard view additions:
  - failure_mode_distribution: top 5 failure modes by count in last 90 days
  - top_failing_assets: top 5 assets by closed corrective WO count
  - top_reporters: top 5 operators by issue count in last 30 days
  - cost_per_failure: total cost / failure count in last 90 days
  - failures_90d: closed corrective WO count in last 90 days
"""
from datetime import timedelta
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import User
from maintenance.models import (
    MaintenanceIssue, WorkOrder, CostTransaction, Machine,
)


class FailureModeDistributionTests(TestCase):
    """Top failure modes KPI: aggregated by failure_mode over the last 90 days."""

    def setUp(self):
        from maintenance.models import FailureMode
        self.user = User.objects.create_user(username="viewer", role=User.Role.MANAGER)
        self.client.force_login(self.user)
        # Use the first 3 existing failure modes (seeded by migration)
        modes = list(FailureMode.objects.order_by("id")[:3])
        self.fm1, self.fm2, self.fm3 = modes
        self.machine = Machine.objects.create(
            name="Test Press", asset_level=3, asset_code="T-1", qr_code=str(uuid.uuid4())
        )
        self.operator = User.objects.create_user(
            username="reporter1", role=User.Role.OPERATOR
        )

    def _create_issue(self, fm, days_ago=1):
        # auto_now_add ignores explicit created_at on create, so use update() to backdate
        issue = MaintenanceIssue.objects.create(
            machine=self.machine,
            failure_mode=fm,
            description=f"Test {fm.code}",
            reported_by=self.operator,
        )
        if days_ago:
            MaintenanceIssue.objects.filter(pk=issue.pk).update(
                created_at=timezone.now() - timedelta(days=days_ago)
            )
        return issue

    def test_top_failure_modes_ordered_by_count(self):
        # 5 issues for fm1, 3 for fm2, 1 for fm3
        for _ in range(5):
            self._create_issue(self.fm1, days_ago=5)
        for _ in range(3):
            self._create_issue(self.fm2, days_ago=10)
        self._create_issue(self.fm3, days_ago=20)

        response = self.client.get(reverse("kpi_dashboard"))
        self.assertEqual(response.status_code, 200)
        dist = response.context["failure_mode_distribution"]
        self.assertEqual(len(dist), 3)
        self.assertEqual(dist[0]["code"], self.fm1.code)
        self.assertEqual(dist[0]["count"], 5)
        self.assertEqual(dist[1]["count"], 3)
        self.assertEqual(dist[2]["count"], 1)

    def test_failure_modes_excludes_issues_outside_90d(self):
        # Inside window
        self._create_issue(self.fm1, days_ago=89)
        # Outside window
        self._create_issue(self.fm1, days_ago=91)

        response = self.client.get(reverse("kpi_dashboard"))
        dist = response.context["failure_mode_distribution"]
        self.assertEqual(len(dist), 1)
        self.assertEqual(dist[0]["count"], 1)

    def test_empty_when_no_failure_modes(self):
        response = self.client.get(reverse("kpi_dashboard"))
        self.assertEqual(response.context["failure_mode_distribution"], [])


class TopFailingAssetsTests(TestCase):
    """Top failing assets KPI: closed corrective WOs grouped by machine/component."""

    def setUp(self):
        self.user = User.objects.create_user(username="viewer2", role=User.Role.MANAGER)
        self.client.force_login(self.user)
        self.m1 = Machine.objects.create(name="Press A", asset_level=3, asset_code="P-A", qr_code=str(uuid.uuid4()))
        self.m2 = Machine.objects.create(name="Press B", asset_level=3, asset_code="P-B", qr_code=str(uuid.uuid4()))
        self.m3 = Machine.objects.create(name="Press C", asset_level=3, asset_code="P-C", qr_code=str(uuid.uuid4()))
        self.tech = User.objects.create_user(
            username="t1", role=User.Role.TECHNICIAN
        )
        self.manager = User.objects.create_user(
            username="m1", role=User.Role.MANAGER
        )

    def _create_breakdown_wo(self, machine, days_ago=1, closed=True):
        wo = WorkOrder.objects.create(
            machine=machine,
            category=WorkOrder.Category.BREAKDOWN,
            lifecycle_status=WorkOrder.LifecycleStatus.CLOSED if closed else WorkOrder.LifecycleStatus.ASSIGNED,
            assigned_technician=self.tech,
            created_by=self.manager,
        )
        WorkOrder.objects.filter(pk=wo.pk).update(
            created_at=timezone.now() - timedelta(days=days_ago),
            updated_at=timezone.now() - timedelta(days=days_ago),
        )
        return wo

    def test_top_failing_assets_ordered_by_failure_count(self):
        # 4 failures on m1, 2 on m2, 1 on m3
        for _ in range(4):
            self._create_breakdown_wo(self.m1)
        for _ in range(2):
            self._create_breakdown_wo(self.m2)
        self._create_breakdown_wo(self.m3)

        response = self.client.get(reverse("kpi_dashboard"))
        assets = response.context["top_failing_assets"]
        self.assertEqual(len(assets), 3)
        self.assertEqual(assets[0]["machine__name"], "Press A")
        self.assertEqual(assets[0]["failure_count"], 4)
        self.assertEqual(assets[1]["failure_count"], 2)
        self.assertEqual(assets[2]["failure_count"], 1)

    def test_excludes_open_and_preventive_wos(self):
        # Open WO — excluded
        WorkOrder.objects.create(
            machine=self.m1,
            category=WorkOrder.Category.BREAKDOWN,
            lifecycle_status=WorkOrder.LifecycleStatus.ASSIGNED,
            created_by=self.manager,
            assigned_technician=self.tech,
        )
        # Preventive WO — excluded
        WorkOrder.objects.create(
            machine=self.m1,
            category=WorkOrder.Category.PREVENTIVE,
            lifecycle_status=WorkOrder.LifecycleStatus.CLOSED,
            created_by=self.manager,
            assigned_technician=self.tech,
            updated_at=timezone.now(),
        )
        # Closed breakdown — counted
        self._create_breakdown_wo(self.m1)

        response = self.client.get(reverse("kpi_dashboard"))
        assets = response.context["top_failing_assets"]
        self.assertEqual(len(assets), 1)
        self.assertEqual(assets[0]["failure_count"], 1)


class TopReportersTests(TestCase):
    """Top reporters KPI: operators ranked by issue count in last 30 days."""

    def setUp(self):
        self.user = User.objects.create_user(username="viewer3", role=User.Role.MANAGER)
        self.client.force_login(self.user)
        self.machine = Machine.objects.create(
            name="Test Press", asset_level=3, asset_code="T-1", qr_code=str(uuid.uuid4())
        )
        self.op1 = User.objects.create_user(
            username="alice", role=User.Role.OPERATOR
        )
        self.op2 = User.objects.create_user(
            username="bob", role=User.Role.OPERATOR
        )
        self.op3 = User.objects.create_user(
            username="charlie", role=User.Role.OPERATOR
        )

    def _create_issue(self, user, days_ago=1):
        issue = MaintenanceIssue.objects.create(
            machine=self.machine,
            description=f"Issue by {user.username}",
            reported_by=user,
        )
        if days_ago:
            MaintenanceIssue.objects.filter(pk=issue.pk).update(
                created_at=timezone.now() - timedelta(days=days_ago)
            )
        return issue

    def test_top_reporters_ordered_by_reports(self):
        # 5 issues by alice, 2 by bob, 1 by charlie
        for _ in range(5):
            self._create_issue(self.op1)
        for _ in range(2):
            self._create_issue(self.op2)
        self._create_issue(self.op3)

        response = self.client.get(reverse("kpi_dashboard"))
        reporters = response.context["top_reporters"]
        self.assertEqual(len(reporters), 3)
        self.assertEqual(reporters[0]["username"], "alice")
        self.assertEqual(reporters[0]["reports"], 5)

    def test_excludes_issues_outside_30d(self):
        # In window: 1 day ago
        self._create_issue(self.op1, days_ago=1)
        # Out of window: 100 days ago
        self._create_issue(self.op1, days_ago=100)

        response = self.client.get(reverse("kpi_dashboard"))
        reporters = response.context["top_reporters"]
        self.assertEqual(len(reporters), 1)
        self.assertEqual(reporters[0]["reports"], 1)

class CostPerFailureTests(TestCase):
    """Cost-per-failure KPI."""

    def setUp(self):
        
        self.user = User.objects.create_user(username="viewer4", role=User.Role.MANAGER)
        self.client.force_login(self.user)
        self.machine = Machine.objects.create(
            name="Test Press", asset_level=3, asset_code="T-1", qr_code=str(uuid.uuid4())
        )
        self.tech = User.objects.create_user(
            username="t1", role=User.Role.TECHNICIAN
        )
        self.manager = User.objects.create_user(
            username="m1", role=User.Role.MANAGER
        )

    def _create_closed_breakdown_with_cost(self, cost_amount, days_ago=1):
        wo = WorkOrder.objects.create(
            machine=self.machine,
            category=WorkOrder.Category.BREAKDOWN,
            lifecycle_status=WorkOrder.LifecycleStatus.CLOSED,
            assigned_technician=self.tech,
            created_by=self.manager,
        )
        # auto_now fields ignore explicit values on create; use update() to backdate
        WorkOrder.objects.filter(pk=wo.pk).update(
            updated_at=timezone.now() - timedelta(days=days_ago),
        )
        txn = CostTransaction.objects.create(
            work_order=wo,
            machine=self.machine,
            amount=cost_amount,
            currency="SAR",
            category='adjustment',
            source_type="cost_adjustment",
            source_id=1,
            actor=self.manager,
        )
        if days_ago:
            CostTransaction.objects.filter(pk=txn.pk).update(
                occurred_at=timezone.now() - timedelta(days=days_ago),
            )
        return wo

    def test_cost_per_failure_with_failures(self):
        # 2 failures totaling 200.00 SAR -> 100.00 SAR / failure
        self._create_closed_breakdown_with_cost(100.00, days_ago=2)
        self._create_closed_breakdown_with_cost(100.00, days_ago=3)

        response = self.client.get(reverse("kpi_dashboard"))
        self.assertEqual(response.context["failures_90d"], 2)
        self.assertAlmostEqual(response.context["cost_per_failure"], 100.00, places=2)

    def test_cost_per_failure_none_when_no_failures(self):
        response = self.client.get(reverse("kpi_dashboard"))
        self.assertEqual(response.context["failures_90d"], 0)
        self.assertIsNone(response.context["cost_per_failure"])

    def test_failures_excludes_outside_90d(self):
        self._create_closed_breakdown_with_cost(50.00, days_ago=89)  # in
        self._create_closed_breakdown_with_cost(50.00, days_ago=91)  # out

        response = self.client.get(reverse("kpi_dashboard"))
        self.assertEqual(response.context["failures_90d"], 1)
        self.assertAlmostEqual(response.context["cost_per_failure"], 50.00, places=2)
