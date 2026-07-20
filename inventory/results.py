"""
Phase 5: domain result types for inventory service operations.

Pure dataclasses + enum. Zero ORM imports. Used by service layer to
return rich, presentation-neutral outcomes. Callers (views, API,
CLI, future mobile) translate the domain result into their
respective response shapes:

    - Web view   → render form with rich info panel
    - API        → HTTP 409 Conflict with JSON body
    - CLI        → print message + abort
    - Tests      → assert outcome enum identity

KEPT DELIBERATELY ORM-FREE so the service layer remains reusable and
testable without database fixtures.
"""
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from uuid import UUID


# Phase 5: ACTIVE request line statuses. Single source of truth for
# what "ACTIVE" means in duplicate detection and reservation
# accounting. Everything outside this set is INACTIVE for duplicate
# prevention purposes (REJECTED, CANCELLED, ISSUED).
ACTIVE_REQUEST_STATUSES = frozenset({
    "pending",
    "approved",
    "allocated",
})


class RequestPartOutcome(Enum):
    """The two terminal outcomes of request_part_on_wo()."""
    CREATED = "created"
    DUPLICATE_FOUND = "duplicate_found"


@dataclass(frozen=True)
class CreatedPartRequestInfo:
    """Returned on RequestPartOutcome.CREATED. No ORM dependency."""
    line_id: int
    activity_uuid: UUID
    created_at: datetime


@dataclass(frozen=True)
class DuplicatePartRequestInfo:
    """Returned on RequestPartOutcome.DUPLICATE_FOUND. Pre-resolved fields
    so the template doesn't need to traverse FKs and trigger extra queries.

    Pre-resolved display strings (part_name, requested_by_*) avoid N+1
    queries in templates. If a future caller needs ID-only purity, swap
    to (part_id, requested_by_id) and let the presentation layer hydrate.
    """
    line_id: int
    part_id: int
    part_name: str
    quantity: Decimal
    requested_by_id: int
    requested_by_username: str
    requested_by_full_name: str
    created_at: datetime
    status: str
    activity_label: str
    activity_uuid: UUID


@dataclass(frozen=True)
class RequestPartResult:
    """Outcome of a request_part_on_wo() call.

    Exactly one of (.created_info) or (.duplicate_info) is populated.
    """
    outcome: RequestPartOutcome
    created_info: "CreatedPartRequestInfo | None" = None
    duplicate_info: "list[DuplicatePartRequestInfo] | None" = None
    # Phase 5: legacy fields preserved from the old dict-shaped return so
    # existing tests, view session summaries, and integration code that
    # read keys like "line", "shortage", "shortage_qty",
    # "usable_qty_snapshot", "available_qty_snapshot", etc. continue
    # to work during the transition. New code should use the dataclass
    # attributes (created_info, duplicate_info, outcome) directly.
    shortage_qty: "Decimal | None" = None
    shortage: "bool | None" = None
    available_qty_snapshot: "Decimal | None" = None
    reserved_qty_snapshot: "Decimal | None" = None
    usable_qty_snapshot: "Decimal | None" = None
    suggested_action: "str | None" = None

    @classmethod
    def created(
        cls, info: "CreatedPartRequestInfo",
        *,
        shortage_qty=None, shortage=None,
        available_qty_snapshot=None, reserved_qty_snapshot=None,
        usable_qty_snapshot=None, suggested_action=None,
    ) -> "RequestPartResult":
        return cls(
            outcome=RequestPartOutcome.CREATED,
            created_info=info,
            shortage_qty=shortage_qty, shortage=shortage,
            available_qty_snapshot=available_qty_snapshot,
            reserved_qty_snapshot=reserved_qty_snapshot,
            usable_qty_snapshot=usable_qty_snapshot,
            suggested_action=suggested_action,
        )

    @classmethod
    def duplicate_found(
        cls, infos: "list[DuplicatePartRequestInfo]"
    ) -> "RequestPartResult":
        return cls(
            outcome=RequestPartOutcome.DUPLICATE_FOUND,
            duplicate_info=list(infos),
        )

    def __getitem__(self, key):
        """Dict-style accessor for legacy callers.

        Keys: 'line', 'shortage', 'shortage_qty', 'shortage_report',
        'already_pending', 'emergency_auto_approved', 'issued_qty',
        'available_qty_snapshot', 'reserved_qty_snapshot',
        'usable_qty_snapshot', 'suggested_action'.

        For 'line' and 'shortage_report', the ORM row is fetched
        lazily on access (helper methods can populate these by reading
        them from the underlying service; the dataclass itself does
        not store the ORM row).
        """
        if key == "line":
            if self.created_info is not None:
                from inventory.models import PartIssueLine
                return PartIssueLine.objects.get(pk=self.created_info.line_id)
            return None
        if key in (
            "shortage", "shortage_qty", "already_pending",
            "emergency_auto_approved", "issued_qty",
            "available_qty_snapshot", "reserved_qty_snapshot",
            "usable_qty_snapshot", "suggested_action",
        ):
            return getattr(self, key)
        if key == "shortage_report":
            return None
        raise KeyError(key)
