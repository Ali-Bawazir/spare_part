"""Tests for PM Schedule component targeting (level-5 Machine within level-3 Machine)."""
from django.test import TestCase
from django.urls import reverse

from accounts.models import User
from maintenance.models import Machine, PMChecklistItem, PMSchedule, PMTemplate, Site


def _make_user(username="pmcomp_mgr", role=User.Role.MANAGER):
    return User.objects.create_user(username=username, password="x", role=role, is_active=True)


def _setup_asset_hierarchy():
    site = Site.objects.filter(is_default=True).first() or Site.objects.create(
        name="X", is_default=True, is_active=True
    )
    machine = Machine.objects.create(
        name="Test Press",
        qr_code="TEST-PRESS",
        asset_level=3,
        asset_code="TEST-PRESS",
        is_active=True,
        site=site,
    )
    subassembly = Machine.objects.create(
        name="Test Subassembly",
        qr_code="TEST-SUB",
        asset_level=4,
        asset_code="TEST-SUB",
        is_active=True,
        site=site,
        parent=machine,
    )
    component_a = Machine.objects.create(
        name="Component A",
        qr_code="TEST-COMP-A",
        asset_level=5,
        asset_code="TEST-COMP-A",
        is_active=True,
        site=site,
        parent=subassembly,
    )
    component_b = Machine.objects.create(
        name="Component B",
        qr_code="TEST-COMP-B",
        asset_level=5,
        asset_code="TEST-COMP-B",
        is_active=True,
        site=site,
        parent=subassembly,
    )
    other_machine = Machine.objects.create(
        name="Other Press",
        qr_code="OTHER-PRESS",
        asset_level=3,
        asset_code="OTHER-PRESS",
        is_active=True,
        site=site,
    )
    other_component = Machine.objects.create(
        name="Other Component",
        qr_code="OTHER-COMP",
        asset_level=5,
        asset_code="OTHER-COMP",
        is_active=True,
        site=site,
        parent=other_machine,
    )
    return {
        "site": site,
        "machine": machine,
        "subassembly": subassembly,
        "component_a": component_a,
        "component_b": component_b,
        "other_machine": other_machine,
        "other_component": other_component,
    }


def _make_template(code="PM-COMP-001"):
    t = PMTemplate.objects.create(
        code=code,
        title="Component test template",
        estimated_duration_minutes=30,
        priority="medium",
        is_active=True,
    )
    PMChecklistItem.objects.create(template=t, order=1, text="Step 1", is_required=True)
    return t


class PMComponentViewTests(TestCase):
    def setUp(self):
        self.manager = _make_user()
        self.assets = _setup_asset_hierarchy()
        self.template = _make_template()

    def test_form_renders_with_component_dropdown(self):
        """The PM form should show a component dropdown that's optional."""
        self.client.force_login(self.manager)
        r = self.client.get(reverse("pm_create"))
        self.assertEqual(r.status_code, 200)
        body = r.content.decode()
        self.assertIn("Component (optional)", body)
        self.assertIn('id="id_component"', body)

    def test_form_view_includes_component_machine_mapping(self):
        """The view should pass a JSON map of machine → components for the JS filter."""
        self.client.force_login(self.manager)
        r = self.client.get(reverse("pm_create"))
        self.assertIn("components_by_machine_json", r.context)
        mapping = r.context["components_by_machine_json"]
        self.assertIsInstance(mapping, str)
        # Should include our test machine PK + its component names
        self.assertIn(str(self.assets["machine"].pk), mapping)
        self.assertIn("Component A", mapping)
        self.assertIn("Component B", mapping)
        # Should also include the other machine's component
        self.assertIn(str(self.assets["other_machine"].pk), mapping)
        self.assertIn("Other Component", mapping)

    def test_create_schedule_with_component(self):
        """A schedule can be created targeting a specific component."""
        self.client.force_login(self.manager)
        from django.utils import timezone
        from datetime import timedelta
        post = {
            "template": str(self.template.pk),
            "machine": str(self.assets["machine"].pk),
            "component": str(self.assets["component_a"].pk),
            "frequency_type": "monthly",
            "interval": "1",
            "start_date": "2026-06-27",
            "next_due_at": (timezone.now() + timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S"),
            "grace_days": "7",
            "reminder_days_before": "7",
            "trigger_type": "time",
            "is_active": "on",
        }
        r = self.client.post(reverse("pm_create"), data=post)
        self.assertEqual(r.status_code, 302)
        s = PMSchedule.objects.get(template=self.template)
        self.assertEqual(s.machine, self.assets["machine"])
        self.assertEqual(s.component, self.assets["component_a"])

    def test_create_schedule_without_component(self):
        """A schedule can target just the machine (component optional)."""
        self.client.force_login(self.manager)
        from django.utils import timezone
        from datetime import timedelta
        post = {
            "template": str(self.template.pk),
            "machine": str(self.assets["machine"].pk),
            "component": "",
            "frequency_type": "monthly",
            "interval": "1",
            "start_date": "2026-06-27",
            "next_due_at": (timezone.now() + timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S"),
            "grace_days": "7",
            "reminder_days_before": "7",
            "trigger_type": "time",
            "is_active": "on",
        }
        r = self.client.post(reverse("pm_create"), data=post)
        self.assertEqual(r.status_code, 302)
        s = PMSchedule.objects.get(template=self.template)
        self.assertEqual(s.machine, self.assets["machine"])
        self.assertIsNone(s.component)

    def test_deep_link_prefills_and_locks_component(self):
        """`?machine=X&component=Y` should pre-fill the form and lock the asset."""
        self.client.force_login(self.manager)
        r = self.client.get(
            reverse("pm_create"),
            data={
                "machine": self.assets["machine"].pk,
                "component": self.assets["component_a"].pk,
            },
        )
        self.assertEqual(r.status_code, 200)
        # Form should be pre-filled
        self.assertEqual(r.context["form"]["machine"].value(), self.assets["machine"].pk)
        self.assertEqual(r.context["form"]["component"].value(), self.assets["component_a"].pk)
        # Asset lock banner should be present
        self.assertIsNotNone(r.context["locked_asset"])
        # Component field should be disabled
        self.assertTrue(r.context["form"].fields["component"].disabled)
        # Machine field should be disabled
        self.assertTrue(r.context["form"].fields["machine"].disabled)

    def test_deep_link_component_only_walks_to_root_machine(self):
        """If only `?component=X` is given, the view should walk the parent chain
        to find the level-3 machine."""
        self.client.force_login(self.manager)
        r = self.client.get(
            reverse("pm_create"),
            data={"component": self.assets["component_a"].pk},
        )
        form = r.context["form"]
        self.assertEqual(form["machine"].value(), self.assets["machine"].pk)
        self.assertEqual(form["component"].value(), self.assets["component_a"].pk)

    def test_component_from_other_machine_rejected(self):
        """Server-side validation must reject a component that doesn't belong
        to the selected machine."""
        self.client.force_login(self.manager)
        from django.utils import timezone
        from datetime import timedelta
        # component_b belongs to "machine" (Test Press)
        # Try to attach it to "other_machine" (Other Press)
        post = {
            "template": str(self.template.pk),
            "machine": str(self.assets["other_machine"].pk),
            "component": str(self.assets["component_b"].pk),
            "frequency_type": "monthly",
            "interval": "1",
            "start_date": "2026-06-27",
            "next_due_at": (timezone.now() + timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S"),
            "grace_days": "7",
            "reminder_days_before": "7",
            "trigger_type": "time",
            "is_active": "on",
        }
        r = self.client.post(reverse("pm_create"), data=post)
        self.assertEqual(r.status_code, 200)  # Form re-rendered with error
        form = r.context["form"]
        self.assertIn("component", form.errors)
        # Verify NO schedule was created
        self.assertEqual(PMSchedule.objects.count(), 0)
