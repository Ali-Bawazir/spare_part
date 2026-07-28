"""Regression tests for PM template formset hidden id field bug.

Phase 1 shipped a template form whose formset loop omitted `{% for hidden in
f.hidden_fields %}`, so on edit the existing rows' `id` was missing from the
submitted POST and the formset failed validation. Result: no updates, no new
items — nothing saved. The fix renders the hidden fields explicitly and adds
a JS "Add another item" button so users can grow the checklist beyond `extra=3`.
"""
from django.test import TestCase
from django.urls import reverse

from accounts.models import User
from maintenance.models import Machine, PMChecklistItem, PMTemplate, PMSchedule, Site, WorkOrder


def _make_user(username="formset_mgr", role=User.Role.MANAGER):
    return User.objects.create_user(username=username, password="x", role=role, is_active=True)


def _make_template(code="PM-FORMSET-001"):
    t = PMTemplate.objects.create(
        code=code,
        title="Formset Test Template",
        estimated_duration_minutes=30,
        priority="medium",
        is_active=True,
    )
    PMChecklistItem.objects.create(template=t, order=1, text="Existing 1", is_required=True)
    PMChecklistItem.objects.create(template=t, order=2, text="Existing 2", is_required=True)
    PMChecklistItem.objects.create(template=t, order=3, text="Existing 3", is_required=True)
    return t


def _post_data(template, include_new_items=True, new_items_text=None):
    """Build POST data simulating a user editing a template with extra items."""
    new_items_text = new_items_text or ["New item 4", "New item 5", "New item 6"]
    data = {
        "code": template.code,
        "title": template.title,
        "description": template.description,
        "estimated_duration_minutes": str(template.estimated_duration_minutes),
        "priority": template.priority,
        "requires_manager_review": "on",
        "is_active": "on",
        "checklist_items-TOTAL_FORMS": "6",
        "checklist_items-INITIAL_FORMS": "3",
        "checklist_items-MIN_NUM_FORMS": "0",
        "checklist_items-MAX_NUM_FORMS": "1000",
        "checklist_items-0-id": str(template.checklist_items.get(order=1).pk),
        "checklist_items-0-order": "1",
        "checklist_items-0-text": "Existing 1 (edited)",
        "checklist_items-0-is_required": "on",
        "checklist_items-1-id": str(template.checklist_items.get(order=2).pk),
        "checklist_items-1-order": "2",
        "checklist_items-1-text": "Existing 2",
        "checklist_items-1-is_required": "on",
        "checklist_items-2-id": str(template.checklist_items.get(order=3).pk),
        "checklist_items-2-order": "3",
        "checklist_items-2-text": "Existing 3",
        "checklist_items-2-is_required": "on",
    }
    if include_new_items:
        for i, text in enumerate(new_items_text, start=3):
            data[f"checklist_items-{i}-order"] = str(i + 1)
            data[f"checklist_items-{i}-text"] = text
            data[f"checklist_items-{i}-is_required"] = "on"
    return data


class PMTemplateFormsetFixTests(TestCase):
    def setUp(self):
        self.manager = _make_user()
        self.template = _make_template()

    def test_existing_item_text_update_persists(self):
        """Regression: editing text of an existing item used to silently fail
        because the form template was missing the `{{ f.id }}` hidden field."""
        post = _post_data(self.template, include_new_items=False)
        self.client.force_login(self.manager)
        r = self.client.post(
            reverse("pm_template_edit", args=[self.template.pk]),
            data=post,
        )
        if r.status_code != 302:
            formset = r.context.get("formset") if hasattr(r, "context") else None
            if formset:
                print("Formset errors:", formset.errors)
                print("Formset non_form_errors:", formset.non_form_errors())
                for f in formset.forms:
                    if f.errors:
                        print(f"  Form errors: {f.errors}")
        self.assertEqual(r.status_code, 302)
        self.template.refresh_from_db()
        edited = self.template.checklist_items.get(order=1)
        self.assertEqual(edited.text, "Existing 1 (edited)")

    def test_new_items_save_alongside_existing_edits(self):
        """Regression: previously, the entire formset was rejected so even
        edits to existing items were lost. With the fix, both edits and
        new items persist in a single POST."""
        post = _post_data(self.template, include_new_items=True)
        self.client.force_login(self.manager)
        r = self.client.post(
            reverse("pm_template_edit", args=[self.template.pk]),
            data=post,
        )
        self.assertEqual(r.status_code, 302)
        self.template.refresh_from_db()
        items = list(self.template.checklist_items.all().order_by("order"))
        self.assertEqual(len(items), 6)
        self.assertEqual(items[0].text, "Existing 1 (edited)")
        self.assertEqual(items[3].text, "New item 4")
        self.assertEqual(items[4].text, "New item 5")
        self.assertEqual(items[5].text, "New item 6")

    def test_empty_extra_rows_are_ignored(self):
        """Empty extra rows (no text) are stripped before save — they should
        not produce blank checklist items in the database."""
        post = _post_data(self.template, include_new_items=True,
                          new_items_text=["Real new item", "", ""])
        self.client.force_login(self.manager)
        r = self.client.post(
            reverse("pm_template_edit", args=[self.template.pk]),
            data=post,
        )
        self.assertEqual(r.status_code, 302)
        self.template.refresh_from_db()
        items = list(self.template.checklist_items.all().order_by("order"))
        self.assertEqual(len(items), 4)
        self.assertEqual(items[3].text, "Real new item")

    def test_duplicates_rejected(self):
        """Two items with the same text (case-insensitive) should fail validation."""
        post = _post_data(self.template, include_new_items=True,
                          new_items_text=["Existing 1 (edited)", "Another", "Another"])
        self.client.force_login(self.manager)
        r = self.client.post(
            reverse("pm_template_edit", args=[self.template.pk]),
            data=post,
        )
        self.assertEqual(r.status_code, 200)
        formset = r.context["formset"]
        text_errors = []
        for f in formset.forms:
            if "text" in f.errors:
                text_errors.extend(f.errors["text"])
        self.assertTrue(any("Duplicate" in e for e in text_errors),
                        f"Expected a duplicate-text error, got: {text_errors}")

    def test_all_empty_checklist_rejected(self):
        """A template with no checklist items should be rejected (at least one required)."""
        template2 = PMTemplate.objects.create(
            code="PM-EMPTY-001",
            title="Empty",
            estimated_duration_minutes=30,
            priority="medium",
        )
        # All 3 extra rows empty
        data = {
            "code": template2.code, "title": template2.title, "description": "",
            "estimated_duration_minutes": "30", "priority": "medium",
            "requires_manager_review": "on", "is_active": "on",
            "checklist_items-TOTAL_FORMS": "3",
            "checklist_items-INITIAL_FORMS": "0",
            "checklist_items-MIN_NUM_FORMS": "0",
            "checklist_items-MAX_NUM_FORMS": "1000",
            "checklist_items-0-order": "0", "checklist_items-0-text": "",
            "checklist_items-0-is_required": "on",
            "checklist_items-1-order": "0", "checklist_items-1-text": "",
            "checklist_items-1-is_required": "on",
            "checklist_items-2-order": "0", "checklist_items-2-text": "",
            "checklist_items-2-is_required": "on",
        }
        self.client.force_login(self.manager)
        r = self.client.post(reverse("pm_template_edit", args=[template2.pk]), data=data)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.context["formset"].non_form_errors() or
                        any("at least one" in str(e).lower()
                            for e in r.context["formset"].non_form_errors() +
                            [form.errors for form in r.context["formset"].forms]))


class PMTemplateFormsetDeleteTests(TestCase):
    def setUp(self):
        self.manager = _make_user(username="del_mgr")
        self.template = _make_template(code="PM-DEL-001")

    def test_delete_marks_existing_for_deletion(self):
        """Toggling the DELETE checkbox on an existing row removes it on save."""
        existing_pk = self.template.checklist_items.get(order=2).pk
        data = {
            "code": self.template.code, "title": self.template.title,
            "description": self.template.description,
            "estimated_duration_minutes": "30", "priority": "medium",
            "requires_manager_review": "on", "is_active": "on",
            "checklist_items-TOTAL_FORMS": "3",
            "checklist_items-INITIAL_FORMS": "3",
            "checklist_items-MIN_NUM_FORMS": "0",
            "checklist_items-MAX_NUM_FORMS": "1000",
            "checklist_items-0-id": str(self.template.checklist_items.get(order=1).pk),
            "checklist_items-0-order": "1",
            "checklist_items-0-text": "Existing 1",
            "checklist_items-0-is_required": "on",
            "checklist_items-1-id": str(existing_pk),
            "checklist_items-1-order": "2",
            "checklist_items-1-text": "Existing 2",
            "checklist_items-1-is_required": "on",
            "checklist_items-1-DELETE": "on",
            "checklist_items-2-id": str(self.template.checklist_items.get(order=3).pk),
            "checklist_items-2-order": "3",
            "checklist_items-2-text": "Existing 3",
            "checklist_items-2-is_required": "on",
        }
        self.client.force_login(self.manager)
        r = self.client.post(reverse("pm_template_edit", args=[self.template.pk]), data=data)
        self.assertEqual(r.status_code, 302)
        self.template.refresh_from_db()
        self.assertEqual(self.template.checklist_items.count(), 2)
        self.assertFalse(PMChecklistItem.objects.filter(pk=existing_pk).exists())
