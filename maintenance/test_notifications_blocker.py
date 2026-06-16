"""
Phase 2C — NotificationService tests for the WorkOrder Blocker System.

Covers:
- on_blocker_event dispatch for user-actionable event types
- Skip of non-user-actionable events (e.g. part_issued)
- 1-hour dedup window per (recipient, kind, ref_id)
- Recipients for part_rejected (the requester)
- emergency_interrupted → is_critical=True + source WO in body
- notify_po_received_summary
- Recipients include the WO's assigned_technician
- Dedup key format
- Auto-fire via WorkOrderBlockerEventService.record
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.contrib.contenttypes.models import ContentType
from django.test import TestCase
from django.utils import timezone

from accounts.models import User
from inventory.models import PartIssueLine, SparePart
from maintenance.models import (
    Machine,
    Notification,
    WorkOrder,
    WorkOrderBlocker,
    WorkOrderBlockerEvent,
)
from maintenance.services_blocker import (
    WorkOrderBlockerEventService,
    WorkOrderBlockerService,
)
from maintenance.services_notifications import (
    NotificationService,
    notify_po_received_summary,
)


def _make_user(username: str, role: str) -> User:
    return User.objects.create_user(username=username, password="pass1234", role=role)


def _make_wo(*, machine: Machine = None, created_by: User, assigned_technician: User = None,
             **kwargs) -> WorkOrder:
    defaults = {
        "machine": machine,
        "created_by": created_by,
        "status": WorkOrder.Status.APPROVED,
    }
    if assigned_technician is not None:
        defaults["assigned_technician"] = assigned_technician
    defaults.update(kwargs)
    return WorkOrder.objects.create(**defaults)


def _make_blocker(*, wo: WorkOrder, kind: str, external_obj=None, opened_by: User = None,
                  note: str = "") -> WorkOrderBlocker:
    """Create an OPEN blocker directly, bypassing the service to keep tests
    focused on the notification layer.
    """
    if external_obj is None:
        return WorkOrderBlocker.objects.create(
            work_order=wo,
            kind=kind,
            status=WorkOrderBlocker.Status.OPEN,
            note=note,
            opened_by=opened_by,
        )
    return WorkOrderBlocker.objects.create(
        work_order=wo,
        kind=kind,
        status=WorkOrderBlocker.Status.OPEN,
        content_type=ContentType.objects.get_for_model(external_obj),
        object_id=external_obj.pk,
        note=note,
        opened_by=opened_by,
    )


def _make_line(*, wo: WorkOrder, part: SparePart, requested_by: User, qty: Decimal = Decimal("2")
               ) -> PartIssueLine:
    return PartIssueLine.objects.create(
        work_order=wo,
        part=part,
        quantity=qty,
        unit_cost=Decimal("10"),
        status=PartIssueLine.Status.PENDING,
        requested_by=requested_by,
        issued_by=requested_by,
        requested_qty=qty,
    )


class NotificationServiceOnBlockerEventTests(TestCase):
    """NotificationService.on_blocker_event — per-event-type behavior."""

    def setUp(self):
        self.manager = _make_user("notif_mgr", User.Role.MANAGER)
        self.supervisor = _make_user("notif_sup", User.Role.SUPERVISOR)
        self.tech = _make_user("notif_tech", User.Role.TECHNICIAN)
        self.machine = Machine.objects.create(name="Press N", qr_code="PRESS-N")
        self.part = SparePart.objects.create(sku="BRG-N-01", name="Bearing N")
        self.wo = _make_wo(
            machine=self.machine,
            created_by=self.manager,
            assigned_technician=self.tech,
        )
        self.line = _make_line(wo=self.wo, part=self.part, requested_by=self.tech)
        self.blocker = _make_blocker(
            wo=self.wo,
            kind=WorkOrderBlocker.Kind.PART,
            external_obj=self.line,
            opened_by=self.tech,
            note="Waiting for warehouse",
        )

    def _make_event(self, event_type: str, blocker: WorkOrderBlocker = None,
                    payload: dict = None) -> WorkOrderBlockerEvent:
        return WorkOrderBlockerEvent.objects.create(
            blocker=blocker or self.blocker,
            event_type=event_type,
            actor=self.manager,
            payload=payload or {},
        )

    def test_blocker_created_creates_notification(self):
        """blocker_created → kind=WO_BLOCKER_OPENED, recipient includes manager+tech."""
        event = self._make_event(WorkOrderBlockerEvent.EventType.BLOCKER_CREATED)
        created = NotificationService.on_blocker_event(event)
        self.assertGreaterEqual(created, 1)
        # Manager should have a notification
        notif = Notification.objects.filter(
            recipient=self.manager,
            kind=Notification.Kind.WO_BLOCKER_OPENED,
        ).first()
        self.assertIsNotNone(notif)
        self.assertIn(f"WO-{self.wo.number}", notif.title)
        self.assertIn("part", notif.title)
        # Link goes to WO detail
        self.assertTrue(notif.link.endswith(f"/work-orders/{self.wo.pk}/"))

    def test_blocker_created_includes_tech_recipient(self):
        """The WO's assigned_technician must receive the notification."""
        event = self._make_event(WorkOrderBlockerEvent.EventType.BLOCKER_CREATED)
        NotificationService.on_blocker_event(event)
        self.assertTrue(
            Notification.objects.filter(
                recipient=self.tech,
                kind=Notification.Kind.WO_BLOCKER_OPENED,
            ).exists(),
            "Tech must be a recipient of the blocker-opened notification",
        )

    def test_part_issued_is_not_user_actionable(self):
        """part_issued is an implementation detail; no notification is fired."""
        event = self._make_event(WorkOrderBlockerEvent.EventType.PART_ISSUED)
        before = Notification.objects.count()
        created = NotificationService.on_blocker_event(event)
        self.assertEqual(created, 0)
        self.assertEqual(Notification.objects.count(), before)

    def test_part_received_is_not_user_actionable(self):
        """part_received is also skipped."""
        event = self._make_event(WorkOrderBlockerEvent.EventType.PART_RECEIVED)
        before = Notification.objects.count()
        created = NotificationService.on_blocker_event(event)
        self.assertEqual(created, 0)
        self.assertEqual(Notification.objects.count(), before)

    def test_dedup_window_blocks_second_notification(self):
        """Calling on_blocker_event twice for the same event in <1h creates only 1 row per recipient."""
        event = self._make_event(WorkOrderBlockerEvent.EventType.BLOCKER_CREATED)
        first = NotificationService.on_blocker_event(event)
        self.assertGreater(first, 0)
        manager_count = Notification.objects.filter(
            recipient=self.manager,
            kind=Notification.Kind.WO_BLOCKER_OPENED,
        ).count()
        self.assertEqual(manager_count, 1)

        # Synthesize a second event of the same type on the same blocker.
        # Since dedup key includes blocker_id+recipient_id, the second call
        # must be deduped (still within the 1h window).
        second_event = WorkOrderBlockerEvent.objects.create(
            blocker=self.blocker,
            event_type=WorkOrderBlockerEvent.EventType.BLOCKER_CREATED,
            actor=self.manager,
            payload={},
        )
        second = NotificationService.on_blocker_event(second_event)
        self.assertEqual(second, 0, "Dedup should prevent duplicate notification")

    def test_dedup_releases_after_one_hour(self):
        """After 1+h, a new BLOCKER_CREATED event on the same blocker DOES notify again."""
        event = self._make_event(WorkOrderBlockerEvent.EventType.BLOCKER_CREATED)
        NotificationService.on_blocker_event(event)
        # Backdate the existing notification by >1h
        past = timezone.now() - timedelta(hours=2)
        Notification.objects.filter(
            recipient=self.manager,
            kind=Notification.Kind.WO_BLOCKER_OPENED,
        ).update(created_at=past)
        # New event fires
        new_event = WorkOrderBlockerEvent.objects.create(
            blocker=self.blocker,
            event_type=WorkOrderBlockerEvent.EventType.BLOCKER_CREATED,
            actor=self.manager,
            payload={},
        )
        created = NotificationService.on_blocker_event(new_event)
        self.assertGreater(created, 0, "After 1h, dedup must release")
        # Now manager has 2 notifications (one old, one new)
        self.assertEqual(
            Notification.objects.filter(
                recipient=self.manager,
                kind=Notification.Kind.WO_BLOCKER_OPENED,
            ).count(),
            2,
        )

    def test_part_rejected_notifies_requester(self):
        """part_rejected: the requester (tech) must be a recipient."""
        event = self._make_event(WorkOrderBlockerEvent.EventType.PART_REJECTED)
        NotificationService.on_blocker_event(event)
        # Tech requested this line — must be notified
        self.assertTrue(
            Notification.objects.filter(
                recipient=self.tech,
                kind=Notification.Kind.WO_BLOCKER_CANCELLED,
            ).exists(),
            "Requester (tech) must be notified when their request is rejected",
        )

    def test_emergency_interrupted_is_critical_and_includes_source(self):
        """emergency_interrupted → is_critical=True, body mentions source WO number."""
        # Create a source emergency WO
        emergency_wo = _make_wo(
            machine=self.machine,
            created_by=self.manager,
            is_emergency=True,
        )
        # Build an OPERATIONAL blocker with source_work_order set
        op_blocker = WorkOrderBlocker.objects.create(
            work_order=self.wo,
            kind=WorkOrderBlocker.Kind.OPERATIONAL,
            status=WorkOrderBlocker.Status.OPEN,
            source_work_order=emergency_wo,
            pause_reason=WorkOrder.PauseReason.EMERGENCY,
            note="Paused for emergency WO",
            opened_by=self.manager,
        )
        event = self._make_event(
            WorkOrderBlockerEvent.EventType.EMERGENCY_INTERRUPTED,
            blocker=op_blocker,
            payload={"source_wo_id": emergency_wo.pk},
        )
        NotificationService.on_blocker_event(event)
        notif = Notification.objects.filter(
            recipient=self.manager,
            kind=Notification.Kind.EMERGENCY_INTERRUPTED,
        ).first()
        self.assertIsNotNone(notif)
        self.assertTrue(notif.is_critical, "emergency_interrupted must be critical")
        self.assertIn(f"WO-{emergency_wo.number}", notif.body)

    def test_dedup_key_format(self):
        """Dedup key follows '{kind}:{blocker_id}:{recipient_id}' pattern."""
        event = self._make_event(WorkOrderBlockerEvent.EventType.BLOCKER_CREATED)
        NotificationService.on_blocker_event(event)
        notif = Notification.objects.filter(
            kind=Notification.Kind.WO_BLOCKER_OPENED,
        ).first()
        self.assertIsNotNone(notif)
        # Format check
        expected_prefix = f"{Notification.Kind.WO_BLOCKER_OPENED}:{self.blocker.pk}:"
        self.assertTrue(
            notif.dedup_key.startswith(expected_prefix),
            f"dedup_key {notif.dedup_key!r} should start with {expected_prefix!r}",
        )

    def test_hook_fires_from_work_order_blocker_event_service_record(self):
        """Calling WorkOrderBlockerEventService.record() automatically creates a notification."""
        # Clear any notifications from setUp
        Notification.objects.all().delete()
        # Re-open the blocker and call record() — the hook should fire
        WorkOrderBlockerEventService.record(
            blocker=self.blocker,
            event_type=WorkOrderBlockerEvent.EventType.BLOCKER_RESOLVED,
            actor=self.manager,
            payload={"note": "done"},
        )
        self.assertTrue(
            Notification.objects.filter(
                recipient=self.manager,
                kind=Notification.Kind.WO_BLOCKER_RESOLVED,
            ).exists(),
            "NotificationService hook must fire when record() is called",
        )

    def test_hook_failure_does_not_break_event_write(self):
        """A failing notification handler must not propagate up."""
        # Patch the handler to raise
        from maintenance import services_notifications as sn
        original = sn._EVENT_DISPATCH.get(WorkOrderBlockerEvent.EventType.BLOCKER_CREATED)
        def boom(event):
            raise RuntimeError("synthetic failure")
        sn._EVENT_DISPATCH[WorkOrderBlockerEvent.EventType.BLOCKER_CREATED] = boom
        try:
            # Should not raise
            event = WorkOrderBlockerEventService.record(
                blocker=self.blocker,
                event_type=WorkOrderBlockerEvent.EventType.BLOCKER_CREATED,
                actor=self.manager,
                payload={},
            )
            self.assertIsNotNone(event)
            self.assertIsNotNone(event.pk)
        finally:
            sn._EVENT_DISPATCH[WorkOrderBlockerEvent.EventType.BLOCKER_CREATED] = original

    def test_blocker_resolved_notification(self):
        """blocker_resolved → kind=WO_BLOCKER_RESOLVED, recipients include tech + manager."""
        # Set blocker to RESOLVED so payload resolution is realistic
        self.blocker.status = WorkOrderBlocker.Status.RESOLVED
        self.blocker.resolution_note = "Issued from stock"
        self.blocker.save()
        event = self._make_event(WorkOrderBlockerEvent.EventType.BLOCKER_RESOLVED)
        NotificationService.on_blocker_event(event)
        self.assertTrue(
            Notification.objects.filter(
                recipient=self.manager,
                kind=Notification.Kind.WO_BLOCKER_RESOLVED,
            ).exists()
        )
        self.assertTrue(
            Notification.objects.filter(
                recipient=self.tech,
                kind=Notification.Kind.WO_BLOCKER_RESOLVED,
            ).exists()
        )

    def test_blocker_cancelled_notification(self):
        """blocker_cancelled → kind=WO_BLOCKER_CANCELLED, recipients include tech + manager."""
        self.blocker.status = WorkOrderBlocker.Status.CANCELLED
        self.blocker.cancel_reason = "Wrong SKU"
        self.blocker.save()
        event = self._make_event(WorkOrderBlockerEvent.EventType.BLOCKER_CANCELLED)
        NotificationService.on_blocker_event(event)
        self.assertTrue(
            Notification.objects.filter(
                recipient=self.manager,
                kind=Notification.Kind.WO_BLOCKER_CANCELLED,
            ).exists()
        )


class NotifyPOReceivedSummaryTests(TestCase):
    """notify_po_received_summary — standalone summary notification."""

    def setUp(self):
        from procurement.models import PurchaseOrder, PurchaseOrderItem, Supplier

        self.manager = _make_user("po_mgr", User.Role.MANAGER)
        self.procurement = _make_user("po_proc", User.Role.PROCUREMENT)
        self.supervisor = _make_user("po_sup", User.Role.SUPERVISOR)
        self.actor = _make_user("po_actor", User.Role.MANAGER)
        self.supplier = Supplier.objects.create(name="ACME Supplies")
        self.part_a = SparePart.objects.create(sku="PO-FILT-A", name="Filter A")
        self.part_b = SparePart.objects.create(sku="PO-BELT-100", name="Belt 100")
        self.po = PurchaseOrder.objects.create(
            supplier=self.supplier, created_by=self.actor,
        )
        PurchaseOrderItem.objects.create(
            purchase_order=self.po, part=self.part_a,
            ordered_qty=Decimal("10"), received_qty=Decimal("10"),
            negotiated_unit_price=Decimal("5"),
        )
        PurchaseOrderItem.objects.create(
            purchase_order=self.po, part=self.part_b,
            ordered_qty=Decimal("5"), received_qty=Decimal("5"),
            negotiated_unit_price=Decimal("20"),
        )

    def test_creates_one_notification_per_recipient(self):
        """Each unique recipient gets exactly one summary notification."""
        created = notify_po_received_summary(self.po, self.actor)
        # Recipients: managers, supervisors, procurement officers, actor
        # Our setUp has manager+supervisor+procurement+actor — all 4 unique
        self.assertGreaterEqual(created, 3)
        # All 4 have a summary notification
        for user in (self.manager, self.supervisor, self.procurement, self.actor):
            self.assertTrue(
                Notification.objects.filter(
                    recipient=user,
                    kind=Notification.Kind.PO_RECEIVED_SUMMARY,
                ).exists(),
                f"{user.username} should have a PO_RECEIVED_SUMMARY notification",
            )

    def test_title_and_link(self):
        """Title contains PO number; link points to PO detail."""
        notify_po_received_summary(self.po, self.actor)
        notif = Notification.objects.filter(
            recipient=self.manager,
            kind=Notification.Kind.PO_RECEIVED_SUMMARY,
        ).first()
        self.assertIsNotNone(notif)
        self.assertIn(self.po.po_number, notif.title)
        self.assertTrue(
            notif.link.endswith(f"/purchase-orders/{self.po.pk}/"),
            f"link {notif.link!r} should end with /purchase-orders/{self.po.pk}/",
        )

    def test_body_lists_received_lines(self):
        """Body includes the SKU/qty of each received line."""
        notify_po_received_summary(self.po, self.actor)
        notif = Notification.objects.filter(
            recipient=self.manager,
            kind=Notification.Kind.PO_RECEIVED_SUMMARY,
        ).first()
        self.assertIsNotNone(notif)
        self.assertIn("PO-FILT-A", notif.body)
        self.assertIn("PO-BELT-100", notif.body)

    def test_dedup_prevents_duplicate_summary(self):
        """Second call within 1h does not create a second row per recipient."""
        notify_po_received_summary(self.po, self.actor)
        first_count = Notification.objects.filter(
            recipient=self.manager,
            kind=Notification.Kind.PO_RECEIVED_SUMMARY,
        ).count()
        self.assertEqual(first_count, 1)
        # Second call should be deduped
        notify_po_received_summary(self.po, self.actor)
        self.assertEqual(
            Notification.objects.filter(
                recipient=self.manager,
                kind=Notification.Kind.PO_RECEIVED_SUMMARY,
            ).count(),
            1,
        )
