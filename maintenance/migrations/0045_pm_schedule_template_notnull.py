from django.db import migrations, models
import django.db.models.deletion


def wipe_and_seed(apps, schema_editor):
    from django.utils import timezone
    from datetime import timedelta

    PMSchedule = apps.get_model("maintenance", "PMSchedule")
    PMTemplate = apps.get_model("maintenance", "PMTemplate")
    PMChecklistItem = apps.get_model("maintenance", "PMChecklistItem")
    PMExecution = apps.get_model("maintenance", "PMExecution")
    Machine = apps.get_model("maintenance", "Machine")
    User = apps.get_model("accounts", "User")

    PMSchedule.objects.all().delete()

    template = PMTemplate.objects.create(
        code="PM-HYD-001",
        title="Monthly Hydraulic Pump Inspection",
        description="Routine monthly inspection of hydraulic pump systems",
        estimated_duration_minutes=30,
        priority="medium",
        requires_manager_review=True,
        is_active=True,
    )

    PMChecklistItem.objects.create(template=template, order=1, text="Check oil level", is_required=True)
    PMChecklistItem.objects.create(template=template, order=2, text="Inspect for leaks", is_required=True)
    PMChecklistItem.objects.create(template=template, order=3, text="Verify pressure gauge", is_required=True)

    machine = Machine.objects.filter(is_active=True, asset_level=3).order_by("pk").first()
    if not machine:
        machine = Machine.objects.filter(asset_level=3).order_by("pk").first()
    if machine:
        manager = User.objects.filter(role="manager", is_active=True).order_by("pk").first()
        schedule = PMSchedule.objects.create(
            template=template,
            machine=machine,
            frequency_type="monthly",
            interval=1,
            start_date=timezone.now().date(),
            next_due_at=timezone.now() + timedelta(days=7),
            grace_days=7,
            reminder_days_before=7,
            is_active=True,
            created_by=manager,
        )
        PMExecution.objects.create(
            pm_schedule=schedule,
            scheduled_due_at=schedule.next_due_at,
            execution_sequence=1,
            status="submitted",
            template_snapshot_json={
                "template_code": template.code,
                "template_title": template.title,
                "template_priority": template.priority,
                "template_duration_minutes": template.estimated_duration_minutes,
                "checklist": [
                    {"order": 1, "text": "Check oil level", "is_required": True},
                    {"order": 2, "text": "Inspect for leaks", "is_required": True},
                    {"order": 3, "text": "Verify pressure gauge", "is_required": True},
                ],
                "grace_days": 7,
                "captured_at": timezone.now().isoformat(),
            },
        )


def reverse_wipe_and_seed(apps, schema_editor):
    PMExecution = apps.get_model("maintenance", "PMExecution")
    PMSchedule = apps.get_model("maintenance", "PMSchedule")
    PMTemplate = apps.get_model("maintenance", "PMTemplate")
    PMExecution.objects.all().delete()
    PMSchedule.objects.all().delete()
    PMTemplate.objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [
        ("maintenance", "0044_pm_schedule_fields"),
    ]

    operations = [
        migrations.RunPython(wipe_and_seed, reverse_wipe_and_seed),
        migrations.AlterField(
            model_name="pmschedule",
            name="template",
            field=models.ForeignKey(
                help_text="Reusable procedure applied to this asset",
                on_delete=django.db.models.deletion.PROTECT,
                related_name="schedules",
                to="maintenance.pmtemplate",
            ),
        ),
    ]