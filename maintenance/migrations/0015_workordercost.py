from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ('maintenance', '0014_add_rejection_fields'),
    ]

    operations = [
        migrations.CreateModel(
            name='WorkOrderCost',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('parts_cost', models.DecimalField(decimal_places=4, default=0, help_text='Sum of PartIssueLine unit_cost × qty', max_digits=14)),
                ('vendor_cost', models.DecimalField(decimal_places=4, default=0, help_text='Sum of ExternalRepairOrder.actual_cost', max_digits=14)),
                ('consumables_cost', models.DecimalField(decimal_places=4, default=0, help_text='StockMovement CONSUMABLE_USE for linked machine', max_digits=14)),
                ('additional_cost', models.DecimalField(decimal_places=4, default=0, max_digits=14)),
                ('additional_cost_note', models.CharField(blank=True, max_length=500)),
                ('work_order', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='cost_record', to='maintenance.workorder')),
            ],
            options={
                'verbose_name': 'Work Order Cost',
                'verbose_name_plural': 'Work Order Costs',
            },
        ),
    ]
