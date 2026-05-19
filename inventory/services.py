from __future__ import annotations

from decimal import Decimal

from django.db import transaction

from maintenance.models import WorkOrder
from maintenance.services import log_audit

from .models import PartIssueLine, SparePart, StockMovement


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
) -> StockMovement:
    part.quantity_on_hand += quantity
    part.save(update_fields=["quantity_on_hand"])
    mov = StockMovement.objects.create(
        part=part,
        movement_type=StockMovement.MovementType.STOCK_IN,
        quantity=quantity,
        performed_by=performed_by,
        supplier_name=supplier_name,
        unit_cost=unit_cost,
        invoice_ref=invoice_ref,
        note=note,
    )
    log_audit(
        actor=performed_by,
        action="stock_in",
        entity="SparePart",
        object_id=part.pk,
        payload={"qty": str(quantity), "invoice": invoice_ref},
    )
    _maybe_notify_low_stock(part)
    return mov


def _create_procurement_for_shortage(
    *,
    part: SparePart,
    quantity: Decimal,
    work_order: WorkOrder,
    created_by,
    note_suffix: str = "",
):
    from maintenance.notifications import notify_procurement_request
    from procurement.models import PurchaseRequest

    pr = PurchaseRequest.objects.create(
        part=part,
        work_order=work_order,
        quantity=quantity,
        urgency="high" if work_order.is_emergency else "normal",
        is_emergency=work_order.is_emergency,
        notes=f"Auto: shortage for WO-{work_order.number}. {note_suffix}".strip(),
        status=PurchaseRequest.Status.PENDING,
        created_by=created_by,
    )
    notify_procurement_request(pr)
    return pr


def _deduct_and_record_issue(
    *,
    wo: WorkOrder,
    part: SparePart,
    quantity: Decimal,
    unit_cost: Decimal,
    invoice_ref: str,
    supplier_name: str,
    issued_by,
) -> None:
    part.quantity_on_hand -= quantity
    part.save(update_fields=["quantity_on_hand"])
    PartIssueLine.objects.create(
        work_order=wo,
        part=part,
        quantity=quantity,
        unit_cost=unit_cost,
        invoice_ref=invoice_ref,
        supplier_name=supplier_name,
        issued_by=issued_by,
    )
    StockMovement.objects.create(
        part=part,
        movement_type=StockMovement.MovementType.ISSUE_TO_WO,
        quantity=quantity,
        work_order=wo,
        performed_by=issued_by,
        supplier_name=supplier_name,
        unit_cost=unit_cost,
        invoice_ref=invoice_ref,
        note=f"Issued to WO-{wo.number}",
    )


def _maybe_notify_low_stock(part: SparePart) -> None:
    part.refresh_from_db()
    if part.is_low_stock():
        from maintenance.notifications import notify_low_stock

        notify_low_stock(part, sku=part.sku, qty=part.quantity_on_hand)


@transaction.atomic
def issue_part_to_work_order(
    *,
    wo: WorkOrder,
    part: SparePart,
    quantity: Decimal,
    unit_cost: Decimal,
    invoice_ref: str,
    supplier_name: str,
    issued_by,
) -> tuple[bool, str]:
    """
    UC-09 aligned: full issue, partial issue + procurement for remainder,
    or zero on-hand → procurement only (no negative stock).
    """
    if quantity <= 0:
        return False, "Quantity must be positive."
    if wo.status == WorkOrder.Status.CLOSED:
        return False, "Cannot issue parts to a closed work order."

    part.refresh_from_db()
    on_hand = part.quantity_on_hand

    if on_hand >= quantity:
        _deduct_and_record_issue(
            wo=wo,
            part=part,
            quantity=quantity,
            unit_cost=unit_cost,
            invoice_ref=invoice_ref,
            supplier_name=supplier_name,
            issued_by=issued_by,
        )
        log_audit(
            actor=issued_by,
            action="issue_part",
            entity="WorkOrder",
            object_id=wo.pk,
            payload={"part": part.sku, "qty": str(quantity), "mode": "full"},
        )
        _maybe_notify_low_stock(part)
        return True, f"Issued full quantity ({quantity})."

    if on_hand > 0:
        short = quantity - on_hand
        _deduct_and_record_issue(
            wo=wo,
            part=part,
            quantity=on_hand,
            unit_cost=unit_cost,
            invoice_ref=invoice_ref,
            supplier_name=supplier_name,
            issued_by=issued_by,
        )
        _create_procurement_for_shortage(
            part=part,
            quantity=short,
            work_order=wo,
            created_by=issued_by,
            note_suffix=f"Remainder qty {short} after partial issue.",
        )
        log_audit(
            actor=issued_by,
            action="issue_part_partial",
            entity="WorkOrder",
            object_id=wo.pk,
            payload={"part": part.sku, "issued": str(on_hand), "pr_qty": str(short)},
        )
        _maybe_notify_low_stock(part)
        return True, f"Partial issue: {on_hand} issued; procurement created for remaining {short}."

    _create_procurement_for_shortage(
        part=part,
        quantity=quantity,
        work_order=wo,
        created_by=issued_by,
        note_suffix="No stock on hand.",
    )
    log_audit(
        actor=issued_by,
        action="issue_part_procurement_only",
        entity="WorkOrder",
        object_id=wo.pk,
        payload={"part": part.sku, "qty": str(quantity)},
    )
    _maybe_notify_low_stock(part)
    return True, "No stock on hand; procurement request created for full quantity."


@transaction.atomic
def consumable_use(
    *,
    part: SparePart,
    quantity: Decimal,
    user,
    machine_id: int | None = None,
) -> tuple[bool, str]:
    if not part.is_consumable:
        return False, "Selected part is not marked as consumable."
    if quantity <= 0:
        return False, "Quantity must be positive."
    part.refresh_from_db()
    if part.quantity_on_hand < quantity:
        return False, "Cannot exceed stock."
    part.quantity_on_hand -= quantity
    part.save(update_fields=["quantity_on_hand"])
    note = ""
    if machine_id:
        note = f"machine_id={machine_id}"
    StockMovement.objects.create(
        part=part,
        movement_type=StockMovement.MovementType.CONSUMABLE_USE,
        quantity=quantity,
        performed_by=user,
        note=note,
    )
    log_audit(
        actor=user,
        action="consumable_use",
        entity="SparePart",
        object_id=part.pk,
        payload={"qty": str(quantity)},
    )
    _maybe_notify_low_stock(part)
    return True, "Logged."
