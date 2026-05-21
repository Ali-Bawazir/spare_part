from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("procurement", "0002_purchaserequest_work_order"),
    ]
    operations = [
        migrations.AddField(
            model_name="supplier",
            name="address",
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name="supplier",
            name="code",
            field=models.SlugField(
                blank=True, db_index=True, help_text="Unique supplier code e.g. SUP-001. Used in QR format SUPPLIER:{code}.", max_length=64, null=True, unique=True
            ),
        ),
        migrations.AddField(
            model_name="supplier",
            name="contact_person",
            field=models.CharField(blank=True, max_length=128),
        ),
        migrations.AddField(
            model_name="supplier",
            name="email",
            field=models.EmailField(blank=True, max_length=254),
        ),
        migrations.AddField(
            model_name="supplier",
            name="is_active",
            field=models.BooleanField(db_index=True, default=True),
        ),
        migrations.AddField(
            model_name="supplier",
            name="phone",
            field=models.CharField(blank=True, max_length=64),
        ),
    ]