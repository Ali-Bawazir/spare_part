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
        _create_procurement_for_shortage(
            part=part, quantity=short, work_order=wo, created_by=issued_by,
            note_suffix=f"Remainder qty {short} after partial issue.",
        )
        log_audit(
            actor=issued_by, action="issue_part_partial", entity="WorkOrder",
            object_id=str(wo.pk),
            payload={"part": part.sku, "issued": str(available), "pr_qty": str(short)},
        )
        _maybe_notify_low_stock(part, site)
        return True, f"Partial issue: {available} issued; procurement created for remaining {short}."

    _create_procurement_for_shortage(
        part=part, quantity=quantity, work_order=wo, created_by=issued_by,
        note_suffix="No stock on hand.",
    )
    log_audit(
        actor=issued_by, action="issue_part_procurement_only", entity="WorkOrder",
        object_id=str(wo.pk), payload={"part": part.sku, "qty": str(quantity)},
    )
    _maybe_notify_low_stock(part, site)
    return True, "No stock on hand; procurement request created for full quantity."


@transaction.atomic
def consumable_use(
    *, part: SparePart, quantity: Decimal, user, machine_id: int | None = None, site=None
) -> tuple[bool, str]:
    if not part.is_consumable:
        return False, "Selected part is not marked as consumable."
    if quantity <= 0:
        return False, "Quantity must be positive."
    site = site or _get_default_site()
    if not site:
        return False, "No default site configured."

    recent = StockMovement.objects.filter(
        part=part, movement_type=StockMovement.MovementType.CONSUMABLE_USE,
        performed_by=user, created_at__gte=timezone.now() - timezone.timedelta(seconds=5)
    ).exists()
    if recent:
        return False, "Duplicate consumable log detected. Please wait."

    inv = Inventory.objects.select_for_update().get(part=part, site=site)
    quantity_before = inv.quantity_available
    if inv.quantity_available < quantity:
        return False, "Cannot exceed stock."
    inv.quantity_available -= quantity
    inv.save()
    quantity_after = inv.quantity_available

    note = ""
    if machine_id:
        note = f"machine_id={machine_id}"
    StockMovement.objects.create(
        part=part, site=site,
        movement_type=StockMovement.MovementType.CONSUMABLE_USE,
        quantity=quantity,
        quantity_before=quantity_before,
        quantity_after=quantity_after,
        performed_by=user, note=note,
    )
    log_audit(
        actor=user, action="consumable_use", entity="SparePart",
        object_id=str(part.pk), payload={"qty": str(quantity)},
    )
    _maybe_notify_low_stock(part, site)
    return True, "Logged."