from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from accounts.models import User
from inventory.models import Inventory, PartIssueLine, SparePart, StockMovement
from maintenance.models import (
    ExternalRepairOrder, Machine, MaintenanceIssue, Site, Tool, ToolAssignment,
    WorkOrder, WorkOrderCost,
)


class MaintenanceFlowTests(TestCase):
    def setUp(self):
        self.manager = User.objects.create_user(
            username="manager1",
            password="pass1234",
            role=User.Role.MANAGER,
        )
        self.tech = User.objects.create_user(
            username="tech1",
            password="pass1234",
            role=User.Role.TECHNICIAN,
        )
        self.operator = User.objects.create_user(
            username="operator1",
            password="pass1234",
            role=User.Role.OPERATOR,
        )
        self.machine = Machine.objects.create(name="Press 1", qr_code="PRESS-01", location="Hall A")

    def test_issue_form_prefills_machine_from_qr_code(self):
        self.client.force_login(self.operator)
        response = self.client.get(reverse("issue_create"), {"qr": "PRESS-01"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["matched_machine"], self.machine)
        self.assertEqual(response.context["form"].initial["machine"], self.machine.pk)

    def test_manager_can_create_machine_from_ui(self):
        self.client.force_login(self.manager)
        response = self.client.post(
            reverse("machine_create"),
            {
                "name": "Conveyor 2",
                "qr_code": "CONV-02",
                "location": "Hall B",
                "is_active": "on",
            },
        )

        self.assertRedirects(response, reverse("machine_list"))
        self.assertTrue(Machine.objects.filter(qr_code="CONV-02", name="Conveyor 2").exists())

    def test_tool_page_resolves_scanned_tool_code(self):
        tool = Tool.objects.create(code="TOOL-01", name="Torque Wrench")
        assignment = ToolAssignment.objects.create(tool=tool, user=self.tech, assigned_by=self.manager)
        tool.status = Tool.Status.IN_USE
        tool.save(update_fields=["status"])

        self.client.force_login(self.manager)
        response = self.client.get(reverse("tool_list"), {"tool": "TOOL-01"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["matched_tool"], tool)
        self.assertEqual(response.context["matched_assignment"], assignment)

    def test_manager_can_create_tool_from_ui(self):
        self.client.force_login(self.manager)
        response = self.client.post(
            reverse("tool_create"),
            {
                "code": "TOOL-02",
                "name": "Allen key set",
                "status": Tool.Status.AVAILABLE,
            },
        )

        self.assertRedirects(response, reverse("tool_list"))
        self.assertTrue(Tool.objects.filter(code="TOOL-02", name="Allen key set").exists())

    def test_technician_queue_orders_active_before_other_items(self):
        issue_high = MaintenanceIssue.objects.create(
            machine=self.machine,
            reported_by=self.operator,
            status=MaintenanceIssue.Status.VALIDATED,
            priority=MaintenanceIssue.Priority.HIGH,
            description="Bearing noise",
        )
        issue_medium = MaintenanceIssue.objects.create(
            machine=self.machine,
            reported_by=self.operator,
            status=MaintenanceIssue.Status.VALIDATED,
            priority=MaintenanceIssue.Priority.MEDIUM,
            description="Oil leak",
        )
        active = WorkOrder.objects.create(
            issue=issue_medium,
            machine=self.machine,
            status=WorkOrder.Status.IN_PROGRESS,
            assigned_technician=self.tech,
            created_by=self.manager,
        )
        queued = WorkOrder.objects.create(
            issue=issue_high,
            machine=self.machine,
            status=WorkOrder.Status.ASSIGNED,
            assigned_technician=self.tech,
            created_by=self.manager,
        )

        self.client.force_login(self.tech)
        response = self.client.get(reverse("work_order_list"))

        self.assertEqual(response.status_code, 200)
        work_orders = list(response.context["work_orders"])
        self.assertEqual(work_orders[0], active)
        self.assertEqual(work_orders[1], queued)

    def test_work_order_creation_uses_review_then_post_flow(self):
        issue = MaintenanceIssue.objects.create(
            machine=self.machine,
            reported_by=self.operator,
            status=MaintenanceIssue.Status.VALIDATED,
            priority=MaintenanceIssue.Priority.HIGH,
            description="Motor coupling issue",
            validated_by=self.manager,
        )

        self.client.force_login(self.manager)
        review_response = self.client.get(reverse("work_order_create", args=[issue.pk]))
        self.assertEqual(review_response.status_code, 200)
        self.assertEqual(review_response.context["issue"], issue)

        create_response = self.client.post(reverse("work_order_create", args=[issue.pk]))
        self.assertEqual(create_response.status_code, 302)

        issue.refresh_from_db()
        wo = WorkOrder.objects.get(issue=issue)
        self.assertEqual(issue.status, MaintenanceIssue.Status.CONVERTED)
        self.assertEqual(wo.status, WorkOrder.Status.APPROVED)
        self.assertEqual(wo.machine, self.machine)

    def test_starting_new_work_order_prompts_for_switch_when_another_is_active(self):
        issue_active = MaintenanceIssue.objects.create(
            machine=self.machine,
            reported_by=self.operator,
            status=MaintenanceIssue.Status.VALIDATED,
            priority=MaintenanceIssue.Priority.MEDIUM,
            description="Active task",
        )
        issue_next = MaintenanceIssue.objects.create(
            machine=self.machine,
            reported_by=self.operator,
            status=MaintenanceIssue.Status.VALIDATED,
            priority=MaintenanceIssue.Priority.HIGH,
            description="Next task",
        )
        active = WorkOrder.objects.create(
            issue=issue_active,
            machine=self.machine,
            status=WorkOrder.Status.IN_PROGRESS,
            assigned_technician=self.tech,
            created_by=self.manager,
        )
        queued = WorkOrder.objects.create(
            issue=issue_next,
            machine=self.machine,
            status=WorkOrder.Status.ASSIGNED,
            assigned_technician=self.tech,
            created_by=self.manager,
        )

        self.client.force_login(self.tech)
        confirm_response = self.client.post(reverse("work_order_start", args=[queued.pk]))
        self.assertEqual(confirm_response.status_code, 200)
        self.assertTemplateUsed(confirm_response, "maintenance/workorder_switch_confirm.html")

        start_response = self.client.post(reverse("work_order_start", args=[queued.pk]), {"confirm_switch": "1"})
        self.assertEqual(start_response.status_code, 302)

        active.refresh_from_db()
        queued.refresh_from_db()
        self.assertEqual(active.status, WorkOrder.Status.PAUSED)
        self.assertEqual(queued.status, WorkOrder.Status.IN_PROGRESS)

    def test_kpi_dashboard_exposes_pdf_aligned_metrics(self):
        closed_issue = MaintenanceIssue.objects.create(
            machine=self.machine,
            reported_by=self.operator,
            status=MaintenanceIssue.Status.CONVERTED,
            priority=MaintenanceIssue.Priority.HIGH,
            description="Closed job source",
        )
        WorkOrder.objects.create(
            issue=closed_issue,
            machine=self.machine,
            status=WorkOrder.Status.CLOSED,
            assigned_technician=self.tech,
            created_by=self.manager,
            labor_started_at=timezone.now() - timedelta(hours=5),
            labor_stopped_at=timezone.now() - timedelta(hours=3),
            downtime_started_at=timezone.now() - timedelta(hours=6),
            downtime_ended_at=timezone.now() - timedelta(hours=2),
        )

        self.client.force_login(self.manager)
        response = self.client.get(reverse("kpi_dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertIn("mttw_hours", response.context)
        self.assertIn("mtbf_hours", response.context)
        self.assertIn("most_used_parts", response.context)
        self.assertIn("machine_failure_rate", response.context)
        self.assertIn("tech_efficiency", response.context)
        self.assertIn("supplier_cost_ranking", response.context)

    @patch("maintenance.views._decode_uploaded_qr", return_value="PRESS-01")
    def test_qr_upload_redirects_with_decoded_value(self, _decode_mock):
        self.client.force_login(self.operator)
        with open(__file__, "rb") as fake_image:
            response = self.client.post(
                reverse("qr_scan"),
                {
                    "next": reverse("issue_create"),
                    "param": "qr",
                    "label": "machine QR",
                    "qr_image": fake_image,
                },
            )

        self.assertRedirects(response, f"{reverse('issue_create')}?qr=PRESS-01")

    @patch("maintenance.views._decode_uploaded_qr", return_value="PRESS-01")
    def test_qr_decode_endpoint_returns_json_value(self, _decode_mock):
        self.client.force_login(self.operator)
        with open(__file__, "rb") as fake_image:
            response = self.client.post(
                reverse("qr_scan_decode"),
                {"qr_image": fake_image},
            )

        self.assertEqual(response.status_code, 200)
        self.assertJSONEqual(response.content, {"decoded_value": "PRESS-01"})


class WorkOrderPauseReasonTests(TestCase):
    """Phase 2.3 — pause reason categorization."""

    def setUp(self):
        self.manager = User.objects.create_user(
            username="manager1", password="pass1234", role=User.Role.MANAGER
        )
        self.tech = User.objects.create_user(
            username="tech1", password="pass1234", role=User.Role.TECHNICIAN
        )
        self.machine = Machine.objects.create(name="Press 1", qr_code="PRESS-01")
        self.wo = WorkOrder.objects.create(
            machine=self.machine,
            status=WorkOrder.Status.IN_PROGRESS,
            assigned_technician=self.tech,
            created_by=self.manager,
        )

    def _start_pause(self, **data):
        self.client.force_login(self.tech)
        payload = {"pause_reason": "operational"}
        payload.update(data)
        return self.client.post(reverse("work_order_pause", args=[self.wo.pk]), payload)

    def test_pause_with_operational_reason_records_field(self):
        response = self._start_pause(pause_reason="operational")
        self.assertEqual(response.status_code, 302)
        self.wo.refresh_from_db()
        self.assertEqual(self.wo.status, WorkOrder.Status.PAUSED)
        self.assertEqual(self.wo.pause_reason, WorkOrder.PauseReason.OPERATIONAL)
        self.assertEqual(self.wo.pause_note, "")

    def test_pause_with_other_requires_note(self):
        response = self._start_pause(pause_reason="other")
        # Should redirect back, not 200, and not transition
        self.assertEqual(response.status_code, 302)
        self.wo.refresh_from_db()
        self.assertEqual(self.wo.status, WorkOrder.Status.IN_PROGRESS)
        self.assertEqual(self.wo.pause_reason, "")

    def test_pause_with_other_and_note_succeeds(self):
        response = self._start_pause(pause_reason="other", pause_note="Power outage in Hall A")
        self.assertEqual(response.status_code, 302)
        self.wo.refresh_from_db()
        self.assertEqual(self.wo.status, WorkOrder.Status.PAUSED)
        self.assertEqual(self.wo.pause_reason, WorkOrder.PauseReason.OTHER)
        self.assertEqual(self.wo.pause_note, "Power outage in Hall A")

    def test_pause_without_reason_is_rejected(self):
        self.client.force_login(self.tech)
        response = self.client.post(reverse("work_order_pause", args=[self.wo.pk]), {})
        self.assertEqual(response.status_code, 302)
        self.wo.refresh_from_db()
        self.assertEqual(self.wo.status, WorkOrder.Status.IN_PROGRESS)

    def test_emergency_start_auto_pauses_other_with_emergency_reason(self):
        other = WorkOrder.objects.create(
            machine=self.machine,
            status=WorkOrder.Status.IN_PROGRESS,
            assigned_technician=self.tech,
            created_by=self.manager,
        )
        em_wo = WorkOrder.objects.create(
            machine=self.machine,
            status=WorkOrder.Status.ASSIGNED,
            assigned_technician=self.tech,
            created_by=self.manager,
            is_emergency=True,
        )
        self.client.force_login(self.tech)
        # confirm_switch is required when another WO is IN_PROGRESS
        self.client.post(reverse("work_order_start", args=[em_wo.pk]), {"confirm_switch": "1"})
        other.refresh_from_db()
        em_wo.refresh_from_db()
        self.assertEqual(other.status, WorkOrder.Status.PAUSED)
        self.assertEqual(other.pause_reason, WorkOrder.PauseReason.EMERGENCY)
        self.assertEqual(em_wo.status, WorkOrder.Status.IN_PROGRESS)

    def test_non_emergency_start_auto_pauses_with_operational_reason(self):
        other = WorkOrder.objects.create(
            machine=self.machine,
            status=WorkOrder.Status.IN_PROGRESS,
            assigned_technician=self.tech,
            created_by=self.manager,
        )
        next_wo = WorkOrder.objects.create(
            machine=self.machine,
            status=WorkOrder.Status.ASSIGNED,
            assigned_technician=self.tech,
            created_by=self.manager,
        )
        self.client.force_login(self.tech)
        self.client.post(reverse("work_order_start", args=[next_wo.pk]), {"confirm_switch": "1"})
        other.refresh_from_db()
        self.assertEqual(other.status, WorkOrder.Status.PAUSED)
        self.assertEqual(other.pause_reason, WorkOrder.PauseReason.OPERATIONAL)


class WorkOrderResumeValidationTests(TestCase):
    """Phase 2.4 — emergency precedence: a paused non-emergency WO cannot
    be resumed while another emergency WO is IN_PROGRESS for the same
    technician (SRS UC-06 step 2D).
    """

    def setUp(self):
        self.manager = User.objects.create_user(
            username="manager1", password="pass1234", role=User.Role.MANAGER
        )
        self.tech = User.objects.create_user(
            username="tech1", password="pass1234", role=User.Role.TECHNICIAN
        )
        self.machine = Machine.objects.create(name="Press 1", qr_code="PRESS-01")
        self.other_machine = Machine.objects.create(name="Press 2", qr_code="PRESS-02")

    def _make_wo(self, *, status, is_emergency=False, **extra):
        defaults = {
            "machine": self.machine,
            "status": status,
            "assigned_technician": self.tech,
            "created_by": self.manager,
            "is_emergency": is_emergency,
        }
        defaults.update(extra)
        return WorkOrder.objects.create(**defaults)

    def test_resume_paused_blocked_when_other_emergency_in_progress(self):
        """SRS UC-06 step 2D: emergency must finish first."""
        emergency = self._make_wo(
            status=WorkOrder.Status.IN_PROGRESS, is_emergency=True
        )
        paused = self._make_wo(status=WorkOrder.Status.PAUSED)

        self.client.force_login(self.tech)
        response = self.client.post(
            reverse("work_order_start", args=[paused.pk])
        )
        # Redirected back to detail (not switch confirm)
        self.assertEqual(response.status_code, 302)
        paused.refresh_from_db()
        emergency.refresh_from_db()
        self.assertEqual(paused.status, WorkOrder.Status.PAUSED)
        self.assertEqual(emergency.status, WorkOrder.Status.IN_PROGRESS)

    def test_resume_paused_blocked_when_emergency_in_pending_parts(self):
        """Even if the emergency is PENDING_PARTS, that's still 'free' for the
        technician — but the rule is about IN_PROGRESS, so we expect this to
        be allowed. Verify we don't over-block.
        """
        # An emergency that is NOT in progress (e.g. waiting for parts)
        # should NOT block resuming a paused non-emergency.
        emergency = self._make_wo(
            status=WorkOrder.Status.PENDING_PARTS, is_emergency=True
        )
        paused = self._make_wo(status=WorkOrder.Status.PAUSED)

        self.client.force_login(self.tech)
        # It will go through the switch-confirm flow? No — get_other_active
        # only finds IN_PROGRESS, so no conflict, no confirm, just start.
        response = self.client.post(
            reverse("work_order_start", args=[paused.pk])
        )
        self.assertEqual(response.status_code, 302)
        paused.refresh_from_db()
        # This should succeed (no active emergency in progress)
        self.assertEqual(paused.status, WorkOrder.Status.IN_PROGRESS)

    def test_resume_paused_allowed_when_no_emergency_active(self):
        paused = self._make_wo(status=WorkOrder.Status.PAUSED)
        self.client.force_login(self.tech)
        response = self.client.post(
            reverse("work_order_start", args=[paused.pk])
        )
        self.assertEqual(response.status_code, 302)
        paused.refresh_from_db()
        self.assertEqual(paused.status, WorkOrder.Status.IN_PROGRESS)

    def test_starting_emergency_itself_unaffected_by_other_emergency(self):
        """A new emergency WO can be started even if another emergency
        is already IN_PROGRESS for the same tech (manager-controlled
        scenario: two emergencies in flight, new one replaces old)."""
        old_emergency = self._make_wo(
            status=WorkOrder.Status.IN_PROGRESS, is_emergency=True
        )
        new_emergency = self._make_wo(
            status=WorkOrder.Status.ASSIGNED, is_emergency=True
        )

        self.client.force_login(self.tech)
        # First call: switch-confirm page is shown because old emergency
        # is IN_PROGRESS (the emergency-check itself is bypassed, but the
        # generic single-active check still triggers).
        first = self.client.post(
            reverse("work_order_start", args=[new_emergency.pk])
        )
        self.assertEqual(first.status_code, 200)
        self.assertTemplateUsed(first, "maintenance/workorder_switch_confirm.html")

        # Second call: confirm_switch=1, proceeds to start, auto-pauses old
        second = self.client.post(
            reverse("work_order_start", args=[new_emergency.pk]),
            {"confirm_switch": "1"},
        )
        self.assertEqual(second.status_code, 302)
        old_emergency.refresh_from_db()
        new_emergency.refresh_from_db()
        self.assertEqual(old_emergency.status, WorkOrder.Status.PAUSED)
        self.assertEqual(new_emergency.status, WorkOrder.Status.IN_PROGRESS)

    def test_has_active_emergency_helper(self):
        from maintenance.services import has_active_emergency
        # No WOs at all
        self.assertFalse(has_active_emergency(self.tech))
        # Active emergency
        self._make_wo(status=WorkOrder.Status.IN_PROGRESS, is_emergency=True)
        self.assertTrue(has_active_emergency(self.tech))
        # Non-emergency IN_PROGRESS
        self._make_wo(status=WorkOrder.Status.IN_PROGRESS, is_emergency=False)
        self.assertTrue(has_active_emergency(self.tech))

    def test_template_disables_button_when_emergency_blocks(self):
        emergency = self._make_wo(
            status=WorkOrder.Status.IN_PROGRESS, is_emergency=True
        )
        paused = self._make_wo(status=WorkOrder.Status.PAUSED)

        self.client.force_login(self.tech)
        response = self.client.get(
            reverse("work_order_detail", args=[paused.pk])
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["emergency_blocks_resume"])
        # The button must be disabled in HTML
        self.assertContains(response, "disabled")
        self.assertContains(response, "Finish the active emergency WO first")


class PartRequestWorkflowTests(TestCase):
    """Phase 2.1 — hybrid approval workflow for parts on a work order.

    Technician request (PENDING) → Manager APPROVE / REJECT / EDIT
    Emergency exception: auto-approve on is_emergency WOs.
    """

    def setUp(self):
        # Use the site auto-seeded by migration 0005 (Main Factory / MF).
        # Don't create a new site — that would create two defaults and
        # `_get_default_site()` would return the wrong one.
        self.site = Site.objects.get(is_default=True)
        self.manager = User.objects.create_user(
            username="manager1", password="pass1234", role=User.Role.MANAGER
        )
        self.tech = User.objects.create_user(
            username="tech1", password="pass1234", role=User.Role.TECHNICIAN
        )
        self.other_tech = User.objects.create_user(
            username="tech2", password="pass1234", role=User.Role.TECHNICIAN
        )
        self.machine = Machine.objects.create(name="Press 1", qr_code="PRESS-01")
        # Per-test part so we can reset stock each time
        self.part = SparePart.objects.create(
            sku=f"BRG-001-{self._testMethodName[:8]}", name="Bearing 6205", status="active"
        )
        self.inv = Inventory.objects.create(
            part=self.part, site=self.site, quantity_available=Decimal("10")
        )

    def _make_wo(self, *, status=WorkOrder.Status.ASSIGNED, is_emergency=False, technician=None):
        return WorkOrder.objects.create(
            machine=self.machine,
            status=status,
            assigned_technician=technician or self.tech,
            created_by=self.manager,
            is_emergency=is_emergency,
        )

    # ----- Service-layer tests -----

    def test_technician_request_creates_pending_line_no_stock_change(self):
        from inventory.services import request_part_on_wo
        wo = self._make_wo()
        line = request_part_on_wo(
            wo=wo, part=self.part, quantity=Decimal("2"), technician=self.tech,
        )
        self.assertEqual(line.status, PartIssueLine.Status.PENDING)
        self.assertEqual(line.requested_by, self.tech)
        self.inv.refresh_from_db()
        self.assertEqual(self.inv.quantity_available, Decimal("10"))
        self.assertEqual(
            StockMovement.objects.filter(part=self.part, movement_type=StockMovement.MovementType.ISSUE_TO_WO).count(),
            0,
        )

    def test_manager_approval_deducts_stock(self):
        from inventory.services import request_part_on_wo, approve_part_request
        wo = self._make_wo()
        line = request_part_on_wo(
            wo=wo, part=self.part, quantity=Decimal("3"), technician=self.tech,
        )
        approve_part_request(line=line, manager=self.manager)
        line.refresh_from_db()
        self.assertEqual(line.status, PartIssueLine.Status.APPROVED)
        self.assertEqual(line.approved_by, self.manager)
        self.assertIsNotNone(line.approved_at)
        self.inv.refresh_from_db()
        self.assertEqual(self.inv.quantity_available, Decimal("7"))
        # StockMovement created
        self.assertTrue(
            StockMovement.objects.filter(
                part=self.part,
                movement_type=StockMovement.MovementType.ISSUE_TO_WO,
                work_order=wo,
                quantity=Decimal("3"),
            ).exists()
        )

    def test_manager_rejection_no_stock_change(self):
        from inventory.services import request_part_on_wo, reject_part_request
        wo = self._make_wo()
        line = request_part_on_wo(
            wo=wo, part=self.part, quantity=Decimal("4"), technician=self.tech,
        )
        reject_part_request(line=line, manager=self.manager, reason="Already in stock")
        line.refresh_from_db()
        self.assertEqual(line.status, PartIssueLine.Status.REJECTED)
        self.assertEqual(line.rejection_reason, "Already in stock")
        self.inv.refresh_from_db()
        self.assertEqual(self.inv.quantity_available, Decimal("10"))

    def test_reject_without_reason_raises(self):
        from inventory.services import request_part_on_wo, reject_part_request
        wo = self._make_wo()
        line = request_part_on_wo(
            wo=wo, part=self.part, quantity=Decimal("1"), technician=self.tech,
        )
        with self.assertRaises(ValueError):
            reject_part_request(line=line, manager=self.manager, reason="")

    def test_edit_qty_keeps_pending(self):
        from inventory.services import request_part_on_wo, edit_part_request_qty
        wo = self._make_wo()
        line = request_part_on_wo(
            wo=wo, part=self.part, quantity=Decimal("5"), technician=self.tech,
        )
        edit_part_request_qty(line=line, manager=self.manager, new_quantity=Decimal("2"))
        line.refresh_from_db()
        self.assertEqual(line.status, PartIssueLine.Status.PENDING)
        self.assertEqual(line.quantity, Decimal("2"))

    def test_emergency_request_auto_approves(self):
        from inventory.services import request_part_on_wo
        wo = self._make_wo(is_emergency=True)
        line = request_part_on_wo(
            wo=wo, part=self.part, quantity=Decimal("2"), technician=self.tech,
        )
        # After emergency auto-approval, line is APPROVED and stock deducted
        self.assertEqual(line.status, PartIssueLine.Status.APPROVED)
        self.assertTrue(line.is_emergency_auto_approved)
        self.inv.refresh_from_db()
        self.assertEqual(self.inv.quantity_available, Decimal("8"))

    def test_emergency_request_with_insufficient_stock_auto_approves_partial(self):
        # P3.1: emergency auto-approve now issues what's available and flags
        # the shortage for manager post-review (does not stay PENDING).
        from inventory.services import request_part_on_wo
        self.inv.quantity_available = Decimal("1")
        self.inv.save()
        wo = self._make_wo(is_emergency=True)
        line = request_part_on_wo(
            wo=wo, part=self.part, quantity=Decimal("5"), technician=self.tech,
        )
        self.assertEqual(line.status, PartIssueLine.Status.APPROVED)
        self.assertTrue(line.is_emergency_auto_approved)
        self.assertEqual(line.approved_qty, Decimal("5"))
        self.assertEqual(line.issued_qty, Decimal("1"))
        self.inv.refresh_from_db()
        self.assertEqual(self.inv.quantity_available, Decimal("0"))

    def test_approval_with_insufficient_stock_issues_whats_available(self):
        # P3.1: manager approval no longer raises on insufficient stock.
        # The system issues min(approved, available) and marks the line
        # APPROVED. The auto-PR covers the shortage.
        from inventory.services import request_part_on_wo, approve_part_request
        self.inv.quantity_available = Decimal("1")
        self.inv.save()
        wo = self._make_wo()
        line = request_part_on_wo(
            wo=wo, part=self.part, quantity=Decimal("5"), technician=self.tech,
        )
        line = approve_part_request(line=line, manager=self.manager)
        self.assertEqual(line.status, PartIssueLine.Status.APPROVED)
        self.assertEqual(line.approved_qty, Decimal("5"))
        self.assertEqual(line.issued_qty, Decimal("1"))
        self.inv.refresh_from_db()
        self.assertEqual(self.inv.quantity_available, Decimal("0"))

    def test_duplicate_pending_for_same_part_wo_is_idempotent(self):
        # P3.1: re-requesting the same part+WO returns the existing
        # PENDING line instead of raising.
        from inventory.services import request_part_on_wo
        wo = self._make_wo()
        first = request_part_on_wo(
            wo=wo, part=self.part, quantity=Decimal("1"), technician=self.tech,
        )
        second = request_part_on_wo(
            wo=wo, part=self.part, quantity=Decimal("2"), technician=self.tech,
        )
        self.assertEqual(first.pk, second.pk)
        # quantity stays at the original request (manager edits via edit view)
        self.assertEqual(first.quantity, Decimal("1"))

    # ----- View-layer tests -----

    def test_tech_request_part_view_creates_pending(self):
        wo = self._make_wo()
        self.client.force_login(self.tech)
        response = self.client.post(
            reverse("work_order_request_part", args=[wo.pk]),
            {"part": self.part.pk, "quantity": "2.0", "note": "Need it"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            PartIssueLine.objects.filter(work_order=wo, status="pending").count(),
            1,
        )
        self.inv.refresh_from_db()
        self.assertEqual(self.inv.quantity_available, Decimal("10"))

    def test_tech_cannot_request_on_other_techs_wo(self):
        wo = self._make_wo(technician=self.other_tech)
        self.client.force_login(self.tech)
        response = self.client.post(
            reverse("work_order_request_part", args=[wo.pk]),
            {"part": self.part.pk, "quantity": "2.0"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(PartIssueLine.objects.count(), 0)

    def test_manager_approve_deducts_stock(self):
        from inventory.services import request_part_on_wo
        wo = self._make_wo()
        line = request_part_on_wo(
            wo=wo, part=self.part, quantity=Decimal("3"), technician=self.tech,
        )
        self.client.force_login(self.manager)
        response = self.client.post(
            reverse("work_order_decide_part", args=[wo.pk, line.pk]),
            {"action": "approve"},
        )
        self.assertEqual(response.status_code, 302)
        self.inv.refresh_from_db()
        self.assertEqual(self.inv.quantity_available, Decimal("7"))

    def test_manager_reject_does_not_deduct(self):
        from inventory.services import request_part_on_wo
        wo = self._make_wo()
        line = request_part_on_wo(
            wo=wo, part=self.part, quantity=Decimal("3"), technician=self.tech,
        )
        self.client.force_login(self.manager)
        response = self.client.post(
            reverse("work_order_decide_part", args=[wo.pk, line.pk]),
            {"action": "reject", "rejection_reason": "Too much"},
        )
        self.assertEqual(response.status_code, 302)
        self.inv.refresh_from_db()
        self.assertEqual(self.inv.quantity_available, Decimal("10"))

    def test_manager_reject_without_reason_redirects_with_error(self):
        from inventory.services import request_part_on_wo
        wo = self._make_wo()
        line = request_part_on_wo(
            wo=wo, part=self.part, quantity=Decimal("3"), technician=self.tech,
        )
        self.client.force_login(self.manager)
        response = self.client.post(
            reverse("work_order_decide_part", args=[wo.pk, line.pk]),
            {"action": "reject"},
        )
        self.assertEqual(response.status_code, 302)
        # Line stays PENDING
        line.refresh_from_db()
        self.assertEqual(line.status, PartIssueLine.Status.PENDING)

    def test_manager_edit_qty(self):
        from inventory.services import request_part_on_wo
        wo = self._make_wo()
        line = request_part_on_wo(
            wo=wo, part=self.part, quantity=Decimal("3"), technician=self.tech,
        )
        self.client.force_login(self.manager)
        response = self.client.post(
            reverse("work_order_decide_part", args=[wo.pk, line.pk]),
            {"action": "edit", "new_qty": "1.5"},
        )
        self.assertEqual(response.status_code, 302)
        line.refresh_from_db()
        self.assertEqual(line.status, PartIssueLine.Status.PENDING)
        self.assertEqual(line.quantity, Decimal("1.5"))

    def test_only_manager_can_approve(self):
        from inventory.services import request_part_on_wo
        wo = self._make_wo()
        line = request_part_on_wo(
            wo=wo, part=self.part, quantity=Decimal("3"), technician=self.tech,
        )
        # Tech tries to approve their own request — should be denied
        self.client.force_login(self.tech)
        response = self.client.post(
            reverse("work_order_decide_part", args=[wo.pk, line.pk]),
            {"action": "approve"},
        )
        # Should redirect (not 200), line stays PENDING
        self.assertEqual(response.status_code, 302)
        line.refresh_from_db()
        self.assertEqual(line.status, PartIssueLine.Status.PENDING)

    def test_wo_detail_shows_pending_requests_to_manager(self):
        from inventory.services import request_part_on_wo
        wo = self._make_wo()
        line = request_part_on_wo(
            wo=wo, part=self.part, quantity=Decimal("3"), technician=self.tech,
        )
        self.client.force_login(self.manager)
        response = self.client.get(reverse("work_order_detail", args=[wo.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertIn(line, response.context["pending_part_requests"])
        self.assertContains(response, "Pending part requests")
        self.assertContains(response, "Submit decision")

    def test_wo_detail_shows_request_form_to_assigned_tech(self):
        wo = self._make_wo()
        self.client.force_login(self.tech)
        response = self.client.get(reverse("work_order_detail", args=[wo.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Request a part")
        self.assertContains(response, "Submit part request")

    def test_wo_detail_emergency_warning_shown_for_emergency_wo(self):
        wo = self._make_wo(is_emergency=True)
        self.client.force_login(self.tech)
        response = self.client.get(reverse("work_order_detail", args=[wo.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Emergency WO:")

    # ----- P3.1 tests (Phase 3.1 — UC-09 Inventory & Procurement Automation) -----

    def _pr_count_for(self, wo, part):
        from procurement.models import PurchaseRequest
        return PurchaseRequest.objects.filter(work_order=wo, part=part).count()

    def test_request_part_partial_stock_creates_pending_line_and_pr(self):
        # available=10, requested=15 → shortage=5 → auto-PR for 5
        from inventory.services import request_part_on_wo
        from procurement.models import PurchaseRequest
        wo = self._make_wo()
        line = request_part_on_wo(
            wo=wo, part=self.part, quantity=Decimal("15"), technician=self.tech,
        )
        self.assertEqual(line.status, PartIssueLine.Status.PENDING)
        self.assertEqual(line.requested_qty, Decimal("15"))
        self.assertEqual(line.shortage_qty, Decimal("5"))
        prs = PurchaseRequest.objects.filter(work_order=wo, part=self.part, status="pending")
        self.assertEqual(prs.count(), 1)
        self.assertEqual(prs.first().quantity, Decimal("5"))
        self.inv.refresh_from_db()
        self.assertEqual(self.inv.quantity_available, Decimal("10"))  # untouched

    def test_request_part_zero_stock_creates_pending_line_and_pr(self):
        from inventory.services import request_part_on_wo
        from procurement.models import PurchaseRequest
        self.inv.quantity_available = Decimal("0")
        self.inv.save()
        wo = self._make_wo()
        line = request_part_on_wo(
            wo=wo, part=self.part, quantity=Decimal("3"), technician=self.tech,
        )
        self.assertEqual(line.status, PartIssueLine.Status.PENDING)
        self.assertEqual(line.shortage_qty, Decimal("3"))
        prs = PurchaseRequest.objects.filter(work_order=wo, part=self.part, status="pending")
        self.assertEqual(prs.count(), 1)
        self.assertEqual(prs.first().quantity, Decimal("3"))

    def test_request_part_full_stock_creates_pending_line_no_pr(self):
        from inventory.services import request_part_on_wo
        from procurement.models import PurchaseRequest
        wo = self._make_wo()
        line = request_part_on_wo(
            wo=wo, part=self.part, quantity=Decimal("5"), technician=self.tech,
        )
        self.assertEqual(line.status, PartIssueLine.Status.PENDING)
        self.assertEqual(line.shortage_qty, Decimal("0"))
        self.assertEqual(
            PurchaseRequest.objects.filter(work_order=wo, part=self.part).count(), 0,
        )

    def test_request_part_is_idempotent_no_duplicate_line_or_pr(self):
        from inventory.services import request_part_on_wo
        from procurement.models import PurchaseRequest
        self.inv.quantity_available = Decimal("0")
        self.inv.save()
        wo = self._make_wo()
        first = request_part_on_wo(
            wo=wo, part=self.part, quantity=Decimal("5"), technician=self.tech,
        )
        second = request_part_on_wo(
            wo=wo, part=self.part, quantity=Decimal("5"), technician=self.tech,
        )
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(
            PurchaseRequest.objects.filter(work_order=wo, part=self.part).count(), 1,
        )

    def test_request_part_appends_to_existing_pr_for_same_wo_part(self):
        # Idempotency: a second pending request for the same WO+part
        # should NOT create a second PR.
        from inventory.services import request_part_on_wo
        from procurement.models import PurchaseRequest
        self.inv.quantity_available = Decimal("0")
        self.inv.save()
        wo = self._make_wo()
        first = request_part_on_wo(
            wo=wo, part=self.part, quantity=Decimal("5"), technician=self.tech,
        )
        second = request_part_on_wo(
            wo=wo, part=self.part, quantity=Decimal("5"), technician=self.tech,
        )
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(PurchaseRequest.objects.count(), 1)

    def test_manager_approve_with_edited_qty_deducts_correctly(self):
        # Manager edits qty from 15 to 8 before approving.
        # shortage_qty stays at requested - approved = 15 - 8 = 7.
        from inventory.services import request_part_on_wo, approve_part_request, edit_part_request_qty
        from procurement.models import PurchaseRequest
        wo = self._make_wo()
        line = request_part_on_wo(
            wo=wo, part=self.part, quantity=Decimal("15"), technician=self.tech,
        )
        # shortage = 5 (15 - 10 available) → PR for 5 created
        self.assertEqual(
            PurchaseRequest.objects.filter(work_order=wo, part=self.part).count(), 1,
        )
        line = edit_part_request_qty(line=line, manager=self.manager, new_quantity=Decimal("8"))
        line.refresh_from_db()
        self.assertEqual(line.quantity, Decimal("8"))
        # shortage_qty = max(0, requested_qty - approved) = max(0, 15 - 8) = 7
        self.assertEqual(line.shortage_qty, Decimal("7"))
        line = approve_part_request(line=line, manager=self.manager)
        line.refresh_from_db()
        self.assertEqual(line.status, PartIssueLine.Status.APPROVED)
        self.assertEqual(line.approved_qty, Decimal("8"))
        self.assertEqual(line.issued_qty, Decimal("8"))
        self.inv.refresh_from_db()
        self.assertEqual(self.inv.quantity_available, Decimal("2"))
        # PR is NOT auto-deleted (manager's procurement decision is separate)
        self.assertEqual(
            PurchaseRequest.objects.filter(work_order=wo, part=self.part).count(), 1,
        )

    def test_manager_reject_keeps_pr_alone(self):
        from inventory.services import request_part_on_wo, reject_part_request
        from procurement.models import PurchaseRequest
        self.inv.quantity_available = Decimal("0")
        self.inv.save()
        wo = self._make_wo()
        line = request_part_on_wo(
            wo=wo, part=self.part, quantity=Decimal("5"), technician=self.tech,
        )
        self.assertEqual(
            PurchaseRequest.objects.filter(work_order=wo, part=self.part).count(), 1,
        )
        line = reject_part_request(
            line=line, manager=self.manager, reason="Use existing stock at Site B",
        )
        line.refresh_from_db()
        self.assertEqual(line.status, PartIssueLine.Status.REJECTED)
        self.assertEqual(line.approved_qty, Decimal("0"))
        self.assertEqual(line.issued_qty, Decimal("0"))
        # PR stays — procurement decision is independent of WO issue decision
        self.assertEqual(
            PurchaseRequest.objects.filter(work_order=wo, part=self.part).count(), 1,
        )

    def test_last_supplier_price_in_form_context(self):
        # Seed a STOCK_IN with unit_cost, then GET WO detail and check
        # last_prices_json is in the context.
        from inventory.services import stock_in
        from decimal import Decimal as D
        stock_in(
            part=self.part, quantity=D("5"), performed_by=self.manager,
            supplier_name="AcmeCorp", unit_cost=D("12.50"), invoice_ref="INV-001",
        )
        wo = self._make_wo()
        self.client.force_login(self.tech)
        response = self.client.get(reverse("work_order_detail", args=[wo.pk]))
        self.assertEqual(response.status_code, 200)
        last_prices_json = response.context["last_prices_json"]
        import json
        parsed = json.loads(last_prices_json)
        self.assertIn(str(self.part.pk), parsed)
        # unit_cost is quantized to 3 decimals (matches model field).
        self.assertEqual(parsed[str(self.part.pk)]["unit_cost"], "12.500")
        self.assertEqual(parsed[str(self.part.pk)]["supplier_name"], "AcmeCorp")

    def test_manager_direct_issue_does_not_auto_create_pr(self):
        # Manager's legacy direct issue path bypasses auto-PR.
        from inventory.services import issue_part_to_work_order
        from procurement.models import PurchaseRequest
        self.inv.quantity_available = Decimal("0")  # shortage scenario
        self.inv.save()
        wo = self._make_wo()
        ok, _msg = issue_part_to_work_order(
            wo=wo, part=self.part, quantity=Decimal("3"),
            unit_cost=Decimal("10"), invoice_ref="INV-DIRECT",
            supplier_name="AcmeCorp", issued_by=self.manager,
        )
        self.assertTrue(ok)
        self.assertEqual(
            PurchaseRequest.objects.filter(work_order=wo, part=self.part).count(), 0,
        )

    def test_part_issue_line_new_fields_present_and_backfilled(self):
        # Migration 0012 backfill: an APPROVED line should have
        # requested_qty=quantity, approved_qty=quantity, issued_qty=quantity, shortage_qty=0.
        from inventory.services import issue_part_to_work_order
        wo = self._make_wo()
        ok, _msg = issue_part_to_work_order(
            wo=wo, part=self.part, quantity=Decimal("4"),
            unit_cost=Decimal("10"), invoice_ref="INV-X",
            supplier_name="X", issued_by=self.manager,
        )
        self.assertTrue(ok)
        line = PartIssueLine.objects.get(work_order=wo, part=self.part)
        self.assertEqual(line.requested_qty, Decimal("4"))
        self.assertEqual(line.approved_qty, Decimal("4"))
        self.assertEqual(line.issued_qty, Decimal("4"))
        self.assertEqual(line.shortage_qty, Decimal("0"))

    def test_emergency_auto_approve_sets_emergency_flag(self):
        # P3.1: emergency auto-approve now succeeds (with partial issue)
        # instead of staying PENDING.
        from inventory.services import request_part_on_wo
        self.inv.quantity_available = Decimal("1")
        self.inv.save()
        wo = self._make_wo(is_emergency=True)
        line = request_part_on_wo(
            wo=wo, part=self.part, quantity=Decimal("5"), technician=self.tech,
        )
        line.refresh_from_db()
        self.assertEqual(line.status, PartIssueLine.Status.APPROVED)
        self.assertTrue(line.is_emergency_auto_approved)
        self.assertEqual(line.issued_qty, Decimal("1"))


class ExternalRepairRequestFlowTests(TestCase):
    """Phase 2.2 — technician requests external repair, manager creates ERO.

    Locked workflow:
      1. Technician (assigned WO only) → submits PENDING request
      2. Manager reviews → APPROVE creates DRAFT ERO | REJECT with reason
      3. EROs are NOT created by technicians
    """

    def setUp(self):
        # Use the site auto-seeded by migration 0005
        self.site = Site.objects.get(is_default=True)
        self.manager = User.objects.create_user(
            username="manager1", password="pass1234", role=User.Role.MANAGER
        )
        self.tech = User.objects.create_user(
            username="tech1", password="pass1234", role=User.Role.TECHNICIAN
        )
        self.other_tech = User.objects.create_user(
            username="tech2", password="pass1234", role=User.Role.TECHNICIAN
        )
        self.supply = User.objects.create_user(
            username="supply1", password="pass1234", role=User.Role.PROCUREMENT
        )
        self.operator = User.objects.create_user(
            username="op1", password="pass1234", role=User.Role.OPERATOR
        )
        self.machine = Machine.objects.create(name="Press 1", qr_code="PRESS-01")

    def _make_wo(self, *, technician=None, status=WorkOrder.Status.ASSIGNED):
        return WorkOrder.objects.create(
            machine=self.machine,
            status=status,
            assigned_technician=technician or self.tech,
            created_by=self.manager,
        )

    # ----- Service-layer tests -----

    def test_technician_can_request_external_repair_on_own_wo(self):
        from maintenance.services import request_external_repair
        wo = self._make_wo()
        err = request_external_repair(
            work_order=wo,
            requested_by=self.tech,
            diagnosis_note="Bearing worn beyond repair; needs refurbishing at vendor",
            part_description="Bearing 6205 — qty 1",
        )
        self.assertEqual(err.status, "pending")
        self.assertEqual(err.requested_by, self.tech)
        self.assertEqual(err.work_order, wo)
        self.assertIsNone(err.reviewed_by)
        self.assertIsNone(err.repair_order)

    def test_other_technician_cannot_request_on_someone_elses_wo(self):
        from maintenance.services import request_external_repair
        wo = self._make_wo(technician=self.tech)
        with self.assertRaises(ValueError):
            request_external_repair(
                work_order=wo,
                requested_by=self.other_tech,
                diagnosis_note="I'm not assigned, just trying",
                part_description="X",
            )

    def test_diagnosis_note_is_required(self):
        from maintenance.services import request_external_repair
        wo = self._make_wo()
        with self.assertRaises(ValueError):
            request_external_repair(
                work_order=wo,
                requested_by=self.tech,
                diagnosis_note="   ",
                part_description="Bearing 6205",
            )

    def test_part_description_is_required(self):
        from maintenance.services import request_external_repair
        wo = self._make_wo()
        with self.assertRaises(ValueError):
            request_external_repair(
                work_order=wo,
                requested_by=self.tech,
                diagnosis_note="broken",
                part_description="",
            )

    def test_manager_approve_creates_draft_ero_linked_to_wo(self):
        from maintenance.services import (
            request_external_repair,
            approve_external_repair_request,
        )
        from maintenance.models import ExternalRepairOrder
        wo = self._make_wo()
        err = request_external_repair(
            work_order=wo,
            requested_by=self.tech,
            diagnosis_note="Worn bearing",
            part_description="Bearing 6205",
        )
        ero = approve_external_repair_request(
            err=err, manager=self.manager, manager_note="Approved by ops director"
        )
        err.refresh_from_db()
        self.assertEqual(err.status, "approved")
        self.assertEqual(err.reviewed_by, self.manager)
        self.assertIsNotNone(err.reviewed_at)
        self.assertEqual(err.repair_order, ero)
        self.assertEqual(ero.work_order, wo)
        self.assertEqual(ero.created_by, self.manager)
        self.assertEqual(ero.handled_by, self.manager)
        self.assertEqual(ero.status, ExternalRepairOrder.Status.DRAFT)
        self.assertEqual(ero.title, "External repair for Bearing 6205")

    def test_manager_reject_requires_reason(self):
        from maintenance.services import (
            request_external_repair,
            reject_external_repair_request,
        )
        wo = self._make_wo()
        err = request_external_repair(
            work_order=wo,
            requested_by=self.tech,
            diagnosis_note="X",
            part_description="Y",
        )
        with self.assertRaises(ValueError):
            reject_external_repair_request(
                err=err, manager=self.manager, manager_note=""
            )
        with self.assertRaises(ValueError):
            reject_external_repair_request(
                err=err, manager=self.manager, manager_note="   "
            )

    def test_manager_reject_sets_status_and_reason(self):
        from maintenance.services import (
            request_external_repair,
            reject_external_repair_request,
        )
        wo = self._make_wo()
        err = request_external_repair(
            work_order=wo,
            requested_by=self.tech,
            diagnosis_note="Worn",
            part_description="Bearing",
        )
        reject_external_repair_request(
            err=err, manager=self.manager, manager_note="Try repair in-house first"
        )
        err.refresh_from_db()
        self.assertEqual(err.status, "rejected")
        self.assertEqual(err.reviewed_by, self.manager)
        self.assertEqual(err.manager_note, "Try repair in-house first")
        self.assertIsNone(err.repair_order)

    def test_cannot_approve_non_pending(self):
        from maintenance.services import (
            request_external_repair,
            approve_external_repair_request,
            reject_external_repair_request,
        )
        wo = self._make_wo()
        err = request_external_repair(
            work_order=wo, requested_by=self.tech,
            diagnosis_note="X", part_description="Y",
        )
        reject_external_repair_request(
            err=err, manager=self.manager, manager_note="No"
        )
        with self.assertRaises(ValueError):
            approve_external_repair_request(
                err=err, manager=self.manager
            )

    # ----- View-layer tests -----

    def test_view_tech_creates_request(self):
        from maintenance.models import ExternalRepairRequest
        wo = self._make_wo()
        self.client.force_login(self.tech)
        response = self.client.post(
            reverse("work_order_request_external_repair", args=[wo.pk]),
            {
                "diagnosis_note": "Bearing worn",
                "part_description": "Bearing 6205 — qty 1",
            },
        )
        self.assertEqual(response.status_code, 302)
        err = ExternalRepairRequest.objects.get(work_order=wo)
        self.assertEqual(err.status, "pending")
        self.assertEqual(err.requested_by, self.tech)

    def test_view_other_tech_gets_404(self):
        wo = self._make_wo(technician=self.tech)
        self.client.force_login(self.other_tech)
        response = self.client.post(
            reverse("work_order_request_external_repair", args=[wo.pk]),
            {"diagnosis_note": "X", "part_description": "Y"},
        )
        self.assertEqual(response.status_code, 404)

    def test_view_manager_approve_creates_ero(self):
        from maintenance.models import (
            ExternalRepairOrder,
            ExternalRepairRequest,
        )
        wo = self._make_wo()
        # Create PENDING request directly
        err = ExternalRepairRequest.objects.create(
            work_order=wo,
            requested_by=self.tech,
            diagnosis_note="X",
            part_description="Y",
        )
        self.client.force_login(self.manager)
        response = self.client.post(
            reverse("work_order_decide_external_repair", args=[wo.pk, err.pk]),
            {"action": "approve", "manager_note": "OK"},
        )
        self.assertEqual(response.status_code, 302)
        err.refresh_from_db()
        self.assertEqual(err.status, "approved")
        self.assertIsNotNone(err.repair_order)
        self.assertEqual(
            err.repair_order.status, ExternalRepairOrder.Status.DRAFT
        )

    def test_view_manager_reject_requires_reason(self):
        from maintenance.models import ExternalRepairRequest
        wo = self._make_wo()
        err = ExternalRepairRequest.objects.create(
            work_order=wo,
            requested_by=self.tech,
            diagnosis_note="X",
            part_description="Y",
        )
        self.client.force_login(self.manager)
        response = self.client.post(
            reverse("work_order_decide_external_repair", args=[wo.pk, err.pk]),
            {"action": "reject", "manager_note": ""},
        )
        self.assertEqual(response.status_code, 302)
        err.refresh_from_db()
        self.assertEqual(err.status, "pending")  # unchanged

    def test_view_manager_reject_with_reason_works(self):
        from maintenance.models import ExternalRepairRequest
        wo = self._make_wo()
        err = ExternalRepairRequest.objects.create(
            work_order=wo,
            requested_by=self.tech,
            diagnosis_note="X",
            part_description="Y",
        )
        self.client.force_login(self.manager)
        response = self.client.post(
            reverse("work_order_decide_external_repair", args=[wo.pk, err.pk]),
            {"action": "reject", "manager_note": "We have spares in stock"},
        )
        self.assertEqual(response.status_code, 302)
        err.refresh_from_db()
        self.assertEqual(err.status, "rejected")
        self.assertEqual(err.manager_note, "We have spares in stock")

    def test_view_supply_officer_cannot_decide_request(self):
        from maintenance.models import ExternalRepairRequest
        wo = self._make_wo()
        err = ExternalRepairRequest.objects.create(
            work_order=wo,
            requested_by=self.tech,
            diagnosis_note="X",
            part_description="Y",
        )
        self.client.force_login(self.supply)
        response = self.client.post(
            reverse("work_order_decide_external_repair", args=[wo.pk, err.pk]),
            {"action": "approve", "manager_note": "OK"},
        )
        # role_required(manager) should bounce
        self.assertIn(response.status_code, (302, 403))
        err.refresh_from_db()
        self.assertEqual(err.status, "pending")

    def test_view_operator_cannot_submit_request(self):
        wo = self._make_wo()
        self.client.force_login(self.operator)
        response = self.client.post(
            reverse("work_order_request_external_repair", args=[wo.pk]),
            {"diagnosis_note": "X", "part_description": "Y"},
        )
        # technician check inside the view should 404 for non-tech
        self.assertIn(response.status_code, (302, 403, 404))

    def test_template_shows_request_form_to_tech_and_panel_to_manager(self):
        from maintenance.models import ExternalRepairRequest
        wo = self._make_wo()
        ExternalRepairRequest.objects.create(
            work_order=wo,
            requested_by=self.tech,
            diagnosis_note="Worn bearing needs vendor",
            part_description="Bearing 6205",
        )
        # Tech sees the request form
        self.client.force_login(self.tech)
        response = self.client.get(reverse("work_order_detail", args=[wo.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Request external repair")
        # Manager sees the pending panel
        self.client.force_login(self.manager)
        response = self.client.get(reverse("work_order_detail", args=[wo.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Manager queue — pending external-repair requests")


class MyWorkOrdersViewTests(TestCase):
    """Phase 2.5 — /work-orders/my/ dedicated URL for technicians."""

    def setUp(self):
        self.tech = User.objects.create_user(
            username="tech1", password="pass1234", role=User.Role.TECHNICIAN
        )
        self.other_tech = User.objects.create_user(
            username="tech2", password="pass1234", role=User.Role.TECHNICIAN
        )
        self.manager = User.objects.create_user(
            username="manager1", password="pass1234", role=User.Role.MANAGER
        )
        self.machine = Machine.objects.create(name="Press 1", qr_code="PRESS-01")

    def _wo(self, *, technician=None, status=WorkOrder.Status.ASSIGNED):
        return WorkOrder.objects.create(
            machine=self.machine,
            status=status,
            assigned_technician=technician or self.tech,
            created_by=self.manager,
        )

    def test_technician_sees_only_their_own_wo(self):
        mine = self._wo()
        not_mine = self._wo(technician=self.other_tech)
        self.client.force_login(self.tech)
        response = self.client.get(reverse("my_work_orders"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, f"WO-{mine.number}")
        self.assertNotContains(response, f"WO-{not_mine.number}")

    def test_excludes_closed_wos(self):
        open_wo = self._wo(status=WorkOrder.Status.ASSIGNED)
        closed_wo = self._wo(status=WorkOrder.Status.CLOSED)
        self.client.force_login(self.tech)
        response = self.client.get(reverse("my_work_orders"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, f"WO-{open_wo.number}")
        self.assertNotContains(response, f"WO-{closed_wo.number}")

    def test_in_progress_badge_in_count(self):
        self._wo(status=WorkOrder.Status.ASSIGNED)
        self._wo(status=WorkOrder.Status.IN_PROGRESS)
        self._wo(status=WorkOrder.Status.IN_PROGRESS)
        self.client.force_login(self.tech)
        response = self.client.get(reverse("my_work_orders"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["in_progress_count"], 2)
        self.assertEqual(response.context["queue_total"], 3)

    def test_other_technician_cannot_see_my_wo(self):
        mine = self._wo()
        self.client.force_login(self.other_tech)
        response = self.client.get(reverse("my_work_orders"))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, f"WO-{mine.number}")

    def test_manager_cannot_access_my_work_orders(self):
        # role_required filters out managers
        self.client.force_login(self.manager)
        response = self.client.get(reverse("my_work_orders"))
        # role_required returns 302 (redirect) for wrong roles
        self.assertEqual(response.status_code, 302)


class TechnicianReportTests(TestCase):
    """Phase 2.6 — /reports/technicians/<id>/ drill-down."""

    def setUp(self):
        self.tech = User.objects.create_user(
            username="tech1", password="pass1234", role=User.Role.TECHNICIAN
        )
        self.other_tech = User.objects.create_user(
            username="tech2", password="pass1234", role=User.Role.TECHNICIAN
        )
        self.manager = User.objects.create_user(
            username="manager1", password="pass1234", role=User.Role.MANAGER
        )
        self.machine = Machine.objects.create(name="Press 1", qr_code="PRESS-01")

    def test_service_technician_stats_empty(self):
        from maintenance.services import technician_stats
        stats = technician_stats(self.tech)
        self.assertEqual(stats["completed_count"], 0)
        self.assertEqual(stats["in_progress_count"], 0)
        self.assertEqual(stats["reopened_count"], 0)
        self.assertEqual(stats["external_repair_count"], 0)
        self.assertIsNone(stats["avg_repair_minutes"])
        self.assertIsNone(stats["avg_response_minutes"])

    def test_service_counts_completed_wos(self):
        from maintenance.services import technician_stats
        from datetime import timedelta
        from django.utils import timezone
        now = timezone.now()
        # 3 closed WOs
        for i in range(3):
            WorkOrder.objects.create(
                machine=self.machine,
                status=WorkOrder.Status.CLOSED,
                assigned_technician=self.tech,
                created_by=self.manager,
                labor_started_at=now - timedelta(hours=2),
                labor_stopped_at=now - timedelta(hours=1),
            )
        # 1 in progress
        WorkOrder.objects.create(
            machine=self.machine,
            status=WorkOrder.Status.IN_PROGRESS,
            assigned_technician=self.tech,
            created_by=self.manager,
        )
        stats = technician_stats(self.tech)
        self.assertEqual(stats["completed_count"], 3)
        self.assertEqual(stats["in_progress_count"], 1)
        self.assertEqual(stats["avg_repair_minutes"], 60.0)  # 1 hour

    def test_service_counts_rejections(self):
        from maintenance.services import technician_stats
        WorkOrder.objects.create(
            machine=self.machine,
            status=WorkOrder.Status.CLOSED,
            assigned_technician=self.tech,
            created_by=self.manager,
            rejection_count=2,
        )
        WorkOrder.objects.create(
            machine=self.machine,
            status=WorkOrder.Status.IN_PROGRESS,
            assigned_technician=self.tech,
            created_by=self.manager,
            rejection_count=1,
        )
        stats = technician_stats(self.tech)
        self.assertEqual(stats["reopened_count"], 3)

    def test_view_manager_can_see_report(self):
        from maintenance.services import technician_stats
        self.client.force_login(self.manager)
        response = self.client.get(
            reverse("technician_report_detail", args=[self.tech.pk])
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "tech1")
        self.assertContains(response, "Completed WOs")

    def test_view_technician_can_see_own_report(self):
        self.client.force_login(self.tech)
        response = self.client.get(
            reverse("technician_report_detail", args=[self.tech.pk])
        )
        self.assertEqual(response.status_code, 200)

    def test_view_returns_404_for_non_technician_user(self):
        operator = User.objects.create_user(
            username="op1", password="pass1234", role=User.Role.OPERATOR
        )
        self.client.force_login(self.manager)
        response = self.client.get(
            reverse("technician_report_detail", args=[operator.pk])
        )
        self.assertEqual(response.status_code, 404)

    def test_reports_page_links_to_drill_down(self):
        WorkOrder.objects.create(
            machine=self.machine,
            status=WorkOrder.Status.CLOSED,
            assigned_technician=self.tech,
            created_by=self.manager,
        )
        self.client.force_login(self.manager)
        response = self.client.get(reverse("reports"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, f'href="/reports/technicians/{self.tech.pk}/"')


class ExternalRepairAcceptanceTests(TestCase):
    """P3.2 — manager acceptance of a RETURNED ExternalRepairOrder (UC-20).

    Verifies that actual_cost and invoice_ref are mandatory on accept,
    and that the cost flows into WorkOrderCost.vendor_cost and the
    machine cost report.
    """

    def setUp(self):
        self.site = Site.objects.get(is_default=True)
        self.manager = User.objects.create_user(
            username="mgr1", password="pass1234", role=User.Role.MANAGER
        )
        self.supply = User.objects.create_user(
            username="sup1", password="pass1234", role=User.Role.PROCUREMENT
        )
        self.tech = User.objects.create_user(
            username="tech1", password="pass1234", role=User.Role.TECHNICIAN
        )
        self.machine = Machine.objects.create(name="Press 1", qr_code="PRESS-PR3")
        self.wo = WorkOrder.objects.create(
            machine=self.machine, status=WorkOrder.Status.WAITING_FOR_VENDOR,
            assigned_technician=self.tech, created_by=self.manager,
        )
        self.ero = ExternalRepairOrder.objects.create(
            work_order=self.wo, title="Repair bearing",
            description="Bearing is shot, send to vendor.",
            vendor_name="AcmeRepair",
            estimated_cost=Decimal("200.00"),
            status=ExternalRepairOrder.Status.RETURNED,
            created_by=self.manager, handled_by=self.supply,
        )

    def test_accept_get_renders_form(self):
        self.client.force_login(self.manager)
        response = self.client.get(
            reverse("repair_manager_accept", args=[self.ero.pk])
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "actual_cost")
        self.assertContains(response, "invoice_ref")
        self.assertContains(response, "UC-20")

    def test_accept_post_requires_invoice_ref(self):
        self.client.force_login(self.manager)
        response = self.client.post(
            reverse("repair_manager_accept", args=[self.ero.pk]),
            {"actual_cost": "175.50", "invoice_ref": "", "note": ""},
        )
        self.assertEqual(response.status_code, 200)  # form re-rendered
        self.ero.refresh_from_db()
        self.assertEqual(self.ero.status, ExternalRepairOrder.Status.RETURNED)
        self.assertIsNone(self.ero.actual_cost)

    def test_accept_post_requires_positive_cost(self):
        self.client.force_login(self.manager)
        response = self.client.post(
            reverse("repair_manager_accept", args=[self.ero.pk]),
            {"actual_cost": "0", "invoice_ref": "INV-001", "note": ""},
        )
        self.assertEqual(response.status_code, 200)  # form re-rendered
        self.ero.refresh_from_db()
        self.assertEqual(self.ero.status, ExternalRepairOrder.Status.RETURNED)

    def test_accept_post_closes_ero_and_records_cost(self):
        self.client.force_login(self.manager)
        response = self.client.post(
            reverse("repair_manager_accept", args=[self.ero.pk]),
            {"actual_cost": "175.50", "invoice_ref": "INV-2026-001", "note": "ok"},
        )
        self.assertEqual(response.status_code, 302)  # redirect to repair_list
        self.ero.refresh_from_db()
        self.assertEqual(self.ero.status, ExternalRepairOrder.Status.CLOSED)
        self.assertEqual(self.ero.actual_cost, Decimal("175.50"))
        self.assertEqual(self.ero.invoice_ref, "INV-2026-001")
        self.assertEqual(self.ero.closed_by, self.manager)
        self.assertIsNotNone(self.ero.closed_at)

    def test_accept_post_pushes_cost_into_workordercost(self):
        self.client.force_login(self.manager)
        # Pre-create a WorkOrderCost to ensure recalculate path runs.
        WorkOrderCost.objects.create(work_order=self.wo, vendor_cost=Decimal("0"))
        self.client.post(
            reverse("repair_manager_accept", args=[self.ero.pk]),
            {"actual_cost": "175.50", "invoice_ref": "INV-X", "note": ""},
        )
        cost = WorkOrderCost.objects.get(work_order=self.wo)
        self.assertEqual(cost.vendor_cost, Decimal("175.50"))

    def test_accept_post_creates_workordercost_if_missing(self):
        self.client.force_login(self.manager)
        self.assertFalse(WorkOrderCost.objects.filter(work_order=self.wo).exists())
        self.client.post(
            reverse("repair_manager_accept", args=[self.ero.pk]),
            {"actual_cost": "100.00", "invoice_ref": "INV-NEW", "note": ""},
        )
        self.assertTrue(WorkOrderCost.objects.filter(work_order=self.wo).exists())
        cost = WorkOrderCost.objects.get(work_order=self.wo)
        self.assertEqual(cost.vendor_cost, Decimal("100.00"))

    def test_accept_post_rejected_if_not_in_returned_status(self):
        self.ero.status = ExternalRepairOrder.Status.SENT_TO_VENDOR
        self.ero.save()
        self.client.force_login(self.manager)
        response = self.client.post(
            reverse("repair_manager_accept", args=[self.ero.pk]),
            {"actual_cost": "100.00", "invoice_ref": "INV-Y", "note": ""},
        )
        self.assertEqual(response.status_code, 302)  # redirect with error message
        self.ero.refresh_from_db()
        self.assertEqual(self.ero.status, ExternalRepairOrder.Status.SENT_TO_VENDOR)
        self.assertIsNone(self.ero.actual_cost)

    def test_machine_cost_report_includes_ero_cost(self):
        # Close the ERO and check the machine cost report aggregates it.
        from decimal import Decimal as D
        self.client.force_login(self.manager)
        self.client.post(
            reverse("repair_manager_accept", args=[self.ero.pk]),
            {"actual_cost": "250.00", "invoice_ref": "INV-MC", "note": ""},
        )
        # Hit the machine cost report
        response = self.client.get(reverse("machine_cost_report"))
        self.assertEqual(response.status_code, 200)
        # Pull vendor cost from context or HTML
        body = response.content.decode()
        self.assertIn("250", body)  # 250.00 visible somewhere in the report


class EmergencyEscalationTests(TestCase):
    """P3.3 — operator emergency issue + supervisor escalation paths."""

    def setUp(self):
        self.site = Site.objects.get(is_default=True)
        self.operator = User.objects.create_user(
            username="op1", password="pass1234", role=User.Role.OPERATOR
        )
        self.supervisor = User.objects.create_user(
            username="sup1", password="pass1234", role=User.Role.SUPERVISOR
        )
        self.manager = User.objects.create_user(
            username="mgr1", password="pass1234", role=User.Role.MANAGER
        )
        self.tech = User.objects.create_user(
            username="tech1", password="pass1234", role=User.Role.TECHNICIAN
        )
        self.machine = Machine.objects.create(name="Press 2", qr_code="PRESS-P3")

    def test_operator_can_flag_issue_as_emergency(self):
        self.client.force_login(self.operator)
        response = self.client.post(
            reverse("issue_create"),
            {
                "machine": self.machine.pk,
                "description": "Belt snapped, line is down",
                "is_emergency": "on",
            },
        )
        self.assertEqual(response.status_code, 302)
        issue = MaintenanceIssue.objects.get(machine=self.machine)
        self.assertTrue(issue.is_emergency)
        self.assertEqual(issue.priority, MaintenanceIssue.Priority.CRITICAL)
        self.assertEqual(issue.reported_by, self.operator)

    def test_non_emergency_issue_does_not_set_priority(self):
        self.client.force_login(self.operator)
        response = self.client.post(
            reverse("issue_create"),
            {"machine": self.machine.pk, "description": "Slow leak", "is_emergency": ""},
        )
        self.assertEqual(response.status_code, 302)
        issue = MaintenanceIssue.objects.get(machine=self.machine)
        self.assertFalse(issue.is_emergency)
        self.assertNotEqual(issue.priority, MaintenanceIssue.Priority.CRITICAL)

    def test_supervisor_can_escalate_normal_issue(self):
        issue = MaintenanceIssue.objects.create(
            machine=self.machine, reported_by=self.operator,
            description="Vibration getting worse",
        )
        self.client.force_login(self.supervisor)
        response = self.client.post(
            reverse("issue_escalate", args=[issue.pk])
        )
        self.assertEqual(response.status_code, 302)
        issue.refresh_from_db()
        self.assertTrue(issue.is_emergency)
        self.assertEqual(issue.priority, MaintenanceIssue.Priority.CRITICAL)
        self.assertEqual(issue.escalated_by, self.supervisor)
        self.assertIsNotNone(issue.escalated_at)

    def test_manager_can_escalate_normal_issue(self):
        issue = MaintenanceIssue.objects.create(
            machine=self.machine, reported_by=self.operator,
            description="Motor smoking",
        )
        self.client.force_login(self.manager)
        self.client.post(reverse("issue_escalate", args=[issue.pk]))
        issue.refresh_from_db()
        self.assertTrue(issue.is_emergency)
        self.assertEqual(issue.escalated_by, self.manager)

    def test_technician_cannot_escalate_issue(self):
        issue = MaintenanceIssue.objects.create(
            machine=self.machine, reported_by=self.operator,
            description="Vibration",
        )
        self.client.force_login(self.tech)
        response = self.client.post(
            reverse("issue_escalate", args=[issue.pk])
        )
        # role_required returns 403 / redirect; just check no change.
        issue.refresh_from_db()
        self.assertFalse(issue.is_emergency)

    def test_escalation_is_idempotent(self):
        issue = MaintenanceIssue.objects.create(
            machine=self.machine, reported_by=self.operator,
            description="Already critical",
            is_emergency=True, priority=MaintenanceIssue.Priority.CRITICAL,
            escalated_by=self.supervisor, escalated_at=timezone.now(),
        )
        self.client.force_login(self.supervisor)
        # Second escalation should be a no-op
        self.client.post(reverse("issue_escalate", args=[issue.pk]))
        issue.refresh_from_db()
        self.assertTrue(issue.is_emergency)
        # escalated_by should not change on the no-op path (view returns
        # "already emergency" message)
        self.assertEqual(issue.escalated_by, self.supervisor)

    def test_wo_from_emergency_issue_inherits_emergency(self):
        from maintenance.models import WorkOrder
        issue = MaintenanceIssue.objects.create(
            machine=self.machine, reported_by=self.operator,
            description="Critical", status=MaintenanceIssue.Status.VALIDATED,
            is_emergency=True, priority=MaintenanceIssue.Priority.CRITICAL,
            validated_by=self.manager, validated_at=timezone.now(),
        )
        self.client.force_login(self.manager)
        response = self.client.post(
            reverse("work_order_create", args=[issue.pk])
        )
        self.assertEqual(response.status_code, 302)
        wo = WorkOrder.objects.get(issue=issue)
        self.assertTrue(wo.is_emergency)

    def test_wo_from_normal_issue_is_not_emergency(self):
        issue = MaintenanceIssue.objects.create(
            machine=self.machine, reported_by=self.operator,
            description="Slow leak", status=MaintenanceIssue.Status.VALIDATED,
            priority=MaintenanceIssue.Priority.MEDIUM,
            validated_by=self.manager, validated_at=timezone.now(),
        )
        self.client.force_login(self.manager)
        self.client.post(reverse("work_order_create", args=[issue.pk]))
        from maintenance.models import WorkOrder
        wo = WorkOrder.objects.get(issue=issue)
        self.assertFalse(wo.is_emergency)


class ReportsAccessTests(TestCase):
    """P3.4 — section-level role filter on /reports/ and /kpis/."""

    def setUp(self):
        self.site = Site.objects.get(is_default=True)
        self.manager = User.objects.create_user(
            username="mgr-r", password="pass1234", role=User.Role.MANAGER
        )
        self.supervisor = User.objects.create_user(
            username="sup-r", password="pass1234", role=User.Role.SUPERVISOR
        )
        self.supply = User.objects.create_user(
            username="supply-r", password="pass1234", role=User.Role.PROCUREMENT
        )
        self.operator = User.objects.create_user(
            username="op-r", password="pass1234", role=User.Role.OPERATOR
        )
        self.super_admin = User.objects.create_superuser(
            username="admin-r", password="pass1234",
        )
        self.super_admin.role = User.Role.SUPER_ADMIN
        self.super_admin.save()

    def test_manager_sees_all_sections(self):
        self.client.force_login(self.manager)
        response = self.client.get(reverse("reports"))
        self.assertEqual(response.status_code, 200)
        self.assertIn("wo_performance", response.context)
        self.assertIn("tech_performance", response.context)
        self.assertIn("spare_parts", response.context)

    def test_supervisor_sees_wo_tech_and_spare(self):
        self.client.force_login(self.supervisor)
        response = self.client.get(reverse("reports"))
        self.assertEqual(response.status_code, 200)
        self.assertIn("wo_performance", response.context)
        self.assertIn("tech_performance", response.context)
        self.assertIn("spare_parts", response.context)

    def test_supply_sees_only_spare_parts(self):
        # Supply (Maintenance Supply Officer) sees spare parts but not
        # the manager-only WO performance / technician sections.
        self.client.force_login(self.supply)
        response = self.client.get(reverse("reports"))
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("wo_performance", response.context)
        self.assertNotIn("tech_performance", response.context)
        self.assertIn("spare_parts", response.context)

    def test_super_admin_sees_all_sections(self):
        self.client.force_login(self.super_admin)
        response = self.client.get(reverse("reports"))
        self.assertEqual(response.status_code, 200)
        self.assertIn("wo_performance", response.context)
        self.assertIn("tech_performance", response.context)
        self.assertIn("spare_parts", response.context)

    def test_operator_blocked_from_reports(self):
        self.client.force_login(self.operator)
        response = self.client.get(reverse("reports"))
        # role_required redirects to /no-role/ on forbidden access
        self.assertIn(response.status_code, (302, 403))

    def test_kpi_dashboard_supervisor_has_access(self):
        self.client.force_login(self.supervisor)
        response = self.client.get(reverse("kpi_dashboard"))
        self.assertEqual(response.status_code, 200)

    def test_supply_does_not_see_tech_drill_down_in_reports_html(self):
        self.client.force_login(self.supply)
        response = self.client.get(reverse("reports"))
        # Supply doesn't see tech_performance block
        self.assertNotContains(response, "Technician throughput")


class PauseReasonEnumTests(TestCase):
    """P3.5 — Phase 2.10 Q6: drop AWAITING_PARTS / AWAITING_VENDOR from
    the WorkOrder.PauseReason enum. Those are WO statuses, not pause reasons.
    """

    def setUp(self):
        self.site = Site.objects.get(is_default=True)
        self.manager = User.objects.create_user(
            username="mgr-pr", password="pass1234", role=User.Role.MANAGER
        )
        self.tech = User.objects.create_user(
            username="tech-pr", password="pass1234", role=User.Role.TECHNICIAN
        )
        self.machine = Machine.objects.create(name="Press 3", qr_code="PRESS-P5")
        self.wo = WorkOrder.objects.create(
            machine=self.machine, status=WorkOrder.Status.IN_PROGRESS,
            assigned_technician=self.tech, created_by=self.manager,
        )

    def test_enum_excludes_awaiting_parts(self):
        choices = dict(WorkOrder.PauseReason.choices)
        self.assertNotIn("awaiting_parts", choices)

    def test_enum_excludes_awaiting_vendor(self):
        choices = dict(WorkOrder.PauseReason.choices)
        self.assertNotIn("awaiting_vendor", choices)

    def test_enum_has_emergency_operational_other(self):
        choices = dict(WorkOrder.PauseReason.choices)
        self.assertIn("emergency", choices)
        self.assertIn("operational", choices)
        self.assertIn("other", choices)

    def test_pause_with_awaiting_parts_raises(self):
        from maintenance.services import pause_other_in_progress
        # P3.5 guard: AWAITING_PARTS is a status, not a pause reason.
        with self.assertRaises(ValueError):
            pause_other_in_progress(
                technician=self.tech,
                except_pk=self.wo.pk,
                reason="awaiting_parts",
            )

    def test_pause_with_awaiting_vendor_raises(self):
        from maintenance.services import pause_other_in_progress
        with self.assertRaises(ValueError):
            pause_other_in_progress(
                technician=self.tech,
                except_pk=self.wo.pk,
                reason="awaiting_vendor",
            )

    def test_pause_form_choices_exclude_awaiting(self):
        from maintenance.forms import WorkOrderPauseForm
        form = WorkOrderPauseForm()
        choice_values = [c[0] for c in form.fields["pause_reason"].choices]
        self.assertNotIn("awaiting_parts", choice_values)
        self.assertNotIn("awaiting_vendor", choice_values)


class DashboardActionCountersTests(TestCase):
    """P3.6 — manager dashboard action counters + technician counters."""

    def setUp(self):
        self.site = Site.objects.get(is_default=True)
        self.manager = User.objects.create_user(
            username="mgr-d", password="pass1234", role=User.Role.MANAGER
        )
        self.tech = User.objects.create_user(
            username="tech-d", password="pass1234", role=User.Role.TECHNICIAN
        )
        self.machine = Machine.objects.create(name="Press 4", qr_code="PRESS-P6")
        self.wo = WorkOrder.objects.create(
            machine=self.machine, status=WorkOrder.Status.IN_PROGRESS,
            assigned_technician=self.tech, created_by=self.manager,
        )
        # Pending part request
        from inventory.models import SparePart, Inventory
        self.part = SparePart.objects.create(
            sku="BRG-DASH-01", name="Bearing", status="active"
        )
        self.inv = Inventory.objects.create(
            part=self.part, site=self.site, quantity_available=Decimal("5")
        )
        from inventory.services import request_part_on_wo
        request_part_on_wo(
            wo=self.wo, part=self.part, quantity=Decimal("3"), technician=self.tech,
        )
        # Pending external repair request
        from maintenance.models import ExternalRepairRequest
        ExternalRepairRequest.objects.create(
            work_order=self.wo, requested_by=self.tech,
            diagnosis_note="Bearing shot", part_description="Bearing #1",
        )

    def test_manager_dashboard_shows_action_counters(self):
        self.client.force_login(self.manager)
        response = self.client.get(reverse("dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertIn("pending_part_requests", response.context)
        self.assertEqual(response.context["pending_part_requests"], 1)
        self.assertIn("pending_external_repair_requests", response.context)
        self.assertEqual(response.context["pending_external_repair_requests"], 1)
        # Quick-actions card rendered
        self.assertContains(response, "Quick actions")
        self.assertContains(response, "part request")
        self.assertContains(response, "ext. repair request")

    def test_technician_dashboard_shows_personal_counters(self):
        self.client.force_login(self.tech)
        response = self.client.get(reverse("dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["my_in_progress_wos"], 1)
        self.assertEqual(response.context["my_pending_requests"], 1)
        # No quick-actions card for tech
        self.assertNotIn("pending_part_requests", response.context)

    def test_manager_dashboard_all_caught_up_when_no_pending(self):
        # Remove the pending items
        from inventory.models import PartIssueLine
        PartIssueLine.objects.all().delete()
        from maintenance.models import ExternalRepairRequest
        ExternalRepairRequest.objects.all().delete()
        self.client.force_login(self.manager)
        response = self.client.get(reverse("dashboard"))
        self.assertEqual(response.context["pending_part_requests"], 0)
        self.assertEqual(response.context["pending_external_repair_requests"], 0)
        self.assertContains(response, "All caught up")
