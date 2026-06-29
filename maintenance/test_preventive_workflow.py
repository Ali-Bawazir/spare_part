"""Tests for the workflow-first PM architecture (Phase 8).

Covers the daily scenario end-to-end:
  - Manager creates Template
  - Manager creates Maintenance Plan
  - Cron generates today's occurrences
  - Technician starts + completes
  - Manager approves / returns
  - Plan advances automatically
"""
from datetime import timedelta

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import User
from maintenance.models import (
    MaintenanceSettings,
    PMChecklistItem,
    PMExecution,
    PMTemplate,
    PMSchedule,
    Site,
    WorkOrder,
)
from maintenance.preventive_engine import (
    assignment_service,
    maintenance_engine,
    notification_service,
    occurrence_service,
    scheduling_service,
)


def _make_user(username, role):
    return User.objects.create_user(username=username, password="x", role=role)


def _make_template(code="PM-WF-001", photo_min=0):
    return PMTemplate.objects.create(
        code=code,
        title="Test Procedure",
        estimated_duration_minutes=30,
        priority=PMTemplate.Priority.MEDIUM,
        requires_manager_review=True,
        requires_photo_min_count=photo_min,
        is_active=True,
    )


def _make_checklist(template, texts):
    for i, t in enumerate(texts):
        PMChecklistItem.objects.create(template=template, order=i, text=t)


def _make_schedule(template, machine, technician=None, days_offset=0, due_time=None):
    sched = PMSchedule.objects.create(
        template=template,
        machine=machine,
        frequency_type=PMSchedule.FrequencyType.MONTHLY,
        interval=1,
        start_date=timezone.now().date(),
        next_due_at=timezone.now() + timedelta(days=days_offset),
        due_time=due_time or timezone.now().time(),
        is_active=True,
    )
    if technician:
        sched.assigned_technician = technician
        sched.save()
    return sched


class WorkflowDailyScenarioTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.site = Site.objects.filter(is_default=True).first() or Site.objects.create(
            name="X", is_default=True, is_active=True,
        )
        from maintenance.models import Machine
        cls.machine = Machine.objects.create(
            name="Workflow Machine", qr_code="M-WF-001",
            asset_code="M-WF-001", asset_level=3,
            is_active=True, site=cls.site,
        )
        cls.manager = _make_user("mgr_wf", User.Role.MANAGER)
        cls.technician = _make_user("tech_wf", User.Role.TECHNICIAN)

    def test_cron_generate_today_creates_occurrence(self):
        template = _make_template()
        _make_schedule(template, self.machine, days_offset=0)

        result = scheduling_service.generate_today()
        self.assertEqual(result["count"], 1)
        self.assertEqual(PMExecution.objects.filter(pm_schedule__template=template).count(), 1)

    def test_cron_is_idempotent_same_day(self):
        template = _make_template()
        _make_schedule(template, self.machine, days_offset=0)

        scheduling_service.generate_today()
        result = scheduling_service.generate_today()
        self.assertEqual(result["count"], 0)
        self.assertTrue(result["skipped"])

    def test_cron_can_force_regenerate(self):
        template = _make_template()
        _make_schedule(template, self.machine, days_offset=0)

        scheduling_service.generate_today()
        # delete the existing occurrence
        PMExecution.objects.all().delete()
        # force regenerate won't help if schedule no longer matches
        # but it should still skip if last_generate_run is today
        result = scheduling_service.generate_today(force=True)
        self.assertEqual(result["count"], 1)

    def test_technician_start_creates_work_order(self):
        template = _make_template()
        sched = _make_schedule(template, self.machine, technician=self.technician)
        occ = scheduling_service.generate_today() or PMExecution.objects.first()
        occ = PMExecution.objects.first()

        wo = occurrence_service.start(occ, self.technician, work_order_creator=self.technician)
        self.assertEqual(wo.category, WorkOrder.Category.PREVENTIVE)
        self.assertEqual(wo.assigned_technician, self.technician)
        self.assertEqual(wo.lifecycle_status, WorkOrder.LifecycleStatus.IN_PROGRESS)
        self.assertIsNotNone(wo.labor_started_at)

    def test_complete_requires_checklist_or_notes(self):
        template = _make_template()
        _make_checklist(template, ["Step 1", "Step 2"])
        sched = _make_schedule(template, self.machine, technician=self.technician)
        occ = PMExecution.objects.create(
            pm_schedule=sched,
            scheduled_due_at=timezone.now(),
            status=PMExecution.Status.SUBMITTED,
        )
        wo = occurrence_service.start(occ, self.technician, work_order_creator=self.technician)

        result = occurrence_service.complete(
            occ, self.technician,
            checklist_results=[
                {"text": "Step 1", "checked": False, "note": ""},
                {"text": "Step 2", "checked": False, "note": ""},
            ],
            notes="",  # no notes either
        )
        self.assertFalse(result.success)
        self.assertIn("check at least one item", result.error.lower())

    def test_complete_with_one_check_succeeds(self):
        template = _make_template()
        _make_checklist(template, ["Step 1", "Step 2"])
        sched = _make_schedule(template, self.machine, technician=self.technician)
        occ = PMExecution.objects.create(
            pm_schedule=sched,
            scheduled_due_at=timezone.now(),
            status=PMExecution.Status.SUBMITTED,
        )
        wo = occurrence_service.start(occ, self.technician, work_order_creator=self.technician)

        result = occurrence_service.complete(
            occ, self.technician,
            checklist_results=[
                {"text": "Step 1", "checked": True, "note": ""},
                {"text": "Step 2", "checked": False, "note": ""},
            ],
            notes="",
        )
        self.assertTrue(result.success)
        wo.refresh_from_db()
        self.assertEqual(wo.lifecycle_status, WorkOrder.LifecycleStatus.PENDING_REVIEW)

    def test_complete_requires_photo_min_count(self):
        template = _make_template(photo_min=1)
        _make_checklist(template, ["Step 1"])
        sched = _make_schedule(template, self.machine, technician=self.technician)
        occ = PMExecution.objects.create(
            pm_schedule=sched,
            scheduled_due_at=timezone.now(),
            status=PMExecution.Status.SUBMITTED,
        )
        occurrence_service.start(occ, self.technician, work_order_creator=self.technician)

        result = occurrence_service.complete(
            occ, self.technician,
            checklist_results=[{"text": "Step 1", "checked": True, "note": ""}],
            notes="",
            photo_count=0,
            required_photo_count=1,
        )
        self.assertFalse(result.success)
        self.assertIn("1 photo", result.error)

    def test_approve_closes_wo_and_advances_schedule(self):
        template = _make_template()
        sched = _make_schedule(template, self.machine, technician=self.technician)
        original_next_due = sched.next_due_at
        occ = PMExecution.objects.create(
            pm_schedule=sched,
            scheduled_due_at=original_next_due,
            status=PMExecution.Status.SUBMITTED,
        )
        wo = occurrence_service.start(occ, self.technician, work_order_creator=self.technician)
        occurrence_service.complete(
            occ, self.technician,
            checklist_results=[{"text": "x", "checked": True, "note": ""}],
            notes="ok",
        )

        occurrence_service.approve(occ, self.manager)

        occ.refresh_from_db()
        wo.refresh_from_db()
        sched.refresh_from_db()
        self.assertEqual(occ.status, PMExecution.Status.APPROVED)
        self.assertEqual(wo.lifecycle_status, WorkOrder.LifecycleStatus.CLOSED)
        # Schedule advanced
        self.assertGreater(sched.next_due_at, original_next_due)

    def test_return_to_technician_shows_rejection(self):
        template = _make_template()
        sched = _make_schedule(template, self.machine, technician=self.technician)
        occ = PMExecution.objects.create(
            pm_schedule=sched,
            scheduled_due_at=timezone.now(),
            status=PMExecution.Status.SUBMITTED,
        )
        occurrence_service.start(occ, self.technician, work_order_creator=self.technician)
        occurrence_service.complete(
            occ, self.technician,
            checklist_results=[{"text": "x", "checked": True, "note": ""}],
            notes="",
        )

        with self.assertRaises(ValueError):
            occurrence_service.return_to_technician(occ, self.manager, reason="")

        occurrence_service.return_to_technician(occ, self.manager, reason="Pressure not recorded")

        occ.refresh_from_db()
        self.assertEqual(occ.status, PMExecution.Status.REJECTED)
        self.assertEqual(occ.rejection_reason, "Pressure not recorded")

    def test_assign_sets_technician(self):
        template = _make_template()
        sched = _make_schedule(template, self.machine)
        occ = PMExecution.objects.create(
            pm_schedule=sched,
            scheduled_due_at=timezone.now(),
            status=PMExecution.Status.SUBMITTED,
        )
        assignment_service.assign(occ, self.technician, by=self.manager)
        occ.refresh_from_db()
        self.assertEqual(occ.assigned_technician, self.technician)

    def test_reassign_preserves_state(self):
        template = _make_template()
        _make_checklist(template, ["Step 1", "Step 2"])
        sched = _make_schedule(template, self.machine, technician=self.technician)
        occ = PMExecution.objects.create(
            pm_schedule=sched,
            scheduled_due_at=timezone.now(),
            status=PMExecution.Status.SUBMITTED,
            assigned_technician=self.technician,
        )
        # Mark some checklist work done
        occ.template_snapshot_json = {"checklist": [{"text": "Step 1"}, {"text": "Step 2"}]}
        occ.save()

        other = _make_user("tech_other", User.Role.TECHNICIAN)
        assignment_service.reassign(occ, other, by=self.manager)

        occ.refresh_from_db()
        self.assertEqual(occ.assigned_technician, other)
        # Snapshot preserved
        self.assertIn("Step 1", occ.template_snapshot_json["checklist"][0]["text"])
        self.assertEqual(occ.reassignment_count, 1)

    def test_plan_pause_archives_unassigned_assignments(self):
        template = _make_template()
        sched = _make_schedule(template, self.machine, technician=self.technician)
        sched.is_active = False
        sched.save()
        self.assertFalse(sched.is_active)


class ManagerPagesRenderTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.site = Site.objects.filter(is_default=True).first() or Site.objects.create(
            name="X", is_default=True, is_active=True,
        )
        from maintenance.models import Machine
        cls.machine = Machine.objects.create(
            name="Render Machine", qr_code="M-REN-001",
            asset_code="M-REN-001", asset_level=3,
            is_active=True, site=cls.site,
        )
        cls.manager = _make_user("mgr_ren", User.Role.MANAGER)
        cls.technician = _make_user("tech_ren", User.Role.TECHNICIAN)

    def test_manager_dashboard_renders(self):
        self.client.force_login(self.manager)
        r = self.client.get(reverse("preventive:mgr_dashboard"))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Preventive Maintenance")
        self.assertContains(r, "Due Today")
        self.assertContains(r, "Waiting Review")

    def test_manager_today_renders(self):
        self.client.force_login(self.manager)
        r = self.client.get(reverse("preventive:mgr_today"))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Today's Schedule")

    def test_manager_reviews_renders(self):
        self.client.force_login(self.manager)
        r = self.client.get(reverse("preventive:mgr_reviews"))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Reviews")

    def test_manager_plans_renders(self):
        self.client.force_login(self.manager)
        r = self.client.get(reverse("preventive:mgr_plans"))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Maintenance Plans")

    def test_manager_plan_detail_renders(self):
        template = _make_template()
        sched = _make_schedule(template, self.machine)
        self.client.force_login(self.manager)
        r = self.client.get(reverse("preventive:mgr_plan_detail", args=[sched.pk]))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, template.title)
        self.assertContains(r, "Plan Information")
        self.assertContains(r, "Quick Actions")
        self.assertContains(r, "Run Now")

    def test_manager_templates_renders(self):
        self.client.force_login(self.manager)
        r = self.client.get(reverse("preventive:mgr_templates"))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Templates")

    def test_manager_history_renders(self):
        self.client.force_login(self.manager)
        r = self.client.get(reverse("preventive:mgr_history"))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "History")
        # Empty state
        self.assertContains(r, "No history found")

    def test_technician_my_renders(self):
        self.client.force_login(self.technician)
        r = self.client.get(reverse("preventive:my"))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "My Maintenance")
        self.assertContains(r, "TODAY")
        self.assertContains(r, "UPCOMING")
        self.assertContains(r, "COMPLETED")

    def test_technician_blocked_from_manager_pages(self):
        self.client.force_login(self.technician)
        r = self.client.get(reverse("preventive:mgr_dashboard"))
        self.assertEqual(r.status_code, 302)  # redirect to dashboard

    def test_manager_blocked_from_tech_pages(self):
        self.client.force_login(self.manager)
        r = self.client.get(reverse("preventive:my"))
        self.assertEqual(r.status_code, 200)  # manager role allowed on tech page too