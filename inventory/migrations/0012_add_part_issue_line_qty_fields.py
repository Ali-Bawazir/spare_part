from django.db import migrations, models


def backfill_qty_fields(apps, schema_editor):
    """Backfill the new qty fields on existing PartIssueLine rows.

    Logic:
    - Existing APPROVED lines were created via the legacy direct-issue flow
      (or via emergency auto-approve). The legacy `quantity` field IS what
      was issued. Set approved_qty = issued_qty = quantity, shortage = 0.
    - Existing PENDING lines: set everything to 0 (manager will fill on approve).
    - Existing REJECTED lines: set everything to 0.
    """
    PartIssueLine = apps.get_model("inventory", "PartIssueLine")
    for line in PartIssueLine.objects.all():
        if line.status == "approved":
            line.approved_qty = line.quantity
            line.issued_qty = line.quantity
            line.shortage_qty = 0
        if not line.requested_qty:
            line.requested_qty = line.quantity
        line.save(update_fields=["approved_qty", "issued_qty", "shortage_qty", "requested_qty"])


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("inventory", "0011_add_part_issue_line_hybrid_approval"),
    ]

    operations = [
        migrations.AddField(
            model_name="partissueline",
            name="requested_qty",
            field=models.DecimalField(
                decimal_places=3, default=0, max_digits=14,
                help_text=(
                    "What the technician originally requested. Mirrors `quantity` at "
                    "request time. Preserved through edits so audit trail is intact."
                ),
            ),
        ),
        migrations.AddField(
            model_name="partissueline",
            name="approved_qty",
            field=models.DecimalField(
                decimal_places=3, default=0, max_digits=14,
                help_text=(
                    "Quantity the manager approved (may differ from requested_qty). "
                    "Set on approval. 0 while PENDING."
                ),
            ),
        ),
        migrations.AddField(
            model_name="partissueline",
            name="issued_qty",
            field=models.DecimalField(
                decimal_places=3, default=0, max_digits=14,
                help_text=(
                    "Quantity actually deducted from stock on approval. May be less "
                    "than approved_qty if stock ran out between request and approval."
                ),
            ),
        ),
        migrations.AddField(
            model_name="partissueline",
            name="shortage_qty",
            field=models.DecimalField(
                decimal_places=3, default=0, max_digits=14,
                help_text=(
                    "Quantity covered by an auto-created PurchaseRequest. Computed as "
                    "max(0, requested_qty - approved_qty). Independent of whether the "
                    "manager edits approved_qty — PR is a separate procurement doc."
                ),
            ),
        ),
        migrations.RunPython(backfill_qty_fields, noop_reverse),
    ]
