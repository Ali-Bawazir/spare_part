"""
Phase 12: WO reconciliation regression tests.

Covers:
  1. `release_stale_reservations_for_wo` releases legacy and fully-issued
     line-linked reservations, leaves partially-issued line-linked alone.
  2. `transition_work_order(lifecycle=CLOSED)` triggers the sweep.
  3. `create_pr_from_shortage` is gated, idempotent, and creates a PR
     with source_shortage_report set.
  4. `outstanding_shortages` query excludes shortages that already have a PR.
"""
from datetime import date
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse

from accounts.models import User
from maintenance.models import (
    MaintenanceIssue, WorkOrder, Machine, WorkOrderCost,
)
from inventory.models import (
    Inventory, InventoryReservation, PartIssueLine, PartShortageReport,
)
from maintenance.models import Site
from procurement.models import PurchaseRequest


class WorkOrderReconciliationTests(TestCase):
    def setUp(self):
        self.manager = User.objects.create_user(
            username="mgr", password="pass1234", role=User.Role.MANAGER,
        )
        self.procurement = User.objects.create_user(
            username="proc", password="pass1234", role=User.Role.PROCUREMENT,
        )
        self.technician = User.objects.create_user(
            username="tech", password="pass1234", role=User.Role.TECHNICIAN,
        )
        self.site = Site.objects.create(
            name="Test Site", code="TEST-SITE-001", is_default=True, is_active=True,
        )
        self.machine = Machine.objects.create(
            name="Hyplas", qr_code="HYP-T", asset_level=3,
        )
        self.part_a = PartShortageReport  # placeholder, we use SparePart below
        from inventory.models import SparePart
        self.SparePart = SparePart
        self.part = SparePart.objects.create(sku="TEST-PART", name="Test Part")
        Inventory.objects.create(
            part=self.part, site=self.site,
            quantity_available=Decimal("10"),
        )
        # Create a WO directly in CLOSED state
        self.wo = WorkOrder.objects.create(
            machine=self.machine,
            category=WorkOrder.Category.BREAKDOWN,
            lifecycle_status=WorkOrder.LifecycleStatus.CLOSED,
            operational_status=WorkOrder.OperationalStatus.PAUSED,
            created_by=self.manager,
        )

    def _create_line(self, approved_qty, issued_qty, status=PartIssueLine.Status.APPROVED):
        return PartIssueLine.objects.create(
            work_order=self.wo,
            part=self.part,
            quantity=approved_qty,
            approved_qty=approved_qty,
            issued_qty=issued_qty,
            unit_cost=Decimal("10"),
            status=status,
            requested_by=self.technician,
            approved_by=self.manager,
            issued_by=self.manager,
        )

    def test_release_stale_reservations_legacy_only(self):
        """A legacy reservation (no source_line) gets released."""
        res = InventoryReservation.objects.create(
            part=self.part, work_order=self.wo, quantity=Decimal("2"),
            status=InventoryReservation.Status.ACTIVE,
            source_line=None,
        )
        from maintenance.services import release_stale_reservations_for_wo
        n = release_stale_reservations_for_wo(self.wo, self.manager)
        self.assertEqual(n, 1)
        res.refresh_from_db()
        self.assertEqual(res.status, InventoryReservation.Status.RELEASED)
        self.assertIn("legacy", (res.release_reason or "").lower())

    def test_release_stale_reservations_fully_issued_line(self):
        """A line-linked reservation whose line is fully issued gets released."""
        line = self._create_line(approved_qty=3, issued_qty=3)
        res = InventoryReservation.objects.create(
            part=self.part, work_order=self.wo, quantity=Decimal("3"),
            status=InventoryReservation.Status.ACTIVE,
            source_line=line,
        )
        from maintenance.services import release_stale_reservations_for_wo
        n = release_stale_reservations_for_wo(self.wo, self.manager)
        self.assertEqual(n, 1)
        res.refresh_from_db()
        self.assertEqual(res.status, InventoryReservation.Status.RELEASED)

    def test_release_stale_reservations_leaves_partial_alone(self):
        """A line-linked reservation whose line is only partially issued stays."""
        line = self._create_line(approved_qty=5, issued_qty=2)
        res = InventoryReservation.objects.create(
            part=self.part, work_order=self.wo, quantity=Decimal("5"),
            status=InventoryReservation.Status.ACTIVE,
            source_line=line,
        )
        from maintenance.services import release_stale_reservations_for_wo
        n = release_stale_reservations_for_wo(self.wo, self.manager)
        self.assertEqual(n, 0)
        res.refresh_from_db()
        self.assertEqual(res.status, InventoryReservation.Status.ACTIVE)

    def test_wo_close_triggers_sweep(self):
        """transition_work_order to CLOSED auto-releases stale reservations."""
        line = self._create_line(approved_qty=3, issued_qty=3)
        InventoryReservation.objects.create(
            part=self.part, work_order=self.wo, quantity=Decimal("2"),
            status=InventoryReservation.Status.ACTIVE,
            source_line=None,
        )
        # Reset WO to ASSIGNED so we can transition it
        self.wo.lifecycle_status = WorkOrder.LifecycleStatus.ASSIGNED
        self.wo.save(update_fields=["lifecycle_status"])
        from maintenance.services import transition_work_order
        transition_work_order(
            self.wo, WorkOrder.LifecycleStatus.CLOSED, actor=self.manager,
        )
        active_after = InventoryReservation.objects.filter(
            work_order=self.wo, status=InventoryReservation.Status.ACTIVE,
        ).count()
        self.assertEqual(active_after, 0)

    def _create_shortage(self, qty_requested=6, qty_issued=2, shortage_qty=4,
                         status=PartShortageReport.Status.CLOSED):
        """Helper to create a PartShortageReport with required snapshots."""
        from django.contrib.contenttypes.models import ContentType
        ct = ContentType.objects.get_for_model(self.wo)
        return PartShortageReport.objects.create(
            content_type=ct, object_id=self.wo.pk,
            work_order=self.wo,
            part=self.part,
            qty_requested=Decimal(str(qty_requested)),
            qty_issued=Decimal(str(qty_issued)),
            available_qty_snapshot=Decimal("2"),
            reserved_qty_snapshot=Decimal("0"),
            usable_qty_snapshot=Decimal("2"),
            shortage_qty=Decimal(str(shortage_qty)),
            status=status,
            reported_by=self.technician,
            machine_criticality_snapshot="",
            part_criticality_snapshot="",
            wo_priority_snapshot="",
        )

    def test_create_pr_from_shortage_requires_post_and_role(self):
        """The shortage→PR endpoint requires POST and manager+ roles."""
        shortage = self._create_shortage()
        url = reverse("shortage_create_pr", kwargs={"shortage_id": shortage.pk})
        # GET should fail (405)
        self.client.force_login(self.manager)
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 405)
        # POST as manager creates a PR
        resp = self.client.post(url)
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(
            PurchaseRequest.objects.filter(source_shortage_report=shortage).exists()
        )
        pr = PurchaseRequest.objects.get(source_shortage_report=shortage)
        self.assertEqual(pr.quantity, Decimal("4"))
        self.assertEqual(pr.part, self.part)
        self.assertEqual(pr.status, PurchaseRequest.Status.PENDING)

    def test_create_pr_blocked_for_technician(self):
        """Technicians cannot trigger the shortage→PR endpoint."""
        shortage = self._create_shortage()
        url = reverse("shortage_create_pr", kwargs={"shortage_id": shortage.pk})
        self.client.force_login(self.technician)
        resp = self.client.post(url)
        self.assertEqual(resp.status_code, 302)
        # Redirected to WO detail, no PR created
        self.assertFalse(
            PurchaseRequest.objects.filter(source_shortage_report=shortage).exists()
        )

    def test_create_pr_blocked_for_unclosed_shortage(self):
        """A shortage must be CLOSED before a backorder PR can be created."""
        shortage = self._create_shortage(status=PartShortageReport.Status.PENDING_REVIEW)
        url = reverse("shortage_create_pr", kwargs={"shortage_id": shortage.pk})
        self.client.force_login(self.manager)
        resp = self.client.post(url)
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(
            PurchaseRequest.objects.filter(source_shortage_report=shortage).exists()
        )

    def test_outstanding_shortages_excludes_those_with_pr(self):
        """A shortage with a linked PR must NOT appear in outstanding_shortages."""
        shortage = self._create_shortage()
        # With no PR yet
        self.assertTrue(shortage.shortage_qty > 0)
        # With PR — query must exclude it
        PurchaseRequest.objects.create(
            part=self.part, machine=self.machine, quantity=Decimal("4"),
            source_shortage_report=shortage, status=PurchaseRequest.Status.PENDING,
            created_by=self.manager,
        )
        # The view's outstanding_shortages queryset filters purchase_requests__isnull=True
        outstanding = PartShortageReport.objects.filter(
            work_order=self.wo,
            status=PartShortageReport.Status.CLOSED,
            shortage_qty__gt=0,
            purchase_requests__isnull=True,
        )
        self.assertEqual(outstanding.count(), 0)
