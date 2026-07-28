from __future__ import annotations

from decimal import Decimal

from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.utils import timezone
from django.utils.translation import gettext as _

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
    supplier=None,
    supplier_name: str = "",
    unit_cost: Decimal,
    invoice_ref: str,
    note: str = "",
    site=None,
) -> StockMovement:
    """Record a stock-in movement.

    `supplier` (Supplier FK) is the canonical reference for new rows. The
    `supplier_name` snapshot is auto-populated from `supplier.name` when
    `supplier` is set, so historical rows always carry the name as it was
    at the time of receipt — even if the Supplier is later renamed or
    deleted.

    If `supplier` is None, the caller can still pass `supplier_name` (e.g.
    for back-compat callers); the FK stays NULL but the snapshot is kept.
    """
    site = site or _get_default_site()
    if not site:
        raise ValueError(_("No default site configured. Please create a Site first."))

    if supplier is not None and not supplier_name:
        supplier_name = supplier.name

    recent = StockMovement.objects.filter(
        part=part, movement_type=StockMovement.MovementType.STOCK_IN,
        performed_by=performed_by, invoice_ref=invoice_ref,
        created_at__gte=timezone.now() - timezone.timedelta(seconds=10)
    ).exists()
    if recent:
        raise ValueError(_("Duplicate stock-in detected. Please wait before submitting again."))

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

    # Stamp the part's default supplier once (first touch wins) so the
    # spare_part_detail page can show a meaningful "Default supplier".
    if supplier is not None and part.supplier_id is None:
        part.supplier = supplier
        part.save(update_fields=["supplier"])

    ref = {
        "invoice_number": invoice_ref,
        "supplier": supplier_name,
        "supplier_id": supplier.pk if supplier is not None else None,
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
        supplier=supplier,
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
        payload={"qty": str(quantity), "invoice": invoice_ref, "supplier_id": supplier.pk if supplier else None},
    )
    _maybe_notify_low_stock(part, site)
    return movement


def _deduct_and_record_issue(
    *, wo, part, quantity, unit_cost, invoice_ref, supplier_name, issued_by, ref, site=None
):
    site = site or _get_default_site()
    inv = Inventory.objects.select_for_update().get(part=part, site=site)
    quantity_before = inv.quantity_available
    inv.quantity_available -= quantity
    inv.save()
    quantity_after = inv.quantity_available

    # Phase 7.4: unit_cost fallback. If the caller passed 0 (e.g. the
    # manager didn't enter a cost), fall back to the part's last
    # purchase cost or weighted average so the cost ledger captures
    # something instead of 0.
    effective_unit_cost = unit_cost
    if effective_unit_cost is None or effective_unit_cost <= 0:
        effective_unit_cost = (
            part.last_purchase_cost or part.avg_cost or Decimal("0")
        )

    pil = PartIssueLine.objects.create(
        work_order=wo, part=part, quantity=quantity,
        requested_qty=quantity, approved_qty=quantity, issued_qty=quantity,
        shortage_qty=0,
        status=PartIssueLine.Status.APPROVED,
        unit_cost=effective_unit_cost, invoice_ref=invoice_ref,
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
        note=_("Issued to WO-%(wo)s") % {"wo": wo.number},
    )
    return pil


def _maybe_notify_low_stock(part: SparePart, site=None) -> None:
    part.refresh_from_db()
    if part.is_low_stock(site=site):
        from maintenance.notifications import notify_low_stock
        inv = part.inventory_items.filter(site=site).first() if site else None
        # Phase 7.8: use live aggregate instead of deprecated quantity_reserved.
        qty = inv.quantity_available - inv.compute_quantity_reserved() if inv else 0
        notify_low_stock(part, sku=part.sku, qty=qty)


@transaction.atomic
def issue_part_to_work_order(
    *, wo, part, quantity, unit_cost, invoice_ref, supplier_name, issued_by, site=None
) -> tuple[bool, str]:
    site = site or _get_default_site()
    if not site:
        return False, _("No default site configured.")
    if quantity <= 0:
        return False, _("Quantity must be positive.")
    if wo.lifecycle_status == WorkOrder.LifecycleStatus.CLOSED:
        return False, _("Cannot issue parts to a closed work order.")

    existing = PartIssueLine.objects.filter(work_order=wo, part=part).exists()
    if existing:
        return False, _("Parts already issued for this work order and part combination.")

    inv = Inventory.objects.select_for_update().get(part=part, site=site)
    # Phase 7.8: live aggregate (sum of ACTIVE reservations) instead of
    # the deprecated quantity_reserved field.
    available = inv.quantity_available - inv.compute_quantity_reserved()
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
        # Phase 2 Cost Ledger: post the material cost for this issue.
        # Phase 2 STRICT: do NOT swallow exceptions. If post_material raises,
        # the outer @transaction.atomic rolls back the entire issue path
        # including inventory deduction. Stock stays, cost not posted.
        # Silent ledger loss is unacceptable (was the bug).
        from maintenance.cost_ledger import CostLedgerService
        CostLedgerService.post_material(
            part_issue_line=pil,
            actor=issued_by,
            memo=_("Part issued: %(name)s x %(qty)s") % {
            "name": part.name,
            "qty": pil.issued_qty,
            },
        )
        # Phase 7.4: fire PART_ISSUED so the WO Blocker resolves
        # (keystone rule: issued == approved → PART blocker resolved).
        # The manager direct-issue path used to skip this event, leaving
        # the blocker open even though the part was fully issued.
        # No-op if no PART blocker exists for this WO+part (e.g. the
        # manager direct-issued a part the tech never requested).
        from maintenance.models import WorkOrderBlocker
        has_part_blocker = WorkOrderBlocker.objects.filter(
            work_order=wo, kind=WorkOrderBlocker.Kind.PART,
            status=WorkOrderBlocker.Status.OPEN,
        ).exists()
        if has_part_blocker:
            try:
                from maintenance.services_blocker import WorkOrderBlockerService
                from maintenance.services_wo_status import WorkOrderService
                WorkOrderBlockerService.sync_from_external_event(
                    external_obj=pil, event_type="PART_ISSUED",
                    actor=issued_by,
                    payload={"line_id": pil.pk, "mode": "manager_direct"},
                )
                WorkOrderService.recompute_operational_status(wo)
            except Exception as e:
                import logging
                logging.getLogger(__name__).warning(
                    f"Failed to fire PART_ISSUED for line {pil.pk}: {e}"
                )
        return True, _("Issued full quantity (%(qty)s).") % {"qty": quantity}

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
        # Phase 2 Cost Ledger: post the material cost for this partial issue.
        # Phase 2 STRICT: do NOT swallow exceptions (see full-issue branch above).
        from maintenance.cost_ledger import CostLedgerService
        CostLedgerService.post_material(
            part_issue_line=pil,
            actor=issued_by,
            memo=_("Part issued (partial): %(name)s x %(qty)s") % {
            "name": part.name,
            "qty": pil.issued_qty,
            },
        )
        # Phase 7.4: fire PART_ISSUED so the WO Blocker resolves
        # (partial issue means approved_qty > issued_qty → blocker
        # stays open; the event handler decides per keystone rule).
        # No-op if no PART blocker exists for this WO+part.
        from maintenance.models import WorkOrderBlocker
        has_part_blocker = WorkOrderBlocker.objects.filter(
            work_order=wo, kind=WorkOrderBlocker.Kind.PART,
            status=WorkOrderBlocker.Status.OPEN,
        ).exists()
        if has_part_blocker:
            try:
                from maintenance.services_blocker import WorkOrderBlockerService
                from maintenance.services_wo_status import WorkOrderService
                WorkOrderBlockerService.sync_from_external_event(
                    external_obj=pil, event_type="PART_ISSUED",
                    actor=issued_by,
                    payload={"line_id": pil.pk, "mode": "manager_direct_partial"},
                )
                WorkOrderService.recompute_operational_status(wo)
            except Exception as e:
                import logging
                logging.getLogger(__name__).warning(
                    f"Failed to fire PART_ISSUED for line {pil.pk}: {e}"
                )
        return True, (
            _("Partial issue: %(available)s issued; %(short)s short. "
            "Manager should open a PurchaseRequest manually.") % {
                "available": available,
                "short": short,
            }
        )

    # Zero stock on hand — refuse to deduct (nothing was issued).
    # The manager must open a PurchaseRequest manually to start the
    # procurement flow. Returning False prevents the caller (typically a
    # success toast) from misleading the user into thinking stock was
    # deducted when it was not.
    log_audit(
        actor=issued_by, action="issue_part_procurement_only", entity="WorkOrder",
        object_id=str(wo.pk), payload={"part": part.sku, "qty": str(quantity)},
    )
    _maybe_notify_low_stock(part, site)
    return False, (
        _("Out of stock for %(sku)s: 0 available, requested %(qty).1f. "
        "Manager must open a PurchaseRequest to procure this part.") % {
            "sku": part.sku,
            "qty": quantity,
        }
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
        return False, _("Selected part is not marked as consumable.")
    if not part.allow_operator_consumption:
        return False, _("This item is not available for operator self-service.")
    if quantity <= 0:
        return False, _("Quantity must be positive.")

    site = site or _get_default_site()
    if not site:
        return False, _("No default site configured.")

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
        return False, _("Duplicate consumable log detected. Please wait.")

    # Lock inventory row
    inv = Inventory.objects.select_for_update().get(part=part, site=site)
    quantity_before = inv.quantity_available
    if inv.quantity_available < quantity:
        return False, _("Cannot exceed stock.")
    inv.quantity_available -= quantity
    inv.save()
    quantity_after = inv.quantity_available

    # Build note string for StockMovement (includes machine if provided)
    movement_note = note
    if machine_id:
        movement_note = f"machine_id={machine_id}" + (f"; {note}" if note else "")

    # Bug #3-style fix: compute effective_unit_cost with fallback to
    # last_purchase_cost / avg_cost so we don't post a zero-cost ledger
    # entry that violates the CheckConstraint.
    effective_uc = part.last_purchase_cost or part.avg_cost or Decimal("0")

    # Create StockMovement first
    stock_movement = StockMovement.objects.create(
        part=part,
        site=site,
        movement_type=StockMovement.MovementType.CONSUMABLE_USE,
        quantity=quantity,
        quantity_before=quantity_before,
        quantity_after=quantity_after,
        performed_by=consumed_by,
        unit_cost=effective_uc,
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

    # Bug fix: link the StockMovement to the active WO for this machine so
    # the consumable cost rolls up into the correct WO via post_consumable.
    # Best effort: pick the in-progress or in_review WO on this machine.
    active_wo = None
    if machine is not None:
        from maintenance.models import WorkOrder
        active_wo = (
            WorkOrder.objects.filter(
                machine=machine,
                lifecycle_status__in=[
                    WorkOrder.LifecycleStatus.IN_PROGRESS,
                    WorkOrder.LifecycleStatus.ASSIGNED,
                    WorkOrder.LifecycleStatus.PENDING_REVIEW,
                ],
            )
            .order_by("-id")
            .first()
        )
        if active_wo is not None:
            stock_movement.work_order = active_wo
            stock_movement.save(update_fields=["work_order"])

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

    # Phase 1+2 Cost Ledger: post the consumable cost. Surface failures
    # instead of silently swallowing so the operator sees a message.
    try:
        from maintenance.cost_ledger import CostLedgerService
        CostLedgerService.post_consumable(
            stock_movement=stock_movement,
            actor=consumed_by,
            memo=_("Consumable: %(name)s x %(qty)s") % {
                "name": part.name,
                "qty": quantity,
            },
        )
    except Exception as e:
        import logging
        logging.getLogger(__name__).exception(
            f"Cost ledger post_consumable failed for movement {stock_movement.pk}: {e}"
        )
        # Re-raise as a soft error: stock already deducted, assignment
        # already created — but the cost ledger entry was skipped.
        # Operator gets a messages.warning via the view's return path.
        raise

    _maybe_notify_low_stock(part, site)
    return True, _("Logged %(qty)s x %(name)s.") % {"qty": quantity, "name": part.name}


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
        return False, _("Selected part is not marked as consumable.")
    if quantity <= 0:
        return False, _("Quantity must be positive.")

    site = site or _get_default_site()
    if not site:
        return False, _("No default site configured.")

    from django.utils import timezone
    from inventory.models import ConsumableAssignment

    # Lock inventory row
    inv = Inventory.objects.select_for_update().get(part=part, site=site)
    quantity_before = inv.quantity_available
    if inv.quantity_available < quantity:
        return False, _("Cannot exceed stock.")
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

    # Phase 2b: post the consumable cost to the ledger. Without this,
    # supervisor-issued consumables never reach the WO cost card. The
    # post_consumable service attaches the CostTransaction to the active
    # WO on the same machine (matching consumable_use() behavior), falling
    # back to UNASSIGNED if no WO matches. Phase 2 STRICT: do NOT swallow
    # exceptions — failures roll back the outer @transaction.atomic and
    # the stock deduction is reversed.
    from maintenance.cost_ledger import CostLedgerService
    CostLedgerService.post_consumable(
        stock_movement=stock_movement,
        actor=issued_by,
        memo=_("Supervisor issued: %(name)s x %(qty)s to %(user)s") % {
            "name": part.name,
            "qty": quantity,
            "user": consumed_by.username,
        },
    )

    _maybe_notify_low_stock(part, site)
    return True, _("Issued %(qty)s x %(name)s to %(user)s.") % {
        "qty": quantity,
        "name": part.name,
        "user": consumed_by.username,
    }


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
    activity_label: str = "",
) -> dict:
    """Technician adds a PENDING part request to their own assigned WO.

    Phase 5 NOTE: kept the dict-shaped return so existing callers and
    tests continue to work. The active_line check returns the existing
    line (legacy idempotency). Future Phase 5.5 work will migrate this
    to a RequestPartResult and surface DUPLICATE_FOUND to the UI.

    Three outcomes, all PENDING (manager approval gate is preserved for
    every flow — no auto-issue even when stock is fully available):

      A. usable >= qty        -> PENDING, no shortage, full qty in requested_qty
      B. 0 < usable < qty     -> PENDING, shortage_qty = qty - usable
      C. usable == 0         -> PENDING, shortage_qty = qty

    Inventory is NEVER deducted here UNLESS the WO is emergency (see
    Phase 3 emergency path below). The deduction happens inside
    approve_part_request() after the manager approves. The technician
    sees the stock badge in the UI to know what they're requesting,
    but the system's authoritative action happens at manager-approval
    time.
    """
    from .results import ACTIVE_REQUEST_STATUSES

    if quantity <= 0:
        raise ValueError(_("Quantity must be positive."))
    if wo.lifecycle_status == WorkOrder.LifecycleStatus.CLOSED:
        raise ValueError(_("Cannot request parts on a closed work order."))

    # Phase 5 concurrency: row-lock the WO for the existing-line
    # check so two simultaneous POSTs can't both create a line.
    WorkOrder.objects.select_for_update().get(pk=wo.pk)

    # Idempotency: one ACTIVE line per (WO, part). ACTIVE = PENDING|APPROVED|ALLOCATED.
    existing = PartIssueLine.objects.filter(
        work_order=wo, part=part,
        status__in=ACTIVE_REQUEST_STATUSES,
    ).select_related("requested_by").order_by("-created_at").first()
    if existing is not None:
        site = _get_default_site()
        inv = Inventory.objects.filter(part=part, site=site).first()
        on_hand   = (inv.quantity_available if inv else Decimal("0"))
        reserved  = (inv.compute_quantity_reserved() if inv else Decimal("0"))
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
    # Phase 7.8: use live aggregate instead of deprecated quantity_reserved.
    reserved  = (inv.compute_quantity_reserved() if inv else Decimal("0"))
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
        is_emergency_auto_approved=False,  # Set to True below if emergency path
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

    # Phase 3 BUG-8 fix: emergency WO auto-approve flow.
    # Production is stopped → no manager pre-approval gate → issue immediately
    # (if stock is available) and flag for manager post-review. Manager
    # will use the dedicated review panel (work_order_emergency_review) to
    # mark Approve / Exception / Investigate. NO path in the codebase reverts
    # the inventory deduction or the cost posting — this preserves the audit
    # principle that emergency actions are visible and traceable.
    if wo.is_emergency:
        if usable >= quantity:
            # Stock available — auto-issue immediately.
            effective_unit_cost = part.last_purchase_cost or part.avg_cost or Decimal("0")
            if effective_unit_cost <= 0:
                effective_unit_cost = Decimal("10.00")
            line.status = PartIssueLine.Status.APPROVED
            line.approved_qty = quantity
            line.issued_qty = quantity
            line.is_emergency_auto_approved = True
            line.approved_by = technician
            line.approved_at = timezone.now()
            line.unit_cost = effective_unit_cost
            line.manager_note = (
                line.manager_note +
                f"\n[EMERGENCY AUTO-APPROVED at {timezone.now().isoformat(timespec='seconds')}. "
                f"Awaiting manager post-review.]"
            )
            line.save(update_fields=[
                "status", "approved_qty", "issued_qty",
                "is_emergency_auto_approved", "approved_by", "approved_at",
                "unit_cost", "manager_note", "updated_at",
            ])

            # Deduct stock + create StockMovement in the same atomic block.
            ref = {"work_order_id": str(wo.number), "emergency": True}
            _deduct_and_record_issue(
                wo=wo, part=part, quantity=quantity,
                unit_cost=effective_unit_cost,
                invoice_ref="EMERGENCY_AUTO",
                supplier_name="EMERGENCY_AUTO",
                issued_by=technician,
                ref=ref,
                site=site,
            )

            # Phase 2 STRICT: post the material cost. Failure rolls back
            # the entire emergency issue path including stock deduction.
            from maintenance.cost_ledger import CostLedgerService
            CostLedgerService.post_material(
                part_issue_line=line,
                actor=technician,
                memo=_("EMERGENCY auto-issued: %(name)s x %(qty)s to WO-%(wo)s") % {
                    "name": part.name,
                    "qty": quantity,
                    "wo": wo.number,
                },
            )

            # Notify the manager that an emergency review is pending.
            try:
                from maintenance.notifications import (
                    notify_manager_emergency_part_issued,
                )
                notify_manager_emergency_part_issued(
                    work_order=wo, line=line,
                )
            except Exception:
                pass  # Best-effort notification; failure must not break the issue

            log_audit(
                actor=technician, action="emergency_auto_issued",
                entity="WorkOrder", object_id=str(wo.pk),
                payload={
                    "part": part.sku,
                    "qty": str(quantity),
                    "unit_cost": str(effective_unit_cost),
                    "is_emergency": True,
                },
            )

            # Mark the line ISSUED if the unit cost was effective.
            line.status = PartIssueLine.Status.ISSUED
            line.save(update_fields=["status", "updated_at"])
            return {
                "line": line,
                "shortage_qty": Decimal("0"),
                "shortage": False,
                "already_pending": False,
                "emergency_auto_approved": True,
                "issued_qty": quantity,
                "available_qty_snapshot": on_hand,
                "reserved_qty_snapshot": reserved,
                "usable_qty_snapshot": usable,
                "shortage_report": None,
            }
        # else: emergency + shortage → falls through to normal
        # PENDING + shortage-report flow. The manager still needs to
        # procure, but the emergency auto-approve is recorded via
        # line.is_emergency_auto_approved=True so the post-review panel
        # surfaces it.
        else:
            line.is_emergency_auto_approved = True
            line.manager_note = (
                line.manager_note +
                "\n[EMERGENCY AUTO-APPROVED but stock insufficient. "
                "Manager must procure before issue.]"
            )
            line.save(update_fields=[
                "is_emergency_auto_approved", "manager_note", "updated_at",
            ])

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
    # Phase 7.8: use live aggregate instead of deprecated quantity_reserved.
    reserved  = (inv.compute_quantity_reserved() if inv else Decimal("0"))
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
        raise ValueError(_("Only PENDING requests can be approved."))
    if line.quantity <= 0:
        raise ValueError(_("Quantity must be positive."))

    # Phase 5 concurrency: row-lock the line so two managers approving
    # the same PENDING line concurrently cannot both succeed.
    PartIssueLine.objects.select_for_update().get(pk=line.pk)

    site = _get_default_site()
    if not site:
        raise ValueError(_("No default site configured."))

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

    # Phase 7.6: refresh the WO cost cache so committed_material_cost
    # reflects the newly-approved line (matches execute_warehouse_issue's
    # pattern at line ~1641).
    try:
        from maintenance.cost_ledger import CostLedgerService
        CostLedgerService._refresh_wo_cache(line.work_order_id)
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(
            f"Failed to refresh WO cost cache after approval for line {line.pk}: {e}"
        )

    return line


@transaction.atomic
def reject_part_request(*, line: PartIssueLine, manager, reason: str) -> PartIssueLine:
    """Manager rejects a PENDING request. Stock is NOT touched.

    No PR is auto-created — procurement is a separate decision from
    the WO issue decision.

    Bug fix: previously this function only validated the status but
    forgot to mutate the line, so the rejection silently no-op'd and
    the line stayed PENDING in the database. A follow-on fix ensures the
    PART WO Blocker opened at request time is cancelled and the WO's
    operational_status is recomputed (was previously stuck on
    `pending_parts` even after the request was rejected).
    """
    if line.status != PartIssueLine.Status.PENDING:
        raise ValueError(_("Only PENDING requests can be rejected."))
    reason_clean = (reason or "").strip()
    if not reason_clean:
        raise ValueError(_("Rejection reason is required."))
    line.status = PartIssueLine.Status.REJECTED
    line.rejection_reason = reason_clean[:1000]
    line.approved_by = manager
    line.save(update_fields=["status", "rejection_reason", "approved_by", "updated_at"])

    # Fire PART_REJECTED so any open PART WO Blocker (opened by
    # request_part_on_wo at request time) is cancelled. Mirrors the
    # pattern in cancel_approved_part_request (line 1024-1029).
    try:
        from maintenance.services_blocker import WorkOrderBlockerService
        from maintenance.services_wo_status import WorkOrderService

        WorkOrderBlockerService.sync_from_external_event(
            external_obj=line,
            event_type="PART_REJECTED",
            actor=manager,
            payload={"line_id": line.pk, "reason": reason},
        )
        WorkOrderService.recompute_operational_status(line.work_order)
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(
            f"Failed to fire PART_REJECTED for rejected line {line.pk}: {e}"
        )

    return line


@transaction.atomic
def cancel_approved_part_request(
    *, line: PartIssueLine, manager, reason: str,
) -> PartIssueLine:
    """Manager cancels an APPROVED or ALLOCATED request that was never
    warehouse-issued. Releases the inventory reservation (if any) and
    fires PART_REJECTED so the PART blocker is cancelled.

    Distinct from reject_part_request() which only works on PENDING.
    Use this for lines that the manager already approved+allocated
    but no longer wants to issue (e.g. wrong part ordered, WO scope
    changed). Rejection reason is required (min 15 chars) for the
    audit trail.
    """
    if line.status not in (
        PartIssueLine.Status.APPROVED,
        PartIssueLine.Status.ALLOCATED,
    ):
        raise ValueError(
            _("Only APPROVED or ALLOCATED requests can be cancelled "
            "(this line is %(status)s).") % {"status": line.status}
        )
    if line.issued_qty and line.issued_qty > 0:
        raise ValueError(
            _("Cannot cancel a line that has already been issued from stock.")
        )
    reason = (reason or "").strip()
    if len(reason) < 15:
        raise ValueError(_("Cancellation reason must be at least 15 characters."))

    # Release the inventory reservation if any.
    # Phase 1: cancel BOTH line-linked reservations AND any legacy reservations
    # (source_line=None) on the same (part, work_order). Legacy reservations
    # can exist on lines that were approved via the pre-Phase-1 shortage-decision
    # path which created source_line=None reservations.
    # Iterate + save so the post_save signal fires for each row and recomputes
    # Inventory.quantity_reserved. (A bulk .update() would bypass the signal and
    # leave the cache stale.)
    from inventory.models import InventoryReservation
    now = timezone.now()
    cancel_reason = _("Line cancelled: %(reason)s") % {"reason": reason[:200]}
    legacy_reason = _("Line cancelled — releasing legacy reservation: %(reason)s") % {"reason": reason[:200]}
    # Phase 1: split into two passes (line-linked + legacy-with-null-source) to
    # avoid PostgreSQL's FOR UPDATE cannot be applied to the nullable side of
    # an outer join error when Q(source_line__isnull=True) is combined with
    # SELECT FOR UPDATE against a join.
    ids_to_release = set(
        InventoryReservation.objects.filter(
            part=line.part,
            work_order=line.work_order,
            status=InventoryReservation.Status.ACTIVE,
            source_line=line,
        ).values_list("id", flat=True)
    ) | set(
        InventoryReservation.objects.filter(
            part=line.part,
            work_order=line.work_order,
            status=InventoryReservation.Status.ACTIVE,
            source_line__isnull=True,
        ).values_list("id", flat=True)
    )
    released_count = 0
    for res in (
        InventoryReservation.objects
        .select_for_update()
        .filter(pk__in=ids_to_release)
    ):
        res.status = InventoryReservation.Status.CANCELLED
        res.released_at = now
        res.release_reason = (
            legacy_reason if res.source_line_id is None else cancel_reason
        )
        res.save(update_fields=["status", "released_at", "release_reason"])
        released_count += 1
    if released_count:
        import logging
        logging.getLogger(__name__).info(
            f"Released {released_count} reservation(s) on line cancellation "
            f"(line #{line.pk}, part {line.part.sku}, WO #{line.work_order.number})"
        )

    line.status = PartIssueLine.Status.REJECTED
    line.rejection_reason = reason[:1000]
    line.approved_qty = Decimal("0")
    line.issued_qty = Decimal("0")
    line.save(update_fields=[
        "status", "rejection_reason", "approved_qty", "issued_qty",
        "updated_at",
    ])

    log_audit(
        actor=manager,
        action="part_request_cancelled",
        entity="WorkOrder",
        object_id=str(line.work_order.pk),
        payload={
            "line_id": line.pk,
            "part": line.part.sku,
            "reason": reason[:200],
            "previous_status": "approved_or_allocated",
        },
    )

    # Fire PART_REJECTED so the WO Blocker is cancelled
    try:
        from maintenance.services_blocker import WorkOrderBlockerService
        WorkOrderBlockerService.sync_from_external_event(
            external_obj=line, event_type="PART_REJECTED",
            actor=manager,
            payload={"line_id": line.pk, "reason": reason},
        )
        from maintenance.services_wo_status import WorkOrderService
        WorkOrderService.recompute_operational_status(line.work_order)
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(
            f"Failed to fire PART_REJECTED for cancelled line {line.pk}: {e}"
        )

    # Phase 7.6: refresh WO cost cache so committed_material_cost
    # drops the just-cancelled line from the committed total.
    try:
        from maintenance.cost_ledger import CostLedgerService
        CostLedgerService._refresh_wo_cache(line.work_order_id)
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(
            f"Failed to refresh WO cost cache after cancel for line {line.pk}: {e}"
        )

    return line


@transaction.atomic
def emergency_review_part_line(
    *, line: PartIssueLine, manager, decision: str, note: str = ""
) -> PartIssueLine:
    """Phase 3: manager post-review on an emergency auto-issued line.

    Three outcomes (decision ∈ {approved, exception, investigate}) — none
    of them revert the inventory deduction or the cost posting. The user's
    emergency design is one-way: issue → audit → review.

    Decision semantics:
      - "approve":     acknowledgment. No further action. Sets the flag.
      - "exception":   something went wrong (wrong spec, wrong part) but
                       accepted as an operational loss. Notifies super_admin.
      - "investigate": escalation. Notifies super_admin + audit log.

    Raises ValueError if the line wasn't auto-approved (i.e. is_emergency_auto_approved
    is False) — review is only valid for emergency-issued lines.
    """
    VALID = {"approved", "exception", "investigate"}
    if decision not in VALID:
        raise ValueError(
            _("Invalid decision %(decision)s. Must be one of %(valid)s.") % {
                "decision": decision, "valid": sorted(VALID),
            }
        )
    if not line.is_emergency_auto_approved:
        raise ValueError(
            _("Only lines with is_emergency_auto_approved=True can be reviewed here.")
        )
    if (note or "").strip():
        # Phase 3: ≥10 char note required for non-approve decisions so
        # the manager explains their call. Matches the rejection reason
        # requirement elsewhere.
        if decision != "approved" and len((note or "").strip()) < 10:
            raise ValueError(
                _("Decision note must be at least 10 characters for non-approve reviews.")
            )

    now = timezone.now()
    line.emergency_review_status = decision
    line.emergency_reviewed_by = manager
    line.emergency_reviewed_at = now
    line.emergency_review_note = (note or "").strip()[:1000]
    line.save(update_fields=[
        "emergency_review_status", "emergency_reviewed_by",
        "emergency_reviewed_at", "emergency_review_note", "updated_at",
    ])

    # Audit log entry for traceability.
    log_audit(
        actor=manager,
        action=f"emergency_review_{decision}",
        entity="PartIssueLine",
        object_id=str(line.pk),
        payload={
            "line_id": line.pk,
            "wo_id": line.work_order_id,
            "decision": decision,
            "note": (note or "")[:200],
        },
    )

    # Fire the corresponding WorkOrderBlockerEvent for traceability.
    try:
        from maintenance.models import WorkOrderBlocker, WorkOrderBlockerEvent
        blocker = WorkOrderBlocker.objects.filter(
            work_order=line.work_order,
            kind=WorkOrderBlocker.Kind.PART,
            status__in=[WorkOrderBlocker.Status.OPEN, WorkOrderBlocker.Status.RESOLVED],
        ).first()
        if blocker is not None:
            event_type = {
                "approved":     "EMERGENCY_REVIEW_APPROVED",
                "exception":    "EMERGENCY_REVIEW_EXCEPTION",
                "investigate": "EMERGENCY_REVIEW_INVESTIGATE",
            }[decision]
            WorkOrderBlockerEvent.objects.create(
                blocker=blocker,
                event_type=event_type,
                actor=manager,
                payload={"line_id": line.pk, "decision": decision, "note": (note or "")[:200]},
            )
    except Exception as _e:
        import logging
        logging.getLogger(__name__).warning(
            f"Failed to fire {event_type} for line {line.pk}: {_e}"
        )

    # For "exception" or "investigate", notify super_admin (best-effort,
    # failure here MUST NOT raise — the review has already been recorded).
    if decision in ("exception", "investigate"):
        try:
            from maintenance.notifications import notify_super_admin_emergency_review
            notify_super_admin_emergency_review(
                work_order=line.work_order,
                line=line,
                decision=decision,
                note=line.emergency_review_note,
            )
        except Exception:
            pass

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
        raise ValueError(_("Only PENDING requests can be edited."))
    new_quantity = Decimal(str(new_quantity))
    if new_quantity <= 0:
        raise ValueError(_("Quantity must be positive."))
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
def reserve_stock(*, part: SparePart, qty: Decimal, source_wo: WorkOrder, actor, source_line=None) -> Inventory:
    """Low-level inventory primitive — reserves exactly `qty` units or raises.

    Increment Inventory.quantity_reserved by `qty` for `part`.

    This is a soft claim — it does not physically remove stock. Used for
    planning/reporting (e.g. "how much is committed but not yet issued?").
    The warehouse can still physically pick from quantity_available; the
    reservation is released as part of execute_warehouse_issue.

    v4.6 check: quantity_available - quantity_reserved >= qty
    (i.e. is there enough UNRESERVED stock to claim?)
    This prevents multiple shortage approvals from over-reserving the
    same on-hand stock.

    Phase 7.8: also creates a corresponding InventoryReservation row so
    the live aggregate (used by `compute_quantity_reserved()`) stays in
    sync. The DB column is kept as a derived cache.

    Phase 1: `source_line` (optional PartIssueLine FK) links the reservation
    to the originating part line. When provided, the reservation can be
    tracked and released through the line's lifecycle. Pre-Phase-1 callers
    that omit `source_line` create "legacy" reservations with source_line=None;
    the reconcile_legacy_reservations management command cleans those up.

    Raises ValueError if (quantity_available - quantity_reserved) < qty.

    Application services should not call this directly. Use
    PartAllocationService.allocate_one() instead — that chokepoint owns
    the gap / free / delta math and is idempotent for the same line.
    Direct calls from application services will fail loudly by design
    when the same line already owns a reservation.
    """
    if qty <= 0:
        raise ValueError(_("Reservation qty must be positive."))

    site = _get_default_site()
    if not site:
        raise ValueError(_("No default site configured."))
    try:
        inv = Inventory.objects.select_for_update().get(part=part, site=site)
    except Inventory.DoesNotExist:
        inv = Inventory.objects.create(part=part, site=site,
                                       quantity_available=Decimal("0"))

    # Use the LIVE aggregate (sum of ACTIVE reservations) for the
    # over-reservation check, not the deprecated field.
    unreserved = inv.quantity_available - inv.compute_quantity_reserved()
    if unreserved < qty:
        raise ValueError(
            _("Cannot reserve %(qty).1f × %(sku)s: only %(unreserved).1f unreserved "
            "(%(available).1f on hand). "
            "Missing %(missing).1f unit(s).") % {
                "qty": qty,
                "sku": part.sku,
                "unreserved": unreserved,
                "available": inv.quantity_available,
                "missing": qty - unreserved,
            }
        )

    # Create the InventoryReservation row FIRST so the post_save signal
    # recomputes quantity_reserved. Then we don't need to manually
    # increment the field at all.
    from inventory.models import InventoryReservation
    release_reason = (
        _("reserve_stock() for PartIssueLine #%(line_id)s") % {"line_id": source_line.pk}
        if source_line is not None
        else _("legacy reserve_stock() call (no source_line)")
    )
    InventoryReservation.objects.create(
        part=part,
        work_order=source_wo,
        quantity=qty,
        status=InventoryReservation.Status.ACTIVE,
        source_line=source_line,
        release_reason=release_reason,
    )
    # Refresh inv from DB to pick up any signal-driven changes
    inv.refresh_from_db()
    log_audit(
        actor=actor, action="stock_reserved",
        entity="Inventory", object_id=str(inv.pk),
        payload={
            "part": part.sku, "qty": str(qty),
            "quantity_available": str(inv.quantity_available),
            "quantity_reserved": str(inv.compute_quantity_reserved()),
            "source_wo": str(source_wo.number) if source_wo else "",
            "source_line_id": source_line.pk if source_line else None,
        },
    )
    return inv


@transaction.atomic
def release_reservation(*, part: SparePart, qty: Decimal, source_wo: WorkOrder, actor) -> Inventory:
    """Decrement Inventory.quantity_reserved by `qty` for `part`.

    Used when:
      - shortage is closed (any un-issued reservation is released)
      - the legacy shortage-decision flow adjusts a reservation

    Phase 7.8: marks InventoryReservation rows as RELEASED so the live
    aggregate stays in sync. Falls back to cancelling legacy
    reservations (no source_line) first.

    Raises ValueError if (live) quantity_reserved < qty.
    """
    if qty <= 0:
        raise ValueError(_("Release qty must be positive."))
    site = _get_default_site()
    if not site:
        raise ValueError(_("No default site configured."))
    inv = Inventory.objects.select_for_update().get(part=part, site=site)

    # Cancel legacy (no source_line) ACTIVE reservations first.
    from inventory.models import InventoryReservation
    remaining = qty
    legacy_qs = (
        InventoryReservation.objects
        .select_for_update()
        .filter(
            part=part,
            work_order=source_wo,
            source_line__isnull=True,
            status=InventoryReservation.Status.ACTIVE,
        )
        .order_by("created_at", "pk")
    )
    from django.utils import timezone as _tz
    now = _tz.now()
    for res in legacy_qs:
        if remaining <= 0:
            break
        take = min(res.quantity, remaining)
        if take >= res.quantity:
            res.status = InventoryReservation.Status.RELEASED
            res.released_at = now
            res.release_reason = _("legacy release_reservation() call by %(actor)s") % {"actor": actor}
            res.save(update_fields=["status", "released_at", "release_reason"])
        else:
            res.quantity -= take
            res.save(update_fields=["quantity"])
            InventoryReservation.objects.create(
                part=res.part, work_order=res.work_order, quantity=take,
                status=InventoryReservation.Status.RELEASED,
                source_line=None, released_at=now,
                release_reason=_("Partial release on legacy release_reservation() by %(actor)s") % {"actor": actor},
                priority_at_creation=res.priority_at_creation,
            )
        remaining -= take

    if remaining > 0:
        # Not enough legacy reservations to release. The DB field would
        # go negative if we decremented it; surface a clear error.
        live_reserved = inv.compute_quantity_reserved()
        raise ValueError(
            _("Cannot release %(qty).1f × %(sku)s: only %(released).1f released "
            "from legacy reservations; %(remaining).1f requested but no matching "
            "reservation rows. Live reserved = %(live_reserved).1f.") % {
                "qty": qty,
                "sku": part.sku,
                "released": qty - remaining,
                "remaining": remaining,
                "live_reserved": live_reserved,
            }
        )
    inv.refresh_from_db()
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
            _("Invalid shortage status transition: %(old)s → %(new)s. "
            "Valid transitions from %(old2)s: %(valid)s") % {
                "old": report.status,
                "new": new_status,
                "old2": report.status,
                "valid": sorted(valid) or "none (terminal)",
            }
        )
    old = report.status
    report.status = new_status
    report.save(update_fields=["status"])
    log_audit(
        actor=actor, action="shortage_status_changed",
        entity="PartShortageReport", object_id=str(report.pk),
        payload={"from": old, "to": new_status, "note": note},
    )

    # On CLOSED: release ALL reservations on this (part, work_order) pair AND
    # cancel pending auto-PRs.
    # Phase 1: release both line-linked reservations (source_line__related_shortage_report=report)
    # AND legacy ones (source_line=None, no PartIssueLine FK). Previously only
    # line-linked reservations were released (and only when decision.approved_issue_qty > 0),
    # leaving legacy reservations orphaned and free stock polluted.
    if new_status == PartShortageReport.Status.CLOSED:
        from inventory.models import InventoryReservation
        from django.utils import timezone as _tz
        legacy_reason = _(
            "Shortage report closed (was %(old)s); releasing legacy reservation"
        ) % {"old": old}
        line_reason = _(
            "Shortage report closed (was %(old)s); releasing line-linked reservation"
        ) % {"old": old}
        now = _tz.now()
        # Two passes: line-linked (inner join — safe to lock) and legacy
        # (no join — also safe). Splitting avoids PostgreSQL's
        # FOR UPDATE cannot be applied to nullable side of an outer join error
        # when Q(source_line__isnull=True) is combined with SELECT FOR UPDATE.
        ids_to_release = set(
            InventoryReservation.objects.filter(
                part=report.part,
                work_order=report.work_order,
                status=InventoryReservation.Status.ACTIVE,
                source_line__related_shortage_report=report,
            ).values_list("id", flat=True)
        ) | set(
            InventoryReservation.objects.filter(
                part=report.part,
                work_order=report.work_order,
                status=InventoryReservation.Status.ACTIVE,
                source_line__isnull=True,
            ).values_list("id", flat=True)
        )
        released_count = 0
        for res in (
            InventoryReservation.objects
            .select_for_update()
            .filter(pk__in=ids_to_release)
        ):
            res.status = InventoryReservation.Status.CANCELLED
            res.released_at = now
            res.release_reason = (
                legacy_reason if res.source_line_id is None else line_reason
            )
            res.save(update_fields=["status", "released_at", "release_reason"])
            released_count += 1
        if released_count:
            import logging
            logging.getLogger(__name__).info(
                f"Released {released_count} reservation(s) on shortage CLOSED "
                f"(report #{report.pk}, part {report.part.sku}, WO #{report.work_order.number})"
            )
        from procurement.models import PurchaseRequest
        for pr in PurchaseRequest.objects.filter(
            source_shortage_report=report,
            status=PurchaseRequest.Status.PENDING,
        ):
            pr.status = PurchaseRequest.Status.CANCELLED
            pr.save(update_fields=["status"])

    # Phase UC-06: when the PSR reaches a terminal state, close the SHORTAGE
    # WO Blocker keyed to this report. Without this, the WO page shows a
    # stale "Awaiting Procurement" badge and WorkOrderService keeps the
    # WO on operational_status=pending_parts even after manager-verified
    # closure (clone of the PART_REJECTED cleanup pattern in
    # create_shortage_decision's REJECT branch).
    if new_status in {
        PartShortageReport.Status.FULFILLED,
        PartShortageReport.Status.CLOSED,
        PartShortageReport.Status.REJECTED,
    }:
        try:
            from maintenance.models import WorkOrderBlocker
            from maintenance.services_blocker import WorkOrderBlockerService
            from django.contrib.contenttypes.models import ContentType
            blocker_ct = ContentType.objects.get_for_model(PartShortageReport)
            blocker = (
                WorkOrderBlocker.objects
                .select_for_update()
                .filter(
                    work_order=report.work_order,
                    kind=WorkOrderBlocker.Kind.SHORTAGE,
                    content_type=blocker_ct,
                    object_id=report.pk,
                    status=WorkOrderBlocker.Status.OPEN,
                )
                .first()
            )
            if blocker is not None:
                if new_status == PartShortageReport.Status.REJECTED:
                    WorkOrderBlockerService.cancel_blocker(
                        blocker=blocker,
                        cancel_reason=_(
                            "PartShortageReport #%(pk)s rejected"
                        ) % {"pk": report.pk},
                        cancelled_by=actor,
                    )
                else:
                    WorkOrderBlockerService.resolve_blocker(
                        blocker=blocker,
                        resolution_note=_(
                            "PartShortageReport #%(pk)s reached %(status)s"
                        ) % {"pk": report.pk, "status": new_status},
                        resolved_by=actor,
                    )
        except Exception as _e:
            import logging
            logging.getLogger(__name__).warning(
                f"Failed to close SHORTAGE blocker on PSR #{report.pk} "
                f"terminal transition to {new_status}: {_e}"
            )

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
            _("Only PENDING_REVIEW reports can receive a decision. "
            "Current status: %(status)s.") % {"status": report.status}
        )

    # Phase UC-06: refuse to silently overwrite an existing decision row.
    # The OneToOne would either raise IntegrityError or, in test prep
    # sessions, get silently deleted and re-created — leaving the audit
    # log with two part_shortage_decided/decision_edited entries that
    # reference different PSD pks. Force the caller to the Edit Decision
    # path if a decision is already recorded.
    if getattr(report, "decision", None) is not None:
        raise ValidationError(
            _("Decision #%(pk)s already exists for this report. "
            "Use the Edit Decision view to change the qty split.") % {
                "pk": report.decision.pk,
            }
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

    # Phase 3 BUG-9 fix: REJECT branch must clean up the PART blocker that
    # request_part_on_wo() opened when this shortage was raised. Without this,
    # the WO stays stuck on operational_status=PENDING_PARTS indefinitely
    # because the open blocker keeps the keystone rule satisfied.
    # We fire PART_REJECTED on the related PartIssueLine (if any) so the
    # blocker subsystem cancels it. If no line exists (shortage raised via
    # shortcut path without a line), the blocker itself is queried directly.
    if decision_type == PartShortageDecision.DecisionType.REJECT:
        try:
            from maintenance.models import WorkOrderBlocker
            from maintenance.services_blocker import WorkOrderBlockerService
            from maintenance.services_wo_status import WorkOrderService

            related_line = None
            try:
                related_line = report.issue_lines.first()
            except Exception:
                related_line = None

            if related_line is not None:
                # Fire PART_REJECTED on the line so the blocker subsystem
                # cancels any open PART blocker keyed to it.
                WorkOrderBlockerService.sync_from_external_event(
                    external_obj=related_line, event_type="PART_REJECTED",
                    actor=decided_by,
                    payload={
                        "line_id": related_line.pk,
                        "reason": rejection_reason or "shortage rejected",
                    },
                )
            else:
                # Fallback: directly cancel any open PART blocker on this WO
                # that was opened against this shortage report.
                blocker = WorkOrderBlocker.objects.filter(
                    work_order=report.work_order,
                    kind=WorkOrderBlocker.Kind.PART,
                    status=WorkOrderBlocker.Status.OPEN,
                    object_id=report.pk,
                ).first()
                if blocker is not None:
                    WorkOrderBlockerService.cancel_blocker(
                        blocker=blocker, actor=decided_by,
                        note=f"Shortage #{report.pk} rejected",
                    )

            # Recompute the WO operational status — PENDING_PARTS should
            # downgrade now that the blocker is gone.
            WorkOrderService.recompute_operational_status(report.work_order)
        except Exception as _e:
            import logging
            logging.getLogger(__name__).warning(
                f"Failed to clean up blockers on shortage REJECT "
                f"(report #{report.pk}): {_e}"
            )

    # Direct side effects
    if decision_type == PartShortageDecision.DecisionType.APPROVE:
        # Phase 7.7: transition the related PartIssueLine to APPROVED so the
        # auto-fulfill loop (auto_fulfill_wo_lines_from_po) can pick it up at
        # PO receive. Idempotent: only acts on PENDING lines and never
        # downgrades. Skipped silently if no line is linked to this report
        # (edge case: PR/PO manually created without going through the
        # request_part_on_wo path).
        related_line = None
        try:
            related_line = report.issue_lines.first()
        except Exception:
            related_line = None

        # Phase reservation chokepoint: route reservation creation through
        # PartAllocationService.allocate_one — the single business primitive
        # that owns the gap / free / delta math. reserve_stock() is a strict
        # low-level primitive and is no longer called from this path.
        # Persist related_line.approved_qty first so allocate_one sees the
        # correct gap (allocate_one reads approved_qty, allocated_qty).
        if related_line is not None and approved_issue_qty > 0:
            related_line.approved_qty = approved_issue_qty
            related_line.save(update_fields=["approved_qty", "updated_at"])
            from inventory.services_allocation import PartAllocationService
            PartAllocationService.allocate_one(related_line)

        if approved_procurement_qty > 0:
            # Local import to avoid circular dependency
            from procurement.services import auto_create_pr_for_shortage
            auto_create_pr_for_shortage(
                report=report, decision=decision, actor=decided_by,
            )

        if related_line is not None and related_line.status == PartIssueLine.Status.PENDING:
            related_line.status = PartIssueLine.Status.APPROVED
            related_line.approved_qty = approved_issue_qty
            related_line.shortage_qty = approved_procurement_qty
            related_line.approved_by = decided_by
            related_line.approved_at = timezone.now()
            related_line.save(update_fields=[
                "status", "approved_qty", "shortage_qty",
                "approved_by", "approved_at", "updated_at",
            ])

            # Fire PART_APPROVED event on the open PART blocker
            try:
                from maintenance.models import WorkOrderBlocker
                from maintenance.services_blocker import WorkOrderBlockerEventService
                blocker = WorkOrderBlocker.objects.filter(
                    work_order=related_line.work_order,
                    kind=WorkOrderBlocker.Kind.PART,
                    status=WorkOrderBlocker.Status.OPEN,
                ).first()
                if blocker:
                    WorkOrderBlockerEventService.record(
                        blocker=blocker,
                        event_type="PART_APPROVED",
                        actor=decided_by,
                        payload={
                            "line_id": related_line.pk,
                            "approved_qty": str(related_line.approved_qty),
                        },
                    )
            except Exception as e:
                import logging
                logging.getLogger(__name__).warning(
                    f"Failed to fire PART_APPROVED event for shortage decision line {related_line.pk}: {e}"
                )

            # Recompute WO operational status
            try:
                from maintenance.services_wo_status import WorkOrderService
                WorkOrderService.recompute_operational_status(related_line.work_order)
            except Exception as e:
                import logging
                logging.getLogger(__name__).warning(
                    f"Failed to recompute WO operational status for shortage decision line {related_line.pk}: {e}"
                )

            # Refresh WO cost cache so committed_material_cost reflects
            # the newly-approved line (matches approve_part_request pattern)
            try:
                from maintenance.cost_ledger import CostLedgerService
                CostLedgerService._refresh_wo_cache(related_line.work_order_id)
            except Exception as e:
                import logging
                logging.getLogger(__name__).warning(
                    f"Failed to refresh WO cost cache for shortage decision line {related_line.pk}: {e}"
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
            _("Decision is locked: report is in %(status)s. "
            "Close this report and create a new shortage if fulfillment needs to change.") % {
                "status": report.status,
            }
        )

    decision = report.decision
    if decision is None:
        raise ValidationError(_("Report has no decision to edit."))

    # v4.8 procurement lock — Phase UC-06: instead of refusing the
    # edit (which left PR rows stuck at their initial quantity and made
    # the Decision display disagree with the PR row), sync the PR's
    # quantity to the new approved_procurement_qty. The PO line item
    # gets reconciled later via purchase_order_receive's existing
    # reallocation path. Caller (the manager edit view) can still
    # overrule by setting approved_procurement_qty to its current
    # decision value (no-op).
    from procurement.models import PurchaseRequest
    existing_pr = PurchaseRequest.objects.filter(source_shortage_report=report).first()
    if existing_pr is not None and approved_procurement_qty != decision.approved_procurement_qty:
        old_pr_qty = existing_pr.quantity
        existing_pr.quantity = approved_procurement_qty
        existing_pr.save(update_fields=["quantity"])
        log_audit(
            actor=edited_by, action="purchase_request_qty_synced",
            entity="PurchaseRequest", object_id=str(existing_pr.pk),
            payload={
                "from": str(old_pr_qty), "to": str(approved_procurement_qty),
                "source_shortage_report": str(report.pk),
            },
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
        # Phase reservation chokepoint: route through allocate_one.
        # Persist related_line.approved_qty first so allocate_one sees the
        # gap. release_reservation below stays untouched (negative path).
        related_line = None
        try:
            related_line = report.issue_lines.first()
        except Exception:
            related_line = None
        if related_line is not None:
            related_line.approved_qty = approved_issue_qty
            related_line.save(update_fields=["approved_qty", "updated_at"])
            from inventory.services_allocation import PartAllocationService
            PartAllocationService.allocate_one(related_line)
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

    # Always persist related_line.approved_qty to reflect the latest
    # decision — even on the negative-delta path where release_reservation
    # alone doesn't update the line field.
    if issue_delta != 0:
        try:
            related_line = report.issue_lines.first()
        except Exception:
            related_line = None
        if related_line is not None and related_line.approved_qty != approved_issue_qty:
            related_line.approved_qty = approved_issue_qty
            related_line.save(update_fields=["approved_qty", "updated_at"])

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
            _("Only IN_FULFILLMENT reports can be marked fulfilled. "
            "Current status: %(status)s.") % {"status": report.status}
        )
    decision = getattr(report, "decision", None)
    if not decision or decision.decision_type != PartShortageDecision.DecisionType.APPROVE:
        raise ValidationError(_("Only approved shortages can be marked fulfilled."))

    if report.qty_issued < decision.approved_issue_qty:
        missing = decision.approved_issue_qty - report.qty_issued
        raise ValidationError(
            _("Cannot mark fulfilled: only %(issued).1f of "
            "%(approved).1f stock units issued. "
            "Issue the remaining %(missing).1f from stock first.") % {
                "issued": report.qty_issued,
                "approved": decision.approved_issue_qty,
                "missing": missing,
            }
        )

    note = (
        _("Manager verified full fulfillment (procurement verification deferred to Sprint 4)")
        if decision.approved_procurement_qty > 0
        else _("Manager verified full fulfillment")
    )
    transition_shortage_status(
        report, PartShortageReport.Status.FULFILLED, actor=actor, note=note,
    )
    return report


@transaction.atomic
def execute_warehouse_issue(
    *,
    line: PartIssueLine,
    qty: Decimal,
    actor,
    skip_approval_check: bool = False,
) -> dict:
    """Warehouse executes a stock issue against a PENDING, APPROVED, or
    ALLOCATED PartIssueLine.

    v4.6: validates against quantity_available (physical on-hand) only.

    Phase 7.8: reservation release is now drawn from the specific
    InventoryReservation rows attached to this line (FIFO by created_at).
    The post_save signal on InventoryReservation automatically recomputes
    Inventory.quantity_reserved from the ACTIVE rows, so the cache stays
    in sync. Replaces the v4.7 approach of decrementing the aggregate
    directly, which could mis-attribute reservation capacity across lines.
    Partial release: if qty < reserved, the original ACTIVE row is shrunk
    and a sibling RELEASED row is created for the consumed portion.

    On the first execution against a related shortage report, transitions
    the report from APPROVED to IN_FULFILLMENT.

    Phase 7.5: accept APPROVED and ALLOCATED too. After the simple
    request_part_on_wo + approve_part_request + allocate flow, the
    line is ALLOCATED (not PENDING) and the warehouse still needs to
    physically issue.

    `skip_approval_check` is a narrowly-scoped override used ONLY by the
    `repair_part_lines` management command to finalize lines where the
    manager split the decision (0 issue + N procurement) and stock later
    arrived without an auto-issue. Do NOT use it from any other code
    path.
    """

    def _effective_unit_cost(pil) -> Decimal:
        """
        Bug #3 fix: prefer the line's stored unit_cost, but fall back to the
        SparePart's last purchase cost or running average. This mirrors the
        fallback that already exists in `_deduct_and_record_issue` (lines
        155-163). Without it, warehouse issues on lines with unit_cost=0
        produce a CostTransaction with amount=0, which violates the
        CheckConstraint and gets silently swallowed.
        """
        if pil.unit_cost and pil.unit_cost > 0:
            return pil.unit_cost
        part = pil.part
        return part.last_purchase_cost or part.avg_cost or Decimal("0")

    if line.status not in (
        PartIssueLine.Status.PENDING,
        PartIssueLine.Status.APPROVED,
        PartIssueLine.Status.ALLOCATED,
    ):
        raise ValueError(_("Line is %(status)s, cannot issue.") % {"status": line.status})
    # Historical repair only.
    # The ONLY validation skipped here is approved_qty > 0 (i.e. the
    # "manager must approve before warehouse issue" workflow check).
    # Every other precondition, side effect, and event still runs:
    #   - status must still be PENDING / APPROVED / ALLOCATED
    #   - inventory.quantity_available must still be > 0
    #   - inventory deduction still happens
    #   - StockMovement(ISSUE_TO_WO) is still written
    #   - PartIssueLine.issued_qty / issued_by are still set
    #   - PART_ISSUED sync event still fires (blocker service reacts)
    # Do NOT extend this flag to skip physical checks or events — the
    # repair command only exists to finalize lines that the procurement
    # flow forgot to issue when stock arrived.
    if not skip_approval_check:
        if not line.approved_qty or line.approved_qty <= 0:
            raise ValueError(
                _("Line has no approved_qty (status=%(status)s). "
                "Manager must approve before warehouse issue.") % {"status": line.status}
            )
    if qty <= 0:
        raise ValueError(_("Issue qty must be positive."))

    site = _get_default_site()
    if not site:
        raise ValueError(_("No default site configured."))
    inv = Inventory.objects.select_for_update().get(part=line.part, site=site)

    # v4.6: check quantity_available (physical on-hand) only.
    if inv.quantity_available <= 0:
        raise ValueError(
            _("Out of stock for %(sku)s: 0 available, requested %(qty).1f.") % {
                "sku": line.part.sku,
                "qty": qty,
            }
        )
    if inv.quantity_available < qty:
        raise ValueError(
            _("Cannot issue %(qty).1f × %(sku)s: only %(available).1f available. "
            "Missing %(missing).1f unit(s). Manager must decide.") % {
                "qty": qty,
                "sku": line.part.sku,
                "available": inv.quantity_available,
                "missing": qty - inv.quantity_available,
            }
        )

    # Release reservation as part of the issue.
    # Phase 7.8: release from specific InventoryReservation rows attached
    # to this line (FIFO by created_at). The post_save signal on
    # InventoryReservation automatically recomputes Inventory.quantity_reserved
    # from the ACTIVE rows, so the cache stays in sync. Replaces the v4.7
    # "KNOWN LIMITATION" approach of decrementing the aggregate directly.
    from inventory.models import InventoryReservation
    reservation_released = Decimal("0")
    remaining = qty
    # First: reservations explicitly tied to this line.
    active_reservations = (
        InventoryReservation.objects
        .select_for_update()
        .filter(
            part=line.part,
            source_line=line,
            status=InventoryReservation.Status.ACTIVE,
        )
        .order_by("created_at", "pk")
    )
    for res in active_reservations:
        if remaining <= 0:
            break
        take = min(res.quantity, remaining)
        if take >= res.quantity:
            res.status = InventoryReservation.Status.RELEASED
            res.released_at = timezone.now()
            res.release_reason = (
                _("Warehouse issue %(qty).1f × %(sku)s to WO-%(wo)s") % {
                    "qty": qty,
                    "sku": line.part.sku,
                    "wo": line.work_order.number,
                }
            )
            res.save(update_fields=["status", "released_at", "release_reason"])
        else:
            # Partial release: shrink the ACTIVE row, create a sibling
            # RELEASED row for the consumed portion.
            res.quantity -= take
            res.save(update_fields=["quantity"])
            InventoryReservation.objects.create(
                part=res.part,
                work_order=res.work_order,
                quantity=take,
                status=InventoryReservation.Status.RELEASED,
                source_line=res.source_line,
                released_at=timezone.now(),
                release_reason=(
                    _("Partial release on warehouse issue %(qty).1f × "
                    "%(sku)s to WO-%(wo)s") % {
                        "qty": qty,
                        "sku": line.part.sku,
                        "wo": line.work_order.number,
                    }
                ),
                priority_at_creation=res.priority_at_creation,
            )
        reservation_released += take
        remaining -= take

    # Phase 7.8 fallback: if no line-linked reservations exist, also release
    # legacy reservations (created via `reserve_stock()` with no source_line)
    # for this (part, work_order). This preserves the v4.8 behavior where
    # `create_shortage_decision` → `reserve_stock` creates synthetic
    # reservations that warehouse issue then releases.
    if remaining > 0:
        legacy_qs = (
            InventoryReservation.objects
            .select_for_update()
            .filter(
                part=line.part,
                work_order=line.work_order,
                source_line__isnull=True,
                status=InventoryReservation.Status.ACTIVE,
            )
            .order_by("created_at", "pk")
        )
        for res in legacy_qs:
            if remaining <= 0:
                break
            take = min(res.quantity, remaining)
            if take >= res.quantity:
                res.status = InventoryReservation.Status.RELEASED
                res.released_at = timezone.now()
                res.release_reason = (
                    _("Warehouse issue %(qty).1f × %(sku)s to WO-%(wo)s "
                    "(legacy reserve)") % {
                        "qty": qty,
                        "sku": line.part.sku,
                        "wo": line.work_order.number,
                    }
                )
                res.save(update_fields=["status", "released_at", "release_reason"])
            else:
                res.quantity -= take
                res.save(update_fields=["quantity"])
                InventoryReservation.objects.create(
                    part=res.part,
                    work_order=res.work_order,
                    quantity=take,
                    status=InventoryReservation.Status.RELEASED,
                    source_line=None,
                    released_at=timezone.now(),
                    release_reason=(
                        _("Partial release on warehouse issue (legacy reserve) "
                        "%(qty).1f × %(sku)s to WO-%(wo)s") % {
                            "qty": qty,
                            "sku": line.part.sku,
                            "wo": line.work_order.number,
                        }
                    ),
                    priority_at_creation=res.priority_at_creation,
                )
            reservation_released += take
            remaining -= take

    # Audit each released reservation row (Phase 7.8). The signal
    # already keeps the aggregate in sync.
    if reservation_released > 0:
        log_audit(
            actor=actor,
            action="part_reservation_released",
            entity="WorkOrder",
            object_id=str(line.work_order.pk),
            payload={
                "line_id": line.pk,
                "part": line.part.sku,
                "qty": str(qty),
                "reservation_released": str(reservation_released),
                "release_kind": "full" if reservation_released == line.approved_qty else "partial",
            },
        )
    # The post_save signal on InventoryReservation may have just updated
    # the reservation rows. Refresh the in-memory copy so we don't clobber
    # the signal's recomputation with our stale value.
    inv.refresh_from_db()
    quantity_before = inv.quantity_available
    inv.quantity_available -= qty
    inv.save()

    is_first_execution = (line.issued_qty == 0)
    # Bug #2 fix: warehouse issue is a physical event. It MUST NOT change
    # approved_qty — that is a manager business decision set at approval time.
    line.issued_qty = (line.issued_qty or Decimal("0")) + qty
    # Bug fix (was PartIssueLine.Status.APPROVED): a line that has been
    # physically issued by the warehouse is in the ISSUED state. Leaving
    # it as APPROVED broke the blocker service's ISSUED-state rule and
    # caused the corresponding PART blocker to stay open forever.
    line.status = PartIssueLine.Status.ISSUED
    line.issued_by = actor
    # Bug #3 fix: if the line has unit_cost=0 (legacy data, manager didn't
    # enter a cost, etc.), backfill it from the part's last purchase cost
    # or weighted average BEFORE posting to the cost ledger. The ledger
    # service multiplies pil.unit_cost × pil.issued_qty, so a 0 unit_cost
    # would otherwise produce a CT with amount=0 and violate the
    # CheckConstraint(amount != 0).
    effective_uc = _effective_unit_cost(line)
    if (line.unit_cost or Decimal("0")) <= 0 and effective_uc > 0:
        line.unit_cost = effective_uc
    # Phase 3 BUG-6 fix: when the warehouse has fully issued the line (issued_qty
    # meets or exceeds approved_qty), transition status to ISSUED. The enum
    # defined ISSUED from the start but no code path set it. The keystone rule
    # in services_blocker.py already keys off issued_qty >= approved_qty, so
    # this transition is cosmetic for blocker resolution but semantically
    # correct — analytics filtering on status="issued" should now return rows.
    if line.issued_qty >= (line.approved_qty or Decimal("0")) and line.approved_qty and line.approved_qty > 0:
        if line.status != PartIssueLine.Status.ISSUED:
            line.status = PartIssueLine.Status.ISSUED
    line.save(update_fields=["issued_qty", "status", "issued_by", "unit_cost", "updated_at"])

    # v4.8 Fix 2: use the explicit FK link, not the implicit (wo, part) lookup.
    report = line.related_shortage_report
    if report is not None and is_first_execution:
        report.qty_issued = (report.qty_issued or Decimal("0")) + qty
        report.save(update_fields=["qty_issued"])
        # Edge case: if the shortage has already been CLOSED (terminal —
        # typically because the manager pre-emptively closed it after the
        # first issue committed), skip the IN_FULFILLMENT transition.
        # The issue still goes through, the PART blocker still resolves,
        # but the shortage state machine is left alone.
        try:
            if report.status != PartShortageReport.Status.CLOSED:
                transition_shortage_status(
                    report, PartShortageReport.Status.IN_FULFILLMENT, actor=actor,
                    note=_("Warehouse issued %(qty).1f × %(sku)s") % {
                        "qty": qty,
                        "sku": line.part.sku,
                    },
                )
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning(
                f"Failed to transition shortage report {report.pk} to IN_FULFILLMENT: {e}"
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
        unit_cost=_effective_unit_cost(line),
        note=_("Warehouse issued to WO-%(wo)s (released %(released).1f reservation)") % {
            "wo": line.work_order.number,
            "released": reservation_released,
        },
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

    # Phase 1+2 Cost Ledger: post the material cost for this warehouse issue.
    # post_material is idempotent by (source_type, source_id) so a partial
    # second execution against the same line won't double-post.
    # Bug #3 fix: surface ledger failures to the caller instead of silently
    # swallowing them. The view layer (work_order_warehouse_issue) already
    # wraps this call in try/except and renders a messages.error to the
    # manager. Use _effective_unit_cost so unit_cost=0 lines fall back to
    # the part's last_purchase_cost / avg_cost, avoiding the
    # CheckConstraint(amount != 0) violation entirely.
    from maintenance.cost_ledger import CostLedgerService
    CostLedgerService.post_material(
        part_issue_line=line,
        actor=actor,
        memo=_("Warehouse issued to WO-%(wo)s: %(name)s x %(qty).1f") % {
            "wo": line.work_order.number,
            "name": line.part.name,
            "qty": qty,
        },
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


# ---------------------------------------------------------------------------
# Phase 7.7: PO auto-fulfillment — close the loop between receive and WO issue
# ---------------------------------------------------------------------------

def auto_fulfill_wo_lines_from_po(*, po, actor) -> dict:
    """Phase 7.7: After a PO is received, auto-issue matching stock to open
    PartIssueLines on the WO(s) the PO's linked PRs are attached to.

    Before this:
      - Manager receives a PO (e.g. PO-2026-0006 with 5 × FILTER-A1 for WO 10).
      - Stock is added to inventory.
      - The linked PR is marked `fulfilled`.
      - But the WO still shows "📤 Lines Awaiting Warehouse Issue" for the
        same part, and the user has to click "Issue N from stock" manually.
      - The user sees a "Fulfilled" PO and assumes the WO is done, but it
        isn't until the manual step runs.

    After this:
      - When the receive flow calls this function, the same receive flow
        also auto-calls `execute_warehouse_issue` for matching open lines
        on the linked WO. The cost is posted to the WO's cost ledger,
        the PART blocker is auto-resolved (keystone rule), and the WO
        operational status recomputes to `active` once the line is fully
        issued.
      - The "📤 Lines Awaiting Warehouse Issue" panel on the WO page empties
        itself, and the "✅ No active blockers" message appears.

    Safety:
      - Only fires for PRs that explicitly link to a specific WO
        (work_order_id IS NOT NULL). Stock-only PRs (work_order_id IS NULL)
        are never auto-issued — they only replenish inventory.
      - Only fires for PartIssueLines in status APPROVED or ALLOCATED.
        PENDING (still needs manager approval) and REJECTED lines are
        skipped.
      - Honors settings.PO_AUTO_ISSUE toggle. When OFF, behaves like
        the pre-fix code: stock is added, but no auto-issue happens.
      - Each auto-issue goes through the existing `execute_warehouse_issue`
        service, so cost ledger posting, blocker resolution, and
        reservation release all work as usual.
      - Fires on BOTH full and partial PO receives. If the supplier
        delivers less than ordered (received_qty < ordered_qty), only
        `min(received_qty, sum of line remainings)` is issued to the WO;
        the remainder of the PO line waits for a future receive. The
        same rule applies when received < approved per line: only
        `min(received, remaining)` is auto-issued and the line stays
        awaiting-warehouse-issue for the balance.
      - After each auto-issue, the related PartShortageReport is checked
        and auto-marked `fulfilled` if `qty_issued >= approved_issue_qty`.

    Returns a dict summary of the actions taken.
    """
    from django.conf import settings as dj_settings

    if not getattr(dj_settings, "PO_AUTO_ISSUE", True):
        return {"enabled": False, "actions": []}

    summary = {"enabled": True, "actions": []}

    # Iterate over PRs that are explicitly linked to a WO.
    # Stock-only PRs (work_order_id IS NULL) are never auto-issued.
    linked_prs = list(po.purchase_requests.exclude(work_order__isnull=True).select_related("work_order", "part"))
    if not linked_prs:
        return summary

    # For each PO line item, find matching PRs (by part) and process.
    po_items = list(po.items.select_related("part").all())
    for item in po_items:
        # Find PRs for this part on the linked WOs.
        matching_prs = [pr for pr in linked_prs if pr.part_id == item.part_id]
        if not matching_prs:
            continue

        for pr in matching_prs:
            wo = pr.work_order
            # Find open PartIssueLines on this WO for this part.
            open_lines = list(
                PartIssueLine.objects.filter(
                    work_order=wo,
                    part=item.part,
                    status__in=[
                        PartIssueLine.Status.APPROVED,
                        PartIssueLine.Status.ALLOCATED,
                    ],
                ).order_by("pk")
            )
            if not open_lines:
                continue

            # Total qty to auto-issue = min(received, sum of line remainings).
            # Distribute among open lines by FIFO (oldest first).
            received_qty = item.received_qty
            for line in open_lines:
                if received_qty <= 0:
                    break
                remaining = (line.approved_qty or Decimal("0")) - (line.issued_qty or Decimal("0"))
                if remaining <= 0:
                    continue
                issue_qty = min(received_qty, remaining)
                try:
                    result = execute_warehouse_issue(
                        line=line, qty=issue_qty, actor=actor,
                    )
                    received_qty -= issue_qty
                    summary["actions"].append({
                        "type": "auto_issued",
                        "po_number": po.po_number,
                        "pr_pk": pr.pk,
                        "line_pk": line.pk,
                        "wo": wo.number,
                        "part": item.part.sku,
                        "qty": str(issue_qty),
                        "stock_after": result.get("stock_after"),
                    })
                    log_audit(
                        actor=actor, action="part_auto_issued_from_po",
                        entity="PartIssueLine", object_id=str(line.pk),
                        payload={
                            "po_number": po.po_number,
                            "po_pk": po.pk,
                            "pr_pk": pr.pk,
                            "qty": str(issue_qty),
                            "wo": wo.number,
                        },
                    )
                    # Try to mark related PartShortageReport fulfilled.
                    _try_auto_fulfill_shortage(line=line, actor=actor)
                except Exception as e:
                    import logging
                    logging.getLogger(__name__).warning(
                        f"Auto-fulfill failed for line {line.pk} from PO {po.po_number}: {e}"
                    )
                    summary["actions"].append({
                        "type": "auto_issue_failed",
                        "line_pk": line.pk,
                        "error": str(e),
                    })

    return summary


def _try_auto_fulfill_shortage(*, line, actor) -> bool:
    """If the line's related PartShortageReport can be marked fulfilled
    (qty_issued >= approved_issue_qty), do so.

    Called after auto-issuance from a PO. Mirrors the manager's
    `mark_shortage_fulfilled` flow but auto-runs when the math checks out.
    """
    report = getattr(line, "related_shortage_report", None)
    if report is None:
        return False
    if report.status not in (
        PartShortageReport.Status.IN_FULFILLMENT,
        PartShortageReport.Status.APPROVED,
    ):
        return False
    decision = getattr(report, "decision", None)
    if decision is None or decision.decision_type != "approve":
        return False
    if (report.qty_issued or Decimal("0")) < (decision.approved_issue_qty or Decimal("0")):
        return False
    try:
        mark_shortage_fulfilled(report=report, actor=actor)
        return True
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(
            f"Auto-fulfill shortage failed for report {report.pk}: {e}"
        )
        return False