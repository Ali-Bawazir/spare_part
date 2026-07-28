from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('maintenance', '0012_rbac_admin_lockdown'),
    ]

    operations = [
        migrations.AddField(
            model_name='attachment',
            name='height',
            field=models.PositiveIntegerField(default=0, help_text='Original image height in px'),
        ),
        migrations.AddField(
            model_name='attachment',
            name='thumbnail',
            field=models.ImageField(blank=True, help_text='Auto-generated 300px thumbnail', upload_to='attachment_thumbnail_path'),
        ),
        migrations.AddField(
            model_name='attachment',
            name='width',
            field=models.PositiveIntegerField(default=0, help_text='Original image width in px'),
        ),
        migrations.AlterField(
            model_name='attachment',
            name='entity_type',
            field=models.CharField(
                choices=[
                    ('work_order', 'Work Order'),
                    ('machine', 'Machine'),
                    ('spare_part', 'Spare Part'),
                    ('purchase_request', 'Purchase Request'),
                    ('purchase_order', 'Purchase Order'),
                    ('maintenance_issue', 'Maintenance Issue'),
                    ('stock_movement', 'Stock Movement'),
                    ('repair_order', 'Repair Order'),
                ],
                max_length=32,
            ),
        ),
    ]