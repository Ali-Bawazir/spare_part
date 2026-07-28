"""Read-only cost views for Machine and Component dashboards.

These are not Django models — they're pure Python dataclasses that query
the CostTransaction ledger live. No snapshot tables, no caching layer.

The data is always fresh: every page load re-aggregates from the ledger.
Performance is fine because we filter by indexed fields (machine_id,
component_id, work_order_id, occurred_at).
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from django.db.models import Q, Sum
from django.utils import timezone

from .models import CostCategory, CostTransaction, Machine


def _by_category(txn_filter, since):
    """Return {category: Decimal} for matching transactions since `since`."""
    sums = (
        CostTransaction.objects
        .filter(txn_filter, occurred_at__gte=since)
        .values("category")
        .annotate(total=Sum("amount"))
    )
    return {
        row["category"]: row["total"] or Decimal("0")
        for row in sums
    }


def _wo_count(wo_filter, since):
    """Count of WOs updated in the period matching the filter."""
    from .models import WorkOrder
    return WorkOrder.objects.filter(wo_filter, updated_at__gte=since).count()


def _failure_count(wo_ids, since):
    """Count of PartIssueLine ISSUED in the period for the given WO ids.

    Uses `created_at` as the proxy timestamp because PartIssueLine does
    not have a dedicated `issued_at` field. This is a reasonable proxy
    for when the failure/request was created.
    """
    from inventory.models import PartIssueLine
    return PartIssueLine.objects.filter(
        work_order_id__in=wo_ids,
        status=PartIssueLine.Status.ISSUED,
        created_at__gte=since,
    ).count()


@dataclass(frozen=True)
class MachineCost:
    """Cost rollup for a Machine (level 3) or Subassembly (level 4)."""
    machine: Machine
    period_days: int
    material: Decimal
    vendor_repair: Decimal
    consumable: Decimal
    adjustment: Decimal
    procurement: Decimal
    wo_count: int
    failure_count: int
    currency: str = "SAR"

    @property
    def total(self) -> Decimal:
        return (
            self.material
            + self.vendor_repair
            + self.consumable
            + self.adjustment
        )

    @property
    def cost_per_failure(self) -> Optional[Decimal]:
        if self.failure_count == 0:
            return None
        return (self.total / self.failure_count).quantize(Decimal("0.01"))

    @classmethod
    def for_machine(cls, machine: Machine, period_days: int = 90) -> "MachineCost":
        since = timezone.now() - timezone.timedelta(days=period_days)
        descendant_ids = [m.id for m in machine.get_descendants()]
        all_ids = [machine.id] + descendant_ids
        # Ledger: transactions that target the machine directly OR via WO
        # (the work_order__machine_id__in filter is a reverse FK lookup)
        txn_filter = (
            Q(machine_id__in=all_ids)
            | Q(work_order__machine_id__in=all_ids)
        )
        by_cat = _by_category(txn_filter, since)
        # WO count: count WorkOrders on the machine or any descendant
        from .models import WorkOrder
        wo_q = Q(machine_id__in=all_ids)
        wo_count = WorkOrder.objects.filter(wo_q, updated_at__gte=since).count()
        # WO ids for failure count
        wo_ids = list(
            WorkOrder.objects.filter(wo_q).values_list("id", flat=True)
        )
        procurement = _procurement_total_for_wos(wo_ids, since)
        return cls(
            machine=machine,
            period_days=period_days,
            material=by_cat.get(CostCategory.MATERIAL, Decimal("0")),
            vendor_repair=by_cat.get(CostCategory.VENDOR_REPAIR, Decimal("0")),
            consumable=by_cat.get(CostCategory.CONSUMABLE, Decimal("0")),
            adjustment=by_cat.get(CostCategory.ADJUSTMENT, Decimal("0")),
            procurement=procurement,
            wo_count=wo_count,
            failure_count=_failure_count(wo_ids, since),
        )


@dataclass(frozen=True)
class ComponentCost:
    """Cost rollup for a Component (level 5)."""
    component: Machine
    period_days: int
    material: Decimal
    vendor_repair: Decimal
    consumable: Decimal
    adjustment: Decimal
    procurement: Decimal
    wo_count: int
    failure_count: int
    currency: str = "SAR"

    @property
    def total(self) -> Decimal:
        return (
            self.material
            + self.vendor_repair
            + self.consumable
            + self.adjustment
        )

    @property
    def cost_per_failure(self) -> Optional[Decimal]:
        if self.failure_count == 0:
            return None
        return (self.total / self.failure_count).quantize(Decimal("0.01"))

    @classmethod
    def for_component(cls, component: Machine, period_days: int = 90) -> "ComponentCost":
        since = timezone.now() - timezone.timedelta(days=period_days)
        # Ledger: transactions that target the component directly OR via WO
        txn_filter = (
            Q(component=component)
            | Q(work_order__component=component)
        )
        by_cat = _by_category(txn_filter, since)
        # WO count: WOs where component=this OR machine=this (parent's WOs
        # can target the component via the machine FK when component FK is null)
        from .models import WorkOrder
        wo_q = Q(component=component) | Q(machine=component)
        wo_count = WorkOrder.objects.filter(wo_q, updated_at__gte=since).count()
        wo_ids = list(WorkOrder.objects.filter(wo_q).values_list("id", flat=True))
        procurement = _procurement_total_for_wos(wo_ids, since)
        return cls(
            component=component,
            period_days=period_days,
            material=by_cat.get(CostCategory.MATERIAL, Decimal("0")),
            vendor_repair=by_cat.get(CostCategory.VENDOR_REPAIR, Decimal("0")),
            consumable=by_cat.get(CostCategory.CONSUMABLE, Decimal("0")),
            adjustment=by_cat.get(CostCategory.ADJUSTMENT, Decimal("0")),
            procurement=procurement,
            wo_count=wo_count,
            failure_count=_failure_count(wo_ids, since),
        )


# Phase 2d: aggregate helper for Machine/Component rollups. Computes
# procurement cost (POItem.received_qty × actual_unit_price) for the
# given WO ids within the time window. Returns Decimal("0") if no PO
# lines match. Cheap because the join path is fully indexed.
def _procurement_total_for_wos(wo_ids, since) -> Decimal:
    if not wo_ids:
        return Decimal("0")
    from django.db.models import F, Sum, DecimalField
    from procurement.models import PurchaseOrderItem
    result = (
        PurchaseOrderItem.objects
        .filter(
            purchase_order__purchase_requests__work_order_id__in=wo_ids,
            received_qty__gt=0,
            purchase_order__purchase_requests__work_order__updated_at__gte=since,
        )
        .annotate(
            line_total=F("received_qty") * F("actual_unit_price"),
        )
        .aggregate(
            total=Sum("line_total", output_field=DecimalField(max_digits=14, decimal_places=2))
        )["total"]
    )
    return result or Decimal("0")


# Standard periods shown on every cost tab
DEFAULT_PERIODS = [30, 90, 365]


def machine_costs_for_periods(machine: Machine) -> dict:
    """Return {days: MachineCost} for the standard periods."""
    return {d: MachineCost.for_machine(machine, period_days=d) for d in DEFAULT_PERIODS}


def component_costs_for_periods(component: Machine) -> dict:
    """Return {days: ComponentCost} for the standard periods."""
    return {d: ComponentCost.for_component(component, period_days=d) for d in DEFAULT_PERIODS}
