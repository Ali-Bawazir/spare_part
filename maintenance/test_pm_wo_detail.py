"""Tests for the dedicated PM Work Order page (/pm/wo/<pk>/)
and the inline PM checklist on the WO detail page (/work-orders/<pk>/)."""
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


class PMInlineChecklistOnWODetailTests(TestCase):
    """The PM inspection checklist is rendered inline on the WO detail
    page so the technician doesn't have to navigate to /pm/wo/<pk>/."""

    @classmethod
    def setUpTestData(cls):
        cls.manager = User.objects.create_user(
            username="manager_inline", password="x", role=User.Role.MANAGER,
        )
        cls.technician = User.objects.create_user(
            username="tech_inline", password="x", role=User.Role.TECHNICIAN,
        )
        cls.site = Site.objects.filter(is_default=True).first() or Site.objects.create(
            name="X", is_default=True, is_active=True,
        )
        cls.machine = Machine.objects.create(
            name="Inline Test Machine", qr_code="M-INL-001",
            asset_code="M-INL-001", asset_level=3,
            is_active=True, site=cls.site,
        )

    def _make_pm_wo(self, lifecycle=WorkOrder.LifecycleStatus.IN_PROGRESS):
        template = PMTemplate.objects.create(
            code="PM-INLINE-001", title="Inline PM",
            estimated_duration_minutes=30,
            priority=PMTemplate.Priority.MEDIUM,
            requires_manager_review=True, is_active=True,
        )
        PMChecklistItem.objects.create(template=template, order=1, text="Step A")
        PMChecklistItem.objects.create(template=template, order=2, text="Step B")
        schedule = PMSchedule.objects.create(
            template=template, machine=self.machine,
            frequency_type=PMSchedule.FrequencyType.MONTHLY, interval=1,
            start_date=timezone.now().date(), next_due_at=timezone.now(),
            is_active=True, created_by=self.manager,
        )
        wo = WorkOrder.objects.create(
            machine=self.machine,
            category=WorkOrder.Category.PREVENTIVE,
            created_by=self.manager,
            assigned_technician=self.technician,
            lifecycle_status=lifecycle,
        )
        return template, schedule, wo

    def test_wo_detail_renders_inline_checklist_for_in_progress_pm_wo(self):
        _, _, wo = self._make_pm_wo()
        self.client.force_login(self.technician)
        r = self.client.get(reverse("work_order_detail", args=[wo.pk]))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "PM Inspection Checklist (2 items)")
        self.assertContains(r, 'name="checklist_0"')
        self.assertContains(r, 'name="checklist_1"')
        self.assertContains(r, 'name="note_0"')
        self.assertContains(r, 'name="note_1"')

    def test_wo_detail_does_not_render_checklist_when_assigned_before_start(self):
        # Lifecycle=assigned: technician must click "Start work" first.
        # The submit/checklist panel only shows in_progress (matches the
        # `work_order_submit` view's lifecycle precondition).
        _, _, wo = self._make_pm_wo(lifecycle=WorkOrder.LifecycleStatus.ASSIGNED)
        self.client.force_login(self.technician)
        r = self.client.get(reverse("work_order_detail", args=[wo.pk]))
        self.assertEqual(r.status_code, 200)
        # Start work button is shown, but no inline checklist yet.
        self.assertContains(r, "Start work")
        self.assertNotContains(r, "PM Inspection Checklist")

    def test_wo_detail_does_not_render_checklist_for_non_pm_wo(self):
        wo = WorkOrder.objects.create(
            machine=self.machine,
            category=WorkOrder.Category.BREAKDOWN,
            created_by=self.manager,
            assigned_technician=self.technician,
            lifecycle_status=WorkOrder.LifecycleStatus.IN_PROGRESS,
        )
        self.client.force_login(self.technician)
        r = self.client.get(reverse("work_order_detail", args=[wo.pk]))
        self.assertEqual(r.status_code, 200)
        self.assertNotContains(r, "PM Inspection Checklist")
        self.assertNotContains(r, 'name="checklist_0"')

    def test_wo_detail_does_not_render_checklist_when_no_template_exists(self):
        # No PM template, no schedule, no checklist → the panel hides.
        wo = WorkOrder.objects.create(
            machine=self.machine,
            category=WorkOrder.Category.PREVENTIVE,
            created_by=self.manager,
            assigned_technician=self.technician,
            lifecycle_status=WorkOrder.LifecycleStatus.IN_PROGRESS,
        )
        self.client.force_login(self.technician)
        r = self.client.get(reverse("work_order_detail", args=[wo.pk]))
        self.assertEqual(r.status_code, 200)
        self.assertNotContains(r, "PM Inspection Checklist")

    def test_inline_submit_stores_structured_action_taken(self):
        _, _, wo = self._make_pm_wo()
        self.client.force_login(self.technician)
        r = self.client.post(
            reverse("work_order_submit", args=[wo.pk]),
            data={
                "checklist_0": "on",
                "note_0": "all good",
                "checklist_1": "",
                "note_1": "needs followup",
                "root_cause": "rc",
                "action_taken": "user-typed action_taken",
                "notes": "user notes",
            },
        )
        self.assertEqual(r.status_code, 302)
        wo.refresh_from_db()
        # Structured format expected by pm_review view.
        self.assertIn("[✓] Step A", wo.action_taken)
        self.assertIn("[✗] Step B", wo.action_taken)
        self.assertIn("all good", wo.action_taken)
        self.assertIn("needs followup", wo.action_taken)
        self.assertEqual(wo.lifecycle_status, WorkOrder.LifecycleStatus.PENDING_REVIEW)

    def test_submit_without_checklist_keeps_action_taken_for_non_pm(self):
        wo = WorkOrder.objects.create(
            machine=self.machine,
            category=WorkOrder.Category.BREAKDOWN,
            created_by=self.manager,
            assigned_technician=self.technician,
            lifecycle_status=WorkOrder.LifecycleStatus.IN_PROGRESS,
        )
        self.client.force_login(self.technician)
        r = self.client.post(
            reverse("work_order_submit", args=[wo.pk]),
            data={
                "root_cause": "broken",
                "action_taken": "replaced",
                "notes": "ok",
            },
        )
        self.assertEqual(r.status_code, 302)
        wo.refresh_from_db()
        self.assertEqual(wo.action_taken, "replaced")
        self.assertEqual(wo.lifecycle_status, WorkOrder.LifecycleStatus.PENDING_REVIEW)

    def test_inline_submit_with_checklist_does_not_break_when_pm_execution_missing(self):
        # Legacy WO without PMExecution: submit should still store action_taken.
        _, _, wo = self._make_pm_wo()
        wo.pm_execution.delete() if hasattr(wo, "pm_execution") else None
        self.client.force_login(self.technician)
        r = self.client.post(
            reverse("work_order_submit", args=[wo.pk]),
            data={
                "checklist_0": "on",
                "note_0": "x",
                "checklist_1": "on",
                "note_1": "y",
                "root_cause": "rc",
                "action_taken": "x",
            },
        )
        self.assertEqual(r.status_code, 302)
        wo.refresh_from_db()
        self.assertIn("[✓] Step A", wo.action_taken)
        self.assertIn("[✓] Step B", wo.action_taken)
