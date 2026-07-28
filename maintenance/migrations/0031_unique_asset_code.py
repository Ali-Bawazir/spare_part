"""Add unique constraint to Machine.asset_code.

Step 1: Generate unique asset codes for any machines that have an empty one.
Step 2: Dedupe any remaining duplicates by appending a counter.
Step 3: Apply unique=True to the field.
"""
from django.db import migrations, models


def populate_unique_asset_codes(apps, schema_editor):
    """Populate empty asset_codes using the auto-generation logic, dedupe any collisions."""
    Machine = apps.get_model("maintenance", "Machine")

    def generate_code(machine):
        """Walk the parent chain and build a hierarchical asset code."""
        parts = []
        current = machine
        while current is not None:
            # Use pk-based identifier (we don't have qr_code guaranteed to be unique)
            parts.insert(0, f"M{current.pk}")
            current = current.parent
        return "-".join(parts) or f"M{machine.pk}"

    # First pass: populate empty codes
    for machine in Machine.objects.filter(asset_code=""):
        machine.asset_code = generate_code(machine)
        machine.save(update_fields=["asset_code"])

    # Second pass: dedupe any remaining collisions (appending counter)
    seen = {}
    for machine in Machine.objects.exclude(asset_code="").order_by("pk"):
        code = machine.asset_code
        if code in seen:
            counter = 2
            new_code = f"{code}-{counter}"
            while new_code in seen:
                counter += 1
                new_code = f"{code}-{counter}"
            machine.asset_code = new_code
            machine.save(update_fields=["asset_code"])
            seen[new_code] = machine
        else:
            seen[code] = machine


def reverse_populate(apps, schema_editor):
    """Reverse: clear asset_code on machines that got auto-generated codes (best-effort)."""
    # We don't have a way to know which were auto-generated, so no-op.
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("maintenance", "0030_externalrepairorder_component_and_more"),
    ]
    operations = [
        migrations.RunPython(populate_unique_asset_codes, reverse_populate),
        migrations.AlterField(
            model_name="machine",
            name="asset_code",
            field=models.CharField(
                blank=True,
                default="",
                help_text="Hierarchical asset code (e.g. FM-01-CONV-BRG-001). Unique.",
                max_length=128,
                unique=True,
            ),
        ),
    ]
