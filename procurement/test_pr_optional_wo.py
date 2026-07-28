"""
Regression tests for PurchaseRequestForm — work_order must be optional
on the standalone `/procurement/new/` page so stock-only PRs save cleanly.
"""
from django.test import TestCase
from django.urls import reverse

from accounts.models import User
from inventory.models import SparePart
from procurement.models import PurchaseRequest


class PurchaseRequestOptionalWOTests(TestCase):
    def setUp(self):
        self.manager = User.objects.create_user(
            username="mgr", password="pass1234", role=User.Role.MANAGER,
        )
        self.part = SparePart.objects.create(
            sku="TEST-PART-001", name="Test part",
        )

    def test_create_pr_form_renders_empty_option_for_work_order(self):
        """The Work Order dropdown must include a blank option so users can
        save a stock-only PR without selecting a WO."""
        self.client.force_login(self.manager)
        resp = self.client.get(reverse("purchase_create"))
        self.assertEqual(resp.status_code, 200)
        # The blank option should appear in the rendered HTML.
        self.assertContains(resp, '<option value=""', html=False)

    def test_create_pr_without_work_order_succeeds(self):
        """POSTing a PR with work_order='' should save successfully
        with work_order=NULL — no 500, no error."""
        self.client.force_login(self.manager)
        resp = self.client.post(
            reverse("purchase_create"),
            data={
                "part": self.part.pk,
                "quantity": "5",
                "notes": "Stock replenishment — no specific WO",
                "work_order": "",        # blank on purpose
                "machine": "",
                "component": "",
            },
            follow=True,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(
            PurchaseRequest.objects.filter(
                work_order__isnull=True,
                notes__icontains="Stock",
            ).exists()
        )

    def test_create_pr_with_work_order_still_works(self):
        """POSTing a PR with work_order=<pk> should also still work
        (regression: don't break the existing flow)."""
        from maintenance.models import WorkOrder, Machine
        m = Machine.objects.create(name="TestMC", qr_code="TMC-01", asset_level=3)
        wo = WorkOrder.objects.create(
            machine=m,
            category=WorkOrder.Category.BREAKDOWN,
            lifecycle_status=WorkOrder.LifecycleStatus.ASSIGNED,
            created_by=self.manager,
        )
        self.client.force_login(self.manager)
        resp = self.client.post(
            reverse("purchase_create"),
            data={
                "part": self.part.pk,
                "quantity": "3",
                "notes": "",
                "work_order": str(wo.pk),
                "machine": str(m.pk),
                "component": "",
            },
            follow=True,
        )
        self.assertEqual(resp.status_code, 200)
        pr = PurchaseRequest.objects.filter(work_order=wo).first()
        self.assertIsNotNone(pr)
        self.assertEqual(pr.work_order_id, wo.pk)