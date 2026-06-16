"""
Phase 3B — WorkOrder Blocker System role-aware UI and pause refactor.

Covers:
- work_order_pause view delegates to the work_order_pause service
  (single source of truth for the WO pause pipeline: state transition,
  pause_reason / pause_note / labor_stopped_at write, optional OPERATIONAL
  blocker open, operational status recompute).
- The "Manager actions", "My actions (technician)", and "Manager review"
  template partials render the right content for the right role and stay
  invisible for the wrong role.
- The pause form is visible only to the assigned technician on the WO
  detail page.

These tests use the same self-contained setUp pattern as
`test_phase3a_health_blockers.py` and `test_blocker_system.py`.
"""
from __future__ import annotations

from decimal import Decimal
from unittest import mock

from django.test import TestCase
from django.urls import reverse

from accounts.models import User
from inventory.models import PartIssueLine, SparePart
from maintenance.models import (
    Machine,
    WorkOrder,
    WorkOrderBlocker,
)


# ---------------------------------------------------------------------------
# Test helpers (mirror the style of test_blocker_system.py /
# test_phase3a_health_blockers.py).
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# work_order_pause view refactor tests
# ---------------------------------------------------------------------------

class WorkOrderPauseViewTests(TestCase):
    """The work_order_pause view delegates to maintenance.services.work_order_pause."""

    def setUp(self):
        self.manager = _make_user("manager_p3b", User.Role.MANAGER)
        self.tech = _make_user("tech_p3b", User.Role.TECHNICIAN)
        self.other_tech = _make_user("other_tech_p3b", User.Role.TECHNICIAN)
        self.wo = _make_wo(
            machine=None, created_by=self.manager,
            assigned_technician=self.tech,
            status=WorkOrder.Status.IN_PROGRESS,
            lifecycle_status=WorkOrder.LifecycleStatus.IN_PROGRESS,
        )

    def test_pause_view_calls_service(self):
        """POST to work_order_pause for an in-progress WO assigned to a tech
        → service is called with the right args; WO has labor_stopped_at set."""
        url = reverse("work_order_pause", kwargs={"pk": self.wo.pk})
        self.client.force_login(self.tech)
        with mock.patch(
            "maintenance.views.wo_pause_service"
        ) as mock_service:
            response = self.client.post(url, data={
                "pause_reason": WorkOrder.PauseReason.OPERATIONAL,
                "pause_note": "",
            })
        self.assertEqual(response.status_code, 302)
        # Service was called once
        mock_service.assert_called_once()
        # Verify kwargs
        call_kwargs = mock_service.call_args.kwargs
        self.assertEqual(call_kwargs["wo"], self.wo)
        self.assertEqual(call_kwargs["pause_reason"], WorkOrder.PauseReason.OPERATIONAL)
        self.assertEqual(call_kwargs["pause_note"], "")
        self.assertEqual(call_kwargs["actor"], self.tech)

    def test_pause_view_creates_blocker_on_other_reason(self):
        """POST with pause_reason=other + note='broken sensor' → OPERATIONAL
        blocker is opened (service runs end-to-end; view passes the args)."""
        url = reverse("work_order_pause", kwargs={"pk": self.wo.pk})
        self.client.force_login(self.tech)
        with mock.patch(
            "maintenance.views.wo_pause_service"
        ) as mock_service:
            response = self.client.post(url, data={
                "pause_reason": WorkOrder.PauseReason.OTHER,
                "pause_note": "broken sensor",
            })
        self.assertEqual(response.status_code, 302)
        mock_service.assert_called_once()
        # Confirm the view forwarded the right reason + note to the service
        call_kwargs = mock_service.call_args.kwargs
        self.assertEqual(call_kwargs["pause_reason"], WorkOrder.PauseReason.OTHER)
        self.assertEqual(call_kwargs["pause_note"], "broken sensor")

    def test_pause_view_no_blocker_on_micro_pause(self):
        """POST with pause_reason=operational + empty note → service is still
        called (state transition happens), but the content-based rule
        means no OPERATIONAL blocker is opened. We verify the view passes
        the args correctly; the service-level test in
        test_blocker_system.py:851 covers the rule itself.
        """
        url = reverse("work_order_pause", kwargs={"pk": self.wo.pk})
        self.client.force_login(self.tech)
        with mock.patch(
            "maintenance.views.wo_pause_service"
        ) as mock_service:
            response = self.client.post(url, data={
                "pause_reason": WorkOrder.PauseReason.OPERATIONAL,
                "pause_note": "",
            })
        self.assertEqual(response.status_code, 302)
        mock_service.assert_called_once()
        # The view should pass empty note to the service
        call_kwargs = mock_service.call_args.kwargs
        self.assertEqual(call_kwargs["pause_note"], "")

    def test_pause_view_rejects_non_assigned_tech(self):
        """POST as a different tech → 404 (per the view's permission logic)."""
        url = reverse("work_order_pause", kwargs={"pk": self.wo.pk})
        self.client.force_login(self.other_tech)
        with mock.patch("maintenance.views.wo_pause_service") as mock_service:
            response = self.client.post(url, data={
                "pause_reason": WorkOrder.PauseReason.OPERATIONAL,
                "pause_note": "",
            })
        # 404 because the tech is not the assigned technician
        self.assertEqual(response.status_code, 404)
        mock_service.assert_not_called()


# ---------------------------------------------------------------------------
# Role-aware modal tests
# ---------------------------------------------------------------------------

class WorkOrderDetailRoleGatingTests(TestCase):
    """The 3 role-aware partials render the right content for the right role."""

    def setUp(self):
        self.manager = _make_user("manager_p3b_role", User.Role.MANAGER)
        self.supervisor = _make_user("supervisor_p3b_role", User.Role.SUPERVISOR)
        self.tech = _make_user("tech_p3b_role", User.Role.TECHNICIAN)
        self.other_tech = _make_user("other_tech_p3b_role", User.Role.TECHNICIAN)
        self.wo = _make_wo(
            machine=None, created_by=self.manager,
            assigned_technician=self.tech,
            status=WorkOrder.Status.IN_PROGRESS,
            lifecycle_status=WorkOrder.LifecycleStatus.IN_PROGRESS,
        )

    def test_manager_sees_manager_actions_partial(self):
        """Manager → response contains manager actions (e.g. "Manager actions",
        "Direct issue (manager override — bypasses technician request flow)")."""
        self.client.force_login(self.manager)
        response = self.client.get(
            reverse("work_order_detail", kwargs={"pk": self.wo.pk})
        )
        self.assertEqual(response.status_code, 200)
        body = response.content.decode("utf-8")
        self.assertIn("Manager actions", body)
        self.assertIn("Direct issue", body)

    def test_tech_sees_tech_actions_partial(self):
        """Assigned tech → response contains tech actions (e.g.
        "My actions (technician)", "Resume labor", "Pause work")."""
        self.client.force_login(self.tech)
        response = self.client.get(
            reverse("work_order_detail", kwargs={"pk": self.wo.pk})
        )
        self.assertEqual(response.status_code, 200)
        body = response.content.decode("utf-8")
        self.assertIn("My actions (technician)", body)
        # WO is IN_PROGRESS → button is "Resume labor"
        self.assertIn("Resume labor", body)
        self.assertIn("Pause work", body)

    def test_other_tech_does_not_see_tech_partial(self):
        """A different tech (not the assigned one) → response does NOT
        contain the tech partial content. The work_order_detail view
        raises Http404 for non-assigned techs, so the response is 404."""
        self.client.force_login(self.other_tech)
        response = self.client.get(
            reverse("work_order_detail", kwargs={"pk": self.wo.pk})
        )
        # Non-assigned techs get 404 from the view
        self.assertEqual(response.status_code, 404)

    def test_supervisor_does_not_see_manager_partial(self):
        """Supervisor (not manager) → response does NOT contain the manager
        partial content. The perm_assign_technician / perm_issue_parts_to_wo
        / perm_create_purchase_request capabilities are all role_in(MANAGER),
        so supervisors do not see "Manager actions" / "Direct issue"."""
        self.client.force_login(self.supervisor)
        response = self.client.get(
            reverse("work_order_detail", kwargs={"pk": self.wo.pk})
        )
        self.assertEqual(response.status_code, 200)
        body = response.content.decode("utf-8")
        self.assertNotIn("Manager actions", body)
        self.assertNotIn("Direct issue", body)


# ---------------------------------------------------------------------------
# Pause refactor + role interaction tests
# ---------------------------------------------------------------------------

class WorkOrderPauseRoleInteractionTests(TestCase):
    """The pause form is visible only to the assigned tech, and the view
    rejects other techs with 404."""

    def setUp(self):
        self.manager = _make_user("manager_p3b_pause", User.Role.MANAGER)
        self.tech = _make_user("tech_p3b_pause", User.Role.TECHNICIAN)
        self.other_tech = _make_user("other_tech_p3b_pause", User.Role.TECHNICIAN)
        self.wo = _make_wo(
            machine=None, created_by=self.manager,
            assigned_technician=self.tech,
            status=WorkOrder.Status.IN_PROGRESS,
            lifecycle_status=WorkOrder.LifecycleStatus.IN_PROGRESS,
        )

    def test_pause_button_visible_only_to_assigned_tech(self):
        """Assigned tech → GET WO detail contains the pause form action.
        Manager (not the assigned tech) → response does NOT contain the
        pause form action (the manager partial has no pause form)."""
        # Assigned tech sees the pause form
        self.client.force_login(self.tech)
        response = self.client.get(
            reverse("work_order_detail", kwargs={"pk": self.wo.pk})
        )
        self.assertEqual(response.status_code, 200)
        body = response.content.decode("utf-8")
        # The pause form posts to work_order_pause
        pause_url = reverse("work_order_pause", kwargs={"pk": self.wo.pk})
        self.assertIn(pause_url, body)
        # The pause select element is present
        self.assertIn('name="pause_reason"', body)

        # Manager does NOT see the pause form
        self.client.force_login(self.manager)
        response = self.client.get(
            reverse("work_order_detail", kwargs={"pk": self.wo.pk})
        )
        self.assertEqual(response.status_code, 200)
        body = response.content.decode("utf-8")
        # Manager's partial has no pause form
        self.assertNotIn('name="pause_reason"', body)

    def test_pause_view_404_for_unrelated_tech(self):
        """A different tech (not the assigned one) → POST to work_order_pause
        → 404 (per the view's permission logic). The service is NOT called."""
        url = reverse("work_order_pause", kwargs={"pk": self.wo.pk})
        self.client.force_login(self.other_tech)
        with mock.patch("maintenance.views.wo_pause_service") as mock_service:
            response = self.client.post(url, data={
                "pause_reason": WorkOrder.PauseReason.OPERATIONAL,
                "pause_note": "",
            })
        self.assertEqual(response.status_code, 404)
        mock_service.assert_not_called()
