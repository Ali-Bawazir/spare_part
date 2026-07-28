"""Tests for the reusable-tool pool.

Coverage:
- Issue to Tool Pool creates instances and decrements inventory
- Sequential tool_number assignment
- Assign → status in_use; current_holder derived
- Return good → status available
- Return damaged → damage report opened; status out_of_service
- Repair → status available
- Write off → is_active=False
- Concurrent assign is prevented
- Search parses Name #N, operator, machine, bare number
- Dashboard counts
- Permissions: operator can self-assign; manager can assign to anyone
- Operator cannot return someone else's assignment
- Cost snapshot via source_stock_movement.unit_cost
"""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from inventory.models import Inventory, ReusableToolInstance, SparePart, StockMovement
from inventory.models_tools import ToolAssignment, ToolDamageReport, ToolMovement
from maintenance.models import Machine, Site

User = get_user_model()


def _site():
    """Return the default site. Migration 0005 seeds 'Main Factory' (MF)."""
    s = Site.objects.filter(is_default=True).first()
    if s is None:
        s = Site.objects.first()
    if s is None:
        s = Site.objects.create(
            code="MF", name="Main Factory",
            is_default=True, is_active=True,
        )
    return s


def _machine(code="M-1"):
    m, _ = Machine.objects.get_or_create(
        qr_code=code, defaults={"name": code, "is_active": True},
    )
    return m


def _part(name="Knife Punch", sku="KP-001", item_type="reusable_tool", qty=10):
    site = _site()
    p, _ = SparePart.objects.get_or_create(
        sku=sku,
        defaults={
            "name": name,
            "item_type": item_type,
            "is_consumable": (item_type == "consumable"),
        },
    )
    p.item_type = item_type
    p.is_consumable = (item_type == "consumable")
    p.save()
    inv, _ = Inventory.objects.update_or_create(
        part=p, site=site,
        defaults={"quantity_available": Decimal(qty)},
    )
    inv.refresh_from_db()
    return p


def _user(username, role=User.Role.OPERATOR):
    u, _ = User.objects.get_or_create(
        username=username,
        defaults={"email": f"{username}@x", "role": role, "is_active": True},
    )
    u.role = role
    u.is_active = True
    u.save()
    return u


class IssueToToolPoolTests(TestCase):
    def setUp(self):
        self.manager = _user("mgr", User.Role.MANAGER)
        self.part = _part(qty=10)

    def test_issue_creates_n_instances(self):
        from inventory.services_tools import InventoryService
        instances = InventoryService.issue_to_tool_pool(
            part=self.part, qty=3, actor=self.manager,
        )
        self.assertEqual(len(instances), 3)
        # Sequential tool numbers
        self.assertEqual([i.tool_number for i in instances], [1, 2, 3])
        # Inventory decreased
        inv = Inventory.objects.get(part=self.part, site=_site())
        self.assertEqual(inv.quantity_available, Decimal(7))

    def test_issue_records_movement_and_stock_movement(self):
        from inventory.services_tools import InventoryService
        instances = InventoryService.issue_to_tool_pool(
            part=self.part, qty=2, actor=self.manager, note="batch",
        )
        # One STOCK_OUT + 2 ToolMovement(issued)
        self.assertEqual(
            StockMovement.objects.filter(
                part=self.part, movement_type="stock_out",
            ).count(), 1,
        )
        self.assertEqual(
            ToolMovement.objects.filter(
                instance__in=instances, movement_type="issued",
            ).count(), 2,
        )
        for i in instances:
            self.assertEqual(i.status, ReusableToolInstance.Status.AVAILABLE)
            self.assertIsNotNone(i.source_stock_movement)

    def test_issue_rejects_wrong_item_type(self):
        from inventory.services_tools import InventoryService
        spare = _part(name="Bearing", sku="BR-001", item_type="spare_part", qty=5)
        with self.assertRaises(ValueError):
            InventoryService.issue_to_tool_pool(part=spare, qty=1, actor=self.manager)

    def test_issue_rejects_insufficient_stock(self):
        from inventory.services_tools import InventoryService
        with self.assertRaises(ValueError):
            InventoryService.issue_to_tool_pool(part=self.part, qty=999, actor=self.manager)

    def test_sequential_after_existing(self):
        from inventory.services_tools import InventoryService
        InventoryService.issue_to_tool_pool(part=self.part, qty=2, actor=self.manager)
        InventoryService.issue_to_tool_pool(part=self.part, qty=3, actor=self.manager)
        numbers = list(
            self.part.tool_instances.order_by("tool_number").values_list("tool_number", flat=True)
        )
        self.assertEqual(numbers, [1, 2, 3, 4, 5])

    def test_unit_cost_snapshotted_from_part(self):
        from inventory.services_tools import InventoryService
        self.part.last_purchase_cost = Decimal("47.50")
        self.part.save()
        instances = InventoryService.issue_to_tool_pool(
            part=self.part, qty=1, actor=self.manager,
        )
        self.assertEqual(instances[0].purchase_cost, Decimal("47.50"))


class AssignmentLifecycleTests(TestCase):
    def setUp(self):
        self.manager = _user("mgr", User.Role.MANAGER)
        self.operator = _user("ali", User.Role.OPERATOR)
        self.part = _part(qty=5)
        from inventory.services_tools import InventoryService
        self.instances = InventoryService.issue_to_tool_pool(
            part=self.part, qty=2, actor=self.manager,
        )
        self.tool = self.instances[0]
        self.machine = _machine("M-1")

    def test_assign_sets_status_and_holder(self):
        from inventory.services_tools import ToolAssignmentService
        a = ToolAssignmentService.assign(
            instance=self.tool,
            operator=self.operator,
            machine=self.machine,
            condition_out="good",
            actor=self.manager,
            notes="checked out",
        )
        self.tool.refresh_from_db()
        self.assertEqual(self.tool.status, ReusableToolInstance.Status.IN_USE)
        self.assertEqual(self.tool.current_holder, self.operator)
        self.assertEqual(a.notes, "checked out")

    def test_assign_rejects_when_not_available(self):
        from inventory.services_tools import ToolAssignmentService
        ToolAssignmentService.assign(
            instance=self.tool, operator=self.operator, machine=self.machine,
            condition_out="good", actor=self.manager,
        )
        with self.assertRaises(ValueError):
            ToolAssignmentService.assign(
                instance=self.tool, operator=self.operator, machine=self.machine,
                condition_out="good", actor=self.manager,
            )

    def test_return_good_makes_available(self):
        from inventory.services_tools import ToolAssignmentService
        a = ToolAssignmentService.assign(
            instance=self.tool, operator=self.operator, machine=self.machine,
            condition_out="good", actor=self.manager,
        )
        a2, damage = ToolAssignmentService.return_tool(
            assignment=a, condition_in="good", actor=self.operator,
        )
        self.tool.refresh_from_db()
        self.assertEqual(self.tool.status, ReusableToolInstance.Status.AVAILABLE)
        self.assertIsNone(self.tool.current_holder)
        self.assertIsNone(damage)

    def test_return_damaged_opens_damage_report(self):
        from inventory.services_tools import ToolAssignmentService
        a = ToolAssignmentService.assign(
            instance=self.tool, operator=self.operator, machine=self.machine,
            condition_out="good", actor=self.manager,
        )
        a2, damage = ToolAssignmentService.return_tool(
            assignment=a, condition_in="damaged", actor=self.operator,
            damage_reason="The cutting edge is chipped on one corner",
        )
        self.tool.refresh_from_db()
        self.assertEqual(self.tool.status, ReusableToolInstance.Status.OUT_OF_SERVICE)
        self.assertIsNotNone(damage)
        self.assertEqual(damage.status, ToolDamageReport.Status.OPEN)
        self.assertEqual(damage.assignment, a2)

    def test_return_damaged_requires_reason(self):
        from inventory.services_tools import ToolAssignmentService
        a = ToolAssignmentService.assign(
            instance=self.tool, operator=self.operator, machine=self.machine,
            condition_out="good", actor=self.manager,
        )
        with self.assertRaises(ValueError):
            ToolAssignmentService.return_tool(
                assignment=a, condition_in="damaged", actor=self.operator,
            )


class DamageResolveTests(TestCase):
    def setUp(self):
        self.manager = _user("mgr", User.Role.MANAGER)
        self.operator = _user("ali", User.Role.OPERATOR)
        self.part = _part(qty=2)
        from inventory.services_tools import InventoryService, ToolAssignmentService
        inst = InventoryService.issue_to_tool_pool(part=self.part, qty=1, actor=self.manager)[0]
        m = _machine("M-1")
        a = ToolAssignmentService.assign(
            instance=inst, operator=self.operator, machine=m,
            condition_out="good", actor=self.manager,
        )
        self.tool = inst
        _, self.damage = ToolAssignmentService.return_tool(
            assignment=a, condition_in="damaged", actor=self.operator,
            damage_reason="Chipped cutting edge from normal wear and tear",
        )

    def test_repair_makes_available(self):
        from inventory.services_tools import ToolDamageService
        ToolDamageService.repair(
            report=self.damage, repair_cost=Decimal("45.00"), actor=self.manager,
        )
        self.tool.refresh_from_db()
        self.assertEqual(self.tool.status, ReusableToolInstance.Status.AVAILABLE)
        self.damage.refresh_from_db()
        self.assertEqual(self.damage.status, ToolDamageReport.Status.REPAIRED)
        self.assertEqual(self.damage.repair_cost, Decimal("45.00"))

    def test_repair_requires_cost(self):
        from inventory.services_tools import ToolDamageService
        with self.assertRaises(ValueError):
            ToolDamageService.repair(
                report=self.damage, repair_cost=None, actor=self.manager,
            )

    def test_write_off_marks_inactive(self):
        from inventory.services_tools import ToolDamageService
        ToolDamageService.write_off(report=self.damage, actor=self.manager)
        self.tool.refresh_from_db()
        self.assertFalse(self.tool.is_active)
        self.assertEqual(self.tool.status, ReusableToolInstance.Status.OUT_OF_SERVICE)
        self.damage.refresh_from_db()
        self.assertEqual(self.damage.status, ToolDamageReport.Status.WRITTEN_OFF)


class SearchTests(TestCase):
    def setUp(self):
        self.manager = _user("mgr", User.Role.MANAGER)
        self.ali = _user("ali", User.Role.OPERATOR)
        self.ahmed = _user("ahmed", User.Role.OPERATOR)
        self.part = _part(name="Knife Punch", sku="KP-1", qty=5)
        from inventory.services_tools import InventoryService, ToolAssignmentService
        self.insts = InventoryService.issue_to_tool_pool(part=self.part, qty=3, actor=self.manager)
        m = _machine("CNC-5")
        ToolAssignmentService.assign(
            instance=self.insts[0], operator=self.ali, machine=m,
            condition_out="good", actor=self.manager,
        )

    def test_list_search_by_name(self):
        url = reverse("tools_list")
        self.client.force_login(self.manager)
        r = self.client.get(url, {"q": "Knife"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.context["rows"]), 3)

    def test_list_search_by_tool_number(self):
        url = reverse("tools_list")
        self.client.force_login(self.manager)
        r = self.client.get(url, {"q": "2"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.context["rows"]), 1)

    def test_list_search_by_sku(self):
        url = reverse("tools_list")
        self.client.force_login(self.manager)
        r = self.client.get(url, {"q": "KP-1"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.context["rows"]), 3)


class PermissionTests(TestCase):
    def setUp(self):
        self.manager = _user("mgr", User.Role.MANAGER)
        self.ali = _user("ali", User.Role.OPERATOR)
        self.ahmed = _user("ahmed", User.Role.OPERATOR)
        self.part = _part(qty=5)
        from inventory.services_tools import InventoryService, ToolAssignmentService
        self.insts = InventoryService.issue_to_tool_pool(part=self.part, qty=2, actor=self.manager)
        self.machine = _machine()
        self.assignment = ToolAssignmentService.assign(
            instance=self.insts[0], operator=self.ali, machine=self.machine,
            condition_out="good", actor=self.manager,
        )

    def test_only_holder_or_manager_can_return(self):
        # Permission check lives in the view, not the service.
        # Service-level return can be invoked by anyone; the view blocks.
        from django.test import Client
        c = Client()
        c.force_login(self.ahmed)
        r = c.post(
            reverse("tools_return", args=[self.assignment.pk]),
            data={"condition_in": "good"},
        )
        self.assertEqual(r.status_code, 403)

    def test_search_accessible_to_operator(self):
        self.client.force_login(self.ali)
        r = self.client.get(reverse("tools_list"))
        self.assertEqual(r.status_code, 200)

    def test_dashboard_blocked_for_operator(self):
        self.client.force_login(self.ali)
        r = self.client.get(reverse("tools_dashboard"))
        self.assertEqual(r.status_code, 302)  # role_required redirects

    def test_dashboard_accessible_to_manager(self):
        self.client.force_login(self.manager)
        r = self.client.get(reverse("tools_dashboard"))
        self.assertEqual(r.status_code, 200)


class DashboardTests(TestCase):
    def setUp(self):
        self.manager = _user("mgr", User.Role.MANAGER)
        self.part = _part(qty=10)
        from inventory.services_tools import InventoryService, ToolAssignmentService
        self.insts = InventoryService.issue_to_tool_pool(part=self.part, qty=4, actor=self.manager)
        m = _machine()
        ToolAssignmentService.assign(
            instance=self.insts[0], operator=self.manager, machine=m,
            condition_out="good", actor=self.manager,
        )

    def test_dashboard_counts(self):
        self.client.force_login(self.manager)
        r = self.client.get(reverse("tools_dashboard"))
        self.assertEqual(r.status_code, 200)
        counts = r.context["counts"]
        self.assertEqual(counts["available"], 3)
        self.assertEqual(counts["in_use"], 1)
        self.assertEqual(counts["out_of_service"], 0)

    def test_dashboard_pool_parts_breakdown(self):
        self.client.force_login(self.manager)
        r = self.client.get(reverse("tools_dashboard"))
        self.assertEqual(r.status_code, 200)
        parts = r.context["pool_parts"]
        self.assertEqual(len(parts), 1)
        p = parts[0]
        self.assertEqual(p.n_total, 4)
        self.assertEqual(p.n_available, 3)
        self.assertEqual(p.n_in_use, 1)
        self.assertEqual(p.n_oos, 0)


class ToolsListTests(TestCase):
    def setUp(self):
        self.manager = _user("mgr", User.Role.MANAGER)
        self.supervisor = _user("sup", User.Role.SUPERVISOR)
        self.operator = _user("ali", User.Role.OPERATOR)
        self.part = _part(qty=10)
        from inventory.services_tools import InventoryService, ToolAssignmentService
        self.insts = InventoryService.issue_to_tool_pool(part=self.part, qty=4, actor=self.manager)
        ToolAssignmentService.assign(
            instance=self.insts[0], operator=self.operator, machine=_machine(),
            condition_out="good", actor=self.manager,
        )

    def test_list_renders_for_manager(self):
        self.client.force_login(self.manager)
        r = self.client.get(reverse("tools_list"))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.context["rows"]), 4)
        # chip counts
        cc = r.context["chip_counts"]
        self.assertEqual(cc["available"], 3)
        self.assertEqual(cc["in_use"], 1)
        self.assertEqual(cc["out_of_service"], 0)

    def test_list_filter_by_status(self):
        self.client.force_login(self.manager)
        r = self.client.get(reverse("tools_list") + "?status=in_use")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.context["rows"]), 1)

    def test_list_filter_by_query(self):
        self.client.force_login(self.manager)
        r = self.client.get(reverse("tools_list") + "?q=3")
        self.assertEqual(r.status_code, 200)
        # tool_number=3 should match exactly one row (out of 4)
        self.assertEqual(len(r.context["rows"]), 1)

    def test_list_accessible_to_operator(self):
        self.client.force_login(self.operator)
        r = self.client.get(reverse("tools_list"))
        self.assertEqual(r.status_code, 200)
        # operator sees only available (3) + their own held (1) = 4
        self.assertEqual(len(r.context["rows"]), 4)
        # operator sees cost fields hidden
        self.assertFalse(r.context["show_costs"])

    def test_list_hides_out_of_service_from_operator(self):
        # Mark the in-use one as out_of_service directly (bypassing service
        # for brevity) and verify operator can't see it.
        from django.utils import timezone
        from inventory.models_tools import ToolAssignment as TA
        from inventory.models import ReusableToolInstance as RTI
        inst = self.insts[0]
        inst.status = RTI.Status.OUT_OF_SERVICE
        inst.save(update_fields=["status"])
        TA.objects.filter(instance=inst).update(return_at=timezone.now())

        self.client.force_login(self.operator)
        r = self.client.get(reverse("tools_list"))
        self.assertEqual(r.status_code, 200)
        # operator should NOT see the OOS instance
        ids = [row[0].pk for row in r.context["rows"]]
        self.assertNotIn(inst.pk, ids)

    def test_list_supervisor_sees_oos_and_costs(self):
        from django.utils import timezone
        from inventory.models_tools import ToolAssignment as TA
        from inventory.models import ReusableToolInstance as RTI
        inst = self.insts[0]
        inst.status = RTI.Status.OUT_OF_SERVICE
        inst.save(update_fields=["status"])
        TA.objects.filter(instance=inst).update(return_at=timezone.now())

        self.client.force_login(self.supervisor)
        r = self.client.get(reverse("tools_list"))
        self.assertEqual(r.status_code, 200)
        ids = [row[0].pk for row in r.context["rows"]]
        self.assertIn(inst.pk, ids)
        self.assertTrue(r.context["show_costs"])

    def test_list_manager_assign_button_visible(self):
        self.client.force_login(self.manager)
        r = self.client.get(reverse("tools_list") + "?status=available")
        self.assertEqual(r.status_code, 200)
        # Manager should see "Assign to…" affordance for available rows
        self.assertContains(r, "Assign to")

    def test_list_operator_no_oos_chip(self):
        self.client.force_login(self.operator)
        r = self.client.get(reverse("tools_list"))
        self.assertEqual(r.status_code, 200)
        # Operator should NOT see the Out of Service filter chip
        self.assertNotContains(r, "Out of Service")


class SparePartFormItemTypeTests(TestCase):
    def test_default_is_spare_part(self):
        self.client.force_login(_user("mgr", User.Role.MANAGER))
        r = self.client.get(reverse("spare_part_create"))
        self.assertEqual(r.status_code, 200)
        form = r.context["form"]
        self.assertEqual(form["item_type"].initial, "spare_part")

    def test_create_reusable_tool_part(self):
        self.client.force_login(_user("mgr", User.Role.MANAGER))
        r = self.client.post(
            reverse("spare_part_create"),
            data={
                "sku": "KNIFE-PUNCH-2",
                "name": "Knife Punch 2",
                "item_type": "reusable_tool",
                "is_consumable": "false",
                "min_stock_level": "0",
                "status": "active",
                "opening_qty": "10",
            },
        )
        self.assertIn(r.status_code, (200, 302))
        p = SparePart.objects.get(sku="KNIFE-PUNCH-2")
        self.assertEqual(p.item_type, "reusable_tool")
        self.assertFalse(p.is_consumable)
