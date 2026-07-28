"""Backfill the cost ledger from existing WorkOrderCost cache rows.

One-shot migration utility. Scans every WorkOrderCost row and posts one
CostTransaction per non-zero category. The backfilled rows are tagged
with the `[migration_backfill]` memo prefix so the command is idempotent
and won't double-post on re-runs.
"""
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction

from maintenance.models import CostCategory, CostTransaction, WorkOrderCost


class Command(BaseCommand):
    help = "Backfill the cost ledger from existing WorkOrderCost cache rows."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        categories = [
            ("material_cost", CostCategory.MATERIAL),
            ("vendor_repair_cost", CostCategory.VENDOR_REPAIR),
            ("consumables_cost", CostCategory.CONSUMABLE),
            ("additional_cost", CostCategory.ADJUSTMENT),
        ]
        count = 0
        for wo_cost in WorkOrderCost.objects.select_related("work_order").iterator():
            wo = wo_cost.work_order
            # Skip WOs that already have ledger entries (the ledger is
            # the source of truth — if it has entries, the cache is already
            # in sync or newer than the legacy data).
            if CostTransaction.objects.filter(work_order=wo).exists():
                continue
            for field_name, category in categories:
                amount = getattr(wo_cost, field_name, Decimal("0"))
                if amount == 0 or amount is None:
                    continue
                # Skip if we already backfilled this WO + category
                existing = CostTransaction.objects.filter(
                    work_order=wo, category=category,
                    memo__startswith="[migration_backfill]",
                ).exists()
                if existing:
                    continue
                if dry_run:
                    self.stdout.write(f"WO-{wo.number}: would post {amount} {category}")
                    count += 1
                    continue
                with transaction.atomic():
                    CostTransaction.objects.create(
                        amount=amount,
                        category=category,
                        currency="SAR",
                        quantity=None, unit_cost=None,
                        work_order=wo,
                        machine=wo.machine,
                        component=wo.component,
                        source_type="cost_adjustment",
                        source_id=0,  # sentinel for backfilled
                        is_reversal=False, supersedes=None,
                        actor=None,
                        memo="[migration_backfill] Historical cost migrated from WorkOrderCost cache",
                    )
                count += 1
        verb = "Would post" if dry_run else "Posted"
        self.stdout.write(self.style.SUCCESS(f"{verb} {count} backfill ledger entries."))
