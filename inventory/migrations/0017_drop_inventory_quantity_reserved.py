"""Drop deprecated Inventory.quantity_reserved cache field.

The live reserved quantity is now computed on demand via
Inventory.compute_quantity_reserved() (sum of ACTIVE InventoryReservation rows).
"""
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("inventory", "0016_inventory_quantity_quarantine_and_more"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="inventory",
            name="quantity_reserved",
        ),
    ]