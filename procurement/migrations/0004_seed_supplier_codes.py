from django.db import migrations


def seed_codes(apps, schema_editor):
    Supplier = apps.get_model("procurement", "Supplier")
    count = Supplier.objects.filter(code__isnull=True).count()
    for i, supplier in enumerate(Supplier.objects.filter(code__isnull=True).order_by("pk"), start=1):
        supplier.code = f"SUP-{i:03d}"
        supplier.save()


def reverse_code(apps, schema_editor):
    pass  # no rollback needed


class Migration(migrations.Migration):
    dependencies = [
        ("procurement", "0003_extend_supplier_fields"),
    ]
    operations = [
        migrations.RunPython(seed_codes, reverse_code),
    ]