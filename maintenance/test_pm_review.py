"""Tests for Phase 3: PM manager review flow (approve/reject + no-drift scheduling)."""
from datetime import timedelta

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import User
from maintenance.models import (
    Machine, PMChecklistItem, PMExecution, PMSchedule, PMTemplate, Site, WorkOrder,
)


def _make_user(username, role):
    return User.objects.create_user(username=username, password="x", role=role)


def _make_machine(qr="PM3-M1"):
    site = Site.objects.filter(is_default=True).first() or Site.objects.create(
        name="X", is_default=True, is_active=True
    )
    return Machine.objects.create(
        name=qr, qr_code=qr, asset_level=3, asset_code=qr, is_active=True, site=site,
    )


def _make_template_with_checklist(code="PM3-T1", priority="medium"):
    t = PMTemplate.objects.create(
        code=code, title="Test " + code, estimated_duration_minutes=30,
        priority=priority, is_active=True,
    )
    PMChecklistItem.objects.create(template=t, order=1, text="Step 1", is_required=True)
    PMChecklistItem.objects.create(template=t, order=2, text="Step 2", is_required=True)
    return t


def _make_schedule_and_wo(machine, template, manager, technician):
    now = timezone.now()
    schedule = PMSchedule.objects.create(
        template=template, machine=machine,
        frequency_type="monthly", interval=1,
        start_date=now.date(),
        next_due_at=now + timedelta(days=7),
        grace_days=7,
        is_active=True,
    )
    wo = WorkOrder.objects.create(
        machine=machine, category=WorkOrder.Category.PREVENTIVE,
        lifecycle_status=WorkOrder.LifecycleStatus.PENDING_REVIEW,
        assigned_technician=technician, created_by=manager,
    )
    execution = PMExecution.objects.create(
        pm_schedule=schedule,
        work_order=wo,
        scheduled_due_at=schedule.next_due_at,
        execution_sequence=1,
        status=PMExecution.Status.SUBMITTED,
        completed_by=technician,
        completed_at=now,
        template_snapshot_json={
            "template_code": template.code,
            "template_title": template.title,
            "template_priority": template.priority,
            "template_duration_minutes": template.estimated_duration_minutes,
            "checklist": [{"order": 1, "text": "Step 1", "is_required": True}],
            "captured_at": now.isoformat(),
        },
    )
    return schedule, wo, execution


class PMReviewRenderTests(TestCase):
    def setUp(self):
        self.manager = _make_user("pmrev_mgr", User.Role.MANAGER)
        self.technician = _make_user("pmrev_tech", User.Role.TECHNICIAN)
        self.machine = _make_machine()
        self.template = _make_template_with_checklist()
        self.schedule, self.wo, self.execution = _make_schedule_and_wo(
            self.machine, self.template, self.manager, self.technician,
        )

    def test_review_view_renders_for_pending_pm_wo(self):
        self.client.force_login(self.manager)
        r = self.client.get(reverse("pm_review", args=[self.wo.pk]))
        self.assertEqual(r.status_code, 200)
        body = r.content.decode()
        self.assertIn("PM Review", body)
        self.assertIn(self.template.code, body)
        self.assertIn("Approve", body)
        self.assertIn("Reject", body)

    def test_review_view_renders_checklist_results_from_action_taken(self):
        self.wo.action_taken = "[✓] Step 1\n  Note: looks good\n[✗] Step 2"
        self.wo.save(update_fields=["action_taken"])
        self.client.force_login(self.manager)
        r = self.client.get(reverse("pm_review", args=[self.wo.pk]))
        self.assertIn("Step 1", r.content.decode())
        self.assertIn("Step 2", r.content.decode())
        self.assertIn("looks good", r.content.decode())

    def test_review_view_rejects_non_preventive_wo(self):
        self.wo.category = WorkOrder.Category.BREAKDOWN
        self.wo.save(update_fields=["category"])
        self.client.force_login(self.manager)
        r = self.client.get(reverse("pm_review", args=[self.wo.pk]))
        self.assertEqual(r.status_code, 302)


class PMReviewApproveTests(TestCase):
    def setUp(self):
        self.manager = _make_user("pmappr_mgr", User.Role.MANAGER)
        self.technician = _make_user("pmappr_tech", User.Role.TECHNICIAN)
        self.machine = _make_machine()
        self.template = _make_template_with_checklist()
        self.schedule, self.wo, self.execution = _make_schedule_and_wo(
            self.machine, self.template, self.manager, self.technician,
        )

    def test_approve_sets_execution_status_approved(self):
        self.client.force_login(self.manager)
        self.client.post(reverse("pm_review", args=[self.wo.pk]), data={"action": "approve"})
        self.execution.refresh_from_db()
        self.assertEqual(self.execution.status, PMExecution.Status.APPROVED)
        self.assertEqual(self.execution.approved_by, self.manager)
        self.assertIsNotNone(self.execution.approved_at)

    def test_approve_closes_wo(self):
        self.client.force_login(self.manager)
        self.client.post(reverse("pm_review", args=[self.wo.pk]), data={"action": "approve"})
        self.wo.refresh_from_db()
        self.assertEqual(self.wo.lifecycle_status, WorkOrder.LifecycleStatus.CLOSED)

    def test_approve_advances_next_due_no_drift(self):
        original_due = self.schedule.next_due_at
        self.client.force_login(self.manager)
        self.client.post(reverse("pm_review", args=[self.wo.pk]), data={"action": "approve"})
        self.schedule.refresh_from_db()
        delta_days = (self.schedule.next_due_at - original_due).days
        self.assertGreaterEqual(delta_days, 27)
        self.assertLessEqual(delta_days, 32)

    def test_approve_sets_last_completed_at(self):
        before = timezone.now()
        self.client.force_login(self.manager)
        self.client.post(reverse("pm_review", args=[self.wo.pk]), data={"action": "approve"})
        self.schedule.refresh_from_db()
        self.assertIsNotNone(self.schedule.last_completed_at)
        self.assertGreaterEqual(self.schedule.last_completed_at, before)


class PMReviewRejectTests(TestCase):
    def setUp(self):
        self.manager = _make_user("pmrej_mgr", User.Role.MANAGER)
        self.technician = _make_user("pmrej_tech", User.Role.TECHNICIAN)
        self.machine = _make_machine()
        self.template = _make_template_with_checklist()
        self.schedule, self.wo, self.execution = _make_schedule_and_wo(
            self.machine, self.template, self.manager, self.technician,
        )

    def test_reject_sets_execution_status_rejected(self):
        self.client.force_login(self.manager)
        self.client.post(reverse("pm_review", args=[self.wo.pk]), data={
            "action": "reject", "reason": "Step 2 not completed properly",
        })
        self.execution.refresh_from_db()
        self.assertEqual(self.execution.status, PMExecution.Status.REJECTED)
        self.assertEqual(self.execution.approved_by, self.manager)

    def test_reject_increments_rejection_count(self):
        self.client.force_login(self.manager)
        self.client.post(reverse("pm_review", args=[self.wo.pk]), data={
            "action": "reject", "reason": "Need redo",
        })
        self.wo.refresh_from_db()
        self.assertEqual(self.wo.rejection_count, 1)

    def test_reject_returns_wo_to_in_progress(self):
        self.client.force_login(self.manager)
        self.client.post(reverse("pm_review", args=[self.wo.pk]), data={
            "action": "reject", "reason": "Need redo",
        })
        self.wo.refresh_from_db()
        self.assertEqual(self.wo.lifecycle_status, WorkOrder.LifecycleStatus.IN_PROGRESS)

    def test_reject_appends_notes(self):
        self.client.force_login(self.manager)
        self.client.post(reverse("pm_review", args=[self.wo.pk]), data={
            "action": "reject", "reason": "Step 2 missing",
        })
        self.execution.refresh_from_db()
        self.assertIn("Step 2 missing", self.execution.notes)
        self.assertIn("Rejected", self.execution.notes)

    def test_reject_requires_reason(self):
        self.client.force_login(self.manager)
        r = self.client.post(reverse("pm_review", args=[self.wo.pk]), data={
            "action": "reject", "reason": "",
        })
        self.execution.refresh_from_db()
        self.assertEqual(self.execution.status, PMExecution.Status.SUBMITTED)


class PMResubmitAfterRejectTests(TestCase):
    def setUp(self):
        self.manager = _make_user("pmresub_mgr", User.Role.MANAGER)
        self.technician = _make_user("pmresub_tech", User.Role.TECHNICIAN)
        self.machine = _make_machine()
        self.template = _make_template_with_checklist()
        self.schedule, self.wo, self.execution = _make_schedule_and_wo(
            self.machine, self.template, self.manager, self.technician,
        )
        from maintenance.services import manager_reject_pm_execution
        manager_reject_pm_execution(
            self.execution, manager=self.manager, reason="Step 1 missed",
        )

    def test_resubmit_moves_rejected_back_to_submitted(self):
        self.client.force_login(self.technician)
        post_data = {
            "checklist_0": "on",
            "note_0": "Done now",
            "checklist_1": "on",
            "note_1": "Done",
            "root_cause": "test",
            "action_taken": "[✓] Step 1 redone",
            "notes": "",
        }
        r = self.client.post(reverse("pm_wo_detail", args=[self.wo.pk]), data=post_data)
        self.assertEqual(r.status_code, 302)
        self.execution.refresh_from_db()
        self.assertEqual(self.execution.status, PMExecution.Status.SUBMITTED)

    def test_resubmit_clears_approved_by_and_approved_at(self):
        self.execution.refresh_from_db()
        self.assertEqual(self.execution.status, PMExecution.Status.REJECTED)
        self.assertIsNotNone(self.execution.approved_by)

        self.client.force_login(self.technician)
        post_data = {
            "checklist_0": "on",
            "note_0": "",
            "checklist_1": "on",
            "note_1": "",
            "root_cause": "test",
            "action_taken": "redo",
            "notes": "",
        }
        self.client.post(reverse("pm_wo_detail", args=[self.wo.pk]), data=post_data)
        self.execution.refresh_from_db()
        self.assertEqual(self.execution.status, PMExecution.Status.SUBMITTED)
        self.assertIsNone(self.execution.approved_by)
        self.assertIsNone(self.execution.approved_at)


class WorkOrderDetailReviewButtonTests(TestCase):
    def setUp(self):
        self.manager = _make_user("pmbtn_mgr", User.Role.MANAGER)
        self.technician = _make_user("pmbtn_tech", User.Role.TECHNICIAN)
        self.machine = _make_machine()
        self.template = _make_template_with_checklist()
        self.schedule, self.wo, self.execution = _make_schedule_and_wo(
            self.machine, self.template, self.manager, self.technician,
        )

    def test_review_button_visible_for_manager_on_pending_pm_wo(self):
        self.client.force_login(self.manager)
        r = self.client.get(reverse("work_order_detail", args=[self.wo.pk]))
        self.assertEqual(r.status_code, 200)
        self.assertIn("Review PM", r.content.decode())

    def test_review_button_hidden_for_technician(self):
        self.client.force_login(self.technician)
        r = self.client.get(reverse("work_order_detail", args=[self.wo.pk]))
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("Review PM", r.content.decode())

    def test_review_button_hidden_when_wo_closed(self):
        self.wo.lifecycle_status = WorkOrder.LifecycleStatus.CLOSED
        self.wo.save(update_fields=["lifecycle_status"])
        self.client.force_login(self.manager)
        r = self.client.get(reverse("work_order_detail", args=[self.wo.pk]))
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("Review PM", r.content.decode())

    def test_review_button_hidden_for_non_preventive_wo(self):
        self.wo.category = WorkOrder.Category.BREAKDOWN
        self.wo.save(update_fields=["category"])
        self.client.force_login(self.manager)
        r = self.client.get(reverse("work_order_detail", args=[self.wo.pk]))
        self.assertNotIn("Review PM", r.content.decode())


class PMReviewRoleTests(TestCase):
    def setUp(self):
        self.manager = _make_user("pmrole_mgr", User.Role.MANAGER)
        self.technician = _make_user("pmrole_tech", User.Role.TECHNICIAN)
        self.operator = _make_user("pmrole_op", User.Role.OPERATOR)
        self.machine = _make_machine()
        self.template = _make_template_with_checklist()
        self.schedule, self.wo, self.execution = _make_schedule_and_wo(
            self.machine, self.template, self.manager, self.technician,
        )

    def test_technician_cannot_review(self):
        self.client.force_login(self.technician)
        r = self.client.get(reverse("pm_review", args=[self.wo.pk]))
        self.assertIn(r.status_code, (302, 403))

    def test_operator_cannot_review(self):
        self.client.force_login(self.operator)
        r = self.client.get(reverse("pm_review", args=[self.wo.pk]))
        self.assertIn(r.status_code, (302, 403))