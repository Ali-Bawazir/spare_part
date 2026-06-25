"""
Signals for the WorkOrder blocker system (Phase 2A).

See ADR-0007 sub-decision 8 — Part Issue 5-stage pipeline. The
`allocated_qty` field on PartIssueLine will eventually be the source of
truth for the per-WO allocation; the inventory aggregate stays as a
denormalized cache for fast "is anything reserved?" checks.
"""
from __future__ import annotations

from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from inventory.models import InventoryReservation
from maintenance.models import WorkOrder, WorkOrderBlocker


@receiver(post_save, sender=InventoryReservation)
@receiver(post_delete, sender=InventoryReservation)
def refresh_inventory_quantity_reserved(sender, instance, **kwargs):
    """
    No-op signal retained as a hook for future Inventory aggregate caches.

    The legacy `Inventory.quantity_reserved` field has been dropped
    (migration 0017). The live reserved quantity is now computed on
    demand via `Inventory.compute_quantity_reserved()` (sum of ACTIVE
    InventoryReservation rows). This signal is kept as a placeholder
    so future per-site reservation aggregates can be wired in here.
    """
    return None


@receiver(post_save, sender=WorkOrderBlocker)
def bump_wo_to_v1_on_first_blocker(sender, instance, created, **kwargs):
    """
    Bump a legacy (v0) WorkOrder to v1 the first time a WO Blocker is
    opened on it. New WOs already start at v1 via WorkOrder.save(); this
    handler covers pre-migration legacy WOs that get their first blocker.

    Idempotent: only updates rows still at v0. We use .update() so we
    don't recurse into the WorkOrder post_save signal.
    """
    if not created:
        return
    WorkOrder.objects.filter(
        pk=instance.work_order_id, blocker_system_version=0
    ).update(blocker_system_version=1)