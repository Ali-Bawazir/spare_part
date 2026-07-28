"""Procurement services (v4.8).

Auto-creates a PurchaseRequest when a shortage decision commits a
procurement quantity. Idempotent on (source_shortage_report).
"""
from __future__ import annotations

from decimal import Decimal

from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext as _

from maintenance.services import log_audit

from .models import PurchaseRequest


@transaction.atomic
def auto_create_pr_for_shortage(*, report, decision, actor) -> PurchaseRequest | None:
    """Auto-create a PurchaseRequest from a shortage decision.

    Idempotent: if a PR already exists for this report, return it.

    The source_shortage_report FK is the explicit link from PR back to the
    shortage. Closing the shortage does NOT cascade-delete the PR (SET_NULL).
    """
    if decision.approved_procurement_qty <= 0:
        return None

    existing = PurchaseRequest.objects.filter(
        source_shortage_report=report,
    ).first()
    if existing is not None:
        return existing

    pr = PurchaseRequest.objects.create(
        part=report.part,
        work_order=report.work_order,
        quantity=decision.approved_procurement_qty,
        notes=_(
            "Auto-created from shortage decision #%(decision_pk)s "
            "(qty requested: %(qty_requested)s, "
            "procurement qty: %(approved_qty)s)"
        ) % {
            "decision_pk": decision.pk,
            "qty_requested": f"{report.qty_requested:g}",
            "approved_qty": f"{decision.approved_procurement_qty:g}",
        },
        status=PurchaseRequest.Status.PENDING,
        created_by=actor,
        source_shortage_report=report,
    )
    log_audit(
        actor=actor, action="purchase_request_auto_created",
        entity="PurchaseRequest", object_id=str(pr.pk),
        payload={
            "source_shortage_report": str(report.pk),
            "part": report.part.sku,
            "qty": str(decision.approved_procurement_qty),
            "source_wo": str(report.work_order.number) if report.work_order_id else "",
        },
    )
    # Phase 7.9: open a SHORTAGE WO Blocker so the WO page shows the
    # "Awaiting Procurement" badge distinctly from the PART blocker.
    # The SHORTAGE blocker resolves when the PR transitions to FULFILLED
    # (see procurement/views.py purchase_order_receive and the SHORTAGE_FULFILLED
    # event emitter wired there).
    if pr.work_order_id and report is not None:
        try:
            from maintenance.models import WorkOrderBlocker
            from maintenance.services_blocker import WorkOrderBlockerService
            WorkOrderBlockerService.open_blocker(
                work_order=pr.work_order,
                kind=WorkOrderBlocker.Kind.SHORTAGE,
                external_obj=report,
                opened_by=actor,
                note=decision.decision_note or "",
                external_label=(
                    f"{report.part.name} (SKU {report.part.sku}) × "
                    f"{decision.approved_procurement_qty}"
                ),
            )
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning(
                f"Failed to open SHORTAGE blocker for PR #{pr.pk}: {e}"
            )
    return pr


import logging
log = logging.getLogger(__name__)


def sync_shortage_blocker_after_pr_change(*, pr, event_type: str, actor):
    """Tell the SHORTAGE WO Blocker keyed to a PR's source PSR that the
    wait state changed. Single chokepoint mirroring the SHORTAGE_FULFILLED
    pattern already used by purchase_order_receive (procurement/views.py).

    Called from every view/service that mutates PurchaseRequest.status
    (purchase_order_cancel, purchase_order_close_short). Returns the
    affected WorkOrderBlocker or None.
    """
    from maintenance.services_blocker import WorkOrderBlockerService
    report = getattr(pr, "source_shortage_report", None)
    if report is None:
        return None
    return WorkOrderBlockerService.sync_from_external_event(
        external_obj=report, event_type=event_type, actor=actor,
    )


class PurchaseOrderService:
    """Domain services for PurchaseOrder workflows."""

    @staticmethod
    @transaction.atomic
    def reorder(*, source_po, created_by):
        """Create a new draft PurchaseOrder by cloning a terminal source PO.

        Copies: supplier, all line items (part, ordered_qty, negotiated_unit_price),
        any linked PRs.
        Source PO must be in a terminal state (RECEIVED, CLOSED_SHORT, or CANCELLED).
        Active POs are rejected with `ValueError` — caller should catch and render
        a message.

        Returns the new `PurchaseOrder` instance (not yet saved — caller decides
        where to redirect).
        """
        from procurement.models import PurchaseOrder, PurchaseOrderItem

        if source_po.status not in (
            PurchaseOrder.Status.RECEIVED,
            PurchaseOrder.Status.CLOSED_SHORT,
            PurchaseOrder.Status.CANCELLED,
        ):
            raise ValueError(
                f"Cannot reorder PO-{source_po.po_number}: "
                f"status is {source_po.status}, must be RECEIVED, CLOSED_SHORT, or CANCELLED."
            )

        new_po = PurchaseOrder(
            supplier=source_po.supplier,
            created_by=created_by,
            reorder_source=source_po,
            notes=(
                f"Reordered from PO-{source_po.po_number} on "
                f"{timezone.now().strftime('%Y-%m-%d')}."
            ),
        )
        # Trigger the auto-generated `po_number` save logic from the model.
        new_po.save()

        for src_line in source_po.items.all():
            # Prefer the last actual_unit_price if the source was RECEIVED or CLOSED_SHORT
            # (those are the terminal states where the actual invoice price is locked in),
            # else fall back to the negotiated price.
            unit_price = src_line.actual_unit_price or src_line.negotiated_unit_price or Decimal("0")
            qty = src_line.ordered_qty
            PurchaseOrderItem.objects.create(
                purchase_order=new_po,
                part=src_line.part,
                ordered_qty=qty,
                negotiated_unit_price=unit_price,
                total_price=qty * unit_price,
            )

        # Audit log
        try:
            from maintenance.services import log_audit
            log_audit(
                actor=created_by,
                action="po_reorder_created",
                entity="PurchaseOrder",
                object_id=new_po.pk,
                payload={
                    "source_po_id": source_po.pk,
                    "source_po_number": source_po.po_number,
                    "new_po_id": new_po.pk,
                    "new_po_number": new_po.po_number,
                    "supplier_id": source_po.supplier_id,
                    "lines_cloned": new_po.items.count(),
                },
            )
        except Exception:
            log.warning("Failed to write audit log for po_reorder_created", exc_info=True)

        return new_po
