from django.db import migrations


class Migration(migrations.Migration):
    """Drop legacy Tool / ToolAssignment / ToolDamageRecord / Incident tables.

    These were the Phase-1 reusable-tool tables. They have been replaced
    by ``inventory.ReusableToolInstance`` + ``inventory.models_tools``
    (ToolAssignment, ToolDamageReport, ToolMovement) under the inventory
    app. This migration removes the legacy tables to keep the schema
    clean.
    """

    dependencies = [
        ("procurement", "0018_remove_purchaseorderitem_tool"),
        ("maintenance", "0054_phase5_activity_uuid"),
    ]

    operations = [
        # WorkOrder.tool FK (legacy Tool ref on WOs created for damaged-tool repair)
        migrations.RemoveField(
            model_name="workorder",
            name="tool",
        ),
        # Delete models. State operations keep Django's internal model
        # registry consistent so later migrations can reference fields
        # that no longer exist.
        migrations.DeleteModel("Incident"),
        migrations.DeleteModel("ToolDamageRecord"),
        migrations.DeleteModel("ToolAssignment"),
        migrations.DeleteModel("Tool"),
    ]
