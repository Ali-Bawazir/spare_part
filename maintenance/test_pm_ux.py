"""
Phase 9 PM UX regression tests:
- mgr_today auto-calls generate_today when last_generate_run is stale
- mgr_history tab=pending_today only shows today's open executions
- mgr_history tab=overdue only shows overdue SUBMITTED executions
- mgr_plan_detail falls back to schedule.next_due_at when no PMExecution
"""
from datetime import date, datetime, timedelta
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import User
from maintenance.models import (
    MaintenanceSettings, PMExecution, Machine, PMTemplate, PMSchedule,
)
from maintenance.preventive_engine import scheduling_service


class PMUXAutoGenerateTests(TestCase):
    """mgr_today auto-calls generate_today when MaintenanceSettings.last_generate_run is stale."""

    def setUp(self):
        self.manager = User.objects.create_user(
            username="mgr", password="pass1234", role=User.Role.MANAGER,
        )
        # Single machine + template + schedule with next_due_at == today
        self.machine = Machine.objects.create(
            name="PressUX", qr_code="PRESS-UX-01", location="Hall A",
        )
        self.template = PMTemplate.objects.create(
            code="PM-UX-001", title="UX inspection",
            description="Phase 9 UX test",
            priority=PMTemplate.Priority.MEDIUM,
            estimated_duration_minutes=30,
        )
        self.schedule = PMSchedule.objects.create(
            template=self.template,
            machine=self.machine,
            frequency_type=PMSchedule.FrequencyType.DAILY,
            interval=1,
            start_date=timezone.now().date(),
            next_due_at=timezone.now(),  # due now → today's slot
            due_time=datetime.now().time().replace(microsecond=0),
            is_active=True,
            created_by=self.manager,
        )

    def test_mgr_today_auto_generates_when_stale(self):
        """Open Today's Schedule with no prior generate_today run → it now runs."""
        # Wipe the settings row to force `needs_generate=True`.
        MaintenanceSettings.objects.all().delete()
        # Verify nothing exists yet:
        self.assertEqual(PMExecution.objects.filter(pm_schedule=self.schedule).count(), 0)
        self.client.force_login(self.manager)
        resp = self.client.get(reverse("preventive:mgr_today"))
        self.assertEqual(resp.status_code, 200)
        # Now at least one PMExecution should exist for today's slot.
        self.assertGreaterEqual(
            PMExecution.objects.filter(pm_schedule=self.schedule).count(), 1,
        )
        # And the MaintenanceSettings should reflect that we ran today.
        s = MaintenanceSettings.objects.first()
        self.assertIsNotNone(s.last_generate_run)
        self.assertEqual(s.last_generate_run.date(), timezone.now().date())

    def test_mgr_today_idempotent_same_day(self):
        """Two GETs on the same day do NOT create duplicates."""
        MaintenanceSettings.objects.all().delete()
        self.client.force_login(self.manager)
        self.client.get(reverse("preventive:mgr_today"))
        first_count = PMExecution.objects.count()
        # Second open of Today's Schedule — should be a no-op for generation.
        self.client.get(reverse("preventive:mgr_today"))
        self.assertEqual(PMExecution.objects.count(), first_count)


class PMHistoryTabsTests(TestCase):
    """mgr_history ?tab= query param filters correctly."""

    def setUp(self):
        self.manager = User.objects.create_user(
            username="mgr", password="pass1234", role=User.Role.MANAGER,
        )
        self.machine = Machine.objects.create(
            name="PressTabs", qr_code="PRESS-TABS-01", location="Hall B",
        )
        self.template = PMTemplate.objects.create(
            code="PM-TABS-001", title="Tabs inspection",
            description="Phase 9 tabs test",
            priority=PMTemplate.Priority.MEDIUM,
            estimated_duration_minutes=30,
        )
        today = timezone.now().date()
        self.schedule = PMSchedule.objects.create(
            template=self.template, machine=self.machine,
            frequency_type=PMSchedule.FrequencyType.MONTHLY, interval=1,
            start_date=today - timedelta(days=10),
            next_due_at=timezone.now() - timedelta(days=10),
            due_time=datetime.now().time().replace(microsecond=0),
            is_active=True, created_by=self.manager,
        )
        # 1 approved (10 days ago)
        PMExecution.objects.create(
            pm_schedule=self.schedule,
            scheduled_due_at=timezone.now() - timedelta(days=10),
            status=PMExecution.Status.APPROVED,
            approved_at=timezone.now() - timedelta(days=10),
            completed_by=self.manager, approved_by=self.manager,
        )
        # 1 SUBMITTED today
        PMExecution.objects.create(
            pm_schedule=self.schedule,
            scheduled_due_at=timezone.now(),
            status=PMExecution.Status.SUBMITTED,
        )
        # 1 SUBMITTED 30 days ago (overdue)
        PMExecution.objects.create(
            pm_schedule=self.schedule,
            scheduled_due_at=timezone.now() - timedelta(days=30),
            status=PMExecution.Status.SUBMITTED,
        )

    def test_tab_completed_default_shows_only_approved(self):
        self.client.force_login(self.manager)
        resp = self.client.get(reverse("preventive:mgr_history"))
        self.assertEqual(resp.status_code, 200)
        rows = list(resp.context["executions"])
        for r in rows:
            self.assertEqual(r.status, PMExecution.Status.APPROVED)

    def test_tab_pending_today_shows_only_today_open(self):
        self.client.force_login(self.manager)
        resp = self.client.get(reverse("preventive:mgr_history") + "?tab=pending_today")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["selected_tab"], "pending_today")
        rows = list(resp.context["executions"])
        for r in rows:
            self.assertIn(r.status, [PMExecution.Status.SUBMITTED, PMExecution.Status.REJECTED])
            self.assertEqual(r.scheduled_due_at.date(), timezone.now().date())

    def test_tab_overdue_shows_only_past_grace(self):
        self.client.force_login(self.manager)
        resp = self.client.get(reverse("preventive:mgr_history") + "?tab=overdue")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["selected_tab"], "overdue")
        rows = list(resp.context["executions"])
        for r in rows:
            self.assertEqual(r.status, PMExecution.Status.SUBMITTED)
            self.assertLess(r.scheduled_due_at.date(), timezone.now().date())

    def test_summary_counts_match_data(self):
        self.client.force_login(self.manager)
        resp = self.client.get(reverse("preventive:mgr_history"))
        self.assertEqual(resp.context["completed_recent_count"], 1)
        self.assertEqual(resp.context["pending_today_count"], 1)
        self.assertGreaterEqual(resp.context["overdue_count"], 1)


class PMPlanDetailFallbackTests(TestCase):
    """Plan detail must fall back to PMSchedule.next_due_at when no future PMExecution exists."""

    def setUp(self):
        self.manager = User.objects.create_user(
            username="mgr", password="pass1234", role=User.Role.MANAGER,
        )
        self.machine = Machine.objects.create(
            name="PressFallback", qr_code="PRESS-FB-01", location="Hall C",
        )
        self.template = PMTemplate.objects.create(
            code="PM-FB-001", title="Fallback inspection",
            description="Phase 9 fallback test",
            priority=PMTemplate.Priority.MEDIUM,
            estimated_duration_minutes=15,
        )
        # Plan with next_due_at set but NO PMExecution rows.
        self.schedule = PMSchedule.objects.create(
            template=self.template, machine=self.machine,
            frequency_type=PMSchedule.FrequencyType.MONTHLY, interval=1,
            start_date=date(2026, 7, 1),
            next_due_at=timezone.now() + timedelta(days=2),
            due_time=datetime.strptime("08:00", "%H:%M").time(),
            is_active=True, created_by=self.manager,
        )

    def test_plan_detail_uses_schedule_next_due_when_no_occurrence(self):
        self.client.force_login(self.manager)
        resp = self.client.get(reverse("preventive:mgr_plan_detail", kwargs={"pk": self.schedule.pk}))
        self.assertEqual(resp.status_code, 200)
        # Confirm context has the right values
        self.assertIsNone(resp.context["next_occ"])
        self.assertIsNotNone(resp.context["schedule"].next_due_at)
        # The template should render the schedule's next_due_at with the "(scheduled — occurrence not generated yet)" suffix
        self.assertContains(resp, "(scheduled — occurrence not generated yet)")

    def test_plan_detail_with_occurrence_uses_occurrence(self):
        # Add a future PMExecution — should be preferred over schedule.next_due_at
        occ = PMExecution.objects.create(
            pm_schedule=self.schedule,
            scheduled_due_at=timezone.now() + timedelta(hours=3),
            status=PMExecution.Status.SUBMITTED,
        )
        self.client.force_login(self.manager)
        resp = self.client.get(reverse("preventive:mgr_plan_detail", kwargs={"pk": self.schedule.pk}))
        self.assertEqual(resp.context["next_occ"].pk, occ.pk)
        self.assertNotContains(resp, "occurrence not generated yet")


class PMPlanCreateGenerationTests(TestCase):
    """Saving a plan whose next_due_at is today should spawn today's occurrence."""

    def setUp(self):
        self.manager = User.objects.create_user(
            username="mgr_pg", password="pass1234", role=User.Role.MANAGER,
        )
        self.machine = Machine.objects.create(
            name="PressPG", qr_code="PRESS-PG-01", location="Hall D",
        )
        self.template = PMTemplate.objects.create(
            code="PM-PG-001", title="Plan-create generation test",
            description="Phase 9 plan-create UX test",
            priority=PMTemplate.Priority.MEDIUM,
            estimated_duration_minutes=20,
        )

    def test_plan_create_generates_today_execution(self):
        """Saving a plan with next_due_at=today should auto-spawn today's occurrence.

        Goes through the manager create-plan form view (the only caller of
        Fix A's _plan_form auto-spawn logic) and asserts the occurrence was
        created as a side-effect of form.save().
        """
        # Clear cached last_generate_run so the auto-trigger path runs.
        MaintenanceSettings.objects.all().delete()
        self.client.force_login(self.manager)
        next_due = timezone.now().replace(hour=5, minute=10, second=0, microsecond=0)
        start = timezone.now().date()
        resp = self.client.post(
            reverse("preventive:mgr_plan_create"),
            data={
                "template": self.template.pk,
                "machine": self.machine.pk,
                "component": "",
                "frequency_type": "daily",
                "interval": "1",
                "start_date": start.isoformat(),
                "next_due_at": next_due.strftime("%Y-%m-%dT%H:%M"),
                "due_time": "05:10",
                "ends_at": "",
                "priority_override": "",
                "estimated_duration_override": "",
                "grace_days": "7",
                "reminder_days_before": "7",
                "trigger_type": "time",
                "is_active": "on",
            },
        )
        # success redirects to plan detail; 200 means form re-rendered with errors
        self.assertEqual(resp.status_code, 302)
        sched = PMSchedule.objects.get(template=self.template)
        # Post-create, an execution should exist for today.
        self.assertGreaterEqual(
            PMExecution.objects.filter(
                pm_schedule=sched,
                scheduled_due_at__date=timezone.now().date(),
            ).count(),
            1,
        )

    def test_mgr_today_smart_regen_when_plan_added_late(self):
        """mgr_today should auto-regenerate if a plan exists with no execution."""
        # Mark MaintenanceSettings as already-run today.
        s, _ = MaintenanceSettings.objects.get_or_create(pk=1)
        s.last_generate_run = timezone.now()
        s.save()
        # Create a plan with today's next_due_at, no executions.
        sched = PMSchedule.objects.create(
            template=self.template, machine=self.machine,
            frequency_type=PMSchedule.FrequencyType.DAILY, interval=1,
            start_date=timezone.now().date(),
            next_due_at=timezone.now().replace(hour=6, minute=0, second=0, microsecond=0),
            due_time=datetime.strptime("06:00", "%H:%M").time(),
            is_active=True, created_by=self.manager,
        )
        self.assertEqual(
            PMExecution.objects.filter(pm_schedule=sched).count(),
            0,
        )
        self.client.force_login(self.manager)
        self.client.get(reverse("preventive:mgr_today"))
        # After hitting Today page, the missing occurrence should appear.
        self.assertGreaterEqual(
            PMExecution.objects.filter(pm_schedule=sched).count(), 1,
        )
