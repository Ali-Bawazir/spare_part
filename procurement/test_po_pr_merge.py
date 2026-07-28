"""Regression test — PR merge in `purchase_order_create`.

Bug: When two PENDING PRs for the same part (different WOs) were selected
during PO creation, the second PR's quantity was silently dropped from the
PO line item — the loop short-circuited on `existing` and never added the
second PR's qty. All PRs were still flipped to `converted_to_po`, leaving
the shortfall unbookable.

Fix: when the second PR for the same part comes in, sum `ordered_qty`
and recompute `total_price` on the existing line item, then continue to
link both PRs to the PO as before.
"""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from inventory.models import SparePart
from maintenance.models import Machine, Site, WorkOrder
from procurement.models import PurchaseOrder, PurchaseOrderItem, PurchaseRequest, Supplier


User = get_user_model()


class PurchaseOrderCreatePRMergeTests(TestCase):
    """Two PRs for the same part must collapse into one PO line whose
    quantity equals the sum of both PRs' quantities."""

    def setUp(self):
        self.procurement = User.objects.create_user(
            username="pro_merge", password="x", role=User.Role.PROCUREMENT,
        )
        self.manager = User.objects.create_user(
            username="mgr_merge", password="x", role=User.Role.MANAGER,
        )
        site = Site.objects.filter(is_default=True).first() or Site.objects.create(
            name="MergeSite", is_default=True, is_active=True,
        )
        self.part = SparePart.objects.create(
            sku="MERGE-001", name="Merge Test Part",
            avg_cost=Decimal("10"), last_purchase_cost=Decimal("10"),
        )
        mach = Machine.objects.create(
            name="MC-MERGE", qr_code="mcmrg", asset_level=3,
            asset_code="MC-MERGE", is_active=True, site=site,
        )
        self.wo_a = WorkOrder.objects.create(
            machine=mach, category=WorkOrder.Category.BREAKDOWN,
            lifecycle_status=WorkOrder.LifecycleStatus.ASSIGNED,
            created_by=self.manager,
        )
        self.wo_b = WorkOrder.objects.create(
            machine=mach, category=WorkOrder.Category.BREAKDOWN,
            lifecycle_status=WorkOrder.LifecycleStatus.ASSIGNED,
            created_by=self.manager,
        )
        self.supplier = Supplier.objects.create(name="MergeTestSupplier", code="MERGE-S")
        self.pr_a = PurchaseRequest.objects.create(
            part=self.part, quantity=Decimal("3.000"),
            work_order=self.wo_a, created_by=self.manager,
            status=PurchaseRequest.Status.PENDING,
            unit_price=Decimal("10"),
        )
        self.pr_b = PurchaseRequest.objects.create(
            part=self.part, quantity=Decimal("2.000"),
            work_order=self.wo_b, created_by=self.manager,
            status=PurchaseRequest.Status.PENDING,
            unit_price=Decimal("10"),
        )

    def test_two_prs_same_part_sum_into_one_line(self):
        self.client.force_login(self.procurement)
        resp = self.client.post(
            reverse("purchase_order_create"),
            data={
                "supplier": str(self.supplier.pk),
                "status": PurchaseOrder.Status.DRAFT,
                "items-TOTAL_FORMS": "0",
                "items-INITIAL_FORMS": "0",
                "items-MIN_NUM_FORMS": "0",
                "items-MAX_NUM_FORMS": "1000",
                "selected_prs": [str(self.pr_a.pk), str(self.pr_b.pk)],
            },
            follow=True,
        )
        self.assertEqual(resp.status_code, 200)
        items = list(PurchaseOrderItem.objects.filter(part=self.part))
        self.assertEqual(len(items), 1, "same-part PRs must collapse into one PO line")
        line = items[0]
        self.assertEqual(line.ordered_qty, Decimal("5.000"),
                         "line qty must equal pr_a + pr_b = 3 + 2")
        self.assertEqual(line.total_price, Decimal("50.0000"),
                         "line total_price must be qty * negotiated_unit_price")
        for pr in (self.pr_a, self.pr_b):
            pr.refresh_from_db()
            self.assertEqual(pr.status, PurchaseRequest.Status.CONVERTED_TO_PO)
            self.assertIsNotNone(pr.purchase_order)
