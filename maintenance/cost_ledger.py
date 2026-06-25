"""Cost ledger posting service.

All cost writes go through CostLedgerService. The service is the only
authority for creating CostTransaction rows and updating the WorkOrderCost
cache. Posting is wrapped in @transaction.atomic for atomicity.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Optional

from django.db import transaction
from django.db.models import Sum

from .models import (
    CostAdjustment,
    CostCategory,
    CostTransaction,
    WorkOrderCost,
)


class CostLedgerService:
    """Idempotent cost poster. All cost writes go through here."""

    @staticmethod
    @transaction.atomic
    def post_material(*, part_issue_line, actor, memo: str = "") -> Optional[CostTransaction]:
        """Called by inventory.services after the PartIssueLine is in a
        'physically issued' state (APPROVED/ISSUED with issued_qty > 0) and
        the StockMovement(ISSUE_TO_WO) has been created.
        """
        from inventory.models import PartIssueLine
        pil = PartIssueLine.objects.select_for_update().get(pk=part_issue_line.pk)
        # The codebase represents a physically-issued line as either
        # status=APPROVED (legacy direct-issue and execute_warehouse_issue
        # paths) or status=ISSUED. The spec phrases it as ISSUED; accept
        # both.
        if pil.status not in (
            PartIssueLine.Status.APPROVED, PartIssueLine.Status.ISSUED,
        ):
            return None
        if (pil.issued_qty or Decimal("0")) <= 0:
            return None
        amount = (Decimal(pil.issued_qty) * Decimal(pil.unit_cost)).quantize(Decimal("0.01"))
        return CostLedgerService._post(
            amount=amount, category=CostCategory.MATERIAL,
            quantity=pil.issued_qty, unit_cost=pil.unit_cost,
            work_order=pil.work_order,
            machine=pil.work_order.machine if pil.work_order_id else None,
            component=pil.work_order.component if pil.work_order_id else None,
            source_type="part_issue_line", source_id=pil.pk,
            actor=actor, memo=memo,
        )

    @staticmethod
    @transaction.atomic
    def post_vendor_repair(*, external_repair_order, actor, memo: str = "") -> Optional[CostTransaction]:
        """Called by maintenance.views.repair_manager_accept when ERO is closed."""
        from maintenance.models import ExternalRepairOrder
        if hasattr(external_repair_order, "pk"):
            ero = ExternalRepairOrder.objects.select_for_update().get(pk=external_repair_order.pk)
        else:
            ero = external_repair_order
        if ero.status != ExternalRepairOrder.Status.CLOSED or not ero.actual_cost:
            return None
        amount = Decimal(ero.actual_cost).quantize(Decimal("0.01"))
        return CostLedgerService._post(
            amount=amount, category=CostCategory.VENDOR_REPAIR,
            quantity=None, unit_cost=None,
            work_order=ero.work_order,
            machine=ero.work_order.machine if ero.work_order_id else None,
            component=ero.work_order.component if ero.work_order_id else None,
            source_type="external_repair_order", source_id=ero.pk,
            actor=actor, memo=memo,
        )

    @staticmethod
    @transaction.atomic
    def post_consumable(*, stock_movement, actor, memo: str = "") -> Optional[CostTransaction]:
        """Called by inventory.services.consumable_use when operator logs a consumable."""
        from inventory.models import StockMovement
        sm = StockMovement.objects.select_for_update().get(pk=stock_movement.pk)
        if sm.movement_type != StockMovement.MovementType.CONSUMABLE_USE:
            return None
        unit_cost = sm.unit_cost or Decimal("0")
        amount = (Decimal(sm.quantity) * Decimal(unit_cost)).quantize(Decimal("0.01"))
        # StockMovement doesn't have its own machine/component FKs — we
        # derive them from the linked WO so the cost rollup can attribute
        # consumable costs to the right asset.
        wo = sm.work_order
        if wo is not None and getattr(wo, "pk", None) is not None:
            machine = wo.machine
            component = wo.component
        else:
            machine = None
            component = None
        return CostLedgerService._post(
            amount=amount, category=CostCategory.CONSUMABLE,
            quantity=sm.quantity, unit_cost=unit_cost,
            work_order=wo,
            machine=machine,
            component=component,
            source_type="stock_movement", source_id=sm.pk,
            actor=actor, memo=memo,
        )

    @staticmethod
    @transaction.atomic
    def post_adjustment(*, work_order, amount, memo: str, actor) -> Optional[CostTransaction]:
        """Manager manual adjustment. Creates CostAdjustment, then CostTransaction."""
        amount = Decimal(amount).quantize(Decimal("0.01"))
        adj = CostAdjustment.objects.create(
            work_order=work_order, amount=amount, memo=memo, created_by=actor,
        )
        return CostLedgerService._post(
            amount=amount, category=CostCategory.ADJUSTMENT,
            quantity=None, unit_cost=None,
            work_order=work_order,
            machine=work_order.machine if work_order.pk else None,
            component=work_order.component if work_order.pk else None,
            source_type="cost_adjustment", source_id=adj.pk,
            adjustment=adj, actor=actor, memo=memo,
        )

    @staticmethod
    def _post(*, amount, category, quantity, unit_cost,
              work_order, machine, component,
              source_type=None, source_id=None, adjustment=None,
              actor=None, memo: str = "") -> Optional[CostTransaction]:
        """Internal: idempotent posting with reversal-on-change semantics.

        Not decorated with @transaction.atomic — callers (post_material,
        post_vendor_repair, post_consumable, post_adjustment) wrap the
        whole flow in their own transaction.

        Rules:
        - If source_type + source_id given, look for the latest non-reversal
          row for that source. If found with same amount + category → no-op.
        - If found with different amount/category, post a reversal row
          (is_reversal=True, amount=-existing.amount, supersedes=existing).
        - Then post the new row (is_reversal=False, supersedes=None).
        - After posting, update the WorkOrderCost cache for the WO.

        Net total for (source_type, source_id) = SUM(amount) over all rows,
        which correctly handles reversals.
        """
        # Delta-based idempotent posting.
        # Current net = SUM(amount) over ALL rows for this source (reversals
        # are negative, so they correctly reduce the running balance).
        # To reach the target `amount`, post `delta = amount - current_net`
        # as a new row. This handles:
        #   - First post: current_net=0, delta=amount → one new row
        #   - Same post again: current_net=amount, delta=0 → no-op
        #   - Different amount: delta = new - old → posts the difference
        #   - After a manual reversal: current_net reflects the reversal,
        #     so a new post at the original amount is a no-op
        #   - After a reversal + new target: delta captures the change
        current_net = Decimal("0")
        if source_type and source_id is not None:
            current_net = (
                CostTransaction.objects
                .filter(source_type=source_type, source_id=source_id)
                .aggregate(total=Sum("amount"))["total"]
                or Decimal("0")
            )

        # Idempotent: current state already matches target → no-op
        if current_net == amount:
            return (
                CostTransaction.objects
                .filter(source_type=source_type, source_id=source_id)
                .order_by("-occurred_at", "-pk")
                .first()
            )

        # Post the delta as a new row
        delta = amount - current_net

        # Post the new transaction (the delta from the current state)
        txn = CostTransaction.objects.create(
            amount=delta,
            category=category,
            currency="SAR",
            quantity=quantity,
            unit_cost=unit_cost,
            work_order=work_order,
            machine=machine,
            component=component,
            source_type=source_type,
            source_id=source_id,
            adjustment=adjustment,
            is_reversal=False,
            supersedes=None,
            actor=actor,
            memo=memo,
        )

        # Update the WO cost cache if a WO is attached
        if work_order is not None and getattr(work_order, "pk", None) is not None:
            CostLedgerService._refresh_wo_cache(work_order.pk)

        return txn

    @staticmethod
    def _refresh_wo_cache(work_order_id: int) -> None:
        """Refresh the WorkOrderCost cache from the ledger for one WO."""
        # Bug #5 fix: do NOT use update_or_create here. The default
        # WorkOrderCost.save() runs _auto_calculate() on first save, which
        # re-aggregates from PartIssueLine/StockMovement and overwrites the
        # ledger-derived values we just computed (especially painful for
        # WO rows that don't yet have a WorkOrderCost — the brand-new row
        # is created with ledger sums in `defaults`, then save() runs and
        # _auto_calculate() wipes them).
        # Instead, get_or_create without defaults so the row is created
        # empty, then explicitly call recalculate_from_ledger() which is
        # the single authoritative path that reads from CostTransaction.
        woc, _ = WorkOrderCost.objects.get_or_create(work_order_id=work_order_id)
        woc.recalculate_from_ledger()


def work_order_id(wo) -> Optional[int]:
    """Small helper to safely get a WO's pk even if unsaved."""
    if wo is None:
        return None
    return getattr(wo, "pk", None)
