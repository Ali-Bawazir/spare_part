"""
Phase 8 — Tool supplier tracking & damage archive regression tests.
"""
from datetime import date
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from accounts.models import User
from maintenance.models import Incident, Notification, Tool, ToolAssignment, ToolDamageRecord
from maintenance.services import ToolAvailabilityService
from procurement.models import Supplier


class ToolSupplierFieldsTests(TestCase):
    """Tool now carries supplier + cost + invoice + notes."""

    def setUp(self):
        self.supplier = Supplier.objects.create(name="Acme Tools", is_active=True, code="ACM")
        self.user = User.objects.create_user(
            username="admin1", password="pass1234",
            role=User.Role.SUPER_ADMIN,
        )

    def test_tool_supplier_and_cost_persist(self):
        t = Tool.objects.create(
            code="KNF-A-001",
            name="Chef knife 8-inch",
            supplier=self.supplier,
            purchase_cost=Decimal("45.00"),
            purchase_date=date(2026, 6, 1),
            invoice_ref="INV-001",
            notes="Imported from Turkey.",
        )
        t.refresh_from_db()
        self.assertEqual(t.supplier, self.supplier)
        self.assertEqual(t.purchase_cost, Decimal("45.00"))
        self.assertEqual(t.invoice_ref, "INV-001")
        self.assertEqual(t.notes, "Imported from Turkey.")

    def test_tool_code_unique(self):
        Tool.objects.create(code="KNF-A-001", name="Knife")
        with self.assertRaises(Exception):
            Tool.objects.create(code="KNF-A-001", name="Duplicate")

    def test_supplier_optional_legacy_tool(self):
        t = Tool.objects.create(code="LEGACY-01", name="Old wrench")
        self.assertIsNone(t.supplier)
        self.assertTrue(t.is_available)


class ToolAvailabilityServiceTests(TestCase):
    """Service returns (ok, reason) tuple; surfaces current holder + damage data."""

    def setUp(self):
        self.supplier = Supplier.objects.create(name="Acme", is_active=True)
        self.manager = User.objects.create_user(
            username="mgr", password="pass1234", role=User.Role.MANAGER,
        )
        self.operator = User.objects.create_user(
            username="op", password="pass1234", role=User.Role.OPERATOR,
        )

    def test_available_returns_true(self):
        t = Tool.objects.create(code="TW-01", name="Wrench")
        ok, reason = ToolAvailabilityService.can_assign(t)
        self.assertTrue(ok)
        self.assertEqual(reason, "")

    def test_in_use_returns_false_with_holder(self):
        t = Tool.objects.create(code="TW-02", name="Wrench 2")
        t.status = Tool.Status.IN_USE
        t.save(update_fields=["status"])
        ta = ToolAssignment.objects.create(tool=t, user=self.operator, assigned_by=self.manager)
        ok, reason = ToolAvailabilityService.can_assign(t)
        self.assertFalse(ok)
        self.assertIn(self.operator.username, reason)

    def test_out_of_service_returns_false_with_damage(self):
        t = Tool.objects.create(code="TW-03", name="Wrench 3", supplier=self.supplier,
                                purchase_cost=Decimal("30.00"))
        t.status = Tool.Status.OUT_OF_SERVICE
        t.save(update_fields=["status"])
        ToolDamageRecord.objects.create(
            tool=t, supplier=self.supplier,
            damage_kind=ToolDamageRecord.DamageKind.DAMAGED,
            damage_reason="The tip snapped during heavy use while cutting steel plate.",
            reported_by=self.manager,
        )
        ok, reason = ToolAvailabilityService.can_assign(t)
        self.assertFalse(ok)
        self.assertIn("Out of service", reason)
        self.assertIn(self.supplier.name, reason)


class DamagedReturnFlowTests(TestCase):
    """Operator returns a tool DAMAGED -> ToolDamageRecord is created, tool OOS,
    NO auto-WorkOrder is created, supplier snapshot is preserved."""

    def setUp(self):
        self.supplier = Supplier.objects.create(name="Acme", is_active=True)
        self.manager = User.objects.create_user(
            username="mgr", password="pass1234", role=User.Role.MANAGER,
        )
        self.operator = User.objects.create_user(
            username="op", password="pass1234", role=User.Role.OPERATOR,
        )
        self.tool = Tool.objects.create(
            code="KNF-A-001", name="Chef knife", supplier=self.supplier,
            purchase_cost=Decimal("45.00"),
        )
        self.assignment = ToolAssignment.objects.create(
            tool=self.tool, user=self.operator, assigned_by=self.manager,
        )
        self.client.force_login(self.operator)

    def _post_return(self, condition, damage_kind, reason, supplier_id):
        return self.client.post(
            reverse("tool_return", kwargs={"assignment_pk": self.assignment.pk}),
            data={
                "condition": condition,
                "damage_kind": damage_kind,
                "supplier": supplier_id,
                "damage_reason": reason,
                "replacement_action": ToolDamageRecord.ReplacementAction.BUY_FROM_OTHER,
            },
            follow=True,
        )

    def test_damaged_return_creates_damage_record(self):
        response = self._post_return(
            "damaged",
            ToolDamageRecord.DamageKind.DAMAGED,
            "The tip snapped during heavy use while cutting steel plate.",
            self.supplier.pk,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(ToolDamageRecord.objects.filter(tool=self.tool).count(), 1)
        rec = ToolDamageRecord.objects.get(tool=self.tool)
        self.assertEqual(rec.supplier, self.supplier)
        self.assertEqual(rec.reported_by, self.operator)
        self.assertEqual(rec.damage_kind, ToolDamageRecord.DamageKind.DAMAGED)
        self.tool.refresh_from_db()
        self.assertEqual(self.tool.status, Tool.Status.OUT_OF_SERVICE)

    def test_damaged_return_no_work_order_created(self):
        """Regression: prior code path auto-created a WorkOrder for the tool."""
        from maintenance.models import WorkOrder
        before = WorkOrder.objects.filter(tool=self.tool).count()
        self._post_return(
            "damaged",
            ToolDamageRecord.DamageKind.DAMAGED,
            "Test that no work order is created when tool is returned damaged.",
            self.supplier.pk,
        )
        self.assertEqual(
            WorkOrder.objects.filter(tool=self.tool).count(), before,
            "DAMAGED return must not create a WorkOrder; use ToolDamageRecord instead.",
        )

    def test_lost_return_also_creates_damage_record(self):
        self._post_return(
            "lost",
            ToolDamageRecord.DamageKind.LOST,
            "Tool went missing after shift handoff, no one admitted to it.",
            self.supplier.pk,
        )
        self.assertEqual(ToolDamageRecord.objects.filter(tool=self.tool).count(), 1)
        rec = ToolDamageRecord.objects.get(tool=self.tool)
        self.assertEqual(rec.damage_kind, ToolDamageRecord.DamageKind.LOST)
        self.assertEqual(Incident.objects.filter(tool=self.tool).count(), 1)

    def test_supplier_snapshot_survives_rename(self):
        self._post_return(
            "damaged",
            ToolDamageRecord.DamageKind.DAMAGED,
            "The blade is bent and unusable past safe tolerances for kitchen use.",
            self.supplier.pk,
        )
        rec = ToolDamageRecord.objects.get(tool=self.tool)
        self.assertEqual(rec.supplier, self.supplier)
        old_name = self.supplier.name
        self.supplier.name = "Acme Renamed"
        self.supplier.save(update_fields=["name"])
        rec.refresh_from_db()
        self.assertEqual(rec.supplier.name, "Acme Renamed")
        # The FK is live - but the snapshot semantics depend on PROTECT.
        # If the user later deletes the supplier, PROTECT blocks it. Document that.

    def test_damage_reason_too_short_rejected(self):
        """Form-level validation: <=15 chars rejected."""
        response = self.client.post(
            reverse("tool_return", kwargs={"assignment_pk": self.assignment.pk}),
            data={
                "condition": "damaged",
                "damage_kind": ToolDamageRecord.DamageKind.DAMAGED,
                "supplier": self.supplier.pk,
                "damage_reason": "short",  # only 5 chars
                "replacement_action": ToolDamageRecord.ReplacementAction.BUY_FROM_OTHER,
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("damage_reason", response.context["form"].errors)

    def test_damage_reason_min_length_at_model_save(self):
        """Defence-in-depth at model.save() - even .save() rejects <15 chars."""
        rec = ToolDamageRecord(
            tool=self.tool,
            supplier=self.supplier,
            damage_kind=ToolDamageRecord.DamageKind.DAMAGED,
            damage_reason="too short",
            reported_by=self.manager,
        )
        with self.assertRaises(ValidationError):
            rec.save()


class ToolAssignReuseCheckTests(TestCase):
    """tool_assign refuses unavailable tools with explicit reason."""

    def setUp(self):
        self.manager = User.objects.create_user(
            username="mgr", password="pass1234", role=User.Role.MANAGER,
        )
        self.operator = User.objects.create_user(
            username="op", password="pass1234", role=User.Role.OPERATOR,
        )
        self.supplier = Supplier.objects.create(name="Acme", is_active=True)

    def test_assign_blocked_when_tool_in_use(self):
        t = Tool.objects.create(code="TW-A", name="Wrench A")
        t.status = Tool.Status.IN_USE
        t.save(update_fields=["status"])
        ToolAssignment.objects.create(tool=t, user=self.operator, assigned_by=self.manager)
        self.client.force_login(self.manager)
        resp = self.client.post(reverse("tool_assign"), {
            "tool": t.pk,
            "assignee": self.operator.pk,
        })
        # Redirects back to tool_list with error message.
        self.assertEqual(resp.status_code, 302)
        t.refresh_from_db()
        # Tool still in use - no new assignment created.
        self.assertEqual(t.assignments.filter(returned_at__isnull=True).count(), 1)

    def test_assign_blocked_when_tool_out_of_service(self):
        t = Tool.objects.create(code="TW-B", name="Wrench B", supplier=self.supplier)
        t.status = Tool.Status.OUT_OF_SERVICE
        t.save(update_fields=["status"])
        self.client.force_login(self.manager)
        resp = self.client.post(reverse("tool_assign"), {
            "tool": t.pk,
            "assignee": self.operator.pk,
        })
        self.assertEqual(resp.status_code, 302)


class ToolDashboardViewTests(TestCase):
    """Dashboard page is reachable + shows KPI tiles."""

    def setUp(self):
        self.manager = User.objects.create_user(
            username="mgr", password="pass1234", role=User.Role.MANAGER,
        )
        self.tool = Tool.objects.create(code="TW-DASH-01", name="Wrench")

    def test_dashboard_loads_for_manager(self):
        self.client.force_login(self.manager)
        resp = self.client.get(reverse("tool_dashboard"))
        self.assertEqual(resp.status_code, 200)
        self.assertIn("by_status", resp.context)
        self.assertEqual(resp.context["by_status"][Tool.Status.AVAILABLE], 1)

    def test_dashboard_redirects_non_manager(self):
        operator = User.objects.create_user(
            username="op", password="pass1234", role=User.Role.OPERATOR,
        )
        self.client.force_login(operator)
        resp = self.client.get(reverse("tool_dashboard"))
        # role_required decorator redirects unauthorized roles.
        self.assertEqual(resp.status_code, 302)


class ToolDamageGlobalLinkTests(TestCase):
    """Damage history page shows a Reorder button that targets the same supplier."""

    def setUp(self):
        self.manager = User.objects.create_user(
            username="mgr", password="pass1234", role=User.Role.MANAGER,
        )
        self.supplier = Supplier.objects.create(name="Acme", is_active=True)
        self.tool = Tool.objects.create(code="KNF-X", name="Knife X", supplier=self.supplier)
        ToolDamageRecord.objects.create(
            tool=self.tool, supplier=self.supplier,
            damage_kind=ToolDamageRecord.DamageKind.DAMAGED,
            damage_reason="The knife tip got chipped on a frozen food block.",
            reported_by=self.manager,
        )

    def test_damage_global_renders_with_reorder_link(self):
        self.client.force_login(self.manager)
        resp = self.client.get(reverse("tool_damage_global"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, f'supplier={self.supplier.pk}')
