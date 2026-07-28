from django.db import migrations

def seed_default_site(apps, schema_editor):
    Site = apps.get_model("maintenance", "Site")
    if not Site.objects.exists():
        Site.objects.create(
            name="Main Factory",
            code="MF",
            is_default=True,
            is_active=True,
        )

def reverse_seed(apps, schema_editor):
    pass

class Migration(migrations.Migration):
    dependencies = [
        ("maintenance", "0004_inventory_site_models"),
    ]
    operations = [
        migrations.RunPython(seed_default_site, reverse_seed),
    ]