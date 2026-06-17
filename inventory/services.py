from __future__ import annotations

from decimal import Decimal

from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from maintenance.models import WorkOrder
from maintenance.services import log_audit

from .models import (
    Inventory,
    PartIssueLine,
    PartShortageDecision,
    PartShortageReport,
    SparePart,
    StockMovement,
)


def _get_default_site():
    from maintenance.models import Site
    return Site.objects.filter(is_default=True).first()


@transaction.atomic
def stock_in(
    *,
    part: SparePart,
    quantity: Decimal,
    performed_by,
    supplier_name: str,
    unit_cost: Decimal,
    invoice_ref: str,
    note: str = "",
    site=None,
) -> StockMovement:
    site = site or _get_default_site()
    if not site:
        raise ValueError("No default site configured. Please create a Site first.")

    recent = StockMovement.objects.filter(
        part=part, movement_type=StockMovement.MovementType.STOCK_IN,
        performed_by=performed_by, invoice_ref=invoice_ref,
        created_at__gte=timezone.now() - timezone.timedelta(seconds=10)
    ).exists()
    if recent:
        raise ValueError("Duplicate stock-in detected. Please wait before submitting again.")

    try:
        inv = Inventory.objects.select_for_update().get(part=part, site=site)
    except Inventory.DoesNotExist:
        inv = Inventory.objects.create(part=part, site=site, quantity_available=0)
    quantity_before = inv.quantity_available
    inv.quantity_available += quantity
    inv.save()
    quantity_after = inv.quantity_available

    if unit_cost and unit_cost > 0:
        old_avg = part.avg_cost or Decimal("0")
        old_qty = quantity_before
        new_qty = quantity
        if old_qty + new_qty > 0:
            part.avg_cost = (old_avg * old_qty + unit_cost * new_qty) / (old_qty + new_qty)
            part.save(update_fields=["avg_cost"])
        part.last_purchase_cost = unit_cost
        part.save(update_fields=["last_purchase_cost"])

    ref = {
        "invoice_number": invoice_ref,
        "supplier": supplier_name,
        "cost": str(quantity * unit_cost) if unit_cost else None,
        "attachments": [],
        "notes": note,
    }

    movement = StockMovement.objects.create(
        part=part,
        site=site,
        movement_type=StockMovement.MovementType.STOCK_IN,
        quantity=quantity,
        quantity_before=quantity_before,
        quantity_after=quantity_after,
        performed_by=performed_by,
        supplier_name=supplier_name,
        unit_cost=unit_cost,
        invoice_ref=invoice_ref,
        reference=ref,
        note=note,
    )
    log_audit(
        actor=performed_by,
        action="stock_in",
        entity="SparePart",
        object_id=str(part.pk),
        payload={"qty": str(quantity), "invoice": invoice_ref},
    )
    _maybe_notify_low_stock(part, site)
    return movement


def _create_procurement_for_shortage(
    *,
    part: SparePart,
    quantity: Decimal,
    work_order: WorkOrder,
    created_by,
    note_suffix: str = "",
):
    """Create a PurchaseRequest for a parts shortage. Idempotent: if a
    PENDING PR already exists for the same (work_order, part), skip
    creation. Manager can edit the existing PR manually if the qty
    needs to change.
    """
    from maintenance.notifications import notify_procurement_request
    from procurement.models import PurchaseRequest

    if quantity <= 0:
        return None

    existing = PurchaseRequest.objects.filter(
        work_order=work_order,
        part=part,
        status=PurchaseRequest.Status.PENDING,
    ).first()
    if existing is not None:
        return existing

    pr = PurchaseRequest.objects.create(
        part=part,
        work_order=work_order,
        quantity=quantity,
        notes=(
            f"Auto: shortage for WO-{work_order.number}. {note_suffix}"
        ).strip(),
        status=PurchaseRequest.Status.PENDING,
        created_by=created_by,
    )
    notify_procurement_request(pr)
    return pr


def _deduct_and_record_issue(
    *, wo, part, quantity, unit_cost, invoice_ref, supplier_name, issued_by, ref, site=None
):
    site = site or _get_default_site()
    inv = Inventory.objects.select_for_update().get(part=part, site=site)
    quantity_before = inv.quantity_available
    inv.quantity_available -= quantity
    inv.save()
    quantity_after = inv.quantity_available

    pil = PartIssueLine.objects.create(
        work_order=wo, part=part, quantity=quantity,
        requested_qty=quantity, approved_qty=quantity, issued_qty=quantity,
        shortage_qty=0,
        status=PartIssueLine.Status.APPROVED,
        unit_cost=unit_cost, invoice_ref=invoice_ref,
        supplier_name=supplier_name, issued_by=issued_by,
    )
    StockMovement.objects.create(
        part=part, site=site,
        movement_type=StockMovement.MovementType.ISSUE_TO_WO,
        quantity=quantity,
        quantity_before=quantity_before,
        quantity_after=quantity_after,
        work_order=wo, performed_by=issued_by,
        supplier_name=supplier_name, unit_cost=unit_cost,
        invoice_ref=invoice_ref, reference=ref,
        note=f"Issued to WO-{wo.number}",
    )
    return pil


def _maybe_notify_low_stock(part: SparePart, site=None) -> None:
    part.refresh_from_db()
    if part.is_low_stock(site=site):
        from maintenance.notifications import notify_low_stock
        inv = part.inventory_items.filter(site=site).first() if site else None
        qty = inv.quantity_available - inv.quantity_reserved if inv else 0
        notify_low_stock(part, sku=part.sku, qty=qty)


@transaction.atomic
def issue_part_to_work_order(
    *, wo, part, quantity, unit_cost, invoice_ref, supplier_name, issued_by, site=None
) -> tuple[bool, str]:
    site = site or _get_default_site()
    if not site:
        return False, "No default site configured."
    if quantity <= 0:
        return False, "Quantity must be positive."
    if wo.lifecycle_status == WorkOrder.LifecycleStatus.CLOSED:
        return False, "Cannot issue parts to a closed work order."

    existing = PartIssueLine.objects.filter(work_order=wo, part=part).exists()
    if existing:
        return False, "Parts already issued for this work order and part combination."

    inv = Inventory.objects.select_for_update().get(part=part, site=site)
    available = inv.quantity_available - inv.quantity_reserved
    quantity_before = inv.quantity_available

    ref = {"work_order_id": str(wo.number), "invoice": invoice_ref}

    if available >= quantity:
        pil = _deduct_and_record_issue(
            wo=wo, part=part, quantity=quantity,
            unit_cost=unit_cost, invoice_ref=invoice_ref,
            supplier_name=supplier_name, issued_by=issued_by,
            ref=ref, site=site,
        )
        log_audit(
            actor=issued_by, action="issue_part", entity="WorkOrder",
            object_id=str(wo.pk),
            payload={"part": part.sku, "qty": str(quantity), "mode": "full"},
        )
        _maybe_notify_low_stock(part, site)
        # Phase 1+2 Cost Ledger: post the material cost for this issue.
        try:
            from maintenance.cost_ledger import CostLedgerService
            CostLedgerService.post_material(
                part_issue_line=pil,
                actor=issued_by,
                memo=f"Part issued: {part.name} x {pil.issued_qty}",
            )
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning(
                f"Cost ledger post_material failed for line {pil.pk}: {e}"
            )
        return True, f"Issued full quantity ({quantity})."

    elif available > 0:
        short = quantity - available
        pil = _deduct_and_record_issue(
            wo=wo, part=part, quantity=available,
            unit_cost=unit_cost, invoice_ref=invoice_ref,
            supplier_name=supplier_name, issued_by=issued_by,
            ref=ref, site=site,
        )
        # P3.1 P1.6: manager direct issue path does NOT auto-create a PR.
        # The manager has full visibility and can open one manually.
        log_audit(
            actor=issued_by, action="issue_part_partial", entity="WorkOrder",
            object_id=str(wo.pk),
            payload={"part": part.sku, "issued": str(available), "shortage": str(short)},
        )
        _maybe_notify_low_stock(part, site)
        # Phase 1+2 Cost Ledger: post the material cost for this partial issue.
        try:
            from maintenance.cost_ledger import CostLedgerService
            CostLedgerService.post_material(
                part_issue_line=pil,
                actor=issued_by,
                memo=f"Part issued (partial): {part.name} x {pil.issued_qty}",
            )
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning(
                f"Cost ledger post_material failed for line {pil.pk}: {e}"
            )
        return True, (
            f"Partial issue: {available} issued; {short} short. "
            f"Manager should open a PurchaseRequest manually."
        )

    log_audit(
        actor=issued_by, action="issue_part_procurement_only", entity="WorkOrder",
        object_id=str(wo.pk), payload={"part": part.sku, "qty": str(quantity)},
    )
    _maybe_notify_low_stock(part, site)
    return True, (
        "No stock on hand. Manager should open a PurchaseRequest manually "
        f"for {quantity} of {part.sku}."
    )


@transaction.atomic
def consumable_use(
    *,
    part: SparePart,
    quantity: Decimal,
    consumed_by,
    note: str = "",
    machine_id: int | None = None,
    site=None,
) -> tuple[bool, str]:
    """
    Operator self-logs an approved consumable item.
    
    Creates both:
    - ConsumableAssignment (business ledger)
    - StockMovement (inventory audit)
    
    Both created atomically; StockMovement FK linked on ConsumableAssignment.
    """
    if not part.is_consumable:
        return False, "Selected part is not marked as consumable."
    if not part.allow_operator_consumption:
        return False, "This item is not available for operator self-service."
    if quantity <= 0:
        return False, "Quantity must be positive."

    site = site or _get_default_site()
    if not site:
        return False, "No default site configured."

    from django.utils import timezone
    from inventory.models import ConsumableAssignment

    # Check for duplicate (within 5 seconds)
    recent = StockMovement.objects.filter(
        part=part,
        movement_type=StockMovement.MovementType.CONSUMABLE_USE,
        performed_by=consumed_by,
        created_at__gte=timezone.now() - timezone.timedelta(seconds=5),
    ).exists()
    if recent:
        return False, "Duplicate consumable log detected. Please wait."

    # Lock inventory row
    inv = Inventory.objects.select_for_update().get(part=part, site=site)
    quantity_before = inv.quantity_available
    if inv.quantity_available < quantity:
        return False, "Cannot exceed stock."
    inv.quantity_available -= quantity
    inv.save()
    quantity_after = inv.quantity_available

    # Build note string for StockMovement (includes machine if provided)
    movement_note = note
    if machine_id:
        movement_note = f"machine_id={machine_id}" + (f"; {note}" if note else "")

    # Create StockMovement first
    stock_movement = StockMovement.objects.create(
        part=part,
        site=site,
        movement_type=StockMovement.MovementType.CONSUMABLE_USE,
        quantity=quantity,
        quantity_before=quantity_before,
        quantity_after=quantity_after,
        performed_by=consumed_by,
        note=movement_note,
    )

    # Resolve machine if provided
    machine = None
    if machine_id:
        from maintenance.models import Machine
        try:
            machine = Machine.objects.get(pk=machine_id)
        except Machine.DoesNotExist:
            pass

    # Create ConsumableAssignment (Phase 1: issued_by = consumed_by, source = SELF_SERVICE)
    assignment = ConsumableAssignment.objects.create(
        part=part,
        consumed_by=consumed_by,
        issued_by=consumed_by,  # Phase 1: self-service
        quantity=quantity,
        source=ConsumableAssignment.Source.SELF_SERVICE,
        approved=True,  # Phase 1: auto-approved
        site=site,
        machine=machine,
        note=note,
        stock_movement=stock_movement,  # Direct FK link
    )

    # Update reference JSON on StockMovement for audit portability
    stock_movement.reference = {"assignment_id": assignment.pk}
    stock_movement.save(update_fields=["reference"])

    log_audit(
        actor=consumed_by,
        action="consumable_use",
        entity="SparePart",
        object_id=str(part.pk),
        payload={"qty": str(quantity), "assignment_id": assignment.pk},
    )

    # Phase 1+2 Cost Ledger: post the consumable cost.
    try:
        from maintenance.cost_ledger import CostLedgerService
        CostLedgerService.post_consumable(
            stock_movement=stock_movement,
            actor=consumed_by,
            memo=f"Consumable: {part.name} x {quantity}",
        )
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(
            f"Cost ledger post_consumable failed for movement {stock_movement.pk}: {e}"
        )

    _maybe_notify_low_stock(part, site)
    return True, f"Logged {quantity} x {part.name}."


def issue_consumable(
    *,
    part: SparePart,
    quantity: Decimal,
    consumed_by,
    issued_by,
    note: str = "",
    machine_id: int | None = None,
    site=None,
) -> tuple[bool, str]:
    """
    Manager/supervisor issues a consumable item to an operator or technician.
    
    Differs from consumable_use():
    - issued_by is the supervisor/manager (not the consumer)
    - Part only needs is_consumable=True (not allow_operator_consumption)
    - Source = SUPERVISOR_ISSUE
    """
    if not part.is_consumable:
        return False, "Selected part is not marked as consumable."
    if quantity <= 0:
        return False, "Quantity must be positive."

    site = site or _get_default_site()
    if not site:
        return False, "No default site configured."

    from django.utils import timezone
    from inventory.models import ConsumableAssignment

    # Lock inventory row
    inv = Inventory.objects.select_for_update().get(part=part, site=site)
    quantity_before = inv.quantity_available
    if inv.quantity_available < quantity:
        return False, "Cannot exceed stock."
    inv.quantity_available -= quantity
    inv.save()
    quantity_after = inv.quantity_available

    movement_note = f"Issued by {issued_by.username}" + (f"; {note}" if note else "")
    if machine_id:
        movement_note = f"machine_id={machine_id}; " + movement_note

    stock_movement = StockMovement.objects.create(
        part=part,
        site=site,
        movement_type=StockMovement.MovementType.CONSUMABLE_USE,
        quantity=quantity,
        quantity_before=quantity_before,
        quantity_after=quantity_after,
        performed_by=issued_by,
        note=movement_note,
    )

    machine = None
    if machine_id:
        from maintenance.models import Machine
        try:
            machine = Machine.objects.get(pk=machine_id)
        except Machine.DoesNotExist:
            pass

    assignment = ConsumableAssignment.objects.create(
        part=part,
        consumed_by=consumed_by,
        issued_by=issued_by,
        quantity=quantity,
        source=ConsumableAssignment.Source.SUPERVISOR_ISSUE,
        approved=True,
        site=site,
        machine=machine,
        note=note,
        stock_movement=stock_movement,
    )

    stock_movement.reference = {"assignment_id": assignment.pk}
    stock_movement.save(update_fields=["reference"])

    log_audit(
        actor=issued_by,
        action="consumable_issue",
        entity="SparePart",
        object_id=str(part.pk),
        payload={"qty": str(quantity), "assignment_id": assignment.pk, "consumed_by": consumed_by.username},
    )

    _maybe_notify_low_stock(part, site)
    return True, f"Issued {quantity} x {part.name} to {consumed_by.username}."


# ---------------------------------------------------------------------------
# Phase 2.1 — Hybrid approval workflow for parts on a work order
# ---------------------------------------------------------------------------


@transaction.atomic
def request_part_on_wo(
    *,
    wo: WorkOrder,
    part: SparePart,
    quantity: Decimal,
    technician,
    note: str = "",
) -> dict:
    """Technician adds a PENDING part request to their own assigned WO.

    Three outcomes, all PENDING (manager approval gate is preserved for
    every flow — no auto-issue even when stock is fully available):

      A. usable >= qty        -> PENDING, no shortage, full qty in requested_qty
      B. 0 < usable < qty     -> PENDING, shortage_qty = qty - usable
      C. usable == 0         -> PENDING, shortage_qty = qty

    Inventory is NEVER deducted here. The deduction happens inside
    approve_part_request() after the manager approves. The technician
    sees the stock badge in the UI to know what they're requesting,
    but the system's authoritative action happens at manager-approval
    time. This preserves the audit principle that every inventory
    movement has an explicit approver.
    """
    if quantity <= 0:
        raise ValueError("Quantity must be positive.")
    if wo.lifecycle_status == WorkOrder.LifecycleStatus.CLOSED:
        raise ValueError("Cannot request parts on a closed work order.")

    # Idempotency: one PENDING line per (WO, part)
    existing = PartIssueLine.objects.filter(
        work_order=wo, part=part, status=PartIssueLine.Status.PENDING
    ).first()
    if existing is not None:
        site = _get_default_site()
        inv = Inventory.objects.filter(part=part, site=site).first()
        on_hand   = (inv.quantity_available if inv else Decimal("0"))
        reserved  = (inv.quantity_reserved  if inv else Decimal("0"))
        return {
            "line": existing,
            "shortage_qty": existing.shortage_qty,
            "shortage": existing.shortage_qty > 0,
            "already_pending": True,
            "available_qty_snapshot": on_hand,
            "reserved_qty_snapshot": reserved,
            "usable_qty_snapshot": on_hand - reserved,
            "suggested_action": "raise_shortage_request_or_full",
            "shortage_report": None,
        }

    site = _get_default_site()
    inv = Inventory.objects.filter(part=part, site=site).first()
    on_hand   = (inv.quantity_available if inv else Decimal("0"))
    reserved  = (inv.quantity_reserved  if inv else Decimal("0"))
    usable    = on_hand - reserved

    machine_crit = (wo.machine.criticality or "") if wo.machine_id else ""
    wo_priority  = (wo.issue.priority or "") if getattr(wo, "issue_id", None) else ""

    # All three flows: PENDING line, no stock change. The manager's
    # approve_part_request() will do the actual deduction at approval time.
    if usable >= quantity:
        # Flow A: stock is available but we STILL go through the
        # approval gate (per v7). shortage_qty = 0.
        shortage_qty = Decimal("0")
        stock_state_hint = "available"
    else:
        # Flow B or C: shortage. recorded for the manager's review UI.
        shortage_qty = quantity - usable  # = quantity - max(usable, 0)
        stock_state_hint = "low" if usable > 0 else "out"

    line = PartIssueLine.objects.create(
        work_order=wo, part=part, quantity=quantity,
        unit_cost=Decimal("0"), invoice_ref="", supplier_name="",
        status=PartIssueLine.Status.PENDING,
        is_emergency_auto_approved=False,  # NEVER auto-approve; manager gate is preserved
        requested_by=technician, issued_by=technician,
        requested_qty=quantity,
        issued_qty=Decimal("0"),  # No deduction yet
        approved_qty=Decimal("0"),  # No approval yet
        shortage_qty=shortage_qty,
        # Per v7, we also record the snapshot context in the manager_note
        # field so the approve view can show "Tech requested 10 when
        # stock was 30" without re-querying.
        manager_note=(
            f"Tech requested at {timezone.now().isoformat(timespec='seconds')}. "
            f"Available: {usable}/{quantity} requested. "
            f"Awaiting manager approval."
        ) if shortage_qty > 0 else "",
    )

    # v4.8 Fix 2: if shortage, create the PartShortageReport atomically
    # with the line and set the explicit FK linkage. This avoids the
    # v4.2 "most recent pending line" lookup ambiguity.
    if shortage_qty > 0:
        ct = ContentType.objects.get_for_model(WorkOrder)
        report = PartShortageReport.objects.create(
            content_type=ct, object_id=wo.pk,
            work_order=wo, part=part,
            qty_requested=quantity,
            qty_issued=Decimal("0"),
            shortage_qty=shortage_qty,
            available_qty_snapshot=on_hand,
            reserved_qty_snapshot=reserved,
            usable_qty_snapshot=usable,
            machine_criticality_snapshot=machine_crit,
            part_criticality_snapshot="",
            wo_priority_snapshot=wo_priority,
            reason="",
            is_emergency=wo.is_emergency,
            reported_by=technician,
            status=PartShortageReport.Status.PENDING_REVIEW,
        )
        line.related_shortage_report = report
        line.save(update_fields=["related_shortage_report"])

    # Audit: one action for all three flows. The shortage_qty field
    # tells the auditor whether the line had a shortage or not.
    log_audit(
        actor=technician, action="part_request_pending",
        entity="WorkOrder", object_id=str(wo.pk),
        payload={
            "part": part.sku,
            "qty_requested": str(quantity),
            "qty_available_at_request": str(usable),
            "shortage_qty": str(shortage_qty),
            "stock_state_hint": stock_state_hint,
            "machine_crit_at_request": machine_crit,
            "wo_priority_at_request": wo_priority,
            "is_emergency": wo.is_emergency,
        },
    )

    # Phase 2B: open a PART WO Blocker for this request, attempt allocation,
    # and recompute the WO's operational status. Best-effort: any failure
    # here MUST NOT break the original request (line + shortage report are
    # already persisted above).
    try:
        from inventory.services_allocation import PartAllocationService
        from maintenance.models import WorkOrderBlocker
        from maintenance.services_blocker import WorkOrderBlockerService
        from maintenance.services_wo_status import WorkOrderService

        WorkOrderBlockerService.open_blocker(
            work_order=wo,
            kind=WorkOrderBlocker.Kind.PART,
            external_obj=line,
            opened_by=technician,
            note=note or "",
            external_label=f"{part.name} (SKU {part.sku}) × {quantity}",
        )
        # No-op while line.approved_qty == 0 (manager hasn't approved yet),
        # but safe to call so future state flows through the same path.
        PartAllocationService.allocate_one(line)
        WorkOrderService.recompute_operational_status(wo)
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(
            f"Failed to open PART blocker for line {line.pk}: {e}"
        )

    return {
        "line": line,
        "shortage_qty": shortage_qty,
        "shortage": shortage_qty > 0,
        "already_pending": False,
        "available_qty_snapshot": on_hand,
        "reserved_qty_snapshot": reserved,
        "usable_qty_snapshot": usable,
        "suggested_action": "raise_shortage_request" if shortage_qty > 0 else "awaiting_manager_approval",
        "shortage_report": getattr(line, "related_shortage_report", None),
    }


@transaction.atomic
def raise_shortage_request(
    *,
    wo: WorkOrder,
    part: SparePart,
    technician,
    note: str = "",
):
    """Idempotent: get-or-create a PENDING PartShortageReport for the (WO, part).

    v4.8: The report is now created atomically in request_part_on_wo when
    the line is created with a shortage. This function handles two cases:

      1. New v4.8+ flow: report already exists (created in request_part_on_wo).
         This function returns the existing report. It updates the reason
         note and re-sends the notification.
      2. Backfill / legacy: a PENDING line exists without a report (pre-v4.8
         data, or the line was created by a different code path). This
         function creates the report and sets the explicit FK linkage on
         the line. Idempotent via the partial UniqueConstraint.

    Two layers of defense: app-level get_or_create and DB-level
    UniqueConstraint(condition=Q(status='pending')).
    """
    site = _get_default_site()
    inv = Inventory.objects.filter(part=part, site=site).first()
    on_hand   = (inv.quantity_available if inv else Decimal("0"))
    reserved  = (inv.quantity_reserved  if inv else Decimal("0"))
    usable    = on_hand - reserved

    line = PartIssueLine.objects.filter(
        work_order=wo, part=part, status=PartIssueLine.Status.PENDING
    ).order_by("-created_at").first()

    qty_requested = line.quantity if line else None
    qty_issued    = line.issued_qty if line else Decimal("0")
    shortage_qty  = line.shortage_qty if line else Decimal("0")
    if shortage_qty == 0 and line is not None:
        shortage_qty = Decimal("0")

    machine_crit = (wo.machine.criticality or "") if wo.machine_id else ""
    wo_priority  = (wo.issue.priority or "") if getattr(wo, "issue_id", None) else ""

    snapshot = {
        "available_qty_snapshot":       on_hand,
        "reserved_qty_snapshot":        reserved,
        "usable_qty_snapshot":          usable,
        "machine_criticality_snapshot": machine_crit,
        "part_criticality_snapshot":     "",
        "wo_priority_snapshot":          wo_priority,
    }

    ct = ContentType.objects.get_for_model(WorkOrder)
    report, created = PartShortageReport.objects.get_or_create(
        content_type=ct, object_id=wo.pk, part=part, status="pending",
        defaults={
            "work_order": wo,
            "qty_requested": qty_requested,
            "qty_issued": qty_issued,
            "shortage_qty": shortage_qty,
            "reason": note,
            "is_emergency": wo.is_emergency,
            "reported_by": technician,
            **snapshot,
        },
    )
    if not created:
        for key, val in snapshot.items():
            setattr(report, key, val)
        if qty_requested is not None:
            report.qty_requested = qty_requested
        report.qty_issued   = qty_issued
        report.shortage_qty = shortage_qty
        if note:
            report.reason = note
        report.save()

    # v4.8: backfill the explicit FK linkage if the line doesn't have one yet.
    if line is not None and line.related_shortage_report_id is None:
        line.related_shortage_report = report
        line.save(update_fields=["related_shortage_report"])

    # Local import to avoid circular dependency.
    from maintenance.notifications import notify_part_shortage
    notify_part_shortage(
        wo, part, qty_requested or shortage_qty, usable, shortage_qty, technician
    )
    snapshot_str = {k: str(v) if isinstance(v, Decimal) else v for k, v in snapshot.items()}
    log_audit(
        actor=technician, action="part_shortage_raised",
        entity="PartShortageReport", object_id=str(report.pk),
        payload={
            "wo": str(wo.pk), "part": part.sku,
            "qty_requested": str(qty_requested) if qty_requested is not None else None,
            "shortage": str(shortage_qty),
            **snapshot_str,
        },
    )
    return report


@transaction.atomic
def approve_part_request(
    *,
    line: PartIssueLine,
    manager,
    is_emergency_auto: bool = False,
) -> PartIssueLine:
    """Manager approves a PENDING request.

    Phase 2B-3 (ADR-0007 sub-decision 7): 5-stage pipeline.
    - Set approved_qty = line.quantity (or line.requested_qty if
      is_emergency_auto). This is what the manager approved.
    - Compute shortage_qty = max(0, requested_qty - approved_qty).
    - Run PartAllocationService.allocate_one to create an
      InventoryReservation (stock is reserved, NOT deducted).
    - Stock deduction and StockMovement(ISSUE_TO_WO) creation happen
      later in execute_warehouse_issue.
    - If is_emergency_auto=True, approved_qty = line.requested_qty
      (skip the manager-edited qty cap).
    """
    if line.status == PartIssueLine.Status.APPROVED:
        return line
    if line.status != PartIssueLine.Status.PENDING:
        raise ValueError("Only PENDING requests can be approved.")
    if line.quantity <= 0:
        raise ValueError("Quantity must be positive.")

    site = _get_default_site()
    if not site:
        raise ValueError("No default site configured.")

    # Phase 2B-3 (ADR-0007 sub-decision 7): 5-stage pipeline.
    # Approval ONLY sets approved_qty and runs allocation. Stock is
    # NOT deducted at approval — execute_warehouse_issue is the only
    # path that deducts stock and creates StockMovement(ISSUE_TO_WO).
    if is_emergency_auto:
        approved = line.requested_qty
    else:
        approved = line.quantity

    shortage = max(Decimal("0"), line.requested_qty - approved)

    now = timezone.now()
    line.status = PartIssueLine.Status.APPROVED
    line.approved_qty = approved
    line.shortage_qty = shortage
    line.approved_by = manager
    line.approved_at = now
    if is_emergency_auto:
        line.is_emergency_auto_approved = True
    line.save(update_fields=[
        "status", "approved_qty", "shortage_qty",
        "approved_by", "approved_at",
        "is_emergency_auto_approved", "updated_at",
    ])

    log_audit(
        actor=manager,
        action="part_request_approved",
        entity="WorkOrder",
        object_id=str(line.work_order.pk),
        payload={
            "line_id": line.pk,
            "part": line.part.sku,
            "requested_qty": str(line.requested_qty),
            "approved_qty": str(approved),
            "shortage_qty": str(shortage),
            "emergency_auto": is_emergency_auto,
        },
    )
    _maybe_notify_low_stock(line.part, site)

    # Phase 2B: fire PART_APPROVED event + run allocation
    try:
        from inventory.services_allocation import PartAllocationService
        from maintenance.models import WorkOrderBlocker
        from maintenance.services_blocker import WorkOrderBlockerEventService
        from maintenance.services_wo_status import WorkOrderService

        blocker = WorkOrderBlocker.objects.filter(
            work_order=line.work_order,
            kind=WorkOrderBlocker.Kind.PART,
            status=WorkOrderBlocker.Status.OPEN,
        ).first()
        if blocker:
            WorkOrderBlockerEventService.record(
                blocker=blocker,
                event_type="PART_APPROVED",
                actor=manager,
                payload={"line_id": line.pk, "approved_qty": str(line.approved_qty)},
            )
        # Run allocation (priority-ranked; this is where the InventoryReservation is created)
        PartAllocationService.allocate_one(line)
        # Recompute WO operational status
        WorkOrderService.recompute_operational_status(line.work_order)
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(
            f"Failed to fire PART_APPROVED event for line {line.pk}: {e}"
        )

    return line


@transaction.atomic
def reject_part_request(*, line: PartIssueLine, manager, reason: str) -> PartIssueLine:
    """Manager rejects a PENDING request. Stock is NOT touched.

    No PR is auto-created — procurement is a separate decision from
    the WO issue decision.
    """
    if line.status != PartIssueLine.Status.PENDING:
        raise ValueError("Only PENDING requests can be rejected.")
    reason = (reason or "").strip()
    if not reason:
        raise ValueError("Rejection reason is required.")
    line.status = PartIssueLine.Status.REJECTED
    line.rejection_reason = reason[:1000]
    line.approved_by = manager
    line.approved_at = timezone.now()
    line.approved_qty = Decimal("0")
    line.issued_qty = Decimal("0")
    line.save(update_fields=[
        "status", "rejection_reason", "approved_by", "approved_at",
        "approved_qty", "issued_qty", "updated_at",
    ])
    log_audit(
        actor=manager,
        action="part_request_rejected",
        entity="WorkOrder",
        object_id=str(line.work_order.pk),
        payload={"line_id": line.pk, "part": line.part.sku, "reason": reason[:200]},
    )
    return line


@transaction.atomic
def edit_part_request_qty(*, line: PartIssueLine, manager, new_quantity: Decimal) -> PartIssueLine:
    """Manager edits the qty of a PENDING request. Stays PENDING.

    Updates `quantity` (the manager's preferred value) and
    `shortage_qty = max(0, requested_qty - new_quantity)`. No stock
    movement. The auto-created PurchaseRequest (if any) is NOT auto-
    updated — the manager can edit the PR separately if the shortage
    figure needs to change.
    """
    if line.status != PartIssueLine.Status.PENDING:
        raise ValueError("Only PENDING requests can be edited.")
    new_quantity = Decimal(str(new_quantity))
    if new_quantity <= 0:
        raise ValueError("Quantity must be positive.")
    old_qty = line.quantity
    new_shortage = max(Decimal("0"), line.requested_qty - new_quantity)
    line.quantity = new_quantity
    line.shortage_qty = new_shortage
    line.save(update_fields=["quantity", "shortage_qty", "updated_at"])
    log_audit(
        actor=manager,
        action="part_request_qty_edited",
        entity="WorkOrder",
        object_id=str(line.work_order.pk),
        payload={
            "line_id": line.pk,
            "part": line.part.sku,
            "old_qty": str(old_qty),
            "new_qty": str(new_quantity),
            "shortage_qty": str(new_shortage),
        },
    )
    return line


# ---------------------------------------------------------------------------
# v4.8 — Shortage decision, reservation, and warehouse issue services
# ---------------------------------------------------------------------------
#
# OPERATION-SPECIFIC CHECKS (v4.6 — see migration 0014 docstring for context):
#
# +--------------------+--------------------------------------+----------------------------------+
# | Operation          | Check                                | Reason                           |
# +--------------------+--------------------------------------+----------------------------------+
# | reserve_stock      | quantity_available -                | Prevent over-reservation when    |
# |                    |   quantity_reserved >= qty          | multiple approvals race.         |
# | release_reservation| quantity_reserved >= qty            | Ledger integrity.                |
# | execute_warehouse_ | quantity_available >= qty           | Physical pick from on-hand. The  |
# |   issue            |                                      | shortage's reservation is        |
# |                    |                                      | released in the same tx.         |
# | mark_shortage_     | qty_issued >= approved_issue_qty    | Stock-side mechanical check.     |
# |   fulfilled        |   (no PR check)                     | Manager verifies procurement.    |
# +--------------------+--------------------------------------+----------------------------------+
#
# KNOWN LIMITATION (v4.7):
#
# Inventory.quantity_reserved is a single aggregate field. It does not track
# which shortage created which reservation. When execute_warehouse_issue runs
# `reservation_released = min(qty, inv.quantity_reserved)`, the released
# amount is drawn from the aggregate, not from a specific shortage's claim.
#
# Consequence: a warehouse issue from shortage A can consume reservation
# capacity originally created by shortage B. The aggregate accounting is
# correct, but per-shortage attribution is lost.
#
# Resolution: introduce a Reservation model when per-shortage visibility,
# ownership, or transfer is required. Phase 1's aggregate field is sufficient.
# Evaluation at Sprint 4 start: see deferred-features note in plan v4.8.


# ---------------------------------------------------------------------------
# Reservation services (v4.8)
# ---------------------------------------------------------------------------

@transaction.atomic
def reserve_stock(*, part: SparePart, qty: Decimal, source_wo: WorkOrder, actor) -> Inventory:
    """Increment Inventory.quantity_reserved by `qty` for `part`.

    This is a soft claim — it does not physically remove stock. Used for
    planning/reporting (e.g. "how much is committed but not yet issued?").
    The warehouse can still physically pick from quantity_available; the
    reservation is released as part of execute_warehouse_issue.

    v4.6 check: quantity_available - quantity_reserved >= qty
    (i.e. is there enough UNRESERVED stock to claim?)
    This prevents multiple shortage approvals from over-reserving the
    same on-hand stock.

    Raises ValueError if (quantity_available - quantity_reserved) < qty.
    """
    if qty <= 0:
        raise ValueError("Reservation qty must be positive.")

    site = _get_default_site()
    if not site:
        raise ValueError("No default site configured.")
    try:
        inv = Inventory.objects.select_for_update().get(part=part, site=site)
    except Inventory.DoesNotExist:
        inv = Inventory.objects.create(part=part, site=site,
                                       quantity_available=Decimal("0"),
                                       quantity_reserved=Decimal("0"))

    unreserved = inv.quantity_available - inv.quantity_reserved
    if unreserved < qty:
        raise ValueError(
            f"Cannot reserve {qty:g} × {part.sku}: only {unreserved:g} unreserved "
            f"({inv.quantity_available:g} on hand, {inv.quantity_reserved:g} already reserved). "
            f"Missing {qty - unreserved:g} unit(s)."
        )

    inv.quantity_reserved += qty
    inv.save(update_fields=["quantity_reserved"])
    log_audit(
        actor=actor, action="stock_reserved",
        entity="Inventory", object_id=str(inv.pk),
        payload={
            "part": part.sku, "qty": str(qty),
            "quantity_available": str(inv.quantity_available),
            "quantity_reserved_before": str(inv.quantity_reserved - qty),
            "quantity_reserved_after": str(inv.quantity_reserved),
            "source_wo": str(source_wo.number) if source_wo else "",
        },
    )
    return inv


@transaction.atomic
def release_reservation(*, part: SparePart, qty: Decimal, source_wo: WorkOrder, actor) -> Inventory:
    """Decrement Inventory.quantity_reserved by `qty` for `part`.

    Used when:
      - execute_warehouse_issue consumes the reservation
      - shortage is closed (any un-issued reservation is released)

    Raises ValueError if quantity_reserved < qty.
    """
    if qty <= 0:
        raise ValueError("Release qty must be positive.")
    site = _get_default_site()
    if not site:
        raise ValueError("No default site configured.")
    inv = Inventory.objects.select_for_update().get(part=part, site=site)
    if inv.quantity_reserved < qty:
        raise ValueError(
            f"Cannot release {qty:g} × {part.sku}: only {inv.quantity_reserved:g} reserved."
        )
    inv.quantity_reserved -= qty
    inv.save(update_fields=["quantity_reserved"])
    log_audit(
        actor=actor, action="stock_reservation_released",
        entity="Inventory", object_id=str(inv.pk),
        payload={"part": part.sku, "qty": str(qty),
                 "source_wo": str(source_wo.number) if source_wo else ""},
    )
    return inv


# ---------------------------------------------------------------------------
# Shortage decision services (v4.8)
# ---------------------------------------------------------------------------

# Valid state transitions for PartShortageReport.status
VALID_SHORTAGE_TRANSITIONS = {
    PartShortageReport.Status.PENDING_REVIEW: {
        PartShortageReport.Status.APPROVED,
        PartShortageReport.Status.REJECTED,
    },
    PartShortageReport.Status.APPROVED: {
        PartShortageReport.Status.IN_FULFILLMENT,
        PartShortageReport.Status.BLOCKED,
        PartShortageReport.Status.CLOSED,
    },
    PartShortageReport.Status.IN_FULFILLMENT: {
        PartShortageReport.Status.FULFILLED,
        PartShortageReport.Status.BLOCKED,
        PartShortageReport.Status.CLOSED,
    },
    PartShortageReport.Status.FULFILLED: {
        PartShortageReport.Status.CLOSED,
    },
    PartShortageReport.Status.BLOCKED: {
        PartShortageReport.Status.IN_FULFILLMENT,
        PartShortageReport.Status.CLOSED,
    },
    PartShortageReport.Status.CLOSED: set(),
    PartShortageReport.Status.REJECTED: set(),
}


@transaction.atomic
def transition_shortage_status(report: PartShortageReport, new_status: str, *, actor, note: str = "") -> PartShortageReport:
    """Move a shortage report through its lifecycle.

    Refuses invalid transitions. On CLOSED, releases the outstanding
    reservation and cancels any pending auto-PRs.

    v4.8 BLOCKED is strict: it does NOT release the reservation or cancel
    PRs. The reservation stays until the manager explicitly closes.
    """
    valid = VALID_SHORTAGE_TRANSITIONS.get(report.status, set())
    if new_status not in valid:
        raise ValueError(
            f"Invalid shortage status transition: {report.status} → {new_status}. "
            f"Valid transitions from {report.status}: {sorted(valid) or 'none (terminal)'}"
        )
    old = report.status
    report.status = new_status
    report.save(update_fields=["status"])
    log_audit(
        actor=actor, action="shortage_status_changed",
        entity="PartShortageReport", object_id=str(report.pk),
        payload={"from": old, "to": new_status, "note": note},
    )

    # On CLOSED: release remaining reservation and cancel pending auto-PRs.
    if new_status == PartShortageReport.Status.CLOSED:
        decision = getattr(report, "decision", None)
        if decision and decision.approved_issue_qty > 0:
            released = decision.approved_issue_qty - report.qty_issued
            if released > 0:
                try:
                    release_reservation(
                        part=report.part, qty=released,
                        source_wo=report.work_order, actor=actor,
                    )
                except ValueError:
                    # Best-effort: if the release fails, log but don't block the close.
                    pass
        from procurement.models import PurchaseRequest
        for pr in PurchaseRequest.objects.filter(
            source_shortage_report=report,
            status=PurchaseRequest.Status.PENDING,
        ):
            pr.status = PurchaseRequest.Status.CANCELLED
            pr.save(update_fields=["status"])

    return report


@transaction.atomic
def create_shortage_decision(
    *,
    report: PartShortageReport,
    decision_type: str,
    approved_issue_qty: Decimal,
    approved_procurement_qty: Decimal,
    rejected_qty: Decimal,
    decided_by,
    expected_availability_date=None,
    decision_note: str = "",
    rejection_reason: str = "",
) -> PartShortageDecision:
    """Create the first PartShortageDecision for a PENDING_REVIEW report.

    Direct side effects (v4.8 — no event bus):
      - On approve: reserve stock, auto-create PR.
      - On reject: no side effects.
    """
    if report.status != PartShortageReport.Status.PENDING_REVIEW:
        raise ValidationError(
            f"Only PENDING_REVIEW reports can receive a decision. "
            f"Current status: {report.status}."
        )

    decision = PartShortageDecision(
        report=report,
        decision_type=decision_type,
        approved_issue_qty=approved_issue_qty,
        approved_procurement_qty=approved_procurement_qty,
        rejected_qty=rejected_qty,
        expected_availability_date=expected_availability_date,
        decision_note=decision_note,
        rejection_reason=rejection_reason,
        decided_by=decided_by,
    )
    decision.full_clean()
    decision.save()

    # Update the report's status
    report.status = (
        PartShortageReport.Status.APPROVED
        if decision_type == PartShortageDecision.DecisionType.APPROVE
        else PartShortageReport.Status.REJECTED
    )
    report.reviewed_by = decided_by
    report.reviewed_at = timezone.now()
    report.rejection_reason = rejection_reason
    report.decision_note = decision_note
    if expected_availability_date:
        report.expected_availability_date = expected_availability_date
    report.save()

    # Direct side effects
    if decision_type == PartShortageDecision.DecisionType.APPROVE:
        if approved_issue_qty > 0:
            reserve_stock(
                part=report.part, qty=approved_issue_qty,
                source_wo=report.work_order, actor=decided_by,
            )
        if approved_procurement_qty > 0:
            # Local import to avoid circular dependency
            from procurement.services import auto_create_pr_for_shortage
            auto_create_pr_for_shortage(
                report=report, decision=decision, actor=decided_by,
            )

    return decision


@transaction.atomic
def edit_shortage_decision(
    *,
    report: PartShortageReport,
    approved_issue_qty: Decimal,
    approved_procurement_qty: Decimal,
    rejected_qty: Decimal,
    edited_by,
    expected_availability_date=None,
    decision_note: str = "",
) -> PartShortageDecision:
    """Edit an APPROVED decision. Refused once execution has started.

    v4.8 procurement lock: refuses to change approved_procurement_qty if
    a PurchaseRequest has already been auto-created for this report.

    Adjusts the reservation based on the issue-qty delta:
      - delta > 0: reserve more
      - delta < 0: release some
    """
    if report.is_decision_locked:
        raise ValidationError(
            f"Decision is locked: report is in {report.status}. "
            f"Close this report and create a new shortage if fulfillment needs to change."
        )

    decision = report.decision
    if decision is None:
        raise ValidationError("Report has no decision to edit.")

    # v4.8 procurement lock
    from procurement.models import PurchaseRequest
    existing_pr = PurchaseRequest.objects.filter(source_shortage_report=report).first()
    if existing_pr is not None and approved_procurement_qty != decision.approved_procurement_qty:
        raise ValidationError(
            f"Cannot edit procurement qty: PR #{existing_pr.pk} already created "
            f"for this shortage. Close this shortage and create a new one, "
            f"or manually edit PR #{existing_pr.pk}."
        )

    old_issue = decision.approved_issue_qty
    decision.approved_issue_qty = approved_issue_qty
    decision.approved_procurement_qty = approved_procurement_qty
    decision.rejected_qty = rejected_qty
    if expected_availability_date is not None:
        decision.expected_availability_date = expected_availability_date
    decision.decision_note = decision_note
    decision.last_edited_by = edited_by
    decision.full_clean()
    decision.save()

    # Adjust reservation based on issue-qty delta
    issue_delta = approved_issue_qty - old_issue
    if issue_delta > 0:
        reserve_stock(
            part=report.part, qty=issue_delta,
            source_wo=report.work_order, actor=edited_by,
        )
    elif issue_delta < 0:
        try:
            release_reservation(
                part=report.part, qty=-issue_delta,
                source_wo=report.work_order, actor=edited_by,
            )
        except ValueError:
            # If the release would go negative (the reservation was already
            # consumed by some other shortage's warehouse issue — see
            # known limitation in module docstring), don't block the edit.
            # The audit log records the partial adjustment.
            pass

    return decision


@transaction.atomic
def mark_shortage_fulfilled(*, report: PartShortageReport, actor) -> PartShortageReport:
    """Manager marks a shortage as FULFILLED after verifying both sides.

    Phase 1 conditions (v4.8):
      - report is in IN_FULFILLMENT state
      - decision exists and is approve
      - qty_issued >= approved_issue_qty (stock side mechanically verified)
      - NO check on PR.status — the manager verifies procurement outside
        the system until Sprint 4 lands the PO-receiving flow.

    Sprint 4 warning shown in the UI (not the service): when
    approved_procurement_qty > 0, the UI displays a banner telling the
    manager to verify procurement externally before marking FULFILLED.
    """
    if report.status != PartShortageReport.Status.IN_FULFILLMENT:
        raise ValidationError(
            f"Only IN_FULFILLMENT reports can be marked fulfilled. "
            f"Current status: {report.status}."
        )
    decision = getattr(report, "decision", None)
    if not decision or decision.decision_type != PartShortageDecision.DecisionType.APPROVE:
        raise ValidationError("Only approved shortages can be marked fulfilled.")

    if report.qty_issued < decision.approved_issue_qty:
        missing = decision.approved_issue_qty - report.qty_issued
        raise ValidationError(
            f"Cannot mark fulfilled: only {report.qty_issued:g} of "
            f"{decision.approved_issue_qty:g} stock units issued. "
            f"Issue the remaining {missing:g} from stock first."
        )

    note = (
        "Manager verified full fulfillment (procurement verification deferred to Sprint 4)"
        if decision.approved_procurement_qty > 0
        else "Manager verified full fulfillment"
    )
    transition_shortage_status(
        report, PartShortageReport.Status.FULFILLED, actor=actor, note=note,
    )
    return report


@transaction.atomic
def execute_warehouse_issue(*, line: PartIssueLine, qty: Decimal, actor) -> dict:
    """Warehouse executes a stock issue against a PENDING PartIssueLine.

    v4.6: validates against quantity_available (physical on-hand) only.
    v4.7 KNOWN LIMITATION: the reservation release is drawn from the
    aggregate Inventory.quantity_reserved, not from the specific shortage's
    claim. A warehouse issue from one shortage can consume reservation
    capacity originally created by another. Per-shortage fulfillment
    progress is reconstructible from PartShortageReport.qty_issued.

    On the first execution against a related shortage report, transitions
    the report from APPROVED to IN_FULFILLMENT.
    """
    if line.status != PartIssueLine.Status.PENDING:
        raise ValueError(f"Line is {line.status}, cannot issue.")
    if qty <= 0:
        raise ValueError("Issue qty must be positive.")

    site = _get_default_site()
    if not site:
        raise ValueError("No default site configured.")
    inv = Inventory.objects.select_for_update().get(part=line.part, site=site)

    # v4.6: check quantity_available (physical on-hand) only.
    if inv.quantity_available <= 0:
        raise ValueError(
            f"Out of stock for {line.part.sku}: 0 available, requested {qty:g}."
        )
    if inv.quantity_available < qty:
        raise ValueError(
            f"Cannot issue {qty:g} × {line.part.sku}: only {inv.quantity_available:g} available. "
            f"Missing {qty - inv.quantity_available:g} unit(s). Manager must decide."
        )

    # Release reservation as part of the issue (capped to what was reserved).
    # v4.7 KNOWN LIMITATION: drawn from the aggregate, not from a specific
    # shortage's claim. See module docstring.
    reservation_released = min(qty, inv.quantity_reserved)
    if reservation_released > 0:
        inv.quantity_reserved -= reservation_released
    quantity_before = inv.quantity_available
    inv.quantity_available -= qty
    inv.save()

    is_first_execution = (line.issued_qty == 0)
    line.approved_qty = (line.approved_qty or Decimal("0")) + qty
    line.issued_qty   = (line.issued_qty   or Decimal("0")) + qty
    line.status = PartIssueLine.Status.APPROVED
    line.issued_by = actor
    line.save(update_fields=["approved_qty", "issued_qty", "status", "issued_by", "updated_at"])

    # v4.8 Fix 2: use the explicit FK link, not the implicit (wo, part) lookup.
    report = line.related_shortage_report
    if report is not None and is_first_execution:
        report.qty_issued = (report.qty_issued or Decimal("0")) + qty
        report.save(update_fields=["qty_issued"])
        transition_shortage_status(
            report, PartShortageReport.Status.IN_FULFILLMENT, actor=actor,
            note=f"Warehouse issued {qty:g} × {line.part.sku}",
        )
    elif report is not None:
        report.qty_issued = (report.qty_issued or Decimal("0")) + qty
        report.save(update_fields=["qty_issued"])

    quantity_after = inv.quantity_available
    StockMovement.objects.create(
        part=line.part, site=site,
        movement_type=StockMovement.MovementType.ISSUE_TO_WO,
        quantity=qty,
        quantity_before=quantity_before,
        quantity_after=quantity_after,
        work_order=line.work_order,
        performed_by=actor,
        unit_cost=line.unit_cost,
        note=f"Warehouse issued to WO-{line.work_order.number} (released {reservation_released:g} reservation)",
        reference={"line_id": line.pk, "reservation_released": str(reservation_released)},
    )
    log_audit(
        actor=actor, action="part_warehouse_issued",
        entity="WorkOrder", object_id=line.work_order.pk,
        payload={
            "line_id": line.pk,
            "part": line.part.sku,
            "qty": str(qty),
            "reservation_released": str(reservation_released),
            "stock_before": str(quantity_before),
            "stock_after": str(quantity_after),
        },
    )
    if is_first_execution and report is not None:
        log_audit(
            actor=actor, action="part_shortage_execution_started",
            entity="PartShortageReport", object_id=str(report.pk),
            payload={"line_id": line.pk, "part": line.part.sku, "first_issue_qty": str(qty)},
        )

    # Low-stock notification (matches the existing pattern in approve_part_request)
    _maybe_notify_low_stock(line.part, site)

    # Phase 2B (keystone): resolve the PART blocker iff the tech now has the
    # full approved qty. The keystone rule from ADR-0007: the blocker resolves
    # on `issued_qty >= approved_qty`, NOT on allocation.
    try:
        from maintenance.services_blocker import WorkOrderBlockerService
        from maintenance.services_wo_status import WorkOrderService

        WorkOrderBlockerService.sync_from_external_event(
            external_obj=line,
            event_type="PART_ISSUED",
            actor=actor,
            payload={
                "line_id": line.pk,
                "issued_qty": str(line.issued_qty),
                "approved_qty": str(line.approved_qty),
            },
        )
        WorkOrderService.recompute_operational_status(line.work_order)
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(
            f"Failed to resolve PART blocker for line {line.pk}: {e}"
        )

    return {
        "actual_issued": qty,
        "stock_before": quantity_before,
        "stock_after": quantity_after,
        "reservation_released": reservation_released,
    }