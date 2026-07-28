"""Tests for Phase 4: PM notification cascade (7d/3d/1d before due + due today)."""
from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from accounts.models import User
from maintenance.models import (
    Machine, Notification, PMChecklistItem, PMSchedule, PMTemplate, Site,
)
from maintenance.notifications import (
    notify_pm_due_today, notify_pm_upcoming, sync_pm_notifications,
)


def _make_user(username, role):
    return User.objects.create_user(username=username, password="x", role=role, is_active=True)


def _make_machine(qr="PM4-M1"):
    site = Site.objects.filter(is_default=True).first() or Site.objects.create(
        name="X", is_default=True, is_active=True
    )
    return Machine.objects.create(
        name=qr, qr_code=qr, asset_level=3, asset_code=qr, is_active=True, site=site,
    )


def _make_template(code="PM4-T1"):
    t = PMTemplate.objects.create(
        code=code, title="Test " + code, estimated_duration_minutes=30,
        priority="medium", is_active=True,
    )
    PMChecklistItem.objects.create(template=t, order=1, text="Step", is_required=True)
    return t


def _make_schedule(template, machine, *, due_at, is_active=True):
    return PMSchedule.objects.create(
        template=template, machine=machine,
        frequency_type="monthly", interval=1,
        start_date=due_at.date(),
        next_due_at=due_at,
        grace_days=7,
        is_active=is_active,
    )


class PMUpcomingNotificationTests(TestCase):
    def setUp(self):
        self.manager = _make_user("pm_up_mgr", User.Role.MANAGER)
        self.supervisor = _make_user("pm_up_sup", User.Role.SUPERVISOR)
        self.technician = _make_user("pm_up_tech", User.Role.TECHNICIAN)
        self.machine = _make_machine()
        self.template = _make_template()
        self.now = timezone.now()

    def test_7d_creates_notification_for_manager(self):
        sched = _make_schedule(self.template, self.machine, due_at=self.now + timedelta(days=7))
        result = notify_pm_upcoming(sched, days_before=7)
        self.assertEqual(result, 1)
        notif = Notification.objects.get(kind=Notification.Kind.PM_UPCOMING_7D)
        self.assertIn("7 days", notif.title)
        self.assertIn(f"pm_sched:{sched.pk}", notif.body)
        self.assertIn("stage:UPCOMING_7D", notif.body)
        self.assertEqual(notif.recipient, self.manager)

    def test_7d_does_not_notify_supervisor(self):
        sched = _make_schedule(self.template, self.machine, due_at=self.now + timedelta(days=7))
        notify_pm_upcoming(sched, days_before=7)
        recipients = Notification.objects.filter(
            kind=Notification.Kind.PM_UPCOMING_7D,
        ).values_list("recipient_id", flat=True)
        self.assertNotIn(self.supervisor.pk, recipients)
        self.assertNotIn(self.technician.pk, recipients)

    def test_3d_creates_notification(self):
        sched = _make_schedule(self.template, self.machine, due_at=self.now + timedelta(days=3))
        result = notify_pm_upcoming(sched, days_before=3)
        self.assertEqual(result, 1)
        self.assertTrue(Notification.objects.filter(kind=Notification.Kind.PM_UPCOMING_3D).exists())

    def test_1d_includes_supervisor(self):
        sched = _make_schedule(self.template, self.machine, due_at=self.now + timedelta(days=1))
        result = notify_pm_upcoming(sched, days_before=1)
        self.assertEqual(result, 1)
        recipients = Notification.objects.filter(
            kind=Notification.Kind.PM_UPCOMING_1D,
        ).values_list("recipient_id", flat=True)
        self.assertIn(self.manager.pk, recipients)
        self.assertIn(self.supervisor.pk, recipients)
        self.assertNotIn(self.technician.pk, recipients)

    def test_due_today_includes_technician(self):
        sched = _make_schedule(self.template, self.machine, due_at=self.now)
        result = notify_pm_due_today(sched)
        self.assertEqual(result, 1)
        recipients = Notification.objects.filter(
            kind=Notification.Kind.PM_DUE_TODAY,
        ).values_list("recipient_id", flat=True)
        self.assertIn(self.manager.pk, recipients)
        self.assertIn(self.supervisor.pk, recipients)
        self.assertIn(self.technician.pk, recipients)

    def test_invalid_days_before_raises(self):
        sched = _make_schedule(self.template, self.machine, due_at=self.now + timedelta(days=5))
        with self.assertRaises(ValueError):
            notify_pm_upcoming(sched, days_before=5)


class PMNotificationDedupeTests(TestCase):
    def setUp(self):
        self.manager = _make_user("pm_ded_mgr", User.Role.MANAGER)
        self.machine = _make_machine()
        self.template = _make_template()
        self.now = timezone.now()

    def test_dedupes_within_same_stage_same_cycle(self):
        sched = _make_schedule(self.template, self.machine, due_at=self.now + timedelta(days=7))
        notify_pm_upcoming(sched, days_before=7)
        notify_pm_upcoming(sched, days_before=7)
        notify_pm_upcoming(sched, days_before=7)
        self.assertEqual(
            Notification.objects.filter(kind=Notification.Kind.PM_UPCOMING_7D).count(),
            1,
        )

    def test_dedupes_across_stages(self):
        sched = _make_schedule(self.template, self.machine, due_at=self.now + timedelta(days=7))
        notify_pm_upcoming(sched, days_before=7)
        sched.next_due_at = self.now + timedelta(days=3)
        sched.save(update_fields=["next_due_at"])
        notify_pm_upcoming(sched, days_before=3)
        self.assertEqual(
            Notification.objects.filter(kind=Notification.Kind.PM_UPCOMING_7D).count(),
            1,
        )
        self.assertEqual(
            Notification.objects.filter(kind=Notification.Kind.PM_UPCOMING_3D).count(),
            1,
        )

    def test_new_cycle_can_fire_same_stage_again(self):
        sched = _make_schedule(self.template, self.machine, due_at=self.now + timedelta(days=7))
        notify_pm_upcoming(sched, days_before=7)
        sched.next_due_at = self.now + timedelta(days=37)
        sched.save(update_fields=["next_due_at"])
        result = notify_pm_upcoming(sched, days_before=7)
        self.assertEqual(result, 1)
        self.assertEqual(
            Notification.objects.filter(kind=Notification.Kind.PM_UPCOMING_7D).count(),
            2,
        )


class SyncPMNotificationsAggregateTests(TestCase):
    def setUp(self):
        self.manager = _make_user("pm_sync_mgr", User.Role.MANAGER)
        self.machine = _make_machine()
        self.template = _make_template()
        self.now = timezone.now()

    def test_sync_handles_all_stages_at_once(self):
        sched_7d = _make_schedule(self.template, self.machine, due_at=self.now + timedelta(days=7))
        sched_3d = _make_schedule(self.template, self.machine, due_at=self.now + timedelta(days=3))
        sched_1d = _make_schedule(self.template, self.machine, due_at=self.now + timedelta(days=1))
        sched_today = _make_schedule(self.template, self.machine, due_at=self.now)
        sched_overdue = _make_schedule(self.template, self.machine, due_at=self.now - timedelta(days=2))

        counts = sync_pm_notifications()
        self.assertEqual(counts["upcoming_7d"], 1)
        self.assertEqual(counts["upcoming_3d"], 1)
        self.assertEqual(counts["upcoming_1d"], 1)
        self.assertEqual(counts["due_today"], 1)
        self.assertGreaterEqual(counts["overdue"], 1)

    def test_sync_idempotent(self):
        _make_schedule(self.template, self.machine, due_at=self.now + timedelta(days=7))
        sync_pm_notifications()
        sync_pm_notifications()
        sync_pm_notifications()
        self.assertEqual(
            Notification.objects.filter(kind=Notification.Kind.PM_UPCOMING_7D).count(),
            1,
        )

    def test_sync_skips_inactive_pms(self):
        _make_schedule(self.template, self.machine, due_at=self.now + timedelta(days=7), is_active=False)
        counts = sync_pm_notifications()
        self.assertEqual(counts["upcoming_7d"], 0)

    def test_sync_returns_zero_for_no_pms(self):
        counts = sync_pm_notifications()
        self.assertEqual(counts["upcoming_7d"], 0)
        self.assertEqual(counts["upcoming_3d"], 0)
        self.assertEqual(counts["upcoming_1d"], 0)
        self.assertEqual(counts["due_today"], 0)


class SyncPMNotificationsCommandTests(TestCase):
    def test_command_runs_without_error(self):
        from django.core.management import call_command
        from io import StringIO
        out = StringIO()
        call_command("sync_pm_notifications", stdout=out)
        output = out.getvalue()
        self.assertIn("PM notifications sync complete.", output)
        self.assertIn("upcoming_7d:", output)
        self.assertIn("overdue:", output)
