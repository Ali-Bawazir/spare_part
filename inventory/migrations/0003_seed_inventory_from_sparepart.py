from django.db import migrations

def seed_inventory(apps, schema_editor):
    Site = apps.get_model("maintenance", "Site")
    SparePart = apps.get_model("inventory", "SparePart")
    Inventory = apps.get_model("inventory", "Inventory")
    
    site = Site.objects.filter(is_default=True).first()
    if not site:
        return
    
    for part in SparePart.objects.all():
        Inventory.objects.get_or_create(
            part=part,
            site=site,
            defaults={"quantity_available": part.quantity_on_hand or 0}
        )

def reverse_seed(apps, schema_editor):
    pass

class Migration(migrations.Migration):
    dependencies = [
        ("inventory", "0002_inventory_site_models"),
    ]
    operations = [
        migrations.RunPython(seed_inventory, reverse_seed),
    ]