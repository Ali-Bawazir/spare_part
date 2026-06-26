"""Tests for Phase 5: PM compliance dashboard + compute_compliance helper."""
from datetime import timedelta

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import User
from maintenance.models import (
    Machine, PMChecklistItem, PMExecution, PMSchedule, PMTemplate, Site,
)
from maintenance.services import compute_compliance


def _make_user(username, role):
    return User.objects.create_user(username=username, password="x", role=role, is_active=True)


def _make_machine(qr="PM5-M"):
    site = Site.objects.filter(is_default=True).first() or Site.objects.create(
        name="X", is_default=True, is_active=True
    )
    return Machine.objects.create(
        name=qr, qr_code=qr, asset_level=3, asset_code=qr, is_active=True, site=site,
    )


def _make_template(code="PM5-T"):
    t = PMTemplate.objects.create(
        code=code, title="Test " + code, estimated_duration_minutes=30,
        priority="medium", is_active=True,
    )
    PMChecklistItem.objects.create(template=t, order=1, text="Step", is_required=True)
    return t


def _make_exec(template, machine, manager, *, due_offset_days, status, approved_offset_days=None):
    now = timezone.now()
    schedule = PMSchedule.objects.create(
        template=template, machine=machine,
        frequency_type="monthly", interval=1,
        start_date=(now + timedelta(days=due_offset_days)).date(),
        next_due_at=now + timedelta(days=due_offset_days),
        grace_days=7,
        is_active=True,
    )
    exec_kwargs = dict(
        pm_schedule=schedule,
        scheduled_due_at=now + timedelta(days=due_offset_days),
        execution_sequence=1,
        status=status,
    )
    if approved_offset_days is not None:
        exec_kwargs["approved_at"] = now + timedelta(days=due_offset_days + approved_offset_days)
        exec_kwargs["approved_by"] = manager
    return PMExecution.objects.create(**exec_kwargs)


class ComputeComplianceTests(TestCase):
    def setUp(self):
        self.manager = _make_user("comp_mgr", User.Role.MANAGER)
        self.machine = _make_machine()
        self.template = _make_template()

    def test_returns_none_pct_when_no_executions(self):
        result = compute_compliance()
        self.assertIsNone(result["pct"])
        self.assertEqual(result["scheduled"], 0)
        self.assertEqual(result["on_time"], 0)
        self.assertEqual(result["missed"], 0)

    def test_compliance_100_when_all_approved_on_time(self):
        _make_exec(self.template, self.machine, self.manager,
                   due_offset_days=-30, status=PMExecution.Status.APPROVED,
                   approved_offset_days=0)
        result = compute_compliance()
        self.assertEqual(result["scheduled"], 1)
        self.assertEqual(result["on_time"], 1)
        self.assertEqual(result["approved_total"], 1)
        self.assertEqual(result["pct"], 100)

    def test_within_grace_period_counts_on_time(self):
        _make_exec(self.template, self.machine, self.manager,
                   due_offset_days=-30, status=PMExecution.Status.APPROVED,
                   approved_offset_days=5)
        result = compute_compliance(grace_days=7)
        self.assertEqual(result["on_time"], 1)
        self.assertEqual(result["pct"], 100)

    def test_approved_after_grace_not_on_time(self):
        _make_exec(self.template, self.machine, self.manager,
                   due_offset_days=-30, status=PMExecution.Status.APPROVED,
                   approved_offset_days=14)
        result = compute_compliance(grace_days=7)
        self.assertEqual(result["on_time"], 0)
        self.assertEqual(result["approved_total"], 1)
        self.assertEqual(result["pct"], 0)

    def test_missed_counts_against(self):
        _make_exec(self.template, self.machine, self.manager,
                   due_offset_days=-30, status=PMExecution.Status.MISSED)
        _make_exec(self.template, self.machine, self.manager,
                   due_offset_days=-25, status=PMExecution.Status.APPROVED,
                   approved_offset_days=0)
        result = compute_compliance()
        self.assertEqual(result["scheduled"], 2)
        self.assertEqual(result["on_time"], 1)
        self.assertEqual(result["missed"], 1)
        self.assertEqual(result["pct"], 50)

    def test_excludes_executions_outside_window(self):
        _make_exec(self.template, self.machine, self.manager,
                   due_offset_days=-100, status=PMExecution.Status.APPROVED,
                   approved_offset_days=0)
        result = compute_compliance(window_days=90)
        self.assertEqual(result["scheduled"], 0)

    def test_machine_filter(self):
        other_machine = _make_machine(qr="PM5-M2")
        _make_exec(self.template, self.machine, self.manager,
                   due_offset_days=-30, status=PMExecution.Status.APPROVED,
                   approved_offset_days=0)
        _make_exec(self.template, other_machine, self.manager,
                   due_offset_days=-30, status=PMExecution.Status.MISSED)
        result = compute_compliance(machine=self.machine)
        self.assertEqual(result["scheduled"], 1)
        self.assertEqual(result["on_time"], 1)
        self.assertEqual(result["pct"], 100)

    def test_custom_grace_days(self):
        _make_exec(self.template, self.machine, self.manager,
                   due_offset_days=-30, status=PMExecution.Status.APPROVED,
                   approved_offset_days=4)
        result_strict = compute_compliance(grace_days=3)
        self.assertEqual(result_strict["on_time"], 0)
        result_loose = compute_compliance(grace_days=7)
        self.assertEqual(result_loose["on_time"], 1)

    def test_pending_executions_counted_separately(self):
        _make_exec(self.template, self.machine, self.manager,
                   due_offset_days=-30, status=PMExecution.Status.SUBMITTED)
        _make_exec(self.template, self.machine, self.manager,
                   due_offset_days=-25, status=PMExecution.Status.REJECTED)
        result = compute_compliance()
        self.assertEqual(result["scheduled"], 2)
        self.assertEqual(result["pending"], 2)
        self.assertEqual(result["on_time"], 0)
        self.assertEqual(result["pct"], 0)


class PMDashboardViewTests(TestCase):
    def setUp(self):
        self.manager = _make_user("dash_mgr", User.Role.MANAGER)
        self.technician = _make_user("dash_tech", User.Role.TECHNICIAN)
        self.machine = _make_machine()
        self.template = _make_template()

    def test_dashboard_renders_for_manager(self):
        self.client.force_login(self.manager)
        r = self.client.get(reverse("pm_dashboard"))
        self.assertEqual(r.status_code, 200)
        body = r.content.decode()
        self.assertIn("Compliance Dashboard", body)
        self.assertIn("Per-Machine", body)

    def test_dashboard_forbidden_for_technician(self):
        self.client.force_login(self.technician)
        r = self.client.get(reverse("pm_dashboard"))
        self.assertIn(r.status_code, (302, 403))

    def test_dashboard_shows_hero_stats_keys(self):
        self.client.force_login(self.manager)
        r = self.client.get(reverse("pm_dashboard"))
        self.assertIn("hero_stats", r.context)
        stats = r.context["hero_stats"]
        for key in ("total_pms", "overdue_pms", "due_this_week", "compliance_pct"):
            self.assertIn(key, stats)

    def test_dashboard_shows_compliance_breakdown_keys(self):
        self.client.force_login(self.manager)
        r = self.client.get(reverse("pm_dashboard"))
        self.assertIn("compliance", r.context)
        comp = r.context["compliance"]
        for key in ("scheduled", "on_time", "missed", "pending", "pct", "window_days", "grace_days"):
            self.assertIn(key, comp)

    def test_dashboard_default_window_90_grace_7(self):
        self.client.force_login(self.manager)
        r = self.client.get(reverse("pm_dashboard"))
        comp = r.context["compliance"]
        self.assertEqual(comp["window_days"], 90)
        self.assertEqual(comp["grace_days"], 7)

    def test_dashboard_per_machine_list_populated_when_pms_exist(self):
        now = timezone.now()
        PMSchedule.objects.create(
            template=self.template, machine=self.machine,
            frequency_type="monthly", interval=1,
            start_date=now.date(),
            next_due_at=now + timedelta(days=7),
            grace_days=7,
            is_active=True,
        )
        self.client.force_login(self.manager)
        r = self.client.get(reverse("pm_dashboard"))
        rows = list(r.context["per_machine"])
        self.assertGreaterEqual(len(rows), 1)
        first = rows[0]
        self.assertIn("machine", first)
        self.assertIn("total_pms", first)
        self.assertIn("overdue_pms", first)
        self.assertIn("compliance", first)

    def test_dashboard_overdue_counted_correctly(self):
        now = timezone.now()
        PMSchedule.objects.create(
            template=self.template, machine=self.machine,
            frequency_type="monthly", interval=1,
            start_date=now.date(),
            next_due_at=now + timedelta(days=7),
            grace_days=7,
            is_active=True,
        )
        PMSchedule.objects.create(
            template=self.template, machine=self.machine,
            frequency_type="monthly", interval=1,
            start_date=(now - timedelta(days=30)).date(),
            next_due_at=now - timedelta(days=5),
            grace_days=7,
            is_active=True,
        )
        self.client.force_login(self.manager)
        r = self.client.get(reverse("pm_dashboard"))
        self.assertEqual(r.context["hero_stats"]["total_pms"], 2)
        self.assertEqual(r.context["hero_stats"]["overdue_pms"], 1)
        self.assertEqual(r.context["hero_stats"]["due_this_week"], 1)
