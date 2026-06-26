"""Tests for Phase 6.1: pm_spawn_wo cleanup (no form on GET, dedupe pending executions)."""
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


def _make_machine(qr="PMS-M"):
    site = Site.objects.filter(is_default=True).first() or Site.objects.create(
        name="X", is_default=True, is_active=True
    )
    return Machine.objects.create(
        name=qr, qr_code=qr, asset_level=3, asset_code=qr, is_active=True, site=site,
    )


def _make_template(code="PMS-T"):
    t = PMTemplate.objects.create(
        code=code, title="Test " + code, estimated_duration_minutes=30,
        priority="medium", is_active=True,
    )
    PMChecklistItem.objects.create(template=t, order=1, text="Step", is_required=True)
    return t


class PMSpawnWOCleanupTests(TestCase):
    def setUp(self):
        self.manager = _make_user("pmsp_mgr", User.Role.MANAGER)
        self.machine = _make_machine()
        self.template = _make_template()
        self.now = timezone.now()
        self.schedule = PMSchedule.objects.create(
            template=self.template, machine=self.machine,
            frequency_type="monthly", interval=1,
            start_date=self.now.date(),
            next_due_at=self.now + timedelta(days=7),
            is_active=True,
        )

    def test_get_shows_clean_form_no_full_schedule_form(self):
        """GET should not render a PMScheduleForm — only a confirm spawn form."""
        self.client.force_login(self.manager)
        r = self.client.get(reverse("pm_spawn_wo", args=[self.schedule.pk]))
        self.assertEqual(r.status_code, 200)
        body = r.content.decode()
        self.assertIn("Schedule", body)
        self.assertIn("Create PM Work Order", body)
        self.assertNotIn("id_template", body)
        self.assertNotIn("id_machine", body)
        self.assertNotIn("frequency_type", body)

    def test_get_redirects_when_inactive(self):
        self.schedule.is_active = False
        self.schedule.save(update_fields=["is_active"])
        self.client.force_login(self.manager)
        r = self.client.get(reverse("pm_spawn_wo", args=[self.schedule.pk]))
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r.url, reverse("pm_list"))

    def test_post_spawns_wo(self):
        self.client.force_login(self.manager)
        r = self.client.post(reverse("pm_spawn_wo", args=[self.schedule.pk]))
        self.assertEqual(r.status_code, 302)
        wo = WorkOrder.objects.filter(category="preventive", machine=self.machine).first()
        self.assertIsNotNone(wo)
        self.assertEqual(wo.lifecycle_status, "assigned")
        exec_row = PMExecution.objects.get(work_order=wo)
        self.assertEqual(exec_row.status, "submitted")
        self.assertEqual(exec_row.scheduled_due_at, self.schedule.next_due_at)
        self.assertEqual(exec_row.execution_sequence, 1)

    def test_post_redirects_when_pending_execution_exists(self):
        """If a SUBMITTED/REJECTED execution already exists for the current due_at,
        redirect to that WO instead of creating a duplicate."""
        wo = WorkOrder.objects.create(
            machine=self.machine, category="preventive",
            lifecycle_status="assigned", created_by=self.manager,
        )
        PMExecution.objects.create(
            pm_schedule=self.schedule,
            work_order=wo,
            scheduled_due_at=self.schedule.next_due_at,
            execution_sequence=1,
            status="submitted",
        )
        self.client.force_login(self.manager)
        r = self.client.post(reverse("pm_spawn_wo", args=[self.schedule.pk]))
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r.url, reverse("work_order_detail", args=[wo.pk]))
        self.assertEqual(WorkOrder.objects.filter(category="preventive").count(), 1)

    def test_propagate_to_children_creates_one_wo_per_child(self):
        child1 = Machine.objects.create(
            name="C1", qr_code="PMS-C1", asset_level=3, asset_code="C1",
            is_active=True, site=self.machine.site, parent=self.machine,
        )
        child2 = Machine.objects.create(
            name="C2", qr_code="PMS-C2", asset_level=3, asset_code="C2",
            is_active=True, site=self.machine.site, parent=self.machine,
        )
        self.client.force_login(self.manager)
        r = self.client.post(
            reverse("pm_spawn_wo", args=[self.schedule.pk]),
            data={"propagate_to_children": "on"},
        )
        self.assertEqual(r.status_code, 302)
        self.assertEqual(WorkOrder.objects.filter(category="preventive").count(), 3)
        self.assertEqual(WorkOrder.objects.filter(machine=child1).count(), 1)
        self.assertEqual(WorkOrder.objects.filter(machine=child2).count(), 1)

    def test_inactive_child_not_propagated(self):
        active_child = Machine.objects.create(
            name="AC1", qr_code="PMS-AC1", asset_level=3, asset_code="AC1",
            is_active=True, site=self.machine.site, parent=self.machine,
        )
        inactive_child = Machine.objects.create(
            name="IC1", qr_code="PMS-IC1", asset_level=3, asset_code="IC1",
            is_active=False, site=self.machine.site, parent=self.machine,
        )
        self.client.force_login(self.manager)
        r = self.client.post(
            reverse("pm_spawn_wo", args=[self.schedule.pk]),
            data={"propagate_to_children": "on"},
        )
        self.assertEqual(r.status_code, 302)
        self.assertEqual(WorkOrder.objects.filter(machine=active_child).count(), 1)
        self.assertEqual(WorkOrder.objects.filter(machine=inactive_child).count(), 0)