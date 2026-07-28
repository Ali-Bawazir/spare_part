"""Recompute every WorkOrderCost cache row from the CostTransaction ledger.

Useful after a backfill, a manual SQL fix, or a deployment glitch. The
command is safe to re-run: it just reads the ledger and writes the cache.
"""
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db.models import Sum
from django.db.models.functions import Coalesce

from maintenance.models import CostTransaction, WorkOrderCost


class Command(BaseCommand):
    help = "Recompute every WorkOrderCost cache row from the CostTransaction ledger."

    def add_arguments(self, parser):
        parser.add_argument("--wo", type=int, default=None, help="Limit to one WO")

    def handle(self, *args, **options):
        qs = WorkOrderCost.objects.all()
        if options["wo"]:
            qs = qs.filter(work_order_id=options["wo"])
        rebuilt = 0
        for wo_cost in qs.iterator():
            sums = (
                CostTransaction.objects
                .filter(work_order=wo_cost.work_order)
                .values("category")
                .annotate(total=Coalesce(Sum("amount"), Decimal("0")))
            )
            by_cat = {row["category"]: row["total"] for row in sums}
            wo_cost.material_cost      = by_cat.get("material", Decimal("0"))
            wo_cost.vendor_repair_cost = by_cat.get("vendor_repair", Decimal("0"))
            wo_cost.consumables_cost   = by_cat.get("consumable", Decimal("0"))
            wo_cost.additional_cost    = by_cat.get("adjustment", Decimal("0"))
            wo_cost.ledger_transaction_count = CostTransaction.objects.filter(
                work_order=wo_cost.work_order,
            ).count()
            wo_cost.save(update_fields=[
                "material_cost", "vendor_repair_cost", "consumables_cost",
                "additional_cost", "ledger_transaction_count", "last_reconciled_at",
            ])
            rebuilt += 1
        self.stdout.write(self.style.SUCCESS(f"Rebuilt {rebuilt} WO cost cache rows."))
