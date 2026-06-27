"""Tests for the dedicated PM Work Order page (/pm/wo/<pk>/)."""
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import User
from maintenance.models import (
    Machine,
    PMChecklistItem,
    PMExecution,
    PMTemplate,
    PMSchedule,
    Site,
    WorkOrder,
)


def _make_pm_wo(technician, manager, machine, component=None):
    template = PMTemplate.objects.create(
        code="PM-TEST-001",
        title="Test PM",
        description="Test PM template",
        estimated_duration_minutes=30,
        priority=PMTemplate.Priority.MEDIUM,
        requires_manager_review=True,
        is_active=True,
    )
    PMChecklistItem.objects.create(template=template, order=1, text="Step 1")
    PMChecklistItem.objects.create(template=template, order=2, text="Step 2")

    schedule = PMSchedule.objects.create(
        template=template,
        machine=machine,
        component=component,
        frequency_type=PMSchedule.FrequencyType.MONTHLY,
        interval=1,
        start_date=timezone.now().date(),
        next_due_at=timezone.now(),
        is_active=True,
        created_by=manager,
    )

    wo = WorkOrder.objects.create(
        machine=machine,
        component=component,
        category=WorkOrder.Category.PREVENTIVE,
        created_by=manager,
        assigned_technician=technician,
        lifecycle_status=WorkOrder.LifecycleStatus.ASSIGNED,
    )
    PMExecution.objects.create(
        pm_schedule=schedule,
        work_order=wo,
        scheduled_due_at=schedule.next_due_at,
        execution_sequence=1,
        status=PMExecution.Status.SUBMITTED,
        template_snapshot_json={
            "code": template.code,
            "title": template.title,
            "checklist": [{"text": "Step 1"}, {"text": "Step 2"}],
        },
    )
    return template, schedule, wo


class PMWorkOrderPageTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.manager = User.objects.create_user(
            username="manager_pmwo", password="x", role=User.Role.MANAGER,
        )
        cls.technician = User.objects.create_user(
            username="tech_pmwo", password="x", role=User.Role.TECHNICIAN,
        )
        cls.site = Site.objects.filter(is_default=True).first() or Site.objects.create(
            name="X", is_default=True, is_active=True,
        )
        cls.machine = Machine.objects.create(
            name="PM WO Test Machine", qr_code="M-PMWO-001",
            asset_code="M-PMWO-001", asset_level=3,
            is_active=True, site=cls.site,
        )

    def test_technician_can_open_pm_wo_page(self):
        _, _, wo = _make_pm_wo(self.technician, self.manager, self.machine)
        self.client.force_login(self.technician)
        r = self.client.get(reverse("pm_wo_detail", args=[wo.pk]))
        self.assertEqual(r.status_code, 200)
        self.assertTemplateUsed(r, "maintenance/pm_wo_detail.html")
        self.assertIn("checklist_items", r.context)
        self.assertEqual(len(r.context["checklist_items"]), 2)
        self.assertTrue(r.context["can_execute"])

    def test_page_shows_pm_context_with_schedule(self):
        _, sched, wo = _make_pm_wo(self.technician, self.manager, self.machine)
        self.client.force_login(self.technician)
        r = self.client.get(reverse("pm_wo_detail", args=[wo.pk]))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.context["sched"].pk, sched.pk)
        self.assertEqual(r.context["pm_execution"].pm_schedule_id, sched.pk)

    def test_page_uses_template_snapshot_for_checklist(self):
        _, sched, wo = _make_pm_wo(self.technician, self.manager, self.machine)
        self.client.force_login(self.technician)
        r = self.client.get(reverse("pm_wo_detail", args=[wo.pk]))
        texts = [item["text"] for item in r.context["checklist_items"]]
        self.assertIn("Step 1", texts)
        self.assertIn("Step 2", texts)

    def test_non_preventive_wo_redirects(self):
        wo = WorkOrder.objects.create(
            machine=self.machine,
            category=WorkOrder.Category.BREAKDOWN,
            created_by=self.manager,
            assigned_technician=self.technician,
        )
        self.client.force_login(self.technician)
        r = self.client.get(reverse("pm_wo_detail", args=[wo.pk]))
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r["Location"], reverse("work_order_detail", args=[wo.pk]))

    def test_closed_wo_is_read_only(self):
        _, _, wo = _make_pm_wo(self.technician, self.manager, self.machine)
        wo.lifecycle_status = WorkOrder.LifecycleStatus.CLOSED
        wo.save()
        self.client.force_login(self.technician)
        r = self.client.get(reverse("pm_wo_detail", args=[wo.pk]))
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.context["can_execute"])

    def test_manager_can_open_pm_wo_page(self):
        _, _, wo = _make_pm_wo(self.technician, self.manager, self.machine)
        self.client.force_login(self.manager)
        r = self.client.get(reverse("pm_wo_detail", args=[wo.pk]))
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.context["can_execute"])

    def test_wo_detail_page_shows_execute_pm_button_for_technician(self):
        _, _, wo = _make_pm_wo(self.technician, self.manager, self.machine)
        self.client.force_login(self.technician)
        r = self.client.get(reverse("work_order_detail", args=[wo.pk]))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Execute PM")
        self.assertContains(r, reverse("pm_wo_detail", args=[wo.pk]))

    def test_wo_detail_page_shows_view_pm_button_for_manager_on_pending_review(self):
        _, _, wo = _make_pm_wo(self.technician, self.manager, self.machine)
        wo.lifecycle_status = WorkOrder.LifecycleStatus.PENDING_REVIEW
        wo.save()
        self.client.force_login(self.manager)
        r = self.client.get(reverse("work_order_detail", args=[wo.pk]))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Review PM")
        self.assertContains(r, reverse("pm_wo_detail", args=[wo.pk]))

    def test_legacy_pm_execute_url_redirects(self):
        _, _, wo = _make_pm_wo(self.technician, self.manager, self.machine)
        self.client.force_login(self.technician)
        r = self.client.get(reverse("pm_execute", args=[wo.pk]))
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r["Location"], reverse("pm_wo_detail", args=[wo.pk]))


class PMWorkOrderSubmitTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.manager = User.objects.create_user(
            username="manager_submit", password="x", role=User.Role.MANAGER,
        )
        cls.technician = User.objects.create_user(
            username="tech_submit", password="x", role=User.Role.TECHNICIAN,
        )
        cls.site = Site.objects.filter(is_default=True).first() or Site.objects.create(
            name="X", is_default=True, is_active=True,
        )
        cls.machine = Machine.objects.create(
            name="Submit Test Machine", qr_code="M-SUB-001",
            asset_code="M-SUB-001", asset_level=3,
            is_active=True, site=cls.site,
        )

    def test_submit_moves_wo_to_pending_review(self):
        _, _, wo = _make_pm_wo(self.technician, self.manager, self.machine)
        self.client.force_login(self.technician)
        r = self.client.post(
            reverse("pm_wo_detail", args=[wo.pk]),
            data={
                "checklist_0": "on",
                "note_0": "All good",
                "checklist_1": "on",
                "note_1": "",
                "root_cause": "test",
                "action_taken": "Done",
            },
        )
        self.assertEqual(r.status_code, 302)
        wo.refresh_from_db()
        self.assertEqual(wo.lifecycle_status, WorkOrder.LifecycleStatus.PENDING_REVIEW)

    def test_submit_stores_checklist_results_in_action_taken(self):
        _, _, wo = _make_pm_wo(self.technician, self.manager, self.machine)
        self.client.force_login(self.technician)
        self.client.post(
            reverse("pm_wo_detail", args=[wo.pk]),
            data={
                "checklist_0": "on",
                "note_0": "ok",
                "checklist_1": "",
                "note_1": "needs work",
                "root_cause": "test",
                "action_taken": "x",
            },
        )
        wo.refresh_from_db()
        self.assertIn("Step 1", wo.action_taken)
        self.assertIn("Step 2", wo.action_taken)
        self.assertIn("ok", wo.action_taken)
        self.assertIn("needs work", wo.action_taken)
