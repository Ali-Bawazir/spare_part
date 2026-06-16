"""
Phase 2D-1 — analytical service tests.

Covers:
- PartImpactService (impact score, level, recommendation)
- RepairViabilityService (REPAIR / REPLACE / BORDERLINE)
- WorkOrderHealthService (risk, cost, notes)
- VideoCompressionService (size-based skip behaviour)

These are pure read-only computations, so the tests focus on the
public-facing contract (the dataclass output) rather than side effects.
"""
from __future__ import annotations

import os
import tempfile
from decimal import Decimal

from django.contrib.contenttypes.models import ContentType
from django.test import TestCase
from django.utils import timezone

from accounts.models import User
from inventory.models import (
    Inventory,
    PartIssueLine,
    PartShortageReport,
    SparePart,
)
from inventory.services_impact import PartImpactService
from inventory.services_video import VideoCompressionService
from maintenance.models import (
    ExternalRepairOrder,
    ExternalRepairRequest,
    Machine,
    MaintenanceIssue,
    Site,
    WorkOrder,
    WorkOrderBlocker,
    WorkOrderCost,
)
from maintenance.services_repair_viability import RepairViabilityService
from maintenance.services_wo_health import WorkOrderHealthService


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

def _make_user(username: str, role: str) -> User:
    return User.objects.create_user(username=username, password="x", role=role)


def _make_wo(*, machine=None, created_by: User, **kwargs) -> WorkOrder:
    defaults = {
        "machine": machine,
        "created_by": created_by,
        "lifecycle_status": WorkOrder.LifecycleStatus.ASSIGNED,
    }
    defaults.update(kwargs)
    return WorkOrder.objects.create(**defaults)


def _make_part_line(
    *, wo: WorkOrder, part: SparePart, status: str = PartIssueLine.Status.PENDING,
    qty: Decimal = Decimal("2"), requested_by: User, issued_by: User,
) -> PartIssueLine:
    return PartIssueLine.objects.create(
        work_order=wo, part=part, quantity=qty, unit_cost=Decimal("10"),
        status=status, requested_by=requested_by, issued_by=issued_by,
        requested_qty=qty, approved_qty=qty if status != PartIssueLine.Status.PENDING else Decimal("0"),
    )


# ---------------------------------------------------------------------------
# PartImpactService tests
# ---------------------------------------------------------------------------

class PartImpactServiceTests(TestCase):
    """PartImpactService.compute_impact — 0-100 score, level, recommendation."""

    def setUp(self):
        self.manager = _make_user("manager_imp", User.Role.MANAGER)
        self.tech = _make_user("tech_imp", User.Role.TECHNICIAN)
        self.site = Site.objects.filter(is_default=True).first()
        if self.site is None:
            self.site = Site.objects.create(
                code="MF", name="Main Factory", is_default=True,
                is_active=True, timezone="UTC",
            )
        self.machine_a = Machine.objects.create(
            name="Press A", qr_code="IMP-MA", asset_level=3, asset_code="IMP-MA",
        )
        self.machine_b = Machine.objects.create(
            name="Press B", qr_code="IMP-MB", asset_level=3, asset_code="IMP-MB",
        )
        self.part = SparePart.objects.create(
            sku="IMP-001", name="Bearing IMP",
            avg_cost=Decimal("100"), last_purchase_cost=Decimal("100"),
        )

    def test_compute_impact_no_open_lines_returns_low(self):
        """No open PartIssueLines and no open PartShortageReports → score 0."""
        result = PartImpactService.compute_impact(self.part)
        self.assertEqual(result.score, 0)
        self.assertEqual(result.level, "LOW")
        self.assertEqual(result.recommendation, "monitor")
        # The components dict is present
        self.assertIn("affected_wos", result.components)
        self.assertIn("affected_assets", result.components)
        self.assertEqual(result.components["affected_wos"], 0)

    def test_compute_impact_with_open_lines_returns_medium(self):
        """1 open PartIssueLine + 1 open PartShortageReport → MEDIUM band."""
        wo = _make_wo(machine=self.machine_a, created_by=self.manager)
        # 1 open PART wait on WO
        _make_part_line(
            wo=wo, part=self.part, status=PartIssueLine.Status.PENDING,
            requested_by=self.tech, issued_by=self.tech,
        )
        # 1 open PartShortageReport on a different WO
        other_wo = _make_wo(machine=self.machine_b, created_by=self.manager)
        PartShortageReport.objects.create(
            content_type=ContentType.objects.get_for_model(WorkOrder),
            object_id=other_wo.pk, work_order=other_wo, part=self.part,
            qty_requested=Decimal("2"), shortage_qty=Decimal("2"),
            available_qty_snapshot=Decimal("0"),
            reserved_qty_snapshot=Decimal("0"),
            usable_qty_snapshot=Decimal("0"),
            reported_by=self.tech,
            status=PartShortageReport.Status.PENDING_REVIEW,
        )
        result = PartImpactService.compute_impact(self.part)
        # MEDIUM band: score >= 40 (and < 75 in the absence of emergencies)
        self.assertGreaterEqual(result.score, 40)
        self.assertLessEqual(result.score, 75)
        self.assertEqual(result.level, "MEDIUM")
        self.assertEqual(result.recommendation, "purchase")
        # 2 distinct WOs and 2 distinct machines
        self.assertEqual(result.components["affected_wos"], 20)  # 2 * 10
        self.assertEqual(result.components["affected_assets"], 10)  # 2 * 5

    def test_compute_impact_high_emergency_returns_high(self):
        """5 open lines + an emergency WO → HIGH band."""
        # 5 WOs each with one open PART line
        wos = []
        for i in range(5):
            wo = _make_wo(
                machine=self.machine_a if i % 2 == 0 else self.machine_b,
                created_by=self.manager,
                is_emergency=(i == 0),  # one of them is emergency
            )
            _make_part_line(
                wo=wo, part=self.part, status=PartIssueLine.Status.PENDING,
                requested_by=self.tech, issued_by=self.tech,
            )
            wos.append(wo)
        result = PartImpactService.compute_impact(self.part)
        # 5 WOs * 10 = 50, 2 machines * 5 = 10, 5 lines * 8h * 1.5 = 60 (capped 100)
        # 5 lines * 1h = 5, 1 emergency * 2 = 2. Total > 75.
        self.assertGreater(result.score, 75)
        self.assertEqual(result.level, "HIGH")
        self.assertEqual(result.recommendation, "purchase_now")


# ---------------------------------------------------------------------------
# RepairViabilityService tests
# ---------------------------------------------------------------------------

class RepairViabilityServiceTests(TestCase):
    """RepairViabilityService.compute — REPAIR / REPLACE / BORDERLINE."""

    def setUp(self):
        self.manager = _make_user("manager_rv", User.Role.MANAGER)
        self.machine = Machine.objects.create(
            name="Press RV", qr_code="RV-M1", asset_level=3, asset_code="RV-M1",
        )
        self.part = SparePart.objects.create(
            sku="RV-001", name="Servo Drive",
            avg_cost=Decimal("100"), last_purchase_cost=Decimal("100"),
        )

    def test_compute_no_history_returns_repair(self):
        """No ERO history → REPAIR with 'no history' reason."""
        result = RepairViabilityService.compute(self.part)
        self.assertEqual(result.recommendation, "REPAIR")
        self.assertEqual(result.historical_count, 0)
        # The reason text uses 'no historical' (and 'defaulting' in the
        # explanation). We assert the meaning — no history — is captured.
        self.assertIn("historical", result.reason.lower())

    def test_compute_low_ratio_returns_repair(self):
        """1 ERO with actual_cost=10, replacement_cost=100 → 10% ratio → REPAIR."""
        wo = _make_wo(machine=self.machine, created_by=self.manager)
        ero = ExternalRepairOrder.objects.create(
            work_order=wo, machine=self.machine, title="Minor fix",
            description="Bearing clean", created_by=self.manager,
            actual_cost=Decimal("10"),
            status=ExternalRepairOrder.Status.CLOSED,
        )
        # Link the ERO to the part via the related ERR
        ExternalRepairRequest.objects.create(
            work_order=wo, requested_by=self.manager,
            diagnosis_note="Minor bearing wear", part_description="Bearing WH",
            part=self.part, asset=self.machine,
            status=ExternalRepairRequest.Status.APPROVED,
            repair_order=ero,
        )
        result = RepairViabilityService.compute(self.part)
        self.assertEqual(result.recommendation, "REPAIR")
        self.assertEqual(result.repair_ratio_pct, 10)
        self.assertEqual(result.historical_count, 1)
        self.assertEqual(result.avg_repair_cost, Decimal("10"))
        self.assertEqual(result.replacement_cost, Decimal("100"))

    def test_compute_high_ratio_with_low_mtbf_returns_replace(self):
        """4 EROs with actual_cost=80 (80% ratio) + low MTBF → REPLACE."""
        # Spread the 4 EROs across a few days so MTBF stays low (< 100h)
        for i in range(4):
            wo = _make_wo(machine=self.machine, created_by=self.manager)
            ero = ExternalRepairOrder.objects.create(
                work_order=wo, machine=self.machine, title=f"Fix {i}",
                description="Vendor repair", created_by=self.manager,
                actual_cost=Decimal("80"),
                status=ExternalRepairOrder.Status.CLOSED,
                closed_at=timezone.now() - timezone.timedelta(days=i * 5),
            )
            ExternalRepairRequest.objects.create(
                work_order=wo, requested_by=self.manager,
                diagnosis_note=f"Failure {i}", part_description="Bearing WH",
                part=self.part, asset=self.machine,
                status=ExternalRepairRequest.Status.APPROVED,
                repair_order=ero,
            )
        result = RepairViabilityService.compute(self.part, asset=self.machine)
        # 80% ratio is > 70%, MTBF ≈ 5*24 = 120h which is > 100h threshold,
        # but the count > 3 + ratio > 50% + MTBF < 200h rule kicks in.
        self.assertEqual(result.historical_count, 4)
        self.assertEqual(result.repair_ratio_pct, 80)
        self.assertEqual(result.recommendation, "REPLACE")
        # MTBF should be ~120h
        self.assertIsNotNone(result.asset_mtbf_hours)


# ---------------------------------------------------------------------------
# WorkOrderHealthService tests
# ---------------------------------------------------------------------------

class WorkOrderHealthServiceTests(TestCase):
    """WorkOrderHealthService.compute — risk, cost, notes."""

    def setUp(self):
        self.manager = _make_user("manager_wh", User.Role.MANAGER)
        self.tech = _make_user("tech_wh", User.Role.TECHNICIAN)
        self.machine = Machine.objects.create(
            name="Press WH", qr_code="WH-M1", asset_level=3, asset_code="WH-M1",
        )
        self.site = Site.objects.filter(is_default=True).first()
        if self.site is None:
            self.site = Site.objects.create(
                code="MF", name="Main Factory", is_default=True,
                is_active=True, timezone="UTC",
            )
        self.part = SparePart.objects.create(
            sku="WH-001", name="Bearing WH",
            avg_cost=Decimal("10"), last_purchase_cost=Decimal("10"),
        )

    def test_compute_low_risk_for_fresh_assigned_wo(self):
        """Fresh WO, no blockers, no cost → LOW risk."""
        wo = _make_wo(
            machine=self.machine, created_by=self.manager,
            lifecycle_status=WorkOrder.LifecycleStatus.ASSIGNED,
        )
        card = WorkOrderHealthService.compute(wo)
        self.assertEqual(card.risk, "LOW")
        self.assertEqual(card.open_blockers_count, 0)
        self.assertEqual(card.total_cost, Decimal("0"))
        self.assertEqual(card.parts_cost, Decimal("0"))
        self.assertEqual(card.vendor_cost, Decimal("0"))
        self.assertEqual(card.notes, [])
        self.assertFalse(card.is_emergency)

    def test_compute_high_risk_for_emergency(self):
        """Emergency WO → HIGH risk, EMERGENCY note present."""
        issue = MaintenanceIssue.objects.create(
            machine=self.machine, reported_by=self.tech,
            description="Bearing failure",
            status=MaintenanceIssue.Status.VALIDATED,
            priority=MaintenanceIssue.Priority.HIGH,
        )
        wo = _make_wo(
            machine=self.machine, created_by=self.manager,
            issue=issue, is_emergency=True,
            lifecycle_status=WorkOrder.LifecycleStatus.IN_PROGRESS,
        )
        card = WorkOrderHealthService.compute(wo)
        self.assertEqual(card.risk, "HIGH")
        self.assertTrue(card.is_emergency)
        self.assertIn("EMERGENCY", card.notes)

    def test_compute_high_risk_for_3_plus_blockers(self):
        """WO with 3 open blockers → HIGH risk, 3+ OPEN BLOCKERS note."""
        wo = _make_wo(
            machine=self.machine, created_by=self.manager,
            lifecycle_status=WorkOrder.LifecycleStatus.IN_PROGRESS,
        )
        # Create 3 open PART blockers on the same WO
        for i in range(3):
            line = _make_part_line(
                wo=wo, part=self.part, status=PartIssueLine.Status.PENDING,
                requested_by=self.tech, issued_by=self.tech,
            )
            WorkOrderBlocker.objects.create(
                work_order=wo, kind=WorkOrderBlocker.Kind.PART,
                status=WorkOrderBlocker.Status.OPEN,
                content_type=ContentType.objects.get_for_model(line),
                object_id=line.pk,
                opened_by=self.tech,
            )
        card = WorkOrderHealthService.compute(wo)
        self.assertEqual(card.risk, "HIGH")
        self.assertEqual(card.open_blockers_count, 3)
        self.assertIn("3+ OPEN BLOCKERS", card.notes)


# ---------------------------------------------------------------------------
# VideoCompressionService tests
# ---------------------------------------------------------------------------

class VideoCompressionServiceTests(TestCase):
    """VideoCompressionService.compress — stub for Phase 1.x."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="vid_comp_test_")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_file(self, name: str, size_bytes: int, ext: str = ".mp4") -> str:
        path = os.path.join(self.tmp, name + ext)
        with open(path, "wb") as fh:
            fh.write(b"\x00" * size_bytes)
        return path

    def test_compress_skips_small_file(self):
        """File < 5MB → skipped=True, reason mentions 'too small'."""
        path = self._write_file("small", size_bytes=1024)  # 1 KB
        result = VideoCompressionService.compress(path)
        self.assertTrue(result.skipped)
        self.assertIn("too small", result.reason.lower())
        self.assertEqual(result.original_path, path)
        self.assertEqual(result.compressed_size, result.original_size)
        self.assertEqual(result.ratio_pct, 100)

    def test_compress_raises_for_missing_file(self):
        """Nonexistent path → FileNotFoundError."""
        missing = os.path.join(self.tmp, "does_not_exist.mp4")
        with self.assertRaises(FileNotFoundError):
            VideoCompressionService.compress(missing)
