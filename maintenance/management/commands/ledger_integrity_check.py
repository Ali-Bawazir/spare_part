"""Verify that the WorkOrderCost cache matches the CostTransaction ledger.

Detects drift between the rolled-up cache (`WorkOrderCost.*_cost`) and
the append-only ledger (`CostTransaction.amount`). Drift usually means
a posting happened outside the service layer, a SQL was applied
directly, or the cache was rebuilt incorrectly.

Run with `--fix` to rebuild drifted WOs from the ledger.
"""
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db.models import Sum
from django.db.models.functions import Coalesce

from maintenance.models import CostCategory, CostTransaction, WorkOrderCost


class Command(BaseCommand):
    help = "Verify that the WorkOrderCost cache matches the CostTransaction ledger."

    def add_arguments(self, parser):
        parser.add_argument("--wo", type=int, default=None)
        parser.add_argument("--fix", action="store_true", help="Rebuild cache for drifted WOs")

    def handle(self, *args, **options):
        CATEGORY_TO_FIELD = {
            CostCategory.MATERIAL: "material_cost",
            CostCategory.VENDOR_REPAIR: "vendor_repair_cost",
            CostCategory.CONSUMABLE: "consumables_cost",
            CostCategory.ADJUSTMENT: "additional_cost",
        }
        qs = WorkOrderCost.objects.select_related("work_order")
        if options["wo"]:
            qs = qs.filter(work_order_id=options["wo"])
        drift_count = 0
        for wo_cost in qs:
            sums = (
                CostTransaction.objects
                .filter(work_order=wo_cost.work_order)
                .values("category")
                .annotate(total=Coalesce(Sum("amount"), Decimal("0")))
            )
            by_cat = {row["category"]: row["total"] for row in sums}
            for cat, field in CATEGORY_TO_FIELD.items():
                ledger_amount = by_cat.get(cat, Decimal("0"))
                cache_amount = getattr(wo_cost, field, Decimal("0"))
                if abs(ledger_amount - cache_amount) > Decimal("0.01"):
                    drift_count += 1
                    self.stdout.write(self.style.WARNING(
                        f"WO-{wo_cost.work_order.number} {field}: "
                        f"cache={cache_amount}, ledger={ledger_amount}, drift={cache_amount - ledger_amount}"
                    ))
                    if options["fix"]:
                        setattr(wo_cost, field, ledger_amount)
                        wo_cost.save(update_fields=[field, "last_reconciled_at"])
        if drift_count == 0:
            self.stdout.write(self.style.SUCCESS("No drift detected. All WOs are in sync."))
        else:
            self.stdout.write(self.style.WARNING(f"Found {drift_count} drifted WOs."))
