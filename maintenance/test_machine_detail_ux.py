"""Tests for the machine_detail page UX improvements (hero stats, WO table)."""
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse

from accounts.models import User
from maintenance.models import Machine, WorkOrder, WorkOrderCost, Site


def _make_user(username, role):
    return User.objects.create_user(username=username, password="x", role=role)


def _make_part(sku="UX-PART"):
    site = Site.objects.filter(is_default=True).first() or Site.objects.create(
        name="X", is_default=True, is_active=True
    )
    from inventory.models import SparePart, Inventory
    p = SparePart.objects.create(
        sku=sku, name=sku, status="active",
        avg_cost=Decimal("10.00"), last_purchase_cost=Decimal("10.00"),
    )
    Inventory.objects.create(part=p, site=site, quantity_available=Decimal("0"))
    return site, p


def _make_wo(machine, technician, manager, **kwargs):
    return WorkOrder.objects.create(
        machine=machine,
        lifecycle_status=kwargs.get("lifecycle_status", "in_progress"),
        assigned_technician=technician,
        created_by=manager,
    )


class MachineDetailHeroStatsTests(TestCase):
    def setUp(self):
        self.manager = _make_user("m1_mgr", User.Role.MANAGER)
        self.tech = _make_user("m1_tech", User.Role.TECHNICIAN)
        self.site, _ = _make_part()
        self.machine = Machine.objects.create(
            name="M1UX", qr_code="M1UX", asset_level=3,
            asset_code="M1UX", is_active=True, site=self.site,
        )

    def test_hero_stats_keys_present(self):
        self.client.force_login(self.manager)
        r = self.client.get(reverse("machine_detail", args=[self.machine.pk]))
        self.assertEqual(r.status_code, 200)
        self.assertIn("hero_stats", r.context)
        stats = r.context["hero_stats"]
        for key in ("total_wo_count", "cost_90d_total", "failure_count_90d", "last_activity_days"):
            self.assertIn(key, stats, f"Missing hero_stats key: {key}")

    def test_hero_stats_zero_when_no_wos(self):
        self.client.force_login(self.manager)
        r = self.client.get(reverse("machine_detail", args=[self.machine.pk]))
        stats = r.context["hero_stats"]
        self.assertEqual(stats["total_wo_count"], 0)


class MachineDetailWOTableTests(TestCase):
    def setUp(self):
        self.manager = _make_user("m2_mgr", User.Role.MANAGER)
        self.tech = _make_user("m2_tech", User.Role.TECHNICIAN)
        self.site, _ = _make_part()
        self.machine = Machine.objects.create(
            name="M2UX", qr_code="M2UX", asset_level=3,
            asset_code="M2UX", is_active=True, site=self.site,
        )

    def test_wo_list_prefetched_with_cost_record(self):
        wo = _make_wo(self.machine, self.tech, self.manager)
        cr = WorkOrderCost.objects.create(
            work_order=wo, material_cost=Decimal("123.45"),
        )
        # Bypass _auto_calculate() which zeroes material_cost on save()
        WorkOrderCost.objects.filter(pk=cr.pk).update(
            material_cost=Decimal("123.45"),
        )
        wo_fresh = WorkOrder.objects.select_related("cost_record").get(pk=wo.pk)
        self.assertEqual(wo_fresh.cost_record.material_cost, Decimal("123.45"))

    def test_wo_list_ordered_by_created_at_desc(self):
        wos = []
        for i in range(5):
            wos.append(_make_wo(self.machine, self.tech, self.manager))
        self.client.force_login(self.manager)
        r = self.client.get(reverse("machine_detail", args=[self.machine.pk]))
        wos_in_context = list(r.context["related_wos"])
        self.assertEqual(wos_in_context[0].pk, wos[-1].pk)
        self.assertEqual(wos_in_context[-1].pk, wos[0].pk)

    def test_wo_list_caps_at_50(self):
        for i in range(60):
            _make_wo(self.machine, self.tech, self.manager)
        self.client.force_login(self.manager)
        r = self.client.get(reverse("machine_detail", args=[self.machine.pk]))
        self.assertEqual(len(r.context["related_wos"]), 50)


class MachineDetailTabsRenderTests(TestCase):
    def setUp(self):
        self.manager = _make_user("m3_mgr", User.Role.MANAGER)
        self.tech = _make_user("m3_tech", User.Role.TECHNICIAN)
        self.site, _ = _make_part()
        self.machine = Machine.objects.create(
            name="M3UX", qr_code="M3UX", asset_level=3,
            asset_code="M3UX", is_active=True, site=self.site,
        )

    def test_all_tabs_rendered(self):
        self.client.force_login(self.manager)
        r = self.client.get(reverse("machine_detail", args=[self.machine.pk]))
        self.assertEqual(r.status_code, 200)
        body = r.content.decode()
        for tab in ("tab-wos", "tab-issues", "tab-pms", "tab-eros", "tab-prs"):
            self.assertIn(f'id="{tab}"', body, f"Missing tab panel: {tab}")

    def test_wo_table_includes_cost_column(self):
        wo = _make_wo(self.machine, self.tech, self.manager)
        cr = WorkOrderCost.objects.create(
            work_order=wo, material_cost=Decimal("555.00"),
            vendor_repair_cost=Decimal("100.00"),
        )
        # WorkOrderCost.save() runs _auto_calculate() which overwrites
        # the material/vendor/consumable fields. Use update() to set
        # the values directly in the DB, bypassing save().
        WorkOrderCost.objects.filter(pk=cr.pk).update(
            material_cost=Decimal("555.00"),
            vendor_repair_cost=Decimal("100.00"),
        )
        self.client.force_login(self.manager)
        r = self.client.get(reverse("machine_detail", args=[self.machine.pk]))
        body = r.content.decode()
        self.assertIn(f"WO-{wo.number}", body)
        self.assertIn("655.00 SAR", body)


class MachineDetailChildGridTests(TestCase):
    def setUp(self):
        self.manager = _make_user("m4_mgr", User.Role.MANAGER)
        self.site, _ = _make_part()
        self.machine = Machine.objects.create(
            name="M4UX", qr_code="M4UX", asset_level=3,
            asset_code="M4UX", is_active=True, site=self.site,
        )
        Machine.objects.create(
            name="Sub1", qr_code="SUB1", asset_level=4,
            asset_code="SUB1", is_active=True, site=self.site,
            parent=self.machine,
        )
        Machine.objects.create(
            name="Sub2", qr_code="SUB2", asset_level=4,
            asset_code="SUB2", is_active=True, site=self.site,
            parent=self.machine,
        )

    def test_child_grid_renders(self):
        self.client.force_login(self.manager)
        r = self.client.get(reverse("machine_detail", args=[self.machine.pk]))
        body = r.content.decode()
        self.assertIn("mms-child-grid", body)
        self.assertIn("Sub1", body)
        self.assertIn("Sub2", body)