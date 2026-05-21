from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("inventory", "0006_add_last_counted_to_inventory"),
    ]
    operations = [
        migrations.AddField(
            model_name="sparepart",
            name="last_purchase_cost",
            field=models.DecimalField(blank=True, decimal_places=4, max_digits=12, null=True),
        ),
        migrations.AddField(
            model_name="sparepart",
            name="status",
            field=models.CharField(choices=[("active", "Active"), ("obsolete", "Obsolete"), ("discontinued", "Discontinued")], db_index=True, default="active", max_length=20),
        ),
        migrations.AddField(
            model_name="sparepart",
            name="supplier",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="parts", to="procurement.supplier"),
        ),
    ]
