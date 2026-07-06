"""
WorkOrder blocker system — core services.

This is the single entry point for all blocker mutations. External services
(PartIssueLine, PartShortageReport, ExternalRepairRequest, ERO) call into
here to open, resolve, or cancel blockers.

State machine (locked in ADR-0007):
    OPEN --resolve--> RESOLVED
    OPEN --cancel--> CANCELLED
    (never reopen; new wait episode = new blocker)

Operational rule: the PART blocker resolves on `issued_qty == approved_qty`
(correction in ADR-0007 top note), NOT on allocation.
"""
from __future__ import annotations

import logging
from typing import Optional, Any

from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext as _

logger = logging.getLogger(__name__)

from inventory.models import (
    InventoryReservation,
    PartIssueLine,
    PartShortageReport,
)

from .models import (
    ExternalRepairOrder,
    ExternalRepairRequest,
    WorkOrder,
    WorkOrderBlocker,
    WorkOrderBlockerEvent,
)


def _recompute_wo_status(work_order: WorkOrder) -> str:
    """Local import to avoid circular dependency between this module and
    `services_wo_status` (which itself depends on the blocker models).
    """
    from .services_wo_status import WorkOrderService
    return WorkOrderService.recompute_operational_status(work_order)


class WorkOrderBlockerService:
    """Single entry point for all WorkOrderBlocker mutations."""

    @classmethod
    @transaction.atomic
    def open_blocker(
        cls,
        *,
        work_order: WorkOrder,
        kind: str,
        external_obj: Any = None,
        opened_by=None,
        note: str = "",
        source_work_order: Optional[WorkOrder] = None,
        related_ero: Optional[ExternalRepairOrder] = None,
        pause_reason: str = "",
        external_label: str = "",
    ) -> Optional[WorkOrderBlocker]:
        """
        Open a new OPEN blocker. Idempotent: if an OPEN blocker already
        exists for (work_order, external_obj), return the existing one
        (no duplicate). The DB-level partial unique constraint on
        (work_order, content_type, object_id) WHERE status='open' makes
        this safe under race conditions.

        Returns None if `external_obj` is None — the blocker service
        requires an external reference to identify the underlying wait
        source. Callers must supply external_obj (or use the dedicated
        open helpers added in Phase 2B+).

        Writes a BLOCKER_CREATED WOBlockerEvent and refreshes the WO's
        operational_status.
        """
        if external_obj is None:
            return None

        ct = ContentType.objects.get_for_model(external_obj)
        object_id = external_obj.pk

        existing = (
            WorkOrderBlocker.objects
            .select_for_update()
            .filter(
                work_order=work_order,
                content_type=ct,
                object_id=object_id,
                status=WorkOrderBlocker.Status.OPEN,
            )
            .first()
        )
        if existing is not None:
            return existing

        blocker = WorkOrderBlocker.objects.create(
            work_order=work_order,
            kind=kind,
            status=WorkOrderBlocker.Status.OPEN,
            content_type=ct,
            object_id=object_id,
            external_label=external_label[:300],
            related_ero=related_ero,
            source_work_order=source_work_order,
            note=note,
            pause_reason=pause_reason,
            opened_by=opened_by,
        )

        WorkOrderBlockerEventService.record(
            blocker=blocker,
            event_type=WorkOrderBlockerEvent.EventType.BLOCKER_CREATED,
            actor=opened_by,
            payload={
                "kind": kind,
                "external_ct": ct.model,
                "external_id": object_id,
            },
        )

        _recompute_wo_status(work_order)
        return blocker

    @classmethod
    @transaction.atomic
    def open_operational_blocker(
        cls,
        *,
        work_order: WorkOrder,
        opened_by=None,
        note: str = "",
        source_work_order: Optional[WorkOrder] = None,
        pause_reason: str = "",
    ) -> Optional[WorkOrderBlocker]:
        """
        Open an OPERATIONAL WO Blocker (no external entity — this is a pause,
        not a wait on a part/vendor/shortage).

        Returns the new blocker, or the existing open one if one is already
        open on this WO (idempotent: we don't stack duplicate "paused"
        blockers).

        Writes a BLOCKER_CREATED event and refreshes the WO's
        operational_status.
        """
        existing = WorkOrderBlocker.objects.filter(
            work_order=work_order,
            kind=WorkOrderBlocker.Kind.OPERATIONAL,
            status=WorkOrderBlocker.Status.OPEN,
        ).first()
        if existing:
            return existing

        blocker = WorkOrderBlocker.objects.create(
            work_order=work_order,
            kind=WorkOrderBlocker.Kind.OPERATIONAL,
            status=WorkOrderBlocker.Status.OPEN,
            note=note or "",
            pause_reason=pause_reason,
            source_work_order=source_work_order,
            opened_by=opened_by,
            external_label=(
                _(f"Operational pause: {pause_reason}") if pause_reason
                else _("Operational pause")
            ),
        )

        WorkOrderBlockerEventService.record(
            blocker=blocker,
            event_type=WorkOrderBlockerEvent.EventType.BLOCKER_CREATED,
            actor=opened_by,
            payload={
                "pause_reason": pause_reason,
                "source_wo_id": (
                    source_work_order.pk if source_work_order else None
                ),
            },
        )

        _recompute_wo_status(work_order)
        return blocker

    @classmethod
    @transaction.atomic
    def resolve_blocker(
        cls,
        *,
        blocker: WorkOrderBlocker,
        resolution_note: str = "",
        resolved_by=None,
    ) -> WorkOrderBlocker:
        """
        Transition OPEN -> RESOLVED. Idempotent: if already RESOLVED,
        returns the existing. Records BLOCKER_RESOLVED event.
        """
        blocker = WorkOrderBlocker.objects.select_for_update().get(pk=blocker.pk)

        if blocker.status == WorkOrderBlocker.Status.RESOLVED:
            return blocker
        if blocker.status == WorkOrderBlocker.Status.CANCELLED:
            raise ValueError(
                _(
                    f"Blocker #{blocker.pk} is CANCELLED; cannot resolve. "
                    f"Open a new blocker for a new wait episode."
                )
            )

        now = timezone.now()
        blocker.status = WorkOrderBlocker.Status.RESOLVED
        blocker.resolved_at = now
        blocker.resolved_by = resolved_by
        blocker.resolution_note = resolution_note
        blocker.save(update_fields=[
            "status", "resolved_at", "resolved_by", "resolution_note",
        ])

        WorkOrderBlockerEventService.record(
            blocker=blocker,
            event_type=WorkOrderBlockerEvent.EventType.BLOCKER_RESOLVED,
            actor=resolved_by,
            payload={"note": resolution_note},
        )

        _recompute_wo_status(blocker.work_order)
        return blocker

    @classmethod
    @transaction.atomic
    def cancel_blocker(
        cls,
        *,
        blocker: WorkOrderBlocker,
        cancel_reason: str = "",
        cancelled_by=None,
    ) -> WorkOrderBlocker:
        """
        Transition OPEN -> CANCELLED. Idempotent: if already CANCELLED,
        returns the existing. Records BLOCKER_CANCELLED event.
        """
        blocker = WorkOrderBlocker.objects.select_for_update().get(pk=blocker.pk)

        if blocker.status == WorkOrderBlocker.Status.CANCELLED:
            return blocker
        if blocker.status == WorkOrderBlocker.Status.RESOLVED:
            raise ValueError(
                _(f"Blocker #{blocker.pk} is RESOLVED; cannot cancel.")
            )

        now = timezone.now()
        blocker.status = WorkOrderBlocker.Status.CANCELLED
        blocker.cancelled_at = now
        blocker.cancelled_by = cancelled_by
        blocker.cancel_reason = cancel_reason
        blocker.save(update_fields=[
            "status", "cancelled_at", "cancelled_by", "cancel_reason",
        ])

        WorkOrderBlockerEventService.record(
            blocker=blocker,
            event_type=WorkOrderBlockerEvent.EventType.BLOCKER_CANCELLED,
            actor=cancelled_by,
            payload={"reason": cancel_reason},
        )

        _recompute_wo_status(blocker.work_order)
        return blocker

    @classmethod
    def sync_from_external_event(
        cls,
        *,
        external_obj: Any,
        event_type: str,
        actor=None,
        payload: Optional[dict] = None,
    ) -> Optional[WorkOrderBlocker]:
        """
        Hook called by other services when their underlying entity changes
        state. Looks up the open blocker for external_obj and decides
        what to do (resolve, cancel, no-op) based on event_type.

        Returns the affected blocker (or None).

        For Phase 2A, support these event types:
        - "PART_ISSUED" — resolve the PART blocker iff the line's
          issued_qty >= approved_qty (correction rule from ADR-0007)
        - "PART_REJECTED" — cancel the PART blocker
        - "SHORTAGE_FULFILLED" — resolve the SHORTAGE blocker
        - "ERO_RETURNED" — resolve the VENDOR_REPAIR blocker (tech can
          resume work before manager accepts)
        - "ERO_ACCEPTED" — resolve the VENDOR_REPAIR blocker

        Other event types are no-op for now (Phase 2B will add more).
        """
        if external_obj is None:
            return None

        # ERO_RETURNED / ERO_ACCEPTED special case: the VENDOR_REPAIR blocker is
        # opened against the ERR (origin_request), not the ERO. Do the fallback
        # lookup BEFORE the None-guard for both event types.
        if event_type in ("ERO_RETURNED", "ERO_ACCEPTED"):
            if not isinstance(external_obj, ExternalRepairRequest) \
                    and hasattr(external_obj, "origin_request"):
                err_obj = external_obj.origin_request
                if err_obj is not None:
                    external_obj = err_obj

        ct = ContentType.objects.get_for_model(external_obj)
        object_id = external_obj.pk

        open_blocker = (
            WorkOrderBlocker.objects
            .select_for_update()
            .filter(
                content_type=ct,
                object_id=object_id,
                status=WorkOrderBlocker.Status.OPEN,
            )
            .first()
        )

        # Bug #4 fix: no blocker is open for this external entity. The most
        # common case is cancelling a PartIssueLine that was approved when
        # stock was available (no blocker ever opened), then later cancelled
        # — nothing to cancel on the blocker side. Silently no-op is the
        # right behaviour here.
        if open_blocker is None:
            return None

        payload = payload or {}

        if event_type == "PART_ISSUED":
            if not isinstance(external_obj, PartIssueLine):
                return None
            # Correction rule: PART blocker resolves on issued == approved,
            # not on allocation. (ADR-0007 top note.)
            if (external_obj.issued_qty or 0) >= (external_obj.approved_qty or 0) \
                    and (external_obj.approved_qty or 0) > 0:
                return cls.resolve_blocker(
                    blocker=open_blocker,
                    resolution_note=payload.get("note", _("Part fully issued")),
                    resolved_by=actor,
                )
            return None

        if event_type == "PART_REJECTED":
            return cls.cancel_blocker(
                blocker=open_blocker,
                cancel_reason=payload.get("reason", _("Part request rejected")),
                cancelled_by=actor,
            )

        if event_type == "SHORTAGE_FULFILLED":
            return cls.resolve_blocker(
                blocker=open_blocker,
                resolution_note=payload.get("note", _("Shortage fulfilled")),
                resolved_by=actor,
            )

        if event_type == "ERO_ACCEPTED":
            return cls.resolve_blocker(
                blocker=open_blocker,
                resolution_note=payload.get("note", _("Vendor repair accepted")),
                resolved_by=actor,
            )

        # v5: when the vendor returns the part, the WO can resume work even
        # before the manager accepts the repair. The blocker resolves at
        # RETURNED so the tech's WO transitions out of waiting_vendor.
        # The ERO still needs manager acceptance to close (which posts the
        # vendor_repair cost to the ledger), but that is now decoupled from
        # the WO state machine.
        if event_type == "ERO_RETURNED":
            return cls.resolve_blocker(
                blocker=open_blocker,
                resolution_note=payload.get("note", _("Vendor returned part")),
                resolved_by=actor,
            )

        return None


class WorkOrderBlockerEventService:
    """Single write path for WOBlockerEvent rows."""

    @classmethod
    def record(
        cls,
        *,
        blocker: WorkOrderBlocker,
        event_type: str,
        actor=None,
        payload: Optional[dict] = None,
    ) -> WorkOrderBlockerEvent:
        """
        Append a structured event to a blocker's history. Always writes;
        never replaces. Returns the created event row.

        Also fires the Phase 2C notification hook so the event can drive
        in-app notifications. Notification failures are logged and never
        propagate (the event write is the source of truth).
        """
        event = WorkOrderBlockerEvent.objects.create(
            blocker=blocker,
            event_type=event_type,
            actor=actor,
            payload=payload or {},
        )
        try:
            from .services_notifications import NotificationService
            NotificationService.on_blocker_event(event)
        except Exception:
            logger.exception(
                "NotificationService.on_blocker_event failed for event %s", event.pk
            )
        return event


__all__ = [
    "WorkOrderBlockerService",
    "WorkOrderBlockerEventService",
]
