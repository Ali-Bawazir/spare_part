from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


class Migration(migrations.Migration):
    dependencies = [
        ("procurement", "0004_seed_supplier_codes"),
        ("maintenance", "0013_attachment_thumbnail"),
    ]

    operations = [
        migrations.CreateModel(
            name="PurchaseOrder",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("po_number", models.CharField(editable=False, max_length=12, unique=True)),
                ("invoice_ref", models.CharField(blank=True, max_length=120)),
                ("expected_delivery", models.DateField(blank=True, null=True)),
                ("status", models.CharField(
                    choices=[
                        ("draft", "Draft"),
                        ("sent", "Sent to supplier"),
                        ("partial", "Partial received"),
                        ("received", "Fully received"),
                        ("closed_short", "Closed short"),
                        ("cancelled", "Cancelled"),
                    ],
                    db_index=True,
                    default="draft",
                    max_length=20,
                )),
                ("notes", models.TextField(blank=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("created_by", models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name="purchase_orders_created",
                    to="accounts.user",
                )),
                ("handled_by", models.ForeignKey(
                    blank=True,
                    null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name="purchase_orders_handled",
                    to="accounts.user",
                )),
                ("supplier", models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name="purchase_orders",
                    to="procurement.supplier",
                )),
            ],
            options={
                "ordering": ["-created_at"],
            },
        ),
        migrations.CreateModel(
            name="PurchaseOrderItem",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("ordered_qty", models.DecimalField(decimal_places=3, max_digits=14)),
                ("received_qty", models.DecimalField(default=0, decimal_places=3, max_digits=14)),
                ("unit_price", models.DecimalField(default=0, decimal_places=4, max_digits=12)),
                ("total_price", models.DecimalField(default=0, decimal_places=4, max_digits=14)),
                ("part", models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name="po_items",
                    to="inventory.sparepart",
                )),
                ("purchase_order", models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="items",
                    to="procurement.purchaseorder",
                )),
            ],
            options={
                "verbose_name_plural": "purchase order items",
            },
        ),
        migrations.AlterField(
            model_name="purchaserequest",
            name="status",
            field=models.CharField(
                choices=[
                    ("pending", "Pending officer"),
                    ("converted_to_po", "Converted to PO"),
                    ("partially_fulfilled", "Partially fulfilled"),
                    ("fulfilled", "Fulfilled"),
                    ("cancelled", "Cancelled"),
                ],
                db_index=True,
                default="pending",
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="purchaserequest",
            name="purchase_order",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="purchase_requests",
                to="procurement.purchaseorder",
            ),
        ),
    ]