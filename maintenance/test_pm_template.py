"""Tests for Phase 1 PM module rebuild: PMTemplate, PMChecklistItem, PMExecution."""
from datetime import timedelta
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import User
from maintenance.models import (
    Machine, PMChecklistItem, PMExecution, PMSchedule, PMTemplate, Site, WorkOrder,
)
from maintenance.services import (
    capture_template_snapshot, create_pm_execution_for_wo, next_pm_execution_sequence,
    compute_next_due_at,
)


def _make_user(username, role):
    return User.objects.create_user(username=username, password="x", role=role)


def _make_machine(qr="PM-M1"):
    site = Site.objects.filter(is_default=True).first() or Site.objects.create(
        name="X", is_default=True, is_active=True
    )
    return Machine.objects.create(
        name=qr, qr_code=qr, asset_level=3, asset_code=qr, is_active=True, site=site,
    )


def _make_template(code="PM-TEST-001", priority="medium"):
    return PMTemplate.objects.create(
        code=code, title="Test Template " + code,
        estimated_duration_minutes=30, priority=priority, is_active=True,
    )


def _add_checklist(template, texts):
    for i, t in enumerate(texts, start=1):
        PMChecklistItem.objects.create(template=template, order=i, text=t, is_required=True)


class PMTemplateModelTests(TestCase):
    def test_create_template_with_checklist(self):
        t = _make_template()
        _add_checklist(t, ["Check oil", "Inspect hoses", "Verify pressure"])
        self.assertEqual(t.checklist_items.count(), 3)
        self.assertEqual(t.checklist_items.first().text, "Check oil")

    def test_template_str(self):
        t = _make_template(code="PM-ABC")
        self.assertIn("PM-ABC", str(t))

    def test_template_default_priority_is_medium(self):
        t = PMTemplate.objects.create(code="PM-X", title="X")
        self.assertEqual(t.priority, "medium")


class PMScheduleEffectivePropertiesTests(TestCase):
    def setUp(self):
        self.template = _make_template(priority="high")
        self.machine = _make_machine()

    def test_effective_priority_uses_override(self):
        s = PMSchedule.objects.create(
            template=self.template, machine=self.machine,
            frequency_type="monthly", interval=1,
            start_date=timezone.now().date(),
            next_due_at=timezone.now() + timedelta(days=7),
            priority_override="critical",
        )
        self.assertEqual(s.effective_priority, "critical")

    def test_effective_priority_falls_back_to_template(self):
        s = PMSchedule.objects.create(
            template=self.template, machine=self.machine,
            frequency_type="monthly", interval=1,
            start_date=timezone.now().date(),
            next_due_at=timezone.now() + timedelta(days=7),
        )
        self.assertEqual(s.effective_priority, "high")

    def test_effective_duration_uses_override(self):
        s = PMSchedule.objects.create(
            template=self.template, machine=self.machine,
            frequency_type="monthly", interval=1,
            start_date=timezone.now().date(),
            next_due_at=timezone.now() + timedelta(days=7),
            estimated_duration_override=120,
        )
        self.assertEqual(s.effective_duration_minutes, 120)

    def test_effective_duration_falls_back_to_template(self):
        self.template.estimated_duration_minutes = 45
        self.template.save()
        s = PMSchedule.objects.create(
            template=self.template, machine=self.machine,
            frequency_type="monthly", interval=1,
            start_date=timezone.now().date(),
            next_due_at=timezone.now() + timedelta(days=7),
        )
        self.assertEqual(s.effective_duration_minutes, 45)


class CaptureTemplateSnapshotTests(TestCase):
    def test_snapshot_captures_checklist_and_metadata(self):
        t = _make_template(code="PM-SNAP-001", priority="high")
        _add_checklist(t, ["Item A", "Item B"])
        snap = capture_template_snapshot(t)
        self.assertEqual(snap["template_code"], "PM-SNAP-001")
        self.assertEqual(snap["template_priority"], "high")
        self.assertEqual(len(snap["checklist"]), 2)
        self.assertEqual(snap["checklist"][0]["text"], "Item A")
        self.assertIn("captured_at", snap)

    def test_snapshot_preserves_after_template_edit(self):
        """Editing the template after snapshot capture must not change the snapshot."""
        t = _make_template()
        _add_checklist(t, ["Original 1", "Original 2"])
        snap = capture_template_snapshot(t)
        t.title = "EDITED"
        t.save()
        PMChecklistItem.objects.create(template=t, order=3, text="ADDED LATER")
        PMChecklistItem.objects.filter(template=t, order=1).delete()
        self.assertEqual(snap["template_title"], "Test Template PM-TEST-001")
        self.assertEqual(len(snap["checklist"]), 2)


class PMExecutionCreationTests(TestCase):
    def setUp(self):
        self.manager = _make_user("pm_mgr", User.Role.MANAGER)
        self.machine = _make_machine()
        self.template = _make_template()
        _add_checklist(self.template, ["Step 1", "Step 2"])
        self.schedule = PMSchedule.objects.create(
            template=self.template, machine=self.machine,
            frequency_type="monthly", interval=1,
            start_date=timezone.now().date(),
            next_due_at=timezone.now() + timedelta(days=7),
            created_by=self.manager,
        )

    def test_create_pm_execution_for_wo_captures_snapshot(self):
        wo = WorkOrder.objects.create(
            machine=self.machine, lifecycle_status="assigned",
            created_by=self.manager,
        )
        exec_row = create_pm_execution_for_wo(self.schedule, wo, actor=self.manager)
        self.assertEqual(exec_row.status, PMExecution.Status.SUBMITTED)
        self.assertEqual(exec_row.scheduled_due_at, self.schedule.next_due_at)
        self.assertEqual(exec_row.execution_sequence, 1)
        self.assertEqual(len(exec_row.template_snapshot_json["checklist"]), 2)
        self.assertEqual(exec_row.work_order, wo)

    def test_execution_sequence_auto_increments(self):
        wo1 = WorkOrder.objects.create(machine=self.machine, lifecycle_status="assigned", created_by=self.manager)
        e1 = create_pm_execution_for_wo(self.schedule, wo1, actor=self.manager)
        self.assertEqual(e1.execution_sequence, 1)
        self.schedule.next_due_at = self.schedule.next_due_at + timedelta(days=30)
        self.schedule.save()
        wo2 = WorkOrder.objects.create(machine=self.machine, lifecycle_status="assigned", created_by=self.manager)
        e2 = create_pm_execution_for_wo(self.schedule, wo2, actor=self.manager)
        self.assertEqual(e2.execution_sequence, 2)

    def test_scheduled_due_at_locks_to_spawn_time(self):
        wo = WorkOrder.objects.create(machine=self.machine, lifecycle_status="assigned", created_by=self.manager)
        original_due = self.schedule.next_due_at
        e = create_pm_execution_for_wo(self.schedule, wo, actor=self.manager)
        self.schedule.next_due_at = original_due + timedelta(days=30)
        self.schedule.save()
        e.refresh_from_db()
        self.assertEqual(e.scheduled_due_at, original_due)

    def test_unique_constraint_per_occurrence(self):
        from django.db import IntegrityError, transaction
        wo = WorkOrder.objects.create(machine=self.machine, lifecycle_status="assigned", created_by=self.manager)
        create_pm_execution_for_wo(self.schedule, wo, actor=self.manager)
        wo2 = WorkOrder.objects.create(machine=self.machine, lifecycle_status="assigned", created_by=self.manager)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                PMExecution.objects.create(
                    pm_schedule=self.schedule,
                    scheduled_due_at=self.schedule.next_due_at,
                    execution_sequence=99,
                    status=PMExecution.Status.SUBMITTED,
                )


class PMTemplateViewsTests(TestCase):
    def setUp(self):
        self.manager = _make_user("pmview_mgr", User.Role.MANAGER)

    def test_template_list_view_renders(self):
        _make_template(code="PM-LIST-001")
        self.client.force_login(self.manager)
        r = self.client.get(reverse("pm_template_list"))
        self.assertEqual(r.status_code, 200)
        self.assertIn("PM-LIST-001", r.content.decode())

    def test_template_create_view_persists_template_and_items(self):
        self.client.force_login(self.manager)
        post_data = {
            "code": "PM-NEW-001",
            "title": "New Procedure",
            "description": "",
            "estimated_duration_minutes": 45,
            "priority": "high",
            "requires_manager_review": "on",
            "is_active": "on",
            "checklist_items-TOTAL_FORMS": "2",
            "checklist_items-INITIAL_FORMS": "0",
            "checklist_items-MIN_NUM_FORMS": "0",
            "checklist_items-MAX_NUM_FORMS": "1000",
            "checklist_items-0-order": "1",
            "checklist_items-0-text": "Step A",
            "checklist_items-0-is_required": "on",
            "checklist_items-1-order": "2",
            "checklist_items-1-text": "Step B",
            "checklist_items-1-is_required": "on",
        }
        r = self.client.post(reverse("pm_template_create"), data=post_data)
        self.assertEqual(r.status_code, 302)
        t = PMTemplate.objects.get(code="PM-NEW-001")
        self.assertEqual(t.priority, "high")
        self.assertEqual(t.estimated_duration_minutes, 45)
        self.assertEqual(t.checklist_items.count(), 2)


class ComputeNextDueAtTests(TestCase):
    def setUp(self):
        self.template = _make_template()
        self.machine = _make_machine()

    def test_daily(self):
        s = PMSchedule.objects.create(
            template=self.template, machine=self.machine,
            frequency_type="daily", interval=3,
            start_date=timezone.now().date(),
            next_due_at=timezone.now(),
        )
        after = timezone.now()
        nxt = compute_next_due_at(s, after)
        self.assertEqual((nxt - after).days, 3)

    def test_monthly(self):
        s = PMSchedule.objects.create(
            template=self.template, machine=self.machine,
            frequency_type="monthly", interval=2,
            start_date=timezone.now().date(),
            next_due_at=timezone.now(),
        )
        after = timezone.now()
        nxt = compute_next_due_at(s, after)
        self.assertEqual((nxt.year - after.year) * 12 + (nxt.month - after.month), 2)