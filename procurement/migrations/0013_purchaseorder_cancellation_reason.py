from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("procurement", "0012_supplier_supplier_type_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="purchaseorder",
            name="cancellation_reason",
            field=models.TextField(blank=True, default=""),
        ),
    ]
