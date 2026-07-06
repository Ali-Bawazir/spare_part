"""Procurement services (v4.8).

Auto-creates a PurchaseRequest when a shortage decision commits a
procurement quantity. Idempotent on (source_shortage_report).
"""
from __future__ import annotations

from decimal import Decimal

from django.db import transaction
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
    return pr
