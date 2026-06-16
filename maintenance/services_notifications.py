"""
Phase 2C — WorkOrder Blocker System: Notification listener.

This module provides a centralized hook that converts `WorkOrderBlockerEvent`
rows into in-app `Notification` rows. It is the single integration point
between the blocker event write path (`WorkOrderBlockerEventService.record`)
and the notification system.

Recipients and dedup
--------------------
- Recipients are computed per event_type from the underlying blocker + WO.
- 1-hour dedup window per (recipient, kind, ref_id) is enforced by setting
  `dedup_key = f"{kind}:{ref_id}:{recipient_id}"` on each Notification and
  checking for an existing row with that key created within the last hour.
- All write paths are wrapped in `try/except` so a notification failure never
  blocks the upstream event write.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from django.urls import reverse
from django.utils import timezone

from accounts.models import User
from inventory.models import PartIssueLine, PartShortageReport
from maintenance.models import (
    ExternalRepairOrder,
    ExternalRepairRequest,
    Notification,
    WorkOrder,
    WorkOrderBlocker,
    WorkOrderBlockerEvent,
)

from .notifications import (
    _managers_supers,
    _managers_supervisors_supers,
    _procurement_supers,
    _unique_users,
)


logger = logging.getLogger(__name__)


_DEDUP_WINDOW = timedelta(hours=1)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _dedup_exists(*, dedup_key: str) -> bool:
    """Return True if a Notification with this dedup_key was created in the
    last `_DEDUP_WINDOW`.
    """
    if not dedup_key:
        return False
    return Notification.objects.filter(
        dedup_key=dedup_key,
        created_at__gte=timezone.now() - _DEDUP_WINDOW,
    ).exists()


def _wo_label(wo: WorkOrder) -> str:
    """Return a human-friendly WO reference: 'WO-123'."""
    return f"WO-{wo.number}"


def _blocker_kind_label(blocker: WorkOrderBlocker) -> str:
    """Map blocker.kind to a user-friendly label for the title."""
    return {
        WorkOrderBlocker.Kind.PART: "part",
        WorkOrderBlocker.Kind.SHORTAGE: "shortage",
        WorkOrderBlocker.Kind.VENDOR_REPAIR: "vendor repair",
        WorkOrderBlocker.Kind.OPERATIONAL: "operational pause",
    }.get(blocker.kind, blocker.kind)


def _resolve_part_name(blocker: WorkOrderBlocker) -> str:
    """Best-effort lookup of the part name referenced by a blocker.

    For PART blockers the external_ref is a PartIssueLine → SparePart.
    For SHORTAGE blockers the external_ref is a PartShortageReport → SparePart.
    Returns empty string if the part cannot be resolved.
    """
    ext = blocker.external_ref
    if isinstance(ext, PartIssueLine):
        return ext.part.name if ext.part else ""
    if isinstance(ext, PartShortageReport):
        return ext.part.name if ext.part else ""
    return ""


def _resolve_requesting_user(blocker: WorkOrderBlocker):
    """For PART / SHORTAGE blockers, return the user who raised the request
    or shortage report. Returns None for other blocker kinds.
    """
    ext = blocker.external_ref
    if isinstance(ext, PartIssueLine):
        return getattr(ext, "requested_by", None)
    if isinstance(ext, PartShortageReport):
        return getattr(ext, "reported_by", None)
    return None


def _resolve_ero(blocker: WorkOrderBlocker):
    """For VENDOR_REPAIR blockers, return the ERO. Prefers related_ero
    (set by approve_external_repair_request) but falls back to external_ref
    if related_ero is not set.
    """
    if blocker.related_ero is not None:
        return blocker.related_ero
    ext = blocker.external_ref
    if isinstance(ext, ExternalRepairOrder):
        return ext
    if isinstance(ext, ExternalRepairRequest):
        return getattr(ext, "repair_order", None)
    return None


def _resolve_err(blocker: WorkOrderBlocker):
    """For VENDOR_REPAIR blockers, return the original ExternalRepairRequest.
    """
    if isinstance(blocker.external_ref, ExternalRepairRequest):
        return blocker.external_ref
    return None


def _wo_recipients(wo: WorkOrder) -> list[User]:
    """Recipients tied to a specific WO: the assigned technician + creator.
    Returns a de-duplicated list, in insertion order.
    """
    out: list[User] = []
    if wo.assigned_technician_id:
        out.append(wo.assigned_technician)
    if wo.created_by_id:
        out.append(wo.created_by)
    return _unique_users(out)


def _notify_one(*, recipient: User, kind: str, title: str, body: str,
                link: str, dedup_key: str, is_critical: bool = False) -> bool:
    """Create a single Notification row, honoring the dedup window.

    Returns True if the row was created, False if it was deduped.
    """
    if _dedup_exists(dedup_key=dedup_key):
        return False
    Notification.objects.create(
        recipient=recipient,
        kind=kind,
        title=title[:255],
        body=body[:2000],
        link=link[:500],
        is_critical=is_critical,
        dedup_key=dedup_key[:200],
        work_order_blocker=None,
    )
    return True


# ---------------------------------------------------------------------------
# Per-event-type notification creators
# ---------------------------------------------------------------------------


def _notify_blocker_opened(event: WorkOrderBlockerEvent) -> int:
    blocker = event.blocker
    wo = blocker.work_order
    kind_label = _blocker_kind_label(blocker)
    title = f"{_wo_label(wo)} blocked: {kind_label}"
    body_parts: list[str] = []
    if blocker.note:
        body_parts.append(blocker.note)
    machine_name = getattr(getattr(wo, "machine", None), "name", None)
    if machine_name:
        body_parts.append(f"Machine: {machine_name}")
    part_name = _resolve_part_name(blocker)
    if part_name:
        body_parts.append(f"Part: {part_name}")
    body = " — ".join(p for p in body_parts if p)

    link = reverse("work_order_detail", kwargs={"pk": wo.pk})
    recipients = _unique_users(
        _managers_supervisors_supers() + _wo_recipients(wo)
    )

    created = 0
    for r in recipients:
        key = f"{Notification.Kind.WO_BLOCKER_OPENED}:{blocker.pk}:{r.pk}"
        if _notify_one(
            recipient=r,
            kind=Notification.Kind.WO_BLOCKER_OPENED,
            title=title,
            body=body,
            link=link,
            dedup_key=key,
        ):
            created += 1
    return created


def _notify_blocker_resolved(event: WorkOrderBlockerEvent) -> int:
    blocker = event.blocker
    wo = blocker.work_order
    kind_label = _blocker_kind_label(blocker)
    title = f"{_wo_label(wo)} unblocked: {kind_label}"
    body_parts: list[str] = []
    note = (event.payload or {}).get("note") or blocker.resolution_note
    if note:
        body_parts.append(note)
    machine_name = getattr(getattr(wo, "machine", None), "name", None)
    if machine_name:
        body_parts.append(f"Machine: {machine_name}")
    body = " — ".join(p for p in body_parts if p)

    link = reverse("work_order_detail", kwargs={"pk": wo.pk})
    recipients = _unique_users(
        _managers_supervisors_supers() + _wo_recipients(wo)
    )

    created = 0
    for r in recipients:
        key = f"{Notification.Kind.WO_BLOCKER_RESOLVED}:{blocker.pk}:{r.pk}"
        if _notify_one(
            recipient=r,
            kind=Notification.Kind.WO_BLOCKER_RESOLVED,
            title=title,
            body=body,
            link=link,
            dedup_key=key,
        ):
            created += 1
    return created


def _notify_blocker_cancelled(event: WorkOrderBlockerEvent) -> int:
    blocker = event.blocker
    wo = blocker.work_order
    kind_label = _blocker_kind_label(blocker)
    title = f"{_wo_label(wo)} blocker cancelled: {kind_label}"
    body_parts: list[str] = []
    reason = (event.payload or {}).get("reason") or blocker.cancel_reason
    if reason:
        body_parts.append(reason)
    machine_name = getattr(getattr(wo, "machine", None), "name", None)
    if machine_name:
        body_parts.append(f"Machine: {machine_name}")
    body = " — ".join(p for p in body_parts if p)

    link = reverse("work_order_detail", kwargs={"pk": wo.pk})
    recipients = _unique_users(
        _managers_supervisors_supers() + _wo_recipients(wo)
    )

    created = 0
    for r in recipients:
        key = f"{Notification.Kind.WO_BLOCKER_CANCELLED}:{blocker.pk}:{r.pk}"
        if _notify_one(
            recipient=r,
            kind=Notification.Kind.WO_BLOCKER_CANCELLED,
            title=title,
            body=body,
            link=link,
            dedup_key=key,
        ):
            created += 1
    return created


def _notify_part_approved(event: WorkOrderBlockerEvent) -> int:
    blocker = event.blocker
    wo = blocker.work_order
    line = blocker.external_ref if isinstance(blocker.external_ref, PartIssueLine) else None
    part_sku = line.part.sku if line and line.part else ""
    part_name = line.part.name if line and line.part else ""
    title = f"{_wo_label(wo)} part approved: {part_sku or 'part'}"
    body = f"{part_name} — manager approved the part request."
    link = reverse("work_order_detail", kwargs={"pk": wo.pk})

    recipients = list(_managers_supervisors_supers())
    requester = _resolve_requesting_user(blocker)
    if requester is not None:
        recipients.append(requester)
    recipients = _unique_users(recipients)

    created = 0
    for r in recipients:
        key = f"{Notification.Kind.WO_BLOCKER_RESOLVED}:approve:{blocker.pk}:{r.pk}"
        if _notify_one(
            recipient=r,
            kind=Notification.Kind.WO_BLOCKER_RESOLVED,
            title=title,
            body=body,
            link=link,
            dedup_key=key,
        ):
            created += 1
    return created


def _notify_part_rejected(event: WorkOrderBlockerEvent) -> int:
    blocker = event.blocker
    wo = blocker.work_order
    line = blocker.external_ref if isinstance(blocker.external_ref, PartIssueLine) else None
    part_sku = line.part.sku if line and line.part else ""
    reason = (event.payload or {}).get("reason") or getattr(line, "rejection_reason", "")
    title = f"{_wo_label(wo)} part request rejected: {part_sku or 'part'}"
    body = f"Reason: {reason}" if reason else "Manager rejected the part request."
    link = reverse("work_order_detail", kwargs={"pk": wo.pk})

    recipients = list(_managers_supers())
    requester = _resolve_requesting_user(blocker)
    if requester is not None:
        recipients.append(requester)
    if wo.created_by_id:
        recipients.append(wo.created_by)
    recipients = _unique_users(recipients)

    created = 0
    for r in recipients:
        key = f"{Notification.Kind.WO_BLOCKER_CANCELLED}:reject:{blocker.pk}:{r.pk}"
        if _notify_one(
            recipient=r,
            kind=Notification.Kind.WO_BLOCKER_CANCELLED,
            title=title,
            body=body,
            link=link,
            dedup_key=key,
        ):
            created += 1
    return created


def _notify_shortage_raised(event: WorkOrderBlockerEvent) -> int:
    blocker = event.blocker
    wo = blocker.work_order
    report = blocker.external_ref if isinstance(blocker.external_ref, PartShortageReport) else None
    part_sku = report.part.sku if report and report.part else ""
    shortage_qty = report.shortage_qty if report else 0
    title = f"{_wo_label(wo)} part shortage reported: {part_sku or 'part'}"
    body = f"Shortage: {shortage_qty} units — awaiting manager decision."
    link = reverse("work_order_detail", kwargs={"pk": wo.pk})

    recipients = _unique_users(_managers_supervisors_supers())
    created = 0
    is_critical = shortage_qty is not None and shortage_qty >= 10
    for r in recipients:
        key = f"{Notification.Kind.WO_BLOCKER_OPENED}:shortage:{blocker.pk}:{r.pk}"
        if _notify_one(
            recipient=r,
            kind=Notification.Kind.WO_BLOCKER_OPENED,
            title=title,
            body=body,
            link=link,
            dedup_key=key,
            is_critical=is_critical,
        ):
            created += 1
    return created


def _notify_shortage_fulfilled(event: WorkOrderBlockerEvent) -> int:
    blocker = event.blocker
    wo = blocker.work_order
    report = blocker.external_ref if isinstance(blocker.external_ref, PartShortageReport) else None
    part_sku = report.part.sku if report and report.part else ""
    title = f"{_wo_label(wo)} shortage resolved: {part_sku or 'part'}"
    body = "Procurement / warehouse fulfilled the shortage."
    link = reverse("work_order_detail", kwargs={"pk": wo.pk})

    recipients = _unique_users(
        _managers_supervisors_supers() + _wo_recipients(wo)
    )
    created = 0
    for r in recipients:
        key = f"{Notification.Kind.WO_BLOCKER_RESOLVED}:shortage:{blocker.pk}:{r.pk}"
        if _notify_one(
            recipient=r,
            kind=Notification.Kind.WO_BLOCKER_RESOLVED,
            title=title,
            body=body,
            link=link,
            dedup_key=key,
        ):
            created += 1
    return created


def _notify_ero_created(event: WorkOrderBlockerEvent) -> int:
    blocker = event.blocker
    wo = blocker.work_order
    ero = _resolve_ero(blocker)
    title = f"{_wo_label(wo)} external repair order created"
    body = f"ERO '{ero.title}' is awaiting vendor assignment." if ero else "External repair order created."
    link = reverse("work_order_detail", kwargs={"pk": wo.pk})

    recipients = list(_managers_supervisors_supers()) + list(_procurement_supers())
    err = _resolve_err(blocker)
    requester = getattr(err, "requested_by", None) if err else None
    if requester is not None:
        recipients.append(requester)
    recipients = _unique_users(recipients)

    created = 0
    for r in recipients:
        key = f"{Notification.Kind.WO_BLOCKER_OPENED}:ero:{blocker.pk}:{r.pk}"
        if _notify_one(
            recipient=r,
            kind=Notification.Kind.WO_BLOCKER_OPENED,
            title=title,
            body=body,
            link=link,
            dedup_key=key,
        ):
            created += 1
    return created


def _notify_ero_accepted(event: WorkOrderBlockerEvent) -> int:
    blocker = event.blocker
    wo = blocker.work_order
    ero = _resolve_ero(blocker)
    title = f"{_wo_label(wo)} vendor repair accepted"
    body = f"Vendor repair '{ero.title}' accepted — part is back." if ero else "Vendor repair accepted."
    link = reverse("work_order_detail", kwargs={"pk": wo.pk})

    recipients = list(_managers_supervisors_supers())
    err = _resolve_err(blocker)
    requester = getattr(err, "requested_by", None) if err else None
    if requester is not None:
        recipients.append(requester)
    recipients = _unique_users(recipients)

    created = 0
    for r in recipients:
        key = f"{Notification.Kind.WO_BLOCKER_RESOLVED}:ero:{blocker.pk}:{r.pk}"
        if _notify_one(
            recipient=r,
            kind=Notification.Kind.WO_BLOCKER_RESOLVED,
            title=title,
            body=body,
            link=link,
            dedup_key=key,
        ):
            created += 1
    return created


def _notify_emergency_interrupted(event: WorkOrderBlockerEvent) -> int:
    blocker = event.blocker
    wo = blocker.work_order
    source_wo = blocker.source_work_order
    title = f"{_wo_label(wo)} paused: emergency override"
    body_parts: list[str] = ["An emergency work order auto-paused this WO."]
    if source_wo is not None:
        body_parts.append(f"Source: WO-{source_wo.number}.")
    machine_name = getattr(getattr(wo, "machine", None), "name", None)
    if machine_name:
        body_parts.append(f"Machine: {machine_name}")
    body = " — ".join(p for p in body_parts if p)

    link = reverse("work_order_detail", kwargs={"pk": wo.pk})
    recipients = _unique_users(
        _managers_supervisors_supers() + _wo_recipients(wo)
    )
    created = 0
    for r in recipients:
        key = f"{Notification.Kind.EMERGENCY_INTERRUPTED}:{blocker.pk}:{r.pk}"
        if _notify_one(
            recipient=r,
            kind=Notification.Kind.EMERGENCY_INTERRUPTED,
            title=title,
            body=body,
            link=link,
            dedup_key=key,
            is_critical=True,
        ):
            created += 1
    return created


def _notify_labor_resumed(event: WorkOrderBlockerEvent) -> int:
    blocker = event.blocker
    wo = blocker.work_order
    title = f"{_wo_label(wo)} labor resumed"
    body_parts: list[str] = ["Technician resumed work on this WO."]
    machine_name = getattr(getattr(wo, "machine", None), "name", None)
    if machine_name:
        body_parts.append(f"Machine: {machine_name}")
    body = " — ".join(p for p in body_parts if p)

    link = reverse("work_order_detail", kwargs={"pk": wo.pk})
    recipients = _unique_users(
        _managers_supervisors_supers() + _wo_recipients(wo)
    )
    created = 0
    for r in recipients:
        key = f"{Notification.Kind.LABOR_RESUMED}:{wo.pk}:{r.pk}"
        if _notify_one(
            recipient=r,
            kind=Notification.Kind.LABOR_RESUMED,
            title=title,
            body=body,
            link=link,
            dedup_key=key,
        ):
            created += 1
    return created


# Map event_type string → handler. Events not listed here are silently
# ignored by the listener (they get their notifications via other paths).
_EVENT_DISPATCH: dict[str, callable] = {
    WorkOrderBlockerEvent.EventType.BLOCKER_CREATED: _notify_blocker_opened,
    WorkOrderBlockerEvent.EventType.BLOCKER_RESOLVED: _notify_blocker_resolved,
    WorkOrderBlockerEvent.EventType.BLOCKER_CANCELLED: _notify_blocker_cancelled,
    WorkOrderBlockerEvent.EventType.PART_APPROVED: _notify_part_approved,
    WorkOrderBlockerEvent.EventType.PART_REJECTED: _notify_part_rejected,
    WorkOrderBlockerEvent.EventType.SHORTAGE_RAISED: _notify_shortage_raised,
    WorkOrderBlockerEvent.EventType.SHORTAGE_FULFILLED: _notify_shortage_fulfilled,
    WorkOrderBlockerEvent.EventType.ERO_CREATED: _notify_ero_created,
    WorkOrderBlockerEvent.EventType.ERO_ACCEPTED: _notify_ero_accepted,
    WorkOrderBlockerEvent.EventType.EMERGENCY_INTERRUPTED: _notify_emergency_interrupted,
    WorkOrderBlockerEvent.EventType.LABOR_RESUMED: _notify_labor_resumed,
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


class NotificationService:
    """Centralized listener for WorkOrderBlockerEvent rows.

    `on_blocker_event(event)` is the single hook. It inspects the event_type
    and dispatches to per-event notification creators. It also enforces a
    1-hour dedup window per (recipient, kind, ref_id) via the dedup_key
    field on Notification.

    Returns the number of Notification rows created (0 if all deduped or the
    event_type is not in the dispatch table).
    """

    @classmethod
    def on_blocker_event(cls, event: "WorkOrderBlockerEvent") -> int:
        handler = _EVENT_DISPATCH.get(event.event_type)
        if handler is None:
            return 0
        try:
            return handler(event)
        except Exception:
            logger.exception(
                "Notification handler %s failed for event %s (blocker #%s)",
                getattr(handler, "__name__", handler),
                event.pk,
                getattr(event, "blocker_id", None),
            )
            return 0


def notify_po_received_summary(po, actor) -> int:
    """When a PO is received, notify managers + procurement of the lines
    received. Fires a single summary notification per recipient. Dedup
    window: 1h per (po, recipient).

    Args:
        po: a `procurement.PurchaseOrder` (passed as object; avoids an
            import cycle).
        actor: the User who performed the receipt.

    Returns:
        Number of Notification rows created.
    """
    line_items = list(po.items.all()) if hasattr(po, "items") else []
    if line_items:
        line_summaries: list[str] = []
        for item in line_items:
            try:
                qty = item.received_qty or 0
            except Exception:
                qty = 0
            if qty and qty > 0:
                part = item.part
                sku = getattr(part, "sku", "") if part else ""
                name = getattr(part, "name", "") if part else ""
                line_summaries.append(f"{qty:g}× {sku or name}".strip())
        body = ", ".join(line_summaries) if line_summaries else "Lines received."
    else:
        body = "PO received."

    po_number = getattr(po, "po_number", None) or str(getattr(po, "pk", ""))
    title = f"\U0001F4E6 PO {po_number} received"
    link = reverse("purchase_order_detail", kwargs={"pk": po.pk})

    recipients = list(_managers_supervisors_supers()) + list(_procurement_supers())
    if actor is not None:
        recipients.append(actor)
    recipients = _unique_users(recipients)

    created = 0
    for r in recipients:
        key = f"{Notification.Kind.PO_RECEIVED_SUMMARY}:{po.pk}:{r.pk}"
        if _notify_one(
            recipient=r,
            kind=Notification.Kind.PO_RECEIVED_SUMMARY,
            title=title,
            body=body,
            link=link,
            dedup_key=key,
        ):
            created += 1
    return created


__all__ = [
    "NotificationService",
    "notify_po_received_summary",
]
