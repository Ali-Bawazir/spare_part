from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ('maintenance', '0013_attachment_thumbnail'),
        ('accounts', '0001_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='workorder',
            name='rejected_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='workorder',
            name='rejected_by',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='rejected_work_orders',
                to='accounts.user',
            ),
        ),
        migrations.AddField(
            model_name='workorder',
            name='rejection_reason',
            field=models.CharField(blank=True, max_length=500),
        ),
        migrations.AddField(
            model_name='workorder',
            name='rejection_count',
            field=models.PositiveIntegerField(default=0),
        ),
    ]