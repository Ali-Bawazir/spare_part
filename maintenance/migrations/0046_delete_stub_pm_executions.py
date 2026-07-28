from django.db import migrations


def delete_stub_executions(apps, schema_editor):
    PMExecution = apps.get_model("maintenance", "PMExecution")
    PMExecution.objects.filter(work_order__isnull=True).delete()


def reverse_delete_stub_executions(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("maintenance", "0045_pm_schedule_template_notnull"),
    ]

    operations = [
        migrations.RunPython(delete_stub_executions, reverse_delete_stub_executions),
    ]