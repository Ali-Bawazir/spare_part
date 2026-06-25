"""
Phase 2 — Supplier analytics dashboard tests.

Covers:
- view renders for manager (200 + content)
- view forbidden for technician (403)
- view respects ?days filter
- total_spend = sum(received_qty * actual_unit_price) per supplier
- on_time_rate reflects received_at within expected_delivery (or fallback window)
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import User
from inventory.models import SparePart
from maintenance.models import Site
from procurement.models import (
    PurchaseOrder,
    PurchaseOrderItem,
    Supplier,
)


def _make_user(username: str, role: str) -> User:
    return User.objects.create_user(username=username, password="pass1234", role=role)


def _make_part(sku: str) -> SparePart:
    return SparePart.objects.create(sku=sku, name=sku)


def _make_po(*, created_by, supplier, items=None, created_at=None, expected_delivery=None, received_at=None, status=PurchaseOrder.Status.RECEIVED):
    po = PurchaseOrder.objects.create(created_by=created_by, supplier=supplier, status=status)
    if created_at:
        po.created_at = created_at
    if expected_delivery is not None:
        po.expected_delivery = expected_delivery
    if received_at is not None:
        po.received_at = received_at
    po.save()
    for it in items or []:
        PurchaseOrderItem.objects.create(
            purchase_order=po,
            part=it["part"],
            ordered_qty=it["ordered_qty"],
            received_qty=it["received_qty"],
            negotiated_unit_price=it.get("negotiated_unit_price", Decimal("10.00")),
            actual_unit_price=it.get("actual_unit_price", it.get("negotiated_unit_price", Decimal("10.00"))),
        )
    return po


class SupplierAnalyticsTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.url = reverse("supplier_analytics")
        cls.manager = _make_user("sa_mgr", User.Role.MANAGER)
        cls.procurement = _make_user("sa_proc", User.Role.PROCUREMENT)
        cls.technician = _make_user("sa_tech", User.Role.TECHNICIAN)

    def test_view_renders_for_manager(self):
        self.client.force_login(self.manager)
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Supplier analytics")
        self.assertContains(resp, "Total spend")

    def test_view_renders_for_procurement(self):
        self.client.force_login(self.procurement)
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 200)

    def test_view_forbidden_for_technician(self):
        self.client.force_login(self.technician)
        resp = self.client.get(self.url)
        # 403 from role_required decorator
        self.assertIn(resp.status_code, (302, 403))

    def test_view_respects_days_filter(self):
        supplier = Supplier.objects.create(name="Old supplier", code="OLD")
        part = _make_part("OLD-1")
        now = timezone.now()
        _make_po(
            created_by=self.manager,
            supplier=supplier,
            items=[{"part": part, "ordered_qty": Decimal("5"), "received_qty": Decimal("5"),
                    "actual_unit_price": Decimal("10.00")}],
            created_at=now - timedelta(days=100),
            received_at=now - timedelta(days=99),
        )
        self.client.force_login(self.manager)
        resp_30 = self.client.get(self.url + "?days=30")
        self.assertEqual(resp_30.status_code, 200)
        self.assertContains(resp_30, "No supplier activity")

        resp_365 = self.client.get(self.url + "?days=365")
        self.assertEqual(resp_365.status_code, 200)
        self.assertNotContains(resp_365, "No supplier activity")

    def test_calculates_total_spend(self):
        supplier_a = Supplier.objects.create(name="Supplier A", code="A")
        supplier_b = Supplier.objects.create(name="Supplier B", code="B")
        part1 = _make_part("SP-A-1")
        part2 = _make_part("SP-B-1")
        now = timezone.now()
        # A: 10 * 5 = 50
        _make_po(
            created_by=self.manager, supplier=supplier_a,
            items=[{"part": part1, "ordered_qty": Decimal("10"), "received_qty": Decimal("10"),
                    "actual_unit_price": Decimal("5.00")}],
            created_at=now - timedelta(days=10),
            received_at=now - timedelta(days=5),
        )
        # B: 4 * 25 = 100
        _make_po(
            created_by=self.manager, supplier=supplier_b,
            items=[{"part": part2, "ordered_qty": Decimal("4"), "received_qty": Decimal("4"),
                    "actual_unit_price": Decimal("25.00")}],
            created_at=now - timedelta(days=10),
            received_at=now - timedelta(days=5),
        )
        self.client.force_login(self.manager)
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 200)
        rows = resp.context["rows"]
        self.assertEqual(len(rows), 2)
        # Sorted by spend DESC: B (100) first, A (50) second
        self.assertEqual(rows[0]["supplier"].code, "B")
        self.assertEqual(rows[0]["total_spend"], Decimal("100.00"))
        self.assertEqual(rows[1]["supplier"].code, "A")
        self.assertEqual(rows[1]["total_spend"], Decimal("50.00"))
        self.assertEqual(resp.context["totals"]["spend"], Decimal("150.00"))

    def test_calculates_on_time_rate(self):
        supplier = Supplier.objects.create(name="Timing Co", code="TIM")
        part = _make_part("TIM-1")
        now = timezone.now()
        # On-time: expected_delivery set, received_at on or before it
        _make_po(
            created_by=self.manager, supplier=supplier,
            items=[{"part": part, "ordered_qty": Decimal("2"), "received_qty": Decimal("2"),
                    "actual_unit_price": Decimal("10.00")}],
            created_at=now - timedelta(days=10),
            expected_delivery=now,
            received_at=now - timedelta(days=1),
        )
        # Late: expected_delivery in the past, received_at after
        _make_po(
            created_by=self.manager, supplier=supplier,
            items=[{"part": part, "ordered_qty": Decimal("3"), "received_qty": Decimal("3"),
                    "actual_unit_price": Decimal("10.00")}],
            created_at=now - timedelta(days=30),
            expected_delivery=now - timedelta(days=20),
            received_at=now - timedelta(days=5),
        )
        self.client.force_login(self.manager)
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 200)
        rows = resp.context["rows"]
        self.assertEqual(len(rows), 1)
        # 1 of 2 on-time = 50%
        self.assertAlmostEqual(rows[0]["on_time_rate"], 50.0, delta=0.5)