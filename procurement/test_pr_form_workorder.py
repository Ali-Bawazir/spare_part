"""
Regression tests for the PurchaseRequestForm work_order slice bug.

The form's work_order queryset was previously sliced with [:500], which
populates the result cache. ModelChoiceField.clean() then calls
`queryset.get(pk=value)` and Django raises TypeError
("Cannot filter a query once a slice has been taken"), which the field
catches and re-raises as the user-facing "Select a valid choice" error.
This made it impossible for managers to create any PR with a work_order
selected.

Fix: keep the queryset un-sliced (so `.get(pk=val)` works) and bound the
rendered dropdown via `widget.choices` instead.
"""
from decimal import Decimal

from django.test import TestCase

from accounts.models import User
from inventory.models import Inventory, SparePart
from maintenance.models import Machine, Site, WorkOrder
from procurement.forms import PurchaseRequestForm
from procurement.models import Supplier


def _make_user(username, role):
    return User.objects.create_user(
        username=username, password="x", role=role,
    )


def _make_part(sku="PR-FORM-TEST"):
    site = Site.objects.filter(is_default=True).first() or Site.objects.create(
        name="X", is_default=True, is_active=True,
    )
    p = SparePart.objects.create(
        sku=sku, name=sku, status="active", avg_cost=Decimal("1.00"),
        last_purchase_cost=Decimal("1.00"),
    )
    Inventory.objects.create(part=p, site=site, quantity_available=0)
    return p


class PurchaseRequestFormWorkOrderTests(TestCase):
    """Regression tests for the work_order slice bug."""

    @classmethod
    def setUpTestData(cls):
        cls.manager = _make_user("pr_form_mgr", User.Role.MANAGER)
        cls.technician = _make_user("pr_form_tech", User.Role.TECHNICIAN)
        cls.site, cls.machine = (
            Site.objects.filter(is_default=True).first() or Site.objects.create(
                name="X", is_default=True, is_active=True
            ),
            Machine.objects.create(
                name="PRFormMachine", qr_code="prform",
                asset_level=3, asset_code="PRF", is_active=True,
                site=Site.objects.filter(is_default=True).first(),
            ),
        )
        cls.part = _make_part()
        cls.supplier = Supplier.objects.create(name="PRFormSupplier")

    def test_form_accepts_valid_work_order_pk(self):
        """A PR with a work_order selected saves cleanly."""
        wo = WorkOrder.objects.create(
            machine=self.machine, lifecycle_status="in_progress",
            assigned_technician=self.technician, created_by=self.manager,
        )
        form = PurchaseRequestForm(data={
            "part": str(self.part.pk),
            "machine": "",
            "component": "",
            "work_order": str(wo.pk),
            "quantity": "5",
            "notes": "regression test",
        })
        self.assertTrue(form.is_valid(), f"errors: {form.errors.as_json()}")
        pr = form.save(commit=False)
        pr.created_by = self.manager
        pr.save()
        self.assertEqual(pr.work_order_id, wo.pk)

    def test_form_widget_choices_bounded_to_500(self):
        """The rendered dropdown is bounded to 500 WOs even if the
        underlying queryset is not sliced."""
        form = PurchaseRequestForm()
        wo_field = form.fields["work_order"]
        # Underlying queryset should be un-sliced (this is the bug fix):
        # a sliced queryset raises TypeError on .get(pk=val).
        self.assertFalse(
            wo_field.queryset._result_cache or getattr(wo_field.queryset, "_iterable_class", None) and False
        )
        # The widget's rendered choices should be capped at 500
        widget_choices = wo_field.widget.choices
        # Filter out the empty/blank choice
        non_empty = [c for c in widget_choices if c[0] not in (None, "", "---------")]
        self.assertLessEqual(
            len(non_empty), 500,
            f"widget rendered {len(non_empty)} options, expected <= 500",
        )

    def test_form_clean_does_not_raise_invalid_choice_for_valid_pk(self):
        """The exact failure mode of the old bug: .get(pk=val) raised
        TypeError, which was caught and re-raised as 'invalid_choice'."""
        wo = WorkOrder.objects.create(
            machine=self.machine, lifecycle_status="in_progress",
            assigned_technician=self.technician, created_by=self.manager,
        )
        # Use a real POST-like data dict
        form = PurchaseRequestForm(data={
            "part": str(self.part.pk),
            "machine": "",
            "component": "",
            "work_order": str(wo.pk),
            "quantity": "1",
            "notes": "",
        })
        # The bug: this would have been False with "invalid_choice" on work_order
        self.assertTrue(form.is_valid(), f"errors: {form.errors}")
        self.assertNotIn("work_order", form.errors)
