from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("inventory", "0005_add_qr_code_to_sparepart"),
    ]
    operations = [
        migrations.AddField(
            model_name="inventory",
            name="last_counted_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="inventory",
            name="last_counted_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="inventory_counts",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
    ]