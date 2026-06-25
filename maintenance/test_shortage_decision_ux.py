"""
Sprint 2 / Step 6 UX — PartShortageReport decision flow.

Covers two improvements to the manager's shortage decision flow on the
Work Order detail page:

A. Pre-fill the shortage decision form with realistic defaults
   (issue the max that can be issued from stock, procure the rest,
   reject zero) so the manager does not have to do the math.

C. Show a "⚠ N in shortage" badge on the Parts table that links to the
   shortage form anchor below.
"""
from decimal import Decimal

from django.contrib.contenttypes.models import ContentType
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import User
from inventory.models import (
    Inventory, PartIssueLine, PartShortageReport, SparePart,
)
from maintenance.models import (
    MaintenanceIssue, Site, WorkOrder, Machine,
)


def _make_user(username: str, role: str) -> User:
    return User.objects.create_user(username=username, password="x", role=role)


def _make_site() -> Site:
    s, _ = Site.objects.get_or_create(
        is_default=True,
        defaults={"name": "Main Factory", "code": "MF", "is_active": True, "timezone": "UTC"},
    )
    return s


def _make_machine(code: str = "SDU-M1") -> Machine:
    return Machine.objects.create(
        name=f"SDU Machine {code}",
        asset_level=3, asset_code=code,
        qr_code=f"qr-{code}-{timezone.now().timestamp()}",
    )


def _make_part(sku: str, name: str) -> SparePart:
    return SparePart.objects.create(
        sku=sku, name=name, is_consumable=False,
    )


def _make_wo(*, machine: Machine, manager: User, technician: User) -> WorkOrder:
    issue = MaintenanceIssue.objects.create(
        description="shortage UX test", machine=machine,
        reported_by=technician,
    )
    issue.validated_by = manager
    issue.save()
    return WorkOrder.objects.create(
        machine=machine,
        assigned_technician=technician,
        created_by=manager,
        issue=issue,
        lifecycle_status=WorkOrder.LifecycleStatus.IN_PROGRESS,
    )


def _make_shortage_report(
    *, wo: WorkOrder, part: SparePart, reported_by: User,
    qty_requested: Decimal, usable_qty_snapshot: Decimal,
    shortage_qty: Decimal,
) -> PartShortageReport:
    return PartShortageReport.objects.create(
        content_type=ContentType.objects.get_for_model(WorkOrder),
        object_id=wo.pk,
        work_order=wo,
        part=part,
        qty_requested=qty_requested,
        shortage_qty=shortage_qty,
        available_qty_snapshot=usable_qty_snapshot,
        reserved_qty_snapshot=Decimal("0"),
        usable_qty_snapshot=usable_qty_snapshot,
        reported_by=reported_by,
        status=PartShortageReport.Status.PENDING_REVIEW,
    )


# ---------------------------------------------------------------------------
# Option A — Pre-fill shortage form with realistic defaults
# ---------------------------------------------------------------------------

class ShortageFormPreFillTests(TestCase):
    """The shortage decision form on the WO detail page must pre-fill
    `approved_issue_qty` / `approved_procurement_qty` / `rejected_qty`
    with a plan that adds up to `qty_requested` and does not exceed
    the currently usable stock.
    """

    def setUp(self):
        self.manager = _make_user("manager_sdux", User.Role.MANAGER)
        self.tech = _make_user("tech_sdux", User.Role.TECHNICIAN)
        self.site = _make_site()
        self.machine = _make_machine("SDU-A1")
        self.part = _make_part("SDU-BRG-001", "Bearing V493y")
        self.wo = _make_wo(
            machine=self.machine, manager=self.manager, technician=self.tech,
        )

    def _get_report(self):
        reports = self.client.get(
            reverse("work_order_detail", args=[self.wo.pk])
        ).context["pending_shortage_reports"]
        return list(reports)[0]

    def test_shortage_form_pre_fills_realistic_defaults(self):
        """Stock has 2 of 4 needed → issue 2, procure 2, reject 0."""
        _make_shortage_report(
            wo=self.wo, part=self.part, reported_by=self.tech,
            qty_requested=Decimal("4"),
            usable_qty_snapshot=Decimal("2"),
            shortage_qty=Decimal("2"),
        )
        self.client.force_login(self.manager)
        report = self._get_report()
        self.assertEqual(report.suggested_issue_qty, Decimal("2"))
        self.assertEqual(report.suggested_procure_qty, Decimal("2"))
        self.assertEqual(report.suggested_reject_qty, Decimal("0"))
        self.assertIn("Stock has 2", report.form_default_hint)
        self.assertIn("issue 2", report.form_default_hint)
        self.assertIn("procure 2", report.form_default_hint)

        # And the rendered HTML reflects those values in the form inputs.
        body = self.client.get(
            reverse("work_order_detail", args=[self.wo.pk])
        ).content.decode()
        # Both `approved_issue_qty` and `approved_procurement_qty` inputs
        # default to 2; the third `rejected_qty` defaults to 0.
        self.assertIn('name="approved_issue_qty"', body)
        self.assertIn('value="2"', body)
        self.assertIn(
            "Stock has 2 of 4 needed. Default: issue 2, procure 2.",
            body,
        )

    def test_shortage_form_handles_zero_stock(self):
        """Stock has 0 of 4 needed → issue 0, procure 4, reject 0."""
        _make_shortage_report(
            wo=self.wo, part=self.part, reported_by=self.tech,
            qty_requested=Decimal("4"),
            usable_qty_snapshot=Decimal("0"),
            shortage_qty=Decimal("4"),
        )
        self.client.force_login(self.manager)
        report = self._get_report()
        self.assertEqual(report.suggested_issue_qty, Decimal("0"))
        self.assertEqual(report.suggested_procure_qty, Decimal("4"))
        self.assertEqual(report.suggested_reject_qty, Decimal("0"))
        self.assertIn("Stock has 0", report.form_default_hint)

    def test_shortage_form_handles_sufficient_stock(self):
        """Stock has 10 of 4 needed → issue 4, procure 0, reject 0."""
        _make_shortage_report(
            wo=self.wo, part=self.part, reported_by=self.tech,
            qty_requested=Decimal("4"),
            usable_qty_snapshot=Decimal("10"),
            shortage_qty=Decimal("0"),
        )
        self.client.force_login(self.manager)
        report = self._get_report()
        self.assertEqual(report.suggested_issue_qty, Decimal("4"))
        self.assertEqual(report.suggested_procure_qty, Decimal("0"))
        self.assertEqual(report.suggested_reject_qty, Decimal("0"))
        self.assertIn("Stock has 10", report.form_default_hint)
        self.assertIn("issue 4", report.form_default_hint)
        self.assertIn("procure 0", report.form_default_hint)

    def test_shortage_form_clamps_negative_usable_snapshot(self):
        """Defensive: a negative `usable_qty_snapshot` (over-allocated
        state) is treated as 0 — issue defaults to 0, procure the rest.
        """
        _make_shortage_report(
            wo=self.wo, part=self.part, reported_by=self.tech,
            qty_requested=Decimal("4"),
            usable_qty_snapshot=Decimal("-3"),
            shortage_qty=Decimal("7"),
        )
        self.client.force_login(self.manager)
        report = self._get_report()
        self.assertEqual(report.suggested_issue_qty, Decimal("0"))
        self.assertEqual(report.suggested_procure_qty, Decimal("4"))


# ---------------------------------------------------------------------------
# Option C — Shortage badge on the Parts table
# ---------------------------------------------------------------------------

class ShortageBadgeTests(TestCase):
    """The Parts table on the WO detail page must show a "⚠ N in shortage"
    badge on any line where `shortage_qty > 0` AND a PENDING_REVIEW
    shortage report exists for (wo, part). The badge must link to the
    shortage form anchor below.
    """

    def setUp(self):
        self.manager = _make_user("manager_bdg", User.Role.MANAGER)
        self.tech = _make_user("tech_bdg", User.Role.TECHNICIAN)
        self.site = _make_site()
        self.machine = _make_machine("SDU-B1")
        self.part = _make_part("SDU-BRG-002", "Bearing V493y")
        self.other_part = _make_part("SDU-FLT-002", "Air Filter")
        self.wo = _make_wo(
            machine=self.machine, manager=self.manager, technician=self.tech,
        )

    def _make_line(
        self, *, part: SparePart, shortage_qty: Decimal = Decimal("0"),
        status: str = PartIssueLine.Status.PENDING,
    ) -> PartIssueLine:
        return PartIssueLine.objects.create(
            work_order=self.wo, part=part,
            quantity=Decimal("4"),
            requested_qty=Decimal("4"),
            unit_cost=Decimal("10"),
            status=status,
            requested_by=self.tech,
            issued_by=self.tech,
            shortage_qty=shortage_qty,
        )

    def test_line_with_shortage_shows_badge(self):
        """Line with shortage_qty=2 + PENDING_REVIEW report → badge appears."""
        line = self._make_line(part=self.part, shortage_qty=Decimal("2"))
        report = _make_shortage_report(
            wo=self.wo, part=self.part, reported_by=self.tech,
            qty_requested=Decimal("4"),
            usable_qty_snapshot=Decimal("2"),
            shortage_qty=Decimal("2"),
        )
        # Link the report to the line so the badge anchors correctly.
        report.issue_lines.add(line)
        self.client.force_login(self.manager)
        body = self.client.get(
            reverse("work_order_detail", args=[self.wo.pk])
        ).content.decode()
        self.assertIn("⚠ 2 in shortage", body)

    def test_line_without_shortage_hides_badge(self):
        """Line with shortage_qty=0 → badge is NOT shown."""
        self._make_line(part=self.part, shortage_qty=Decimal("0"))
        _make_shortage_report(
            wo=self.wo, part=self.part, reported_by=self.tech,
            qty_requested=Decimal("4"),
            usable_qty_snapshot=Decimal("2"),
            shortage_qty=Decimal("2"),
        )
        self.client.force_login(self.manager)
        body = self.client.get(
            reverse("work_order_detail", args=[self.wo.pk])
        ).content.decode()
        self.assertNotIn("in shortage", body)

    def test_badge_links_to_shortage_anchor(self):
        """Badge `href` points to #shortage-{line.id} and that anchor exists."""
        line = self._make_line(part=self.part, shortage_qty=Decimal("2"))
        report = _make_shortage_report(
            wo=self.wo, part=self.part, reported_by=self.tech,
            qty_requested=Decimal("4"),
            usable_qty_snapshot=Decimal("2"),
            shortage_qty=Decimal("2"),
        )
        report.issue_lines.add(line)
        self.client.force_login(self.manager)
        body = self.client.get(
            reverse("work_order_detail", args=[self.wo.pk])
        ).content.decode()
        # The badge's href must point to the anchor.
        self.assertIn(f'href="#shortage-{line.pk}"', body)
        # The anchor must exist somewhere on the page (in the shortage form).
        self.assertIn(f'id="shortage-{line.pk}"', body)

    def test_badge_disappears_when_shortage_resolved(self):
        """Once the shortage report moves past PENDING_REVIEW (e.g.
        APPROVED), the badge is hidden because the manager has already
        recorded a decision.
        """
        line = self._make_line(part=self.part, shortage_qty=Decimal("2"))
        report = _make_shortage_report(
            wo=self.wo, part=self.part, reported_by=self.tech,
            qty_requested=Decimal("4"),
            usable_qty_snapshot=Decimal("2"),
            shortage_qty=Decimal("2"),
        )
        report.issue_lines.add(line)
        report.status = PartShortageReport.Status.APPROVED
        report.save()
        self.client.force_login(self.manager)
        body = self.client.get(
            reverse("work_order_detail", args=[self.wo.pk])
        ).content.decode()
        # No pending shortage → no badge.
        self.assertNotIn("in shortage", body)
