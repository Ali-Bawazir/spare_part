from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from unittest.mock import patch

from accounts.models import User
from maintenance.models import Machine, MaintenanceIssue, Tool, ToolAssignment, WorkOrder


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
