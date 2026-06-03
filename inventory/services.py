from __future__ import annotations

from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from maintenance.models import WorkOrder
from maintenance.services import log_audit

from .models import Inventory, PartIssueLine, SparePart, StockMovement


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

    inv = Inventory.objects.select_for_update().get(part=part, site=site)
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
        urgency="high" if work_order.is_emergency else "normal",
        is_emergency=work_order.is_emergency,
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

    PartIssueLine.objects.create(
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
    if wo.status == WorkOrder.Status.CLOSED:
        return False, "Cannot issue parts to a closed work order."

    existing = PartIssueLine.objects.filter(work_order=wo, part=part).exists()
    if existing:
        return False, "Parts already issued for this work order and part combination."

    inv = Inventory.objects.select_for_update().get(part=part, site=site)
    available = inv.quantity_available - inv.quantity_reserved
    quantity_before = inv.quantity_available

    ref = {"work_order_id": str(wo.number), "invoice": invoice_ref}

    if available >= quantity:
        _deduct_and_record_issue(
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
        return True, f"Issued full quantity ({quantity})."

    elif available > 0:
        short = quantity - available
        _deduct_and_record_issue(
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

    _maybe_notify_low_stock(part, site)
    return True, f"Logged {quantity} x {part.name}."


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
) -> PartIssueLine:
    """Technician adds a PENDING part request to their own assigned WO.

    P3.1 design (locked P1.5 grill):
    - Always creates a PENDING line. No stock deduction.
    - Computes available stock for the part/site.
    - Computes shortage = max(0, requested - available).
    - If shortage > 0, auto-creates a PurchaseRequest for the shortage
      (idempotent — existing PENDING PR for same WO+part is reused).
    - Manager decides approved_qty at review time.
    - Emergency exception: when wo.is_emergency=True, the request is
      auto-approved and stock is deducted immediately, flagged with
      is_emergency_auto_approved=True for manager post-review.
    """
    if quantity <= 0:
        raise ValueError("Quantity must be positive.")
    if wo.status == WorkOrder.Status.CLOSED:
        raise ValueError("Cannot request parts on a closed work order.")

    # Idempotency: if a PENDING request already exists for this
    # WO+part, return it instead of creating a duplicate.
    existing = PartIssueLine.objects.filter(
        work_order=wo, part=part, status=PartIssueLine.Status.PENDING
    ).first()
    if existing is not None:
        return existing

    site = _get_default_site()
    inv = Inventory.objects.filter(part=part, site=site).first()
    available = (
        (inv.quantity_available - inv.quantity_reserved)
        if inv is not None else Decimal("0")
    )
    shortage = max(Decimal("0"), Decimal(str(quantity)) - available)

    line = PartIssueLine.objects.create(
        work_order=wo,
        part=part,
        quantity=quantity,
        requested_qty=quantity,
        unit_cost=Decimal("0"),
        invoice_ref="",
        supplier_name="",
        status=PartIssueLine.Status.PENDING,
        requested_by=technician,
        issued_by=technician,
        shortage_qty=shortage,
    )

    if shortage > 0:
        _create_procurement_for_shortage(
            part=part,
            quantity=shortage,
            work_order=wo,
            created_by=technician,
            note_suffix=(
                f"requested {quantity}, available {available}, "
                f"shortage {shortage}. Manager will review."
            ),
        )

    log_audit(
        actor=technician,
        action="part_request_created",
        entity="WorkOrder",
        object_id=str(wo.pk),
        payload={
            "part": part.sku,
            "qty": str(quantity),
            "available": str(available),
            "shortage": str(shortage),
            "wo_emergency": wo.is_emergency,
        },
    )

    # Emergency exception: auto-approve the request immediately.
    if wo.is_emergency:
        try:
            line = approve_part_request(
                line=line,
                manager=technician,
                is_emergency_auto=True,
            )
        except ValueError as e:
            log_audit(
                actor=technician,
                action="part_request_emergency_partial",
                entity="WorkOrder",
                object_id=str(wo.pk),
                payload={"part": part.sku, "qty": str(quantity), "error": str(e)},
            )
    return line


@transaction.atomic
def approve_part_request(
    *,
    line: PartIssueLine,
    manager,
    is_emergency_auto: bool = False,
) -> PartIssueLine:
    """Manager approves a PENDING request.

    P3.1 design:
    - Set approved_qty = line.quantity (manager's "approve" means
      issue what the tech asked for).
    - Compute issued_qty = min(approved_qty, available_at_approval_time).
      If stock has been consumed since the request, issued_qty < approved_qty.
    - Compute shortage_qty = max(0, requested_qty - approved_qty).
    - Deduct issued_qty from inventory. If issued_qty < approved_qty, the
      difference is a 'stock ran out' event — no auto-PR is created here
      because one already exists from the request step.
    - Auto-created PR (if any) is left alone — manager edits it manually.
    - If is_emergency_auto=True, deduct the full requested_qty (skip
      approved_qty cap) to handle the case where the line was auto-approved
      by the emergency path.
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

    inv = Inventory.objects.select_for_update().get(part=line.part, site=site)
    available = inv.quantity_available - inv.quantity_reserved

    if is_emergency_auto:
        # Emergency auto-approve: try to deduct the full requested qty.
        # If stock is insufficient, issue what's available and flag the
        # shortage for manager post-review.
        approved = line.requested_qty
        issued = min(approved, available)
    else:
        approved = line.quantity
        issued = min(approved, available)

    shortage = max(Decimal("0"), line.requested_qty - approved)

    if available <= 0 and not is_emergency_auto:
        # No stock and not an emergency — refuse. Manager should
        # reject the request and the auto-PR will handle procurement.
        raise ValueError(
            f"No stock available for {line.part.sku}. "
            f"Reject the request — the auto-created PurchaseRequest will cover procurement."
        )

    quantity_before = inv.quantity_available
    inv.quantity_available -= issued
    inv.save()
    quantity_after = inv.quantity_available

    now = timezone.now()
    line.status = PartIssueLine.Status.APPROVED
    line.approved_qty = approved
    line.issued_qty = issued
    line.shortage_qty = shortage
    line.approved_by = manager
    line.approved_at = now
    line.issued_by = manager
    if is_emergency_auto:
        line.is_emergency_auto_approved = True
    line.save(update_fields=[
        "status", "approved_qty", "issued_qty", "shortage_qty",
        "approved_by", "approved_at",
        "issued_by", "is_emergency_auto_approved", "updated_at",
    ])

    ref = {
        "work_order_id": str(line.work_order.number),
        "approved_by": manager.username,
        "emergency_auto": is_emergency_auto,
        "approved_qty": str(approved),
        "issued_qty": str(issued),
        "requested_qty": str(line.requested_qty),
    }
    if issued > 0:
        StockMovement.objects.create(
            part=line.part,
            site=site,
            movement_type=StockMovement.MovementType.ISSUE_TO_WO,
            quantity=issued,
            quantity_before=quantity_before,
            quantity_after=quantity_after,
            work_order=line.work_order,
            performed_by=manager,
            unit_cost=line.unit_cost,
            note=f"Issued to WO-{line.work_order.number}",
            reference=ref,
        )
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
            "issued_qty": str(issued),
            "shortage_qty": str(shortage),
            "emergency_auto": is_emergency_auto,
        },
    )
    _maybe_notify_low_stock(line.part, site)
    return line


@transaction.atomic
def reject_part_request(*, line: PartIssueLine, manager, reason: str) -> PartIssueLine:
    """Manager rejects a PENDING request. Stock is NOT touched.

    The auto-created PurchaseRequest (if any) stays — procurement is a
    separate decision from the WO issue decision.
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