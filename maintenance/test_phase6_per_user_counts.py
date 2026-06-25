"""
Phase 6 — Per-user counts and operator consumable enhancements.

Covers:
- Per-user report counts on the operator dashboard
  (my_issues_count_30d, _7d, _unresolved).
- Per-user nav counters in the context processor
  (nav_my_issues_30d, nav_my_issues_unresolved).
- Period filter on issue_list (?mine=1&period=7|30|90).
- Period filter + counters on consumables_view
  (my_today_count, my_7d_count, my_30d_count, period_days).
- 30-day supply counters on the KPI dashboard
  (parts_consumed_30d_qty, eros_accepted_30d, pos_received_30d).
- can_see_procurement gate (operators and technicians must NOT see it).
"""
from datetime import timedelta
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import User
from inventory.models import (
    ConsumableAssignment, Inventory, SparePart, StockMovement,
)
from maintenance.context_processors import mms_nav
from maintenance.models import (
    ExternalRepairOrder, MaintenanceIssue, Site, WorkOrder, Machine,
)
from procurement.models import PurchaseOrder


def _make_user(username, role):
    return User.objects.create_user(username=username, role=role)


def _make_site(name="TST-Site"):
    return Site.objects.create(name=name, code=name.upper(), is_default=True)


def _make_machine(code="M-1"):
    return Machine.objects.create(
        name=f"Test Machine {code}",
        asset_level=3,
        asset_code=code,
        qr_code=f"qr-{code}-{timezone.now().timestamp()}",
    )


def _make_part(sku="P-1", name="Test Part", allow_op=True):
    return SparePart.objects.create(
        sku=sku, name=name, is_consumable=True,
        allow_operator_consumption=allow_op,
    )


def _stock_in(part, site, qty, unit_cost=Decimal("10.00")):
    inv, _ = Inventory.objects.get_or_create(part=part, site=site)
    inv.quantity_available = qty
    inv.save()
    return inv


# -------- Area 1: Per-user report counts --------

class OperatorReportCountersTests(TestCase):
    """The operator dashboard exposes per-user reporting counters that
    the operator can drill into via the filtered issue list."""

    def setUp(self):
        self.operator = _make_user("op1", User.Role.OPERATOR)
        self.other_op = _make_user("op2", User.Role.OPERATOR)
        self.machine = _make_machine("OP-1")
        self.client.force_login(self.operator)

    def _create_issue(self, reporter, days_ago=0):
        i = MaintenanceIssue.objects.create(
            description="x", machine=self.machine, reported_by=reporter,
        )
        if days_ago:
            # auto_now_add ignores our created_at on .create(); update.
            MaintenanceIssue.objects.filter(pk=i.pk).update(
                created_at=timezone.now() - timedelta(days=days_ago),
            )
            i.refresh_from_db()
        return i

    def test_dashboard_shows_my_30d_7d_unresolved_counts(self):
        # 3 NEW issues in last 7 days
        for i in range(3):
            self._create_issue(self.operator, days_ago=0)
        # 1 issue 14 days ago (in 30d window, not 7d)
        self._create_issue(self.operator, days_ago=14)
        # 1 issue 60 days ago (not in any window)
        self._create_issue(self.operator, days_ago=60)
        # 1 issue from another operator (should not count)
        self._create_issue(self.other_op, days_ago=0)
        # 1 already-converted issue in the 7d window (in 7d/30d but
        # not unresolved)
        converted = self._create_issue(self.operator, days_ago=0)
        converted.status = MaintenanceIssue.Status.CONVERTED
        converted.save()
        response = self.client.get(reverse("dashboard"))
        self.assertEqual(response.status_code, 200)
        # 30d: 3 (today) + 1 (14d) + 1 (converted) = 5
        self.assertEqual(response.context["my_issues_count_30d"], 5)
        # 7d: 3 (today) + 1 (converted) = 4
        self.assertEqual(response.context["my_issues_count_7d"], 4)
        # 6 operator issues total (3 today + 1 14d + 1 60d + 1 converted
        # today) - 1 converted = 5 unresolved
        self.assertEqual(response.context["my_issues_count_unresolved"], 5)

    def test_dashboard_zero_count_when_no_issues(self):
        response = self.client.get(reverse("dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["my_issues_count_30d"], 0)
        self.assertEqual(response.context["my_issues_count_7d"], 0)
        self.assertEqual(response.context["my_issues_count_unresolved"], 0)


class NavMyIssuesCountersTests(TestCase):
    """The context processor exposes per-user issue counters as nav
    badges for operators and technicians (NOT for managers, etc.)."""

    def setUp(self):
        self.op = _make_user("op", User.Role.OPERATOR)
        self.tech = _make_user("tech", User.Role.TECHNICIAN)
        self.mgr = _make_user("mgr", User.Role.MANAGER)
        self.machine = _make_machine("NAV-1")

    def _req(self, user):
        from django.test import RequestFactory
        return RequestFactory().get("/")
        # attach user

    def test_nav_counters_present_for_operator(self):
        # 2 issues in last 30d
        for i in range(2):
            MaintenanceIssue.objects.create(
                description="x", machine=self.machine, reported_by=self.op,
            )
        from django.test import RequestFactory
        req = RequestFactory().get("/")
        req.user = self.op
        ctx = mms_nav(req)
        self.assertEqual(ctx["nav_my_issues_30d"], 2)
        # both unresolved (status=NEW)
        self.assertEqual(ctx["nav_my_issues_unresolved"], 2)

    def test_nav_counters_present_for_technician(self):
        MaintenanceIssue.objects.create(
            description="x", machine=self.machine, reported_by=self.tech,
        )
        from django.test import RequestFactory
        req = RequestFactory().get("/")
        req.user = self.tech
        ctx = mms_nav(req)
        self.assertEqual(ctx["nav_my_issues_30d"], 1)
        self.assertEqual(ctx["nav_my_issues_unresolved"], 1)

    def test_nav_counters_zero_for_manager(self):
        from django.test import RequestFactory
        req = RequestFactory().get("/")
        req.user = self.mgr
        ctx = mms_nav(req)
        # managers don't get these per-user badges
        self.assertEqual(ctx["nav_my_issues_30d"], 0)
        self.assertEqual(ctx["nav_my_issues_unresolved"], 0)

    def test_nav_counters_exclude_converted_issues(self):
        i = MaintenanceIssue.objects.create(
            description="x", machine=self.machine, reported_by=self.op,
        )
        i.status = MaintenanceIssue.Status.CONVERTED
        i.save()
        from django.test import RequestFactory
        req = RequestFactory().get("/")
        req.user = self.op
        ctx = mms_nav(req)
        self.assertEqual(ctx["nav_my_issues_30d"], 1)  # still in 30d window
        self.assertEqual(ctx["nav_my_issues_unresolved"], 0)  # not unresolved


class IssueListPeriodFilterTests(TestCase):
    """issue_list supports ?mine=1 and ?period=7|30|90 filters."""

    def setUp(self):
        self.op = _make_user("op", User.Role.OPERATOR)
        self.other = _make_user("other", User.Role.OPERATOR)
        self.machine = _make_machine("IL-1")
        self.client.force_login(self.op)

    def _issue(self, reporter, days_ago):
        i = MaintenanceIssue.objects.create(
            description="x", machine=self.machine, reported_by=reporter,
        )
        if days_ago:
            MaintenanceIssue.objects.filter(pk=i.pk).update(
                created_at=timezone.now() - timedelta(days=days_ago),
            )
            i.refresh_from_db()
        return i

    def test_default_no_filter_returns_all_my_issues(self):
        self._issue(self.op, 0)
        self._issue(self.op, 60)
        self._issue(self.other, 0)  # operator can't see this normally
        response = self.client.get(reverse("issue_list"))
        # operator's default scope is their own issues
        self.assertEqual(response.context["mine_only"], False)
        self.assertEqual(response.context["period_days"], 0)

    def test_mine_filter_shows_only_own(self):
        self._issue(self.op, 0)
        self._issue(self.other, 0)
        response = self.client.get(reverse("issue_list") + "?mine=1")
        self.assertEqual(response.context["mine_only"], True)
        issues = list(response.context["issues"])
        for i in issues:
            self.assertEqual(i.reported_by, self.op)

    def test_period_30_filter(self):
        self._issue(self.op, 5)   # in 30d
        self._issue(self.op, 45)  # out
        self._issue(self.op, 100)  # out
        response = self.client.get(reverse("issue_list") + "?period=30")
        self.assertEqual(response.context["period_days"], 30)
        issues = list(response.context["issues"])
        self.assertEqual(len(issues), 1)

    def test_period_7_filter(self):
        self._issue(self.op, 3)
        self._issue(self.op, 10)  # out
        response = self.client.get(reverse("issue_list") + "?period=7")
        self.assertEqual(response.context["period_days"], 7)
        issues = list(response.context["issues"])
        self.assertEqual(len(issues), 1)

    def test_invalid_period_ignored(self):
        self._issue(self.op, 0)
        response = self.client.get(reverse("issue_list") + "?period=999")
        self.assertEqual(response.context["period_days"], 0)


# -------- Area 2: Operator consumable view enhancements --------

class ConsumableViewCounterTests(TestCase):
    """/consumables/ shows per-user 30d/7d/today counters and supports
    a period filter on the history table."""

    def setUp(self):
        self.op = _make_user("op", User.Role.OPERATOR)
        self.part = _make_part(sku="CON-P1", name="Consumable P1")
        self.site = _make_site()
        _stock_in(self.part, self.site, Decimal("100"))
        self.client.force_login(self.op)

    def _log(self, qty, days_ago=0):
        # Create the ConsumableAssignment + StockMovement directly to
        # control timing. Backdate via .update() because auto_now_add
        # ignores our value on .create().
        inv = Inventory.objects.get(part=self.part, site=self.site)
        inv.quantity_available -= qty
        inv.save()
        sm = StockMovement.objects.create(
            part=self.part, site=self.site,
            movement_type=StockMovement.MovementType.CONSUMABLE_USE,
            quantity=qty,
            quantity_before=inv.quantity_available + qty,
            quantity_after=inv.quantity_available,
            unit_cost=Decimal("5.00"),
            performed_by=self.op,
        )
        a = ConsumableAssignment.objects.create(
            part=self.part,
            consumed_by=self.op,
            issued_by=self.op,
            quantity=qty,
            source=ConsumableAssignment.Source.SELF_SERVICE,
            approved=True,
            site=self.site,
            stock_movement=sm,
        )
        if days_ago:
            ts = timezone.now() - timedelta(days=days_ago)
            # ConsumableAssignment.created_at is auto_now_add; backdate
            # via .update() since .create() ignores our value.
            ConsumableAssignment.objects.filter(pk=a.pk).update(created_at=ts)
            a.refresh_from_db()
        return a

    def test_counters_aggregate_across_windows(self):
        # 2 today
        self._log(Decimal("1"), days_ago=0)
        self._log(Decimal("2"), days_ago=0)
        # 1 in 7d (not today)
        self._log(Decimal("3"), days_ago=3)
        # 1 in 30d (not 7d)
        self._log(Decimal("4"), days_ago=15)
        # 1 outside 30d
        self._log(Decimal("5"), days_ago=45)
        response = self.client.get(reverse("consumables"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["my_today_count"], 2)
        self.assertEqual(response.context["my_7d_count"], 3)
        self.assertEqual(response.context["my_30d_count"], 4)
        self.assertEqual(response.context["my_today_qty"], Decimal("3"))
        self.assertEqual(response.context["my_7d_qty"], Decimal("6"))
        self.assertEqual(response.context["my_30d_qty"], Decimal("10"))
        # Top part in 30d
        self.assertEqual(response.context["top_part_30d"]["part__name"], "Consumable P1")
        self.assertEqual(response.context["top_part_30d"]["total"], Decimal("10"))

    def test_period_filter_scopes_history(self):
        self._log(Decimal("1"), days_ago=2)   # in 7d
        self._log(Decimal("2"), days_ago=20)  # not in 7d, in 30d
        self._log(Decimal("3"), days_ago=60)  # not in 30d
        # ?period=7 should show 1
        response = self.client.get(reverse("consumables") + "?period=7")
        self.assertEqual(response.context["period_days"], 7)
        self.assertEqual(len(list(response.context["assignments"])), 1)
        # ?period=30 should show 2
        response = self.client.get(reverse("consumables") + "?period=30")
        self.assertEqual(response.context["period_days"], 30)
        self.assertEqual(len(list(response.context["assignments"])), 2)
        # no filter shows all 3
        response = self.client.get(reverse("consumables"))
        self.assertEqual(response.context["period_days"], 0)
        self.assertEqual(len(list(response.context["assignments"])), 3)

    def test_counters_zero_for_new_user(self):
        response = self.client.get(reverse("consumables"))
        self.assertEqual(response.context["my_today_count"], 0)
        self.assertEqual(response.context["my_30d_count"], 0)
        self.assertIsNone(response.context["top_part_30d"])


# -------- Area 3: Manager KPI 30-day supply counters --------

class KpiDashboardSupplyCountersTests(TestCase):
    """The KPI dashboard shows 30-day supply counters and gates them
    behind can_see_procurement."""

    def setUp(self):
        self.mgr = _make_user("mgr", User.Role.MANAGER)
        self.op = _make_user("op", User.Role.OPERATOR)
        self.site = _make_site()
        self.machine = _make_machine("KPI-1")
        self.part = _make_part(sku="KPI-P1", name="KPI Part")

    def test_manager_sees_supply_counters(self):
        # Create: 1 consumable log in last 30d
        _stock_in(self.part, self.site, Decimal("100"))
        inv = Inventory.objects.get(part=self.part, site=self.site)
        inv.quantity_available -= Decimal("7")
        inv.save()
        sm = StockMovement.objects.create(
            part=self.part, site=self.site,
            movement_type=StockMovement.MovementType.CONSUMABLE_USE,
            quantity=Decimal("7"),
            quantity_before=Decimal("100"),
            quantity_after=Decimal("93"),
            unit_cost=Decimal("5"),
            performed_by=self.op,
        )
        ConsumableAssignment.objects.create(
            part=self.part, consumed_by=self.op, issued_by=self.op,
            quantity=Decimal("7"),
            source=ConsumableAssignment.Source.SELF_SERVICE,
            approved=True, site=self.site, stock_movement=sm,
        )
        # 1 ERO accepted in last 30d
        ero = ExternalRepairOrder.objects.create(
            work_order=None, title="x", description="y",
            created_by=self.mgr, status=ExternalRepairOrder.Status.CLOSED,
            closed_at=timezone.now(),
        )
        # 1 PO received in last 30d
        from procurement.models import Supplier
        sup = Supplier.objects.create(name="Acme", code="AC1")
        po = PurchaseOrder.objects.create(
            po_number="PO-T1", supplier=sup,
            status=PurchaseOrder.Status.RECEIVED,
            received_at=timezone.now(),
            created_by=self.mgr, handled_by=self.mgr,
        )
        self.client.force_login(self.mgr)
        response = self.client.get(reverse("kpi_dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["parts_consumed_30d_qty"], 7)
        self.assertEqual(response.context["parts_consumed_30d_logs"], 1)
        self.assertEqual(response.context["eros_accepted_30d"], 1)
        self.assertEqual(response.context["pos_received_30d"], 1)
        self.assertEqual(response.context["pos_fully_received_30d"], 1)
        self.assertTrue(response.context["can_see_procurement"])

    def test_technician_does_not_see_supply_counters(self):
        # Operators can't see the KPI page at all (redirected). The
        # can_see_procurement gate is what hides supply counters from
        # technicians, who CAN see the KPI page.
        tech = _make_user("tech", User.Role.TECHNICIAN)
        self.client.force_login(tech)
        response = self.client.get(reverse("kpi_dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context["can_see_procurement"])
        # The context values are still computed (so the template can
        # decide), but the template will not render them.
        self.assertEqual(response.context["parts_consumed_30d_qty"], 0)
        self.assertEqual(response.context["eros_accepted_30d"], 0)
        self.assertEqual(response.context["pos_received_30d"], 0)

    def test_po_partial_received_counts_as_received(self):
        from procurement.models import Supplier
        sup = Supplier.objects.create(name="Acme2", code="AC2")
        po = PurchaseOrder.objects.create(
            po_number="PO-P1", supplier=sup,
            status=PurchaseOrder.Status.PARTIAL_RECEIVED,
            received_at=timezone.now(),
            created_by=self.mgr, handled_by=self.mgr,
        )
        self.client.force_login(self.mgr)
        response = self.client.get(reverse("kpi_dashboard"))
        self.assertEqual(response.context["pos_received_30d"], 1)
        self.assertEqual(response.context["pos_fully_received_30d"], 0)
