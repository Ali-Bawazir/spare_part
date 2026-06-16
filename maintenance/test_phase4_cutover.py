"""
Phase 4 — Cutover from legacy UI/fields to the new blocker system.

Covers:
- The "Waiting for parts" and "Waiting for vendor" buttons are removed
  from the technician actions template.
- pause_reason and pause_note are read-only in WorkOrderAdmin.
"""
from __future__ import annotations

from django.test import TestCase
from django.urls import reverse

from accounts.models import User
from maintenance.admin import WorkOrderAdmin
from maintenance.models import Machine, WorkOrder


def _make_user(username: str, role: str) -> User:
    return User.objects.create_user(username=username, password="x", role=role)


def _make_wo(
    *,
    machine: Machine = None,
    created_by: User,
    assigned_technician: User = None,
    status: str = WorkOrder.Status.IN_PROGRESS,
    lifecycle_status: str = WorkOrder.LifecycleStatus.IN_PROGRESS,
    **kwargs,
) -> WorkOrder:
    defaults = {
        "machine": machine,
        "created_by": created_by,
        "status": status,
        "lifecycle_status": lifecycle_status,
        "assigned_technician": assigned_technician,
    }
    defaults.update(kwargs)
    return WorkOrder.objects.create(**defaults)


class MarkButtonsRemovedTests(TestCase):
    """The old "Waiting for parts" / "Waiting for vendor" buttons are removed."""

    def setUp(self):
        self.manager = _make_user("manager_p4", User.Role.MANAGER)
        self.tech = _make_user("tech_p4", User.Role.TECHNICIAN)
        self.wo = _make_wo(
            machine=None, created_by=self.manager,
            assigned_technician=self.tech,
            status=WorkOrder.Status.IN_PROGRESS,
            lifecycle_status=WorkOrder.LifecycleStatus.IN_PROGRESS,
        )

    def test_mark_parts_button_removed(self):
        """GET as assigned tech → "Waiting for parts" button text is NOT in response."""
        self.client.force_login(self.tech)
        response = self.client.get(
            reverse("work_order_detail", kwargs={"pk": self.wo.pk})
        )
        self.assertEqual(response.status_code, 200)
        body = response.content.decode("utf-8")
        self.assertNotIn("Waiting for parts", body)

    def test_mark_vendor_button_removed(self):
        """GET as assigned tech → "Waiting for vendor" button text is NOT in response."""
        self.client.force_login(self.tech)
        response = self.client.get(
            reverse("work_order_detail", kwargs={"pk": self.wo.pk})
        )
        self.assertEqual(response.status_code, 200)
        body = response.content.decode("utf-8")
        self.assertNotIn("Waiting for vendor", body)


class PauseFieldsReadonlyInAdminTests(TestCase):
    """pause_reason and pause_note are read-only in WorkOrderAdmin."""

    def test_pause_fields_readonly_in_admin(self):
        """assert pause_reason is in readonly_fields of WorkOrderAdmin."""
        admin = WorkOrderAdmin
        self.assertIn("pause_reason", admin.readonly_fields)
        self.assertIn("pause_note", admin.readonly_fields)
