"""Small services for the reusable-tool pool.

One verb per service function. Each is wrapped in ``transaction.atomic``
and locks the relevant rows with ``select_for_update``. ToolMovement
audit rows are written at the end of every successful transition.

Public surface:
    InventoryService.issue_to_tool_pool(part, qty, actor, note)
    ToolAssignmentService.assign(instance, operator, machine, condition_out, actor, notes)
    ToolAssignmentService.return_tool(assignment, condition_in, actor, damage_reason=None, notes)
    ToolDamageService.report(instance, reason, machine, actor, assignment=None)
    ToolDamageService.repair(report, repair_cost, actor)
    ToolDamageService.write_off(report, actor)
"""
from decimal import Decimal
from typing import Optional

from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import F, Max
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from inventory.models import (
    Inventory,
    ReusableToolInstance,
    SparePart,
    StockMovement,
)
from inventory.models_tools import (
    ToolAssignment,
    ToolDamageReport,
    ToolMovement,
)

User = get_user_model()


# ---------------------------------------------------------------------------
# Inventory
# ---------------------------------------------------------------------------

class InventoryService:
    """Inventory-level mutations. Reusable tools are issued here."""

    @staticmethod
    @transaction.atomic
    def issue_to_tool_pool(
        part: SparePart,
        qty,
        actor,
        site=None,
        note: str = "",
    ):
        """Move ``qty`` units of ``part`` from inventory into the tool pool.

        Inventory decreases by qty. ``qty`` new ``ReusableToolInstance``
        rows are created with sequential tool numbers. One
        StockMovement(STOCK_OUT) row records the source-of-truth
        cost/date/supplier link. One ToolMovement(issued) row per
        instance is written for the audit log.

        Args:
            part: must have item_type=reusable_tool
            qty: positive integer (number of physical units)
            actor: User performing the action
            site: inventory site to draw from (default site if None)
            note: free-text note stored on the StockMovement

        Returns:
            list[ReusableToolInstance] in tool_number order
        """
        qty = int(qty)
        if qty <= 0:
            raise ValueError(_("Quantity must be positive."))
        if part.item_type != SparePart.ItemType.REUSABLE_TOOL:
            raise ValueError(_("Only reusable-tool parts can be issued to the tool pool."))

        from maintenance.models import Site as _Site
        if site is None:
            site = _Site.objects.filter(is_default=True).first()
            if site is None:
                site = _Site.objects.order_by("pk").first()
        if site is None:
            raise ValueError(_("No site configured."))

        inv = (
            Inventory.objects
            .select_for_update()
            .filter(part=part, site=site)
            .first()
        )
        if inv is None:
            raise ValueError(_("No inventory record for this part at this site."))
        if inv.quantity_available < Decimal(qty):
            raise ValueError(
                _("Insufficient stock: %(have)s available, %(need)s requested.") % {
                    "have": inv.quantity_available,
                    "need": qty,
                }
            )

        before = inv.quantity_available
        inv.quantity_available = F("quantity_available") - Decimal(qty)
        inv.save(update_fields=["quantity_available", "updated_at"])
        inv.refresh_from_db(fields=["quantity_available"])
        after = inv.quantity_available

        movement = StockMovement.objects.create(
            part=part,
            movement_type=StockMovement.MovementType.STOCK_OUT,
            quantity=Decimal(qty),
            quantity_before=before,
            quantity_after=after,
            site=site,
            performed_by=actor,
            supplier=part.supplier,
            supplier_name=part.supplier.name if part.supplier else "",
            unit_cost=part.last_purchase_cost,
            invoice_ref="",
            reference={"reason": "issue_to_tool_pool"},
            note=note[:500],
        )

        existing_max = (
            part.tool_instances.aggregate(m=Max("tool_number"))["m"] or 0
        )
        start = int(existing_max) + 1

        instances = []
        for n in range(start, start + qty):
            inst = ReusableToolInstance.objects.create(
                part=part,
                tool_number=n,
                status=ReusableToolInstance.Status.AVAILABLE,
                source_stock_movement=movement,
            )
            ToolMovement.objects.create(
                instance=inst,
                movement_type=ToolMovement.MovementType.ISSUED,
                actor=actor,
                note=note[:500],
            )
            instances.append(inst)
        return instances


# ---------------------------------------------------------------------------
# Tool Assignment
# ---------------------------------------------------------------------------

class ToolAssignmentService:
    """Assignment-level mutations. One assignment per checkout."""

    @staticmethod
    @transaction.atomic
    def assign(
        instance: ReusableToolInstance,
        operator,
        machine,
        condition_out: str,
        actor,
        notes: str = "",
    ) -> ToolAssignment:
        """Open a new assignment for an available instance."""
        instance = ReusableToolInstance.objects.select_for_update().get(pk=instance.pk)
        if not instance.is_active:
            raise ValueError(_("Tool is inactive."))
        if instance.status != ReusableToolInstance.Status.AVAILABLE:
            raise ValueError(
                _("Tool is not available (current status: %(status)s).") % {
                    "status": instance.get_status_display(),
                }
            )
        if instance.active_assignment is not None:
            raise ValueError(_("Tool already has an open assignment."))

        a = ToolAssignment.objects.create(
            instance=instance,
            operator=operator,
            machine=machine,
            checkout_at=timezone.now(),
            condition_out=condition_out,
            notes=notes[:500],
        )
        instance.status = ReusableToolInstance.Status.IN_USE
        instance.save(update_fields=["status"])

        ToolMovement.objects.create(
            instance=instance,
            movement_type=ToolMovement.MovementType.ASSIGNED,
            actor=actor,
            machine=machine,
            assignment=a,
            note=notes[:500],
        )
        return a

    @staticmethod
    @transaction.atomic
    def return_tool(
        assignment: ToolAssignment,
        condition_in: str,
        actor,
        damage_reason: Optional[str] = None,
        notes: str = "",
    ):
        """Close an assignment. If condition_in=damaged, also open a damage report.

        Returns:
            (assignment, damage_report or None)
        """
        a = ToolAssignment.objects.select_for_update().get(pk=assignment.pk)
        if a.return_at is not None:
            raise ValueError(_("Assignment is already closed."))
        if not condition_in:
            raise ValueError(_("Return condition is required."))

        instance = ReusableToolInstance.objects.select_for_update().get(pk=a.instance_id)
        a.return_at = timezone.now()
        a.condition_in = condition_in
        if notes:
            a.notes = (a.notes + "\n" if a.notes else "") + notes[:500]
        a.save(update_fields=["return_at", "condition_in", "notes"])

        damage_report = None
        if condition_in == ToolAssignment.Condition.DAMAGED:
            if not damage_reason or not damage_reason.strip():
                raise ValueError(_("Damage reason is required when returning damaged."))
            instance.status = ReusableToolInstance.Status.OUT_OF_SERVICE
            instance.save(update_fields=["status"])
            damage_report = ToolDamageReport.objects.create(
                instance=instance,
                reported_by=a.operator,
                machine=a.machine,
                assignment=a,
                damage_date=timezone.now(),
                reason=damage_reason.strip(),
                status=ToolDamageReport.Status.OPEN,
            )
            ToolMovement.objects.create(
                instance=instance,
                movement_type=ToolMovement.MovementType.DAMAGED,
                actor=actor,
                machine=a.machine,
                assignment=a,
                damage_report=damage_report,
                note=damage_reason[:500],
            )
        else:
            instance.status = ReusableToolInstance.Status.AVAILABLE
            instance.save(update_fields=["status"])

        ToolMovement.objects.create(
            instance=instance,
            movement_type=ToolMovement.MovementType.RETURNED,
            actor=actor,
            machine=a.machine,
            assignment=a,
            note=notes[:500],
        )
        return a, damage_report


# ---------------------------------------------------------------------------
# Damage
# ---------------------------------------------------------------------------

class ToolDamageService:
    """Damage lifecycle: report, repair, write off."""

    @staticmethod
    @transaction.atomic
    def report(
        instance: ReusableToolInstance,
        reason: str,
        machine,
        actor,
        assignment: Optional[ToolAssignment] = None,
    ) -> ToolDamageReport:
        instance = ReusableToolInstance.objects.select_for_update().get(pk=instance.pk)
        if not reason or not reason.strip():
            raise ValueError(_("Damage reason is required."))
        report = ToolDamageReport.objects.create(
            instance=instance,
            reported_by=actor,
            machine=machine,
            assignment=assignment,
            damage_date=timezone.now(),
            reason=reason.strip(),
            status=ToolDamageReport.Status.OPEN,
        )
        instance.status = ReusableToolInstance.Status.OUT_OF_SERVICE
        instance.save(update_fields=["status"])
        ToolMovement.objects.create(
            instance=instance,
            movement_type=ToolMovement.MovementType.DAMAGED,
            actor=actor,
            machine=machine,
            assignment=assignment,
            damage_report=report,
            note=reason[:500],
        )
        return report

    @staticmethod
    @transaction.atomic
    def repair(report: ToolDamageReport, repair_cost, actor) -> ToolDamageReport:
        report = ToolDamageReport.objects.select_for_update().get(pk=report.pk)
        if report.status != ToolDamageReport.Status.OPEN:
            raise ValueError(_("Only open reports can be repaired."))
        if repair_cost is None:
            raise ValueError(_("Repair cost is required."))
        if Decimal(repair_cost) < 0:
            raise ValueError(_("Repair cost cannot be negative."))

        instance = ReusableToolInstance.objects.select_for_update().get(pk=report.instance_id)
        report.status = ToolDamageReport.Status.REPAIRED
        report.repair_cost = Decimal(repair_cost)
        report.resolved_at = timezone.now()
        report.resolved_by = actor
        report.save(update_fields=["status", "repair_cost", "resolved_at", "resolved_by"])

        if instance.status == ReusableToolInstance.Status.OUT_OF_SERVICE:
            instance.status = ReusableToolInstance.Status.AVAILABLE
            instance.save(update_fields=["status"])

        ToolMovement.objects.create(
            instance=instance,
            movement_type=ToolMovement.MovementType.REPAIRED,
            actor=actor,
            damage_report=report,
        )
        return report

    @staticmethod
    @transaction.atomic
    def write_off(report: ToolDamageReport, actor) -> ToolDamageReport:
        report = ToolDamageReport.objects.select_for_update().get(pk=report.pk)
        if report.status != ToolDamageReport.Status.OPEN:
            raise ValueError(_("Only open reports can be written off."))
        instance = ReusableToolInstance.objects.select_for_update().get(pk=report.instance_id)
        report.status = ToolDamageReport.Status.WRITTEN_OFF
        report.resolved_at = timezone.now()
        report.resolved_by = actor
        report.save(update_fields=["status", "resolved_at", "resolved_by"])

        instance.is_active = False
        instance.status = ReusableToolInstance.Status.OUT_OF_SERVICE
        instance.save(update_fields=["is_active", "status"])

        ToolMovement.objects.create(
            instance=instance,
            movement_type=ToolMovement.MovementType.WRITTEN_OFF,
            actor=actor,
            damage_report=report,
        )
        return report
