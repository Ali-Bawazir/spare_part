"""
Work Order Health Card (Phase 2D-1).

Computes a 1-glance summary card for a Work Order, intended for the
top of the WO Detail page. Pure read-only computation.

Outputs:
    - lifecycle_status, operational_status
    - priority (from the originating issue, or 'n/a')
    - risk level (LOW / MEDIUM / HIGH)
    - open_blockers_count
    - waiting_days (days since WO creation)
    - full cost breakdown (parts, vendor, procurement, consumables,
      additional, total) — downtime is excluded from the total per
      the Phase 1.x glossary for the Work Order Health Card.
    - human-readable notes ("WAITING > 30 DAYS", "EMERGENCY", etc.)

Risk rules (per CONTEXT.md glossary for Work Order Health Card):
    HIGH if any of:
        - wo.is_emergency is True
        - wo.issue.priority == 'critical'
        - waiting_days > 30
        - open_blockers_count >= 3
        - total_cost > Decimal("5000")
    MEDIUM if any of (and not HIGH):
        - waiting_days > 7
        - open_blockers_count >= 1
        - total_cost > Decimal("1000")
    else LOW.

Notes (list of human-readable flags):
    "WAITING > 30 DAYS"  if waiting_days > 30
    "EMERGENCY"          if wo.is_emergency
    "CRITICAL PRIORITY"  if priority == 'critical'
    "3+ OPEN BLOCKERS"   if open_blockers_count >= 3
    "HIGH COST"          if total_cost > 5000
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, List

from django.utils import timezone
from django.utils.translation import gettext as _

if TYPE_CHECKING:
    from .models import WorkOrder


# Cost thresholds for risk classification. Placeholders for Phase 2D-1.
_COST_HIGH = Decimal("5000")
_COST_MEDIUM = Decimal("1000")

# Waiting-day thresholds for risk classification.
_WAIT_HIGH_DAYS = 30
_WAIT_MEDIUM_DAYS = 7

# Blocker count thresholds for risk classification.
_BLOCKERS_HIGH = 3
_BLOCKERS_MEDIUM = 1


class WorkOrderHealthService:
    """1-glance summary card for a Work Order.

    Stateless. Call `WorkOrderHealthService.compute(wo)` to obtain a
    frozen HealthCard.
    """

    @dataclass(frozen=True)
    class HealthCard:
        """Output of `compute`."""
        lifecycle_status: str
        operational_status: str
        priority: str        # 'critical'|'high'|'medium'|'low' or 'n/a'
        risk: str            # 'LOW' | 'MEDIUM' | 'HIGH'
        open_blockers_count: int
        waiting_days: int
        parts_cost: Decimal
        vendor_cost: Decimal
        consumables_cost: Decimal
        additional_cost: Decimal
        total_cost: Decimal
        is_emergency: bool
        notes: List[str] = field(default_factory=list)

    @classmethod
    def compute(cls, wo: "WorkOrder") -> "WorkOrderHealthService.HealthCard":
        """Compute the health card for a Work Order.

        Pure read-only. Looks at the WO, its blocker rows, and its
        cost_record (if any). Does not write to the DB.

        `waiting_days` is `(now - wo.created_at).days`. We use creation
        time rather than the lifecycle transition time so that a WO that
        has been sitting in the queue is still flagged as waiting even
        if it was just recently paused.
        """
        from .models import WorkOrderBlocker, WorkOrderCost

        # Lifecycle & operational status — read directly from the WO.
        lifecycle_status = wo.lifecycle_status or ""
        operational_status = wo.operational_status or ""

        # Priority: prefer the originating issue's priority, else 'n/a'.
        priority = "n/a"
        if getattr(wo, "issue_id", None):
            issue = wo.issue
            if issue is not None and issue.priority:
                priority = issue.priority

        # is_emergency: read from the WO. The field is on the model so
        # we use getattr defensively in case the column is missing in
        # some legacy deployment.
        is_emergency = bool(getattr(wo, "is_emergency", False))

        # Open blockers count.
        open_blockers_count = WorkOrderBlocker.objects.filter(
            work_order=wo, status=WorkOrderBlocker.Status.OPEN,
        ).count()

        # Waiting days.
        waiting_days = 0
        if wo.created_at is not None:
            waiting_days = max(0, (timezone.now() - wo.created_at).days)

        # Cost breakdown — zeros if no cost_record yet.
        cost = getattr(wo, "cost_record", None)
        if isinstance(cost, WorkOrderCost):
            parts_cost = Decimal(cost.material_cost or 0)
            vendor_cost = Decimal(cost.vendor_repair_cost or 0)
            consumables_cost = Decimal(cost.consumables_cost or 0)
            additional_cost = Decimal(cost.additional_cost or 0)
            # Downtime cost is intentionally EXCLUDED from the health
            # card total. It is a finance-team field, not maintenance cost.
            total_cost = (
                parts_cost + vendor_cost
                + consumables_cost + additional_cost
            )
        else:
            parts_cost = Decimal("0")
            vendor_cost = Decimal("0")
            consumables_cost = Decimal("0")
            additional_cost = Decimal("0")
            total_cost = Decimal("0")

        # Risk classification.
        high_triggers = [
            is_emergency,
            priority == "critical",
            waiting_days > _WAIT_HIGH_DAYS,
            open_blockers_count >= _BLOCKERS_HIGH,
            total_cost > _COST_HIGH,
        ]
        if any(high_triggers):
            risk = "HIGH"
        else:
            medium_triggers = [
                waiting_days > _WAIT_MEDIUM_DAYS,
                open_blockers_count >= _BLOCKERS_MEDIUM,
                total_cost > _COST_MEDIUM,
            ]
            risk = "MEDIUM" if any(medium_triggers) else "LOW"

        # Human-readable notes (only the most relevant flags).
        notes: List[str] = []
        if waiting_days > _WAIT_HIGH_DAYS:
            notes.append(_(f"WAITING > {_WAIT_HIGH_DAYS} DAYS"))
        if is_emergency:
            notes.append(_("EMERGENCY"))
        if priority == "critical":
            notes.append(_("CRITICAL PRIORITY"))
        if open_blockers_count >= _BLOCKERS_HIGH:
            notes.append(_(f"{_BLOCKERS_HIGH}+ OPEN BLOCKERS"))
        if total_cost > _COST_HIGH:
            notes.append(_("HIGH COST"))

        return cls.HealthCard(
            lifecycle_status=lifecycle_status,
            operational_status=operational_status,
            priority=priority,
            risk=risk,
            open_blockers_count=open_blockers_count,
            waiting_days=waiting_days,
            parts_cost=parts_cost,
            vendor_cost=vendor_cost,
            consumables_cost=consumables_cost,
            additional_cost=additional_cost,
            total_cost=total_cost,
            is_emergency=is_emergency,
            notes=notes,
        )


__all__ = ["WorkOrderHealthService"]
