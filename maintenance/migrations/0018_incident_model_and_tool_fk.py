# Generated manually on 2026-05-22 for Phase 1.2

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("maintenance", "0017_add_allow_operator_consumption_consumableassignment"),
    ]

    operations = [
        # --- Add REPAIR category via AlterField (choices change doesn't need model change) ---
        migrations.AlterField(
            model_name="workorder",
            name="category",
            field=models.CharField(
                choices=[
                    ("breakdown", "Breakdown / corrective"),
                    ("preventive", "Preventive"),
                    ("emergency", "Emergency"),
                    ("repair", "Repair"),
                ],
                db_index=True,
                default="breakdown",
                max_length=20,
            ),
        ),
        # --- Add tool FK to WorkOrder (nullable, for DAMAGED tool repair WOs) ---
        migrations.AddField(
            model_name="workorder",
            name="tool",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="work_orders",
                to="maintenance.tool",
            ),
        ),
        # --- Create Incident model ---
        migrations.CreateModel(
            name="Incident",
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
                    "title",
                    models.CharField(max_length=255),
                ),
                ("description", models.TextField(blank=True)),
                (
                    "reported_by",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="reported_incidents",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "tool",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="incidents",
                        to="maintenance.tool",
                    ),
                ),
                (
                    "work_order",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="incidents",
                        to="maintenance.workorder",
                    ),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("open", "Open"),
                            ("investigating", "Investigating"),
                            ("closed", "Closed"),
                        ],
                        db_index=True,
                        default="open",
                        max_length=20,
                    ),
                ),
                ("resolved_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={
                "ordering": ["-created_at"],
            },
        ),
    ]
