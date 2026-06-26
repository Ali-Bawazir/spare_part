"""Tests for Phase 2: Machine detail PM tab + sidebar overdue badge."""
from datetime import timedelta

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import User
from maintenance.models import (
    Machine, PMExecution, PMSchedule, PMTemplate, PMChecklistItem, Site, WorkOrder,
)


def _make_user(username, role):
    return User.objects.create_user(username=username, password="x", role=role)


def _make_machine(qr="PM2-M1"):
    site = Site.objects.filter(is_default=True).first() or Site.objects.create(
        name="X", is_default=True, is_active=True
    )
    return Machine.objects.create(
        name=qr, qr_code=qr, asset_level=3, asset_code=qr, is_active=True, site=site,
    )


def _make_template(code="PM2-T1", priority="medium"):
    t = PMTemplate.objects.create(
        code=code, title="Test " + code, estimated_duration_minutes=30,
        priority=priority, is_active=True,
    )
    PMChecklistItem.objects.create(template=t, order=1, text="Step 1", is_required=True)
    return t


class MachinePMTabRenderTests(TestCase):
    def setUp(self):
        self.manager = _make_user("pmtab_mgr", User.Role.MANAGER)
        self.machine = _make_machine()
        self.template = _make_template()

    def test_pm_tab_renders_when_schedule_exists(self):
        PMSchedule.objects.create(
            template=self.template, machine=self.machine,
            frequency_type="monthly", interval=1,
            start_date=timezone.now().date(),
            next_due_at=timezone.now() + timedelta(days=7),
        )
        self.client.force_login(self.manager)
        r = self.client.get(reverse("machine_detail", args=[self.machine.pk]))
        self.assertEqual(r.status_code, 200)
        body = r.content.decode()
        self.assertIn('id="tab-pms"', body)
        self.assertIn(self.template.code, body)
        self.assertIn(self.template.title, body)

    def test_pm_tab_shows_empty_state_when_no_schedules(self):
        self.client.force_login(self.manager)
        r = self.client.get(reverse("machine_detail", args=[self.machine.pk]))
        body = r.content.decode()
        self.assertIn('id="tab-pms"', body)
        self.assertIn("No PM schedules yet", body)


class MachinePMStatsTests(TestCase):
    def setUp(self):
        self.manager = _make_user("pmstats_mgr", User.Role.MANAGER)
        self.machine = _make_machine()
        self.template = _make_template()
        now = timezone.now()

        PMSchedule.objects.create(
            template=self.template, machine=self.machine,
            frequency_type="monthly", interval=1,
            start_date=now.date(), next_due_at=now + timedelta(days=5),
            is_active=True,
        )
        PMSchedule.objects.create(
            template=self.template, machine=self.machine,
            frequency_type="monthly", interval=1,
            start_date=now.date(), next_due_at=now + timedelta(days=30),
            is_active=True,
        )
        PMSchedule.objects.create(
            template=self.template, machine=self.machine,
            frequency_type="monthly", interval=1,
            start_date=(now - timedelta(days=60)).date(),
            next_due_at=now - timedelta(days=10),
            is_active=True,
        )
        PMSchedule.objects.create(
            template=self.template, machine=self.machine,
            frequency_type="monthly", interval=1,
            start_date=now.date(), next_due_at=now + timedelta(days=5),
            is_active=False,
        )

    def test_stats_keys_present(self):
        self.client.force_login(self.manager)
        r = self.client.get(reverse("machine_detail", args=[self.machine.pk]))
        self.assertIn("pm_stats", r.context)
        stats = r.context["pm_stats"]
        for key in ("active_count", "due_this_week_count", "overdue_count", "compliance_pct"):
            self.assertIn(key, stats)

    def test_active_count_excludes_inactive(self):
        self.client.force_login(self.manager)
        r = self.client.get(reverse("machine_detail", args=[self.machine.pk]))
        self.assertEqual(r.context["pm_stats"]["active_count"], 3)

    def test_overdue_count(self):
        self.client.force_login(self.manager)
        r = self.client.get(reverse("machine_detail", args=[self.machine.pk]))
        self.assertEqual(r.context["pm_stats"]["overdue_count"], 1)

    def test_due_this_week_count(self):
        self.client.force_login(self.manager)
        r = self.client.get(reverse("machine_detail", args=[self.machine.pk]))
        self.assertEqual(r.context["pm_stats"]["due_this_week_count"], 1)

    def test_compliance_pct_none_when_no_executions(self):
        self.client.force_login(self.manager)
        r = self.client.get(reverse("machine_detail", args=[self.machine.pk]))
        self.assertIsNone(r.context["pm_stats"]["compliance_pct"])


class MachinePMComplianceTests(TestCase):
    def setUp(self):
        self.manager = _make_user("pmcomp_mgr", User.Role.MANAGER)
        self.machine = _make_machine()
        self.template = _make_template()
        now = timezone.now()
        self.schedule = PMSchedule.objects.create(
            template=self.template, machine=self.machine,
            frequency_type="monthly", interval=1,
            start_date=(now - timedelta(days=30)).date(),
            next_due_at=now - timedelta(days=5),
            is_active=True,
        )

    def test_compliance_100_when_approved_on_time(self):
        PMExecution.objects.create(
            pm_schedule=self.schedule,
            scheduled_due_at=self.schedule.next_due_at,
            execution_sequence=1,
            status=PMExecution.Status.APPROVED,
            approved_at=self.schedule.next_due_at,
        )
        self.client.force_login(self.manager)
        r = self.client.get(reverse("machine_detail", args=[self.machine.pk]))
        self.assertEqual(r.context["pm_stats"]["compliance_pct"], 100)

    def test_compliance_lower_with_missed_executions(self):
        PMExecution.objects.create(
            pm_schedule=self.schedule,
            scheduled_due_at=self.schedule.next_due_at,
            execution_sequence=1,
            status=PMExecution.Status.APPROVED,
            approved_at=self.schedule.next_due_at,
        )
        past_due = self.schedule.next_due_at - timedelta(days=30)
        PMExecution.objects.create(
            pm_schedule=self.schedule,
            scheduled_due_at=past_due,
            execution_sequence=1,
            status=PMExecution.Status.MISSED,
        )
        self.client.force_login(self.manager)
        r = self.client.get(reverse("machine_detail", args=[self.machine.pk]))
        self.assertEqual(r.context["pm_stats"]["compliance_pct"], 50)


class MachinePMTableLinkTests(TestCase):
    def setUp(self):
        self.manager = _make_user("pmlink_mgr", User.Role.MANAGER)
        self.machine = _make_machine()
        self.template = _make_template()
        now = timezone.now()
        self.schedule = PMSchedule.objects.create(
            template=self.template, machine=self.machine,
            frequency_type="monthly", interval=1,
            start_date=now.date(), next_due_at=now + timedelta(days=7),
        )

    def test_pm_tab_links_to_template_detail_and_spawn_wo(self):
        self.client.force_login(self.manager)
        r = self.client.get(reverse("machine_detail", args=[self.machine.pk]))
        body = r.content.decode()
        self.assertIn(reverse("pm_template_detail", args=[self.template.pk]), body)
        self.assertIn(reverse("pm_spawn_wo", args=[self.schedule.pk]), body)


class PMSidebarBadgeTests(TestCase):
    def setUp(self):
        self.manager = _make_user("pmbadge_mgr", User.Role.MANAGER)
        self.machine = _make_machine()
        self.template = _make_template()
        self.now = timezone.now()

    def test_nav_pm_overdue_zero_when_no_overdue(self):
        PMSchedule.objects.create(
            template=self.template, machine=self.machine,
            frequency_type="monthly", interval=1,
            start_date=self.now.date(), next_due_at=self.now + timedelta(days=7),
        )
        self.client.force_login(self.manager)
        r = self.client.get(reverse("dashboard"))
        self.assertEqual(r.context["nav_pm_overdue"], 0)

    def test_nav_pm_overdue_counts_overdue(self):
        PMSchedule.objects.create(
            template=self.template, machine=self.machine,
            frequency_type="monthly", interval=1,
            start_date=(self.now - timedelta(days=60)).date(),
            next_due_at=self.now - timedelta(days=10),
        )
        PMSchedule.objects.create(
            template=self.template, machine=self.machine,
            frequency_type="monthly", interval=1,
            start_date=self.now.date(), next_due_at=self.now + timedelta(days=7),
        )
        self.client.force_login(self.manager)
        r = self.client.get(reverse("dashboard"))
        self.assertEqual(r.context["nav_pm_overdue"], 1)

    def test_nav_pm_overdue_excludes_inactive(self):
        PMSchedule.objects.create(
            template=self.template, machine=self.machine,
            frequency_type="monthly", interval=1,
            start_date=(self.now - timedelta(days=60)).date(),
            next_due_at=self.now - timedelta(days=10),
            is_active=False,
        )
        self.client.force_login(self.manager)
        r = self.client.get(reverse("dashboard"))
        self.assertEqual(r.context["nav_pm_overdue"], 0)


class MachinePMTabLastExecutionTests(TestCase):
    def setUp(self):
        self.manager = _make_user("pmlast_mgr", User.Role.MANAGER)
        self.machine = _make_machine()
        self.template = _make_template()
        now = timezone.now()
        self.schedule = PMSchedule.objects.create(
            template=self.template, machine=self.machine,
            frequency_type="monthly", interval=1,
            start_date=now.date(), next_due_at=now + timedelta(days=7),
        )

    def test_pm_last_executions_dict_in_context(self):
        exec_row = PMExecution.objects.create(
            pm_schedule=self.schedule,
            scheduled_due_at=self.schedule.next_due_at - timedelta(days=23),
            execution_sequence=1,
            status=PMExecution.Status.APPROVED,
        )
        self.client.force_login(self.manager)
        r = self.client.get(reverse("machine_detail", args=[self.machine.pk]))
        last_dict = r.context["pm_last_executions"]
        self.assertIn(self.schedule.pk, last_dict)
        self.assertEqual(last_dict[self.schedule.pk].pk, exec_row.pk)

    def test_pm_last_executions_dict_none_for_no_execution(self):
        self.client.force_login(self.manager)
        r = self.client.get(reverse("machine_detail", args=[self.machine.pk]))
        last_dict = r.context["pm_last_executions"]
        self.assertIn(self.schedule.pk, last_dict)
        self.assertIsNone(last_dict[self.schedule.pk])
