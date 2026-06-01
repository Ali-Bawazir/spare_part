from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('maintenance', '0015_workordercost'),
    ]

    operations = [
        migrations.AddField(
            model_name='notification',
            name='is_critical',
            field=models.BooleanField(db_index=True, default=False),
        ),
    ]