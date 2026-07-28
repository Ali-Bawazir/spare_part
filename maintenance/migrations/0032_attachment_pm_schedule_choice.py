"""Extend Attachment.EntityType choices with PM_SCHEDULE."""
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("maintenance", "0031_unique_asset_code"),
    ]
    operations = [
        migrations.AlterField(
            model_name="attachment",
            name="entity_type",
            field=models.CharField(
                choices=[
                    ("work_order", "Work Order"),
                    ("machine", "Machine"),
                    ("spare_part", "Spare Part"),
                    ("purchase_request", "Purchase Request"),
                    ("purchase_order", "Purchase Order"),
                    ("maintenance_issue", "Maintenance Issue"),
                    ("stock_movement", "Stock Movement"),
                    ("repair_order", "Repair Order"),
                    ("consumable_assignment", "Consumable Assignment"),
                    ("pm_schedule", "Pm Schedule"),
                ],
                max_length=32,
            ),
        ),
    ]
