"""
Phase 3A — WorkOrder Blocker System UI components.

Covers:
- WorkOrderHealthService.compute(wo) is exposed in the WO detail context
  as `health_card` (HealthCard dataclass), and the partial correctly
  computes risk/cost fields.
- The `active_blockers` queryset contains only OPEN blockers on the WO.
- The `blocker_history` queryset contains only non-OPEN blockers
  (RESOLVED + CANCELLED), ordered by opened_at DESC, capped at 20.
- The 3 new template partials render the right fragments in the
  response body when the WO detail page is requested.

These tests use the same self-contained setUp pattern as
`test_blocker_system.py` and `test_phase2d_services.py` to keep them
isolated from the broader test suite.
"""
from __future__ import annotations

from decimal import Decimal

from django.contrib.contenttypes.models import ContentType
from django.test import TestCase
from django.urls import reverse

from accounts.models import User
from inventory.models import PartIssueLine, SparePart
from maintenance.models import (
    Machine,
    WorkOrder,
    WorkOrderBlocker,
    WorkOrderBlockerEvent,
    WorkOrderCost,
)


# ---------------------------------------------------------------------------
# Test helpers (mirror the style of test_blocker_system.py /
# test_phase2d_services.py).
# ---------------------------------------------------------------------------

def _make_user(username: str, role: str) -> User:
    return User.objects.create_user(username=username, password="x", role=role)


def _make_wo(*, machine: Machine = None, created_by: User, **kwargs) -> WorkOrder:
    defaults = {
        "machine": machine,
        "created_by": created_by,
        "lifecycle_status": WorkOrder.LifecycleStatus.ASSIGNED,
    }
    defaults.update(kwargs)
    return WorkOrder.objects.create(**defaults)


def _make_part_line(*, wo: WorkOrder, part: SparePart, qty=Decimal("2"),
                    requested_by: User, issued_by: User) -> PartIssueLine:
    return PartIssueLine.objects.create(
        work_order=wo, part=part, quantity=qty, unit_cost=Decimal("10"),
        status=PartIssueLine.Status.PENDING,
        requested_by=requested_by, issued_by=issued_by,
        requested_qty=qty, approved_qty=Decimal("0"),
        issued_qty=Decimal("0"),
    )


def _open_blocker(*, wo: WorkOrder, kind: str, external_obj, opened_by: User,
                  external_label: str = "test", note: str = "",
                  source_work_order=None, pause_reason: str = "") -> WorkOrderBlocker:
    """Create an OPEN WO Blocker with a real GenericForeignKey to a domain
    object (PartIssueLine, etc.). Does not call the service — direct ORM
    write is fine for view-context tests."""
    ct = ContentType.objects.get_for_model(external_obj)
    return WorkOrderBlocker.objects.create(
        work_order=wo, kind=kind, status=WorkOrderBlocker.Status.OPEN,
        content_type=ct, object_id=external_obj.pk,
        external_label=external_label, opened_by=opened_by, note=note,
        source_work_order=source_work_order, pause_reason=pause_reason,
    )


# ---------------------------------------------------------------------------
# Service-level context tests (do not require HTTP)
# ---------------------------------------------------------------------------

class WorkOrderDetailContextTests(TestCase):
    """Verify the WO detail view exposes health_card / active_blockers /
    blocker_history correctly in its context dict."""

    def setUp(self):
        self.manager = _make_user("manager_p3a_ctx", User.Role.MANAGER)
        self.tech = _make_user("tech_p3a_ctx", User.Role.TECHNICIAN)
        self.machine = Machine.objects.create(name="Press P3A", qr_code="P3A-M1")
        self.part = SparePart.objects.create(sku="P3A-001", name="Bearing P3A")
        self.wo = _make_wo(
            machine=self.machine, created_by=self.manager,
            lifecycle_status=WorkOrder.LifecycleStatus.ASSIGNED,
        )

    def test_health_card_low_risk_for_fresh_wo(self):
        """A fresh WO with no blockers and no cost record has risk=LOW,
        open_blockers_count=0, total_cost=0, is_emergency=False."""
        self.client.force_login(self.manager)
        response = self.client.get(
            reverse("work_order_detail", kwargs={"pk": self.wo.pk})
        )
        self.assertEqual(response.status_code, 200)
        card = response.context["health_card"]
        self.assertEqual(card.risk, "LOW")
        self.assertEqual(card.open_blockers_count, 0)
        self.assertEqual(card.total_cost, Decimal("0"))
        self.assertFalse(card.is_emergency)
        self.assertEqual(card.parts_cost, Decimal("0"))
        self.assertEqual(card.vendor_cost, Decimal("0"))
        self.assertEqual(card.procurement_cost, Decimal("0"))
        self.assertEqual(card.consumables_cost, Decimal("0"))
        self.assertEqual(card.additional_cost, Decimal("0"))

    def test_health_card_high_risk_for_emergency(self):
        """is_emergency=True forces risk=HIGH and pushes 'EMERGENCY' into notes."""
        self.wo.is_emergency = True
        self.wo.save(update_fields=["is_emergency", "updated_at"])
        self.client.force_login(self.manager)
        response = self.client.get(
            reverse("work_order_detail", kwargs={"pk": self.wo.pk})
        )
        card = response.context["health_card"]
        self.assertEqual(card.risk, "HIGH")
        self.assertTrue(card.is_emergency)
        self.assertIn("EMERGENCY", card.notes)

    def test_health_card_uses_cost_record(self):
        """WorkOrderCost fields flow into the HealthCard total_cost.

        Note: WorkOrderCost.save() auto-calculates material_cost /
        vendor_repair_cost / consumables_cost from related records on the
        first save. We save once (which zeroes the auto-calculated
        fields) and then update the desired values with update_fields so
        the auto-calculate doesn't run again.
        """
        cost = WorkOrderCost(work_order=self.wo)
        cost.save()
        cost.material_cost = Decimal("100")
        cost.vendor_repair_cost = Decimal("50")
        cost.procurement_cost = Decimal("0")
        cost.consumables_cost = Decimal("10")
        cost.additional_cost = Decimal("5")
        cost.save(update_fields=[
            "material_cost", "vendor_repair_cost", "procurement_cost",
            "consumables_cost", "additional_cost",
        ])
        self.client.force_login(self.manager)
        response = self.client.get(
            reverse("work_order_detail", kwargs={"pk": self.wo.pk})
        )
        card = response.context["health_card"]
        # 100 + 50 + 0 + 10 + 5 = 165 (downtime excluded per glossary)
        self.assertEqual(card.parts_cost, Decimal("100"))
        self.assertEqual(card.vendor_cost, Decimal("50"))
        self.assertEqual(card.procurement_cost, Decimal("0"))
        self.assertEqual(card.consumables_cost, Decimal("10"))
        self.assertEqual(card.additional_cost, Decimal("5"))
        self.assertEqual(card.total_cost, Decimal("165"))

    def test_active_blockers_in_context(self):
        """A WO with 2 OPEN blockers exposes both in context['active_blockers']."""
        line_a = _make_part_line(wo=self.wo, part=self.part, qty=Decimal("1"),
                                 requested_by=self.tech, issued_by=self.tech)
        line_b = _make_part_line(wo=self.wo, part=self.part, qty=Decimal("3"),
                                 requested_by=self.tech, issued_by=self.tech)
        _open_blocker(wo=self.wo, kind=WorkOrderBlocker.Kind.PART,
                      external_obj=line_a, opened_by=self.tech,
                      external_label=f"{self.part.sku} x 1")
        _open_blocker(wo=self.wo, kind=WorkOrderBlocker.Kind.PART,
                      external_obj=line_b, opened_by=self.tech,
                      external_label=f"{self.part.sku} x 3")

        self.client.force_login(self.manager)
        response = self.client.get(
            reverse("work_order_detail", kwargs={"pk": self.wo.pk})
        )
        active = list(response.context["active_blockers"])
        self.assertEqual(len(active), 2)
        self.assertTrue(all(b.status == WorkOrderBlocker.Status.OPEN for b in active))

    def test_blocker_history_excludes_open(self):
        """blocker_history contains only RESOLVED + CANCELLED; active_blockers
        contains only OPEN. They are disjoint sets of the same WO."""
        # 3 separate PartIssueLines (one per blocker) so each blocker has
        # a distinct (work_order, content_type, object_id) tuple and
        # the partial unique constraint is satisfied while all are OPEN.
        line_a = _make_part_line(wo=self.wo, part=self.part, qty=Decimal("1"),
                                 requested_by=self.tech, issued_by=self.tech)
        line_b = _make_part_line(wo=self.wo, part=self.part, qty=Decimal("2"),
                                 requested_by=self.tech, issued_by=self.tech)
        line_c = _make_part_line(wo=self.wo, part=self.part, qty=Decimal("3"),
                                 requested_by=self.tech, issued_by=self.tech)
        b_open = _open_blocker(wo=self.wo, kind=WorkOrderBlocker.Kind.PART,
                               external_obj=line_a, opened_by=self.tech,
                               external_label="open")
        b_resolved = _open_blocker(
            wo=self.wo, kind=WorkOrderBlocker.Kind.PART,
            external_obj=line_b, opened_by=self.tech, external_label="resolved",
        )
        b_resolved.status = WorkOrderBlocker.Status.RESOLVED
        b_resolved.save(update_fields=["status"])

        b_cancelled = _open_blocker(
            wo=self.wo, kind=WorkOrderBlocker.Kind.PART,
            external_obj=line_c, opened_by=self.tech, external_label="cancelled",
        )
        b_cancelled.status = WorkOrderBlocker.Status.CANCELLED
        b_cancelled.save(update_fields=["status"])

        self.client.force_login(self.manager)
        response = self.client.get(
            reverse("work_order_detail", kwargs={"pk": self.wo.pk})
        )
        history = list(response.context["blocker_history"])
        active = list(response.context["active_blockers"])
        self.assertEqual(len(history), 2)
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0].pk, b_open.pk)
        self.assertEqual(
            sorted([b.pk for b in history]),
            sorted([b_resolved.pk, b_cancelled.pk]),
        )

    def test_blocker_history_orders_by_opened_at_desc(self):
        """blocker_history is ordered by opened_at DESC (newest first)."""
        # The 3 blockers need distinct (wo, content_type, object_id) tuples
        # to satisfy the partial unique constraint when open. We use 3
        # separate PartIssueLines (one per blocker), then resolve all 3.
        line_a = _make_part_line(wo=self.wo, part=self.part, qty=Decimal("1"),
                                 requested_by=self.tech, issued_by=self.tech)
        line_b = _make_part_line(wo=self.wo, part=self.part, qty=Decimal("2"),
                                 requested_by=self.tech, issued_by=self.tech)
        line_c = _make_part_line(wo=self.wo, part=self.part, qty=Decimal("3"),
                                 requested_by=self.tech, issued_by=self.tech)
        b_first = _open_blocker(wo=self.wo, kind=WorkOrderBlocker.Kind.PART,
                                external_obj=line_a, opened_by=self.tech,
                                external_label="first")
        b_middle = _open_blocker(wo=self.wo, kind=WorkOrderBlocker.Kind.PART,
                                 external_obj=line_b, opened_by=self.tech,
                                 external_label="middle")
        b_last = _open_blocker(wo=self.wo, kind=WorkOrderBlocker.Kind.PART,
                               external_obj=line_c, opened_by=self.tech,
                               external_label="last")

        # Resolve all three so they fall into the history queryset
        for b in (b_first, b_middle, b_last):
            b.status = WorkOrderBlocker.Status.RESOLVED
            b.save(update_fields=["status"])

        self.client.force_login(self.manager)
        response = self.client.get(
            reverse("work_order_detail", kwargs={"pk": self.wo.pk})
        )
        history = list(response.context["blocker_history"])
        self.assertEqual(len(history), 3)
        # Newest first
        self.assertEqual(history[0].pk, b_last.pk)
        self.assertEqual(history[1].pk, b_middle.pk)
        self.assertEqual(history[2].pk, b_first.pk)


# ---------------------------------------------------------------------------
# Template-level tests (verify the partials actually render)
# ---------------------------------------------------------------------------

class WorkOrderDetailTemplateTests(TestCase):
    """Verify the 3 new template partials render their key fragments
    in the response body when the WO detail page is requested."""

    def setUp(self):
        self.manager = _make_user("manager_p3a_tpl", User.Role.MANAGER)
        self.tech = _make_user("tech_p3a_tpl", User.Role.TECHNICIAN)
        self.machine = Machine.objects.create(name="Press P3A-T", qr_code="P3A-T-M1")
        self.part = SparePart.objects.create(sku="P3A-T-001", name="Bearing P3A-T")
        self.wo = _make_wo(
            machine=self.machine, created_by=self.manager,
            lifecycle_status=WorkOrder.LifecycleStatus.ASSIGNED,
        )

    def test_template_renders_health_card(self):
        """The Health card is rendered with the right risk label."""
        self.client.force_login(self.manager)
        response = self.client.get(
            reverse("work_order_detail", kwargs={"pk": self.wo.pk})
        )
        self.assertEqual(response.status_code, 200)
        body = response.content.decode("utf-8")
        self.assertIn("Health:", body)
        # A fresh WO with no blockers is risk=LOW
        self.assertIn("LOW", body)

    def test_template_renders_active_blockers_panel(self):
        """The Active Blockers panel renders a kind label when an OPEN
        PART blocker exists on the WO."""
        line = _make_part_line(wo=self.wo, part=self.part, qty=Decimal("2"),
                               requested_by=self.tech, issued_by=self.tech)
        _open_blocker(wo=self.wo, kind=WorkOrderBlocker.Kind.PART,
                      external_obj=line, opened_by=self.tech,
                      external_label=f"{self.part.sku} x 2")
        self.client.force_login(self.manager)
        response = self.client.get(
            reverse("work_order_detail", kwargs={"pk": self.wo.pk})
        )
        self.assertEqual(response.status_code, 200)
        body = response.content.decode("utf-8")
        self.assertIn("Active Blockers", body)
        self.assertIn("Awaiting Spare Part", body)
        # The external label "BRG-P3A-T-001 x 2" or similar is rendered
        self.assertIn("x 2", body)
