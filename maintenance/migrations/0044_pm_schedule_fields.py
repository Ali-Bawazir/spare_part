import django.utils.timezone
from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("maintenance", "0043_pm_template_models"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="pmschedule",
            name="checklist",
        ),
        migrations.RemoveField(
            model_name="pmschedule",
            name="frequency_days",
        ),
        migrations.RemoveField(
            model_name="pmschedule",
            name="title",
        ),
        migrations.AddField(
            model_name="pmschedule",
            name="auto_generate_wo",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="pmschedule",
            name="created_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="pm_schedules_created",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="pmschedule",
            name="estimated_duration_override",
            field=models.PositiveIntegerField(
                blank=True,
                help_text="If null, fall back to template.estimated_duration_minutes",
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="pmschedule",
            name="frequency_type",
            field=models.CharField(
                choices=[
                    ("daily", "Daily"),
                    ("weekly", "Weekly"),
                    ("monthly", "Monthly"),
                    ("yearly", "Yearly"),
                ],
                default="monthly",
                max_length=16,
            ),
        ),
        migrations.AddField(
            model_name="pmschedule",
            name="grace_days",
            field=models.PositiveIntegerField(default=7),
        ),
        migrations.AddField(
            model_name="pmschedule",
            name="interval",
            field=models.PositiveIntegerField(
                default=1, help_text="e.g. MONTHLY × 3 = every 3 months"
            ),
        ),
        migrations.AddField(
            model_name="pmschedule",
            name="last_completed_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="pmschedule",
            name="priority_override",
            field=models.CharField(
                blank=True,
                choices=[
                    ("low", "Low"),
                    ("medium", "Medium"),
                    ("high", "High"),
                    ("critical", "Critical"),
                ],
                help_text="If null, fall back to template.priority",
                max_length=16,
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="pmschedule",
            name="reminder_days_before",
            field=models.PositiveIntegerField(default=7),
        ),
        migrations.AddField(
            model_name="pmschedule",
            name="start_date",
            field=models.DateField(default=django.utils.timezone.now),
        ),
        migrations.AddField(
            model_name="pmschedule",
            name="trigger_type",
            field=models.CharField(
                choices=[("time", "Time-based"), ("meter", "Meter-based")],
                default="time",
                max_length=16,
            ),
        ),
        migrations.AddField(
            model_name="pmschedule",
            name="template",
            field=models.ForeignKey(
                blank=True,
                help_text="Reusable procedure applied to this asset",
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="schedules",
                to="maintenance.pmtemplate",
            ),
        ),
    ]