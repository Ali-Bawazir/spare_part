"""
Repair viability scoring (Phase 2D-1).

Computes a REPAIR / REPLACE / BORDERLINE recommendation for a spare part
on a specific asset, based on the historical average vendor-repair cost
versus replacement cost, augmented with MTBF and repair count.

Inputs:
    - `part`:      SparePart being evaluated
    - `asset`:     optional Machine (level 3) — used for asset-specific MTBF
    - `component`: optional Machine (level 5) — used to anchor the
                   ExternalRepairOrder.component FK when filtering

Algorithm (per ADR-0007):
    repair_ratio = avg(ERO.actual_cost) / replacement_cost
    replacement_cost = part.last_purchase_cost or part.avg_cost
    mtbf = average hours between failures for this part on this asset,
           computed from ERO history (each ERO is a failure event);
           None when fewer than 2 historical failures exist.

Recommendation rules:
    repair_ratio < 30%                                   -> REPAIR
    repair_ratio > 70% AND (mtbf is None or mtbf < 100h) -> REPLACE
    repair_ratio > 50% AND repair_count > 3
        AND (mtbf is None or mtbf < 200h)                -> REPLACE
    50% <= repair_ratio <= 70%                           -> BORDERLINE
    otherwise                                            -> REPAIR

The 100/200 hour MTBF thresholds are placeholders. They should be
re-calibrated as we collect data on real parts.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Optional

from django.db.models import Avg
from django.utils.translation import gettext as _

if TYPE_CHECKING:
    from inventory.models import SparePart
    from maintenance.models import Machine

from .models import ExternalRepairOrder


# Thresholds on repair_ratio (expressed as percentages 0-100).
_REPAIR_BELOW = Decimal("30")
_BORDERLINE_BELOW = Decimal("70")
_REPLACE_REPAIR_OVER = Decimal("50")  # combined with high repair count

# MTBF thresholds (hours). Placeholders for Phase 2D-1 — recalibrate later.
_MTBF_REPLACE_BORDER = Decimal("100")
_MTBF_REPLACE_HIGH_COUNT = Decimal("200")

# Minimum number of historical failures to compute a per-asset MTBF.
_MIN_FAILURES_FOR_MTBF = 2


class RepairViabilityService:
    """REPAIR / REPLACE / BORDERLINE recommendation for a part on an asset.

    Stateless service. Call `RepairViabilityService.compute(part, asset,
    component)` to obtain a frozen ViabilityResult.
    """

    @dataclass(frozen=True)
    class ViabilityResult:
        """Output of `compute`."""
        repair_ratio_pct: int            # 0-100
        recommendation: str             # 'REPAIR' | 'REPLACE' | 'BORDERLINE'
        avg_repair_cost: Decimal
        replacement_cost: Decimal
        historical_count: int
        asset_mtbf_hours: Optional[Decimal]
        reason: str                     # human-readable explanation

    @classmethod
    def compute(
        cls,
        part: "SparePart",
        asset: Optional["Machine"] = None,
        component: Optional["Machine"] = None,
    ) -> "RepairViabilityService.ViabilityResult":
        """Compute the repair-vs-replace recommendation.

        Pure read-only computation. Looks at ERO history linked to the
        part (and optionally the asset / component) to derive the
        historical cost average and asset-specific MTBF.
        """
        # Historical EROs: filter by part; further narrow by asset /
        # component if provided. An ERO without an actual_cost recorded
        # (e.g. still DRAFT or SENT_TO_VENDOR) is excluded from the
        # average cost.
        #
        # NOTE: ExternalRepairOrder does NOT have a direct `part` FK in
        # this codebase — the part is reached via the related
        # ExternalRepairRequest (one-to-one, `origin_request` reverse).
        # We filter through that relation so that an ERO is only
        # considered "for this part" if its origin request named the
        # part explicitly. EROs without an origin request are not
        # attributed to a part.
        eros = ExternalRepairOrder.objects.filter(
            actual_cost__isnull=False,
            origin_request__part=part,
        )
        if asset is not None:
            eros = eros.filter(machine=asset)
        if component is not None:
            eros = eros.filter(component=component)

        # Replacement cost: prefer last_purchase_cost, fall back to avg_cost.
        replacement_cost = (
            part.last_purchase_cost if part.last_purchase_cost is not None
            else part.avg_cost
        ) or Decimal("0")

        historical_count = eros.count()

        # No history: short-circuit with REPAIR + "no history".
        if historical_count == 0:
            return cls.ViabilityResult(
                repair_ratio_pct=0,
                recommendation="REPAIR",
                avg_repair_cost=Decimal("0"),
                replacement_cost=replacement_cost,
                historical_count=0,
                asset_mtbf_hours=None,
                reason=_("No historical external repairs for this part — defaulting to REPAIR."),
            )

        # Average repair cost across historical EROs.
        avg_cost = (
            eros.aggregate(avg=Avg("actual_cost"))["avg"]
            or Decimal("0")
        )

        # Compute repair ratio. Avoid divide-by-zero.
        if replacement_cost <= 0:
            repair_ratio = Decimal("0")
        else:
            repair_ratio = (avg_cost / replacement_cost) * Decimal("100")

        # Asset-specific MTBF: average hours between failures for this
        # part on this asset. We use the gap between consecutive
        # closed_at timestamps on the filtered EROs. With <2 failures,
        # MTBF is undefined and we return None.
        asset_mtbf = cls._compute_asset_mtbf(eros, asset=asset, count=historical_count)

        # Apply the decision rules.
        if repair_ratio < _REPAIR_BELOW:
            recommendation = "REPAIR"
            reason = _(
                f"Repair cost is {repair_ratio:.0f}% of replacement — "
                f"well below threshold; REPAIR is cheaper."
            )
        elif (
            repair_ratio > _BORDERLINE_BELOW
            and (asset_mtbf is None or asset_mtbf < _MTBF_REPLACE_BORDER)
        ):
            recommendation = "REPLACE"
            reason = _(
                f"Repair cost is {repair_ratio:.0f}% of replacement AND "
                f"asset MTBF is {asset_mtbf or _('unknown')}; REPLACE is justified."
            )
        elif (
            repair_ratio > _REPLACE_REPAIR_OVER
            and historical_count > 3
            and (asset_mtbf is None or asset_mtbf < _MTBF_REPLACE_HIGH_COUNT)
        ):
            recommendation = "REPLACE"
            reason = _(
                f"Repair cost is {repair_ratio:.0f}% of replacement, "
                f"{historical_count} historical failures, and asset MTBF is "
                f"{asset_mtbf or _('unknown')}; REPLACE to break the cycle."
            )
        elif _REPLACE_REPAIR_OVER <= repair_ratio <= _BORDERLINE_BELOW:
            recommendation = "BORDERLINE"
            reason = _(
                f"Repair cost is {repair_ratio:.0f}% of replacement — "
                f"in the borderline band; manager judgment required."
            )
        else:
            recommendation = "REPAIR"
            reason = _(
                f"Repair cost is {repair_ratio:.0f}% of replacement with "
                f"acceptable MTBF; REPAIR is recommended."
            )

        return cls.ViabilityResult(
            repair_ratio_pct=int(repair_ratio),
            recommendation=recommendation,
            avg_repair_cost=Decimal(avg_cost),
            replacement_cost=replacement_cost,
            historical_count=historical_count,
            asset_mtbf_hours=asset_mtbf,
            reason=reason,
        )

    @classmethod
    def _compute_asset_mtbf(
        cls,
        eros,  # already-filtered queryset
        asset: Optional["Machine"],
        count: int,
    ) -> Optional[Decimal]:
        """Average hours between consecutive closed EROs for this part+asset.

        Returns None if fewer than 2 failures exist (insufficient data
        for a meaningful MTBF) or if `asset` is None.
        """
        if asset is None or count < _MIN_FAILURES_FOR_MTBF:
            return None
        # Get closed_at timestamps in chronological order.
        closed_times = list(
            eros.filter(closed_at__isnull=False)
                .order_by("closed_at")
                .values_list("closed_at", flat=True)
        )
        if len(closed_times) < _MIN_FAILURES_FOR_MTBF:
            return None
        total_seconds = sum(
            (closed_times[i] - closed_times[i - 1]).total_seconds()
            for i in range(1, len(closed_times))
        )
        avg_seconds = total_seconds / (len(closed_times) - 1)
        return Decimal(int(avg_seconds // 3600))


__all__ = ["RepairViabilityService"]
