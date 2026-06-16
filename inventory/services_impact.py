"""
Part impact scoring (Phase 2D-1).

Computes a composite 0-100 impact score for spare parts awaiting
procurement, used by the manager-facing Shortage Dashboard and the
Part Request modal to drive a "purchase now" recommendation.

Formula (per ADR-0007):
    affected_wos          * 10     # distinct WOs with open part waits
    affected_assets       * 5      # distinct machines in those WOs
    estimated_downtime_h  * 1.5    # rough downtime proxy
    blocked_labor_hours   * 1      # 1h per open PartIssueLine
    revenue_impact_k      * 2      # 1 point per emergency WO

Each component is capped at 100; total is capped at 100.
Bands: <40 LOW, 40-75 MEDIUM, >75 HIGH.
Recommendation: HIGH -> purchase_now, MEDIUM -> purchase, LOW -> monitor.

This is intentionally rough — the value is in relative ranking, not the
absolute number. The numbers and weights can be tuned as we collect
real-world data in Phase 2.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING

from django.db.models import Q

if TYPE_CHECKING:
    from .models import SparePart

from .models import PartIssueLine, PartShortageReport


# Heuristic hours-per-WO contribution to the downtime proxy.
# Reflects that a PENDING part request keeps the machine stopped until
# stock arrives, an APPROVED one is partially mitigated, and an
# ALLOCATED one is ready to issue from the warehouse.
_HOURS_PER_PENDING = Decimal("8")
_HOURS_PER_APPROVED = Decimal("4")
_HOURS_PER_ALLOCATED = Decimal("2")
_REVENUE_PER_EMERGENCY_K = Decimal("1")  # thousands of dollars per emergency WO

# Component cap: each subscore cannot exceed this value before summing.
_COMPONENT_CAP = 100
_TOTAL_CAP = 100

# Band thresholds on the final score.
_HIGH_THRESHOLD = 75
_MEDIUM_THRESHOLD = 40


class PartImpactService:
    """Composite 0-100 impact score for spare parts awaiting procurement.

    The class is stateless — all methods are classmethods. Call
    `PartImpactService.compute_impact(part)` to obtain a frozen
    ImpactResult.
    """

    @dataclass(frozen=True)
    class ImpactResult:
        """Output of `compute_impact`."""
        score: int                # 0-100, capped total
        level: str                # 'LOW' | 'MEDIUM' | 'HIGH'
        components: dict          # breakdown of the five subscores
        recommendation: str       # 'purchase_now' | 'purchase' | 'monitor'

    @classmethod
    def compute_impact(cls, part: "SparePart") -> "PartImpactService.ImpactResult":
        """Compute the impact score for a single SparePart.

        Pure read-only computation. Does not touch the DB outside of
        SELECTs on PartIssueLine, PartShortageReport, and WorkOrder.
        """
        # --- affected_wos: distinct WOs that have either an open PART wait
        # or an open SHORTAGE report for this part.
        open_part_qs = PartIssueLine.objects.filter(
            part=part,
            status__in=(
                PartIssueLine.Status.PENDING,
                PartIssueLine.Status.APPROVED,
                PartIssueLine.Status.ALLOCATED,
            ),
        )
        open_part_wo_ids = set(
            open_part_qs.values_list("work_order_id", flat=True).distinct()
        )
        open_shortage_wo_ids = set(
            PartShortageReport.objects.filter(
                part=part,
                status__in=(
                    PartShortageReport.Status.PENDING_REVIEW,
                    PartShortageReport.Status.APPROVED,
                    PartShortageReport.Status.IN_FULFILLMENT,
                    PartShortageReport.Status.BLOCKED,
                ),
            ).values_list("work_order_id", flat=True).distinct()
        )
        all_wo_ids = open_part_wo_ids | open_shortage_wo_ids
        affected_wos = len(all_wo_ids)

        # --- affected_assets: distinct machines linked to those WOs.
        from maintenance.models import WorkOrder
        affected_assets = (
            WorkOrder.objects
            .filter(pk__in=all_wo_ids, machine_id__isnull=False)
            .values_list("machine_id", flat=True)
            .distinct()
            .count()
        )

        # --- estimated_downtime_hours: weighted sum per open PartIssueLine
        # based on its current status. Shortage reports also contribute,
        # but we use the ALLOCATED weight (least impactful) since a
        # shortage report usually means procurement is the path forward.
        estimated_downtime = Decimal("0")
        for line in open_part_qs.only("status"):
            status = line.status
            if status == PartIssueLine.Status.PENDING:
                estimated_downtime += _HOURS_PER_PENDING
            elif status == PartIssueLine.Status.APPROVED:
                estimated_downtime += _HOURS_PER_APPROVED
            elif status == PartIssueLine.Status.ALLOCATED:
                estimated_downtime += _HOURS_PER_ALLOCATED
        # Add the open-shortage count * 4h as a floor estimate.
        estimated_downtime += Decimal(len(open_shortage_wo_ids)) * Decimal("4")

        # --- blocked_labor_hours: 1 hour per open PartIssueLine.
        blocked_labor = Decimal(open_part_qs.count())

        # --- revenue_impact_k: count of emergency WOs (rough proxy).
        emergency_count = (
            WorkOrder.objects
            .filter(pk__in=all_wo_ids, is_emergency=True)
            .count()
        )
        revenue_impact_k = Decimal(emergency_count) * _REVENUE_PER_EMERGENCY_K

        # --- weighted components, each capped.
        comp_affected_wos = min(affected_wos * 10, _COMPONENT_CAP)
        comp_assets = min(affected_assets * 5, _COMPONENT_CAP)
        comp_downtime = min(int(estimated_downtime * Decimal("1.5")), _COMPONENT_CAP)
        comp_labor = min(int(blocked_labor * Decimal("1")), _COMPONENT_CAP)
        comp_revenue = min(int(revenue_impact_k * Decimal("2")), _COMPONENT_CAP)

        score = min(
            comp_affected_wos + comp_assets + comp_downtime + comp_labor + comp_revenue,
            _TOTAL_CAP,
        )

        if score > _HIGH_THRESHOLD:
            level = "HIGH"
            recommendation = "purchase_now"
        elif score > _MEDIUM_THRESHOLD:
            level = "MEDIUM"
            recommendation = "purchase"
        else:
            level = "LOW"
            recommendation = "monitor"

        return cls.ImpactResult(
            score=int(score),
            level=level,
            components={
                "affected_wos": int(comp_affected_wos),
                "affected_assets": int(comp_assets),
                "downtime_hours": int(comp_downtime),
                "blocked_labor_hours": int(comp_labor),
                "revenue_impact_k": int(comp_revenue),
            },
            recommendation=recommendation,
        )


__all__ = ["PartImpactService"]
