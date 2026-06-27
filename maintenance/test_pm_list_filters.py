"""Tests for Phase 6: PM list filters + days-until-due + batch spawn."""
from datetime import timedelta

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import User
from maintenance.models import (
    Machine, PMChecklistItem, PMExecution, PMSchedule, PMTemplate, Site, WorkOrder,
)


def _make_user(username, role):
    return User.objects.create_user(username=username, password="x", role=role, is_active=True)


def _make_machine(qr="PM6-M"):
    site = Site.objects.filter(is_default=True).first() or Site.objects.create(
        name="X", is_default=True, is_active=True
    )
    return Machine.objects.create(
        name=qr, qr_code=qr, asset_level=3, asset_code=qr, is_active=True, site=site,
    )


def _make_template(code="PM6-T"):
    t = PMTemplate.objects.create(
        code=code, title="Test " + code, estimated_duration_minutes=30,
        priority="medium", is_active=True,
    )
    PMChecklistItem.objects.create(template=t, order=1, text="Step", is_required=True)
    return t


def _make_schedule(template, machine, *, due_offset_days, is_active=True):
    now = timezone.now()
    return PMSchedule.objects.create(
        template=template, machine=machine,
        frequency_type="monthly", interval=1,
        start_date=(now + timedelta(days=due_offset_days)).date(),
        next_due_at=now + timedelta(days=due_offset_days),
        grace_days=7,
        is_active=is_active,
    )


class PMListFiltersTests(TestCase):
    def setUp(self):
        self.manager = _make_user("filt_mgr", User.Role.MANAGER)
        self.technician = _make_user("filt_tech", User.Role.TECHNICIAN)
        self.machine = _make_machine()
        self.template = _make_template()
        self.now = timezone.now()
        self.s_overdue = _make_schedule(self.template, self.machine, due_offset_days=-5)
        self.s_soon = _make_schedule(self.template, self.machine, due_offset_days=3)
        self.s_future = _make_schedule(self.template, self.machine, due_offset_days=30)
        self.s_inactive = _make_schedule(self.template, self.machine, due_offset_days=3, is_active=False)

    def test_default_active_filter_excludes_inactive(self):
        self.client.force_login(self.manager)
        r = self.client.get(reverse("pm_list"))
        self.assertEqual(r.context["active_filter"], "active")
        self.assertEqual(len(r.context["schedule_data"]), 3)

    def test_active_all_includes_inactive(self):
        self.client.force_login(self.manager)
        r = self.client.get(reverse("pm_list") + "?active=all")
        self.assertEqual(r.context["active_filter"], "all")
        self.assertEqual(len(r.context["schedule_data"]), 4)

    def test_active_inactive_only(self):
        self.client.force_login(self.manager)
        r = self.client.get(reverse("pm_list") + "?active=inactive")
        self.assertEqual(r.context["active_filter"], "inactive")
        self.assertEqual(len(r.context["schedule_data"]), 1)
        self.assertEqual(r.context["schedule_data"][0]["schedule"].pk, self.s_inactive.pk)

    def test_status_overdue_filter(self):
        self.client.force_login(self.manager)
        r = self.client.get(reverse("pm_list") + "?status=overdue")
        rows = r.context["schedule_data"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["schedule"].pk, self.s_overdue.pk)

    def test_status_due_soon_filter(self):
        self.client.force_login(self.manager)
        r = self.client.get(reverse("pm_list") + "?status=due_soon")
        rows = r.context["schedule_data"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["schedule"].pk, self.s_soon.pk)

    def test_status_future_filter(self):
        self.client.force_login(self.manager)
        r = self.client.get(reverse("pm_list") + "?status=future")
        rows = r.context["schedule_data"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["schedule"].pk, self.s_future.pk)

    def test_machine_filter(self):
        other_machine = _make_machine(qr="PM6-M2")
        self.client.force_login(self.manager)
        r = self.client.get(reverse("pm_list") + f"?machine={other_machine.pk}")
        self.assertEqual(r.context["machine_filter"], str(other_machine.pk))
        self.assertEqual(len(r.context["schedule_data"]), 0)

    def test_invalid_filter_falls_back_to_default(self):
        self.client.force_login(self.manager)
        r = self.client.get(reverse("pm_list") + "?active=bogus&status=invalid")
        self.assertEqual(r.context["active_filter"], "active")
        self.assertEqual(r.context["status_filter"], "all")

    def test_filter_combinations(self):
        self.client.force_login(self.manager)
        r = self.client.get(reverse("pm_list") + "?active=all&status=overdue")
        rows = r.context["schedule_data"]
        self.assertEqual(len(rows), 1)


class PMListDaysUntilDueTests(TestCase):
    def setUp(self):
        self.manager = _make_user("days_mgr", User.Role.MANAGER)
        self.machine = _make_machine()
        self.template = _make_template()

    def test_overdue_has_danger_color(self):
        s = _make_schedule(self.template, self.machine, due_offset_days=-5)
        self.client.force_login(self.manager)
        r = self.client.get(reverse("pm_list"))
        row = next(row for row in r.context["schedule_data"] if row["schedule"].pk == s.pk)
        self.assertEqual(row["days_color"], "danger")
        self.assertEqual(row["days_until_due"], -5)
        self.assertIn("overdue", row["days_label"])

    def test_due_soon_has_warning_color(self):
        s = _make_schedule(self.template, self.machine, due_offset_days=3)
        self.client.force_login(self.manager)
        r = self.client.get(reverse("pm_list"))
        row = next(row for row in r.context["schedule_data"] if row["schedule"].pk == s.pk)
        self.assertEqual(row["days_color"], "warning")
        self.assertEqual(row["days_until_due"], 3)

    def test_future_has_muted_color(self):
        s = _make_schedule(self.template, self.machine, due_offset_days=30)
        self.client.force_login(self.manager)
        r = self.client.get(reverse("pm_list"))
        row = next(row for row in r.context["schedule_data"] if row["schedule"].pk == s.pk)
        self.assertEqual(row["days_color"], "muted")
        self.assertEqual(row["days_until_due"], 30)


class PMBatchSpawnTests(TestCase):
    def setUp(self):
        self.manager = _make_user("batch_mgr", User.Role.MANAGER)
        self.technician = _make_user("batch_tech", User.Role.TECHNICIAN)
        self.machine = _make_machine()
        self.template = _make_template()
        self.s1 = _make_schedule(self.template, self.machine, due_offset_days=-10)
        self.s2 = _make_schedule(self.template, self.machine, due_offset_days=-5)
        self.s3 = _make_schedule(self.template, self.machine, due_offset_days=30)

    def test_batch_spawn_creates_wos_for_selected_overdue(self):
        self.client.force_login(self.manager)
        r = self.client.post(reverse("pm_batch_spawn_wo"), data={
            "schedule_ids": [str(self.s1.pk), str(self.s2.pk)],
        })
        self.assertEqual(r.status_code, 302)
        self.assertEqual(WorkOrder.objects.filter(category="preventive").count(), 2)
        self.assertEqual(PMExecution.objects.filter(status=PMExecution.Status.SUBMITTED).count(), 2)
        self.assertEqual(WorkOrder.objects.filter(notes__contains="batch spawn").count(), 2)

    def test_batch_spawn_empty_selection_redirects(self):
        self.client.force_login(self.manager)
        r = self.client.post(reverse("pm_batch_spawn_wo"), data={"schedule_ids": []})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(WorkOrder.objects.count(), 0)

    def test_batch_spawn_skips_inactive_schedules(self):
        inactive = _make_schedule(self.template, self.machine, due_offset_days=-5, is_active=False)
        self.client.force_login(self.manager)
        r = self.client.post(reverse("pm_batch_spawn_wo"), data={
            "schedule_ids": [str(inactive.pk)],
        })
        self.assertEqual(r.status_code, 302)
        self.assertEqual(WorkOrder.objects.count(), 0)

    def test_batch_spawn_skips_invalid_ids(self):
        self.client.force_login(self.manager)
        r = self.client.post(reverse("pm_batch_spawn_wo"), data={
            "schedule_ids": ["99999", "abc"],
        })
        self.assertEqual(r.status_code, 302)
        self.assertEqual(WorkOrder.objects.count(), 0)

    def test_batch_spawn_get_shows_confirmation_page(self):
        """GET on the batch-spawn URL should now show a confirmation page,
        not return 405. POST still does the actual work."""
        self.client.force_login(self.manager)
        r = self.client.get(
            reverse("pm_batch_spawn_wo"),
            data={"schedule_ids": f"{self.s1.pk},{self.s2.pk}"},
        )
        self.assertEqual(r.status_code, 200)
        self.assertIn("schedules", r.context)
        self.assertEqual(len(r.context["schedules"]), 2)
        body = r.content.decode()
        self.assertIn("Confirm", body)

    def test_batch_spawn_get_no_ids_shows_empty(self):
        """GET with no schedule_ids shows a 'no valid schedules' empty state."""
        self.client.force_login(self.manager)
        r = self.client.get(reverse("pm_batch_spawn_wo"))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.context["schedules"]), 0)
        self.assertIn("No valid schedules", r.content.decode())

    def test_batch_spawn_skips_already_pending_schedules(self):
        """If a schedule already has a SUBMITTED PMExecution at next_due_at,
        a second batch spawn should skip it (no duplicate WOs)."""
        # First spawn creates the WO + PMExecution
        self.client.force_login(self.manager)
        self.client.post(reverse("pm_batch_spawn_wo"), data={
            "schedule_ids": [str(self.s1.pk), str(self.s2.pk)],
        })
        self.assertEqual(WorkOrder.objects.filter(category="preventive").count(), 2)

        # Second batch for the same schedules should be deduped
        r = self.client.post(reverse("pm_batch_spawn_wo"), data={
            "schedule_ids": [str(self.s1.pk), str(self.s2.pk)],
        })
        # No new WOs created
        self.assertEqual(WorkOrder.objects.filter(category="preventive").count(), 2)

    def test_batch_spawn_forbidden_for_technician(self):
        self.client.force_login(self.technician)
        r = self.client.post(reverse("pm_batch_spawn_wo"), data={
            "schedule_ids": [str(self.s1.pk)],
        })
        self.assertIn(r.status_code, (302, 403))

    def test_batch_spawn_includes_snapshot(self):
        self.client.force_login(self.manager)
        self.client.post(reverse("pm_batch_spawn_wo"), data={
            "schedule_ids": [str(self.s1.pk)],
        })
        exec_row = PMExecution.objects.get()
        self.assertEqual(exec_row.template_snapshot_json["template_code"], self.template.code)
        self.assertEqual(exec_row.status, PMExecution.Status.SUBMITTED)