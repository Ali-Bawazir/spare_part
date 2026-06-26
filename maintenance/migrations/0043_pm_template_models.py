from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("maintenance", "0042_workorder_blocker_system_version"),
    ]

    operations = [
        migrations.CreateModel(
            name="PMTemplate",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "code",
                    models.SlugField(
                        help_text="e.g. PM-HYD-001", max_length=64, unique=True
                    ),
                ),
                ("title", models.CharField(max_length=255)),
                ("description", models.TextField(blank=True)),
                ("estimated_duration_minutes", models.PositiveIntegerField(default=30)),
                (
                    "priority",
                    models.CharField(
                        choices=[
                            ("low", "Low"),
                            ("medium", "Medium"),
                            ("high", "High"),
                            ("critical", "Critical"),
                        ],
                        default="medium",
                        max_length=16,
                    ),
                ),
                ("requires_manager_review", models.BooleanField(default=True)),
                ("is_active", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={
                "ordering": ["code"],
            },
        ),
        migrations.CreateModel(
            name="PMChecklistItem",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("order", models.PositiveIntegerField(default=0)),
                ("text", models.CharField(max_length=500)),
                ("is_required", models.BooleanField(default=True)),
                (
                    "template",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="checklist_items",
                        to="maintenance.pmtemplate",
                    ),
                ),
            ],
            options={
                "ordering": ["order", "pk"],
            },
        ),
        migrations.CreateModel(
            name="PMExecution",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "scheduled_due_at",
                    models.DateTimeField(
                        help_text="Locks this execution to a specific due occurrence"
                    ),
                ),
                (
                    "execution_sequence",
                    models.PositiveIntegerField(
                        default=1, help_text="Cycle counter per PMSchedule"
                    ),
                ),
                (
                    "template_snapshot_json",
                    models.JSONField(
                        blank=True,
                        default=dict,
                        help_text="Immutable snapshot of template state at WO spawn time",
                    ),
                ),
                ("completed_at", models.DateTimeField(blank=True, null=True)),
                ("approved_at", models.DateTimeField(blank=True, null=True)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("submitted", "Submitted"),
                            ("approved", "Approved"),
                            ("rejected", "Rejected"),
                            ("missed", "Missed"),
                        ],
                        db_index=True,
                        default="submitted",
                        max_length=16,
                    ),
                ),
                ("notes", models.TextField(blank=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "approved_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="pm_executions_approved",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "completed_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="pm_executions_completed",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "pm_schedule",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="executions",
                        to="maintenance.pmschedule",
                    ),
                ),
                (
                    "work_order",
                    models.OneToOneField(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="pm_execution",
                        to="maintenance.workorder",
                    ),
                ),
            ],
            options={
                "ordering": ["-scheduled_due_at", "-execution_sequence"],
            },
        ),
        migrations.AddConstraint(
            model_name="pmexecution",
            constraint=models.UniqueConstraint(
                fields=("pm_schedule", "scheduled_due_at"),
                name="unique_pm_execution_per_occurrence",
            ),
        ),
    ]