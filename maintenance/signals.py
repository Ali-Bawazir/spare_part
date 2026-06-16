"""
Signals for the WorkOrder blocker system (Phase 2A).

The reservation cache (Inventory.quantity_reserved) is a derived aggregate
of all ACTIVE InventoryReservation rows per (part, site). Whenever a
reservation is created, updated, or deleted, we recompute the cache.

See ADR-0007 sub-decision 8 — Part Issue 5-stage pipeline. The
`allocated_qty` field on PartIssueLine will eventually be the source of
truth for the per-WO allocation; the inventory aggregate stays as a
denormalized cache for fast "is anything reserved?" checks.
"""
from __future__ import annotations

from django.db.models import Sum
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from inventory.models import Inventory, InventoryReservation


@receiver(post_save, sender=InventoryReservation)
@receiver(post_delete, sender=InventoryReservation)
def refresh_inventory_quantity_reserved(sender, instance, **kwargs):
    """
    Maintain Inventory.quantity_reserved as a derived cache.

    Triggered on every InventoryReservation create/update/delete. Only
    ACTIVE reservations count toward the cache — RELEASED and CANCELLED
    rows are excluded.
    """
    part = instance.part
    active_sum = (
        InventoryReservation.objects
        .filter(
            part=part,
            status=InventoryReservation.Status.ACTIVE,
        )
        .aggregate(total=Sum("quantity"))["total"]
        or 0
    )
    Inventory.objects.filter(part=part).update(quantity_reserved=active_sum)
