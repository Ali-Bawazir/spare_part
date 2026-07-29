# Phase 1 v1.0.0 — wipe seed/sample data for production baseline.
#
# The seed migration inventory.0010_seed_operator_consumables and
# maintenance.0045_pm_schedule_template_notnull both created sample
# consumables and PM templates on first deploy. Production should start
# empty; operators/manager create real data through the UI.
#
# This migration deletes:
#   - SpareParts: TAPE-ELEC, OIL-ENGINE, GREASE-5L, CLEANER-01, GLOVES-N, ZIP-100
#   - PMTemplate + PMSchedule + PMExecution + PMChecklistItem (all rows)
#
# Cascades handle Inventory, StockMovement, PartIssueLine, etc. that
# reference the deleted SpareParts.
#
# Idempotent — re-running on a clean DB is a no-op.

from django.db import migrations


SEED_SKUS = [
    "TAPE-ELEC",
    "OIL-ENGINE",
    "GREASE-5L",
    "CLEANER-01",
    "GLOVES-N",
    "ZIP-100",
]


def wipe_seed_data(apps, schema_editor):
    SparePart = apps.get_model("inventory", "SparePart")
    PMTemplate = apps.get_model("maintenance", "PMTemplate")
    PMSchedule = apps.get_model("maintenance", "PMSchedule")
    PMChecklistItem = apps.get_model("maintenance", "PMChecklistItem")
    PMExecution = apps.get_model("maintenance", "PMExecution")

    # Delete PM scheduling chain first (child → parent)
    PMExecution.objects.all().delete()
    PMChecklistItem.objects.all().delete()
    PMSchedule.objects.all().delete()
    PMTemplate.objects.all().delete()

    # Delete seed SpareParts (Inventory/StockMovement cascade via FK)
    SparePart.objects.filter(sku__in=SEED_SKUS).delete()


def reverse_noop(apps, schema_editor):
    # No reverse — fresh production DBs must stay empty
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("inventory", "0024_stockmovement_stockmv_part_created_idx"),
        ("maintenance", "0056_uniq_default_site"),
    ]

    operations = [
        migrations.RunPython(wipe_seed_data, reverse_noop),
    ]