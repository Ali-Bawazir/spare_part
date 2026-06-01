# Generated manually on 2026-05-22 for Phase 1.2 — FailureMode
from django.db import migrations, models
import django.db.models.deletion


def _derive_sub_code(name: str) -> str:
    words = name.replace("-", " ").split()
    significant = [w for w in words if len(w) > 2 and w.lower() not in {"the", "and", "for", "fault", "failure", "leak"}]
    if not significant:
        significant = words
    letters = "".join(w[0].upper() for w in significant if w)
    vowels = set("AEIOU")
    letters = "".join(c for c in letters if c not in vowels)
    return (letters[:3] or "XXX").ljust(3, "X")


def seed_failure_categories(apps, schema_editor):
    FailureCategory = apps.get_model("maintenance", "FailureCategory")
    categories = [
        ("MECH", "Mechanical"),
        ("ELEC", "Electrical"),
        ("HYDR", "Hydraulic"),
        ("INST", "Instrumentation"),
        ("PNUM", "Pneumatic"),
    ]
    for code, name in categories:
        FailureCategory.objects.get_or_create(
            code=code,
            defaults={"name": name, "is_active": True},
        )


def reverse_failure_categories(apps, schema_editor):
    FailureCategory = apps.get_model("maintenance", "FailureCategory")
    FailureCategory.objects.filter(code__in=["MECH", "ELEC", "HYDR", "INST", "PNUM"]).delete()


def seed_failure_modes(apps, schema_editor):
    FailureCategory = apps.get_model("maintenance", "FailureCategory")
    FailureMode = apps.get_model("maintenance", "FailureMode")

    modes_by_category = {
        "MECH": [
            ("Bearing Failure", "BRG"),
            ("Broken Gear", "BRK"),
            ("Seal Leak", "SEL"),
            ("Engine Fault", "ENG"),
            ("Belt Slippage", "BLT"),
        ],
        "ELEC": [
            ("Wiring Fault", "WIR"),
            ("Motor Failure", "MTR"),
            ("Switch Fault", "SWT"),
            ("Power Supply Fault", "PWR"),
            ("Connector Failure", "CON"),
        ],
        "HYDR": [
            ("Valve Fault", "VLV"),
            ("Pump Failure", "PMP"),
            ("Cylinder Leak", "CYL"),
            ("Pipe Burst", "PIP"),
            ("Pressure Fault", "PRS"),
        ],
        "INST": [
            ("Sensor Fault", "SNS"),
            ("Gauge Malfunction", "GGE"),
            ("Transmitter Failure", "TXM"),
            ("Display Error", "DSP"),
        ],
        "PNUM": [
            ("Cylinder Fault", "CYL"),
            ("Valve Fault", "VLV"),
            ("Air Leak", "AIR"),
            ("Regulator Fault", "REG"),
        ],
    }

    for cat_code, modes in modes_by_category.items():
        category = FailureCategory.objects.filter(code=cat_code).first()
        if not category:
            continue
        for name, sub_code_override in modes:
            sub_code = sub_code_override or _derive_sub_code(name)
            prefix = f"{cat_code[:4].upper()}-{sub_code.upper()}-"
            existing_codes = list(
                FailureMode.objects.filter(code__startswith=prefix)
                .values_list("code", flat=True)
            )
            max_seq = 0
            for ec in existing_codes:
                try:
                    seq = int(ec.split("-")[-1])
                    if seq > max_seq:
                        max_seq = seq
                except (ValueError, IndexError):
                    pass

            code = f"{prefix}{max_seq + 1:03d}"

            if not FailureMode.objects.filter(code=code).exists():
                FailureMode.objects.create(
                    category=category,
                    code=code,
                    name=name,
                    description="",
                    is_active=True,
                )


def reverse_failure_modes(apps, schema_editor):
    FailureMode = apps.get_model("maintenance", "FailureMode")
    FailureMode.objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [
        ("maintenance", "0018_incident_model_and_tool_fk"),
    ]

    operations = [
        # Step 1: Create FailureMode model (no FK yet)
        migrations.CreateModel(
            name="FailureMode",
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
                ("code", models.CharField(max_length=32, unique=True)),
                ("name", models.CharField(max_length=255)),
                ("description", models.TextField(blank=True)),
                ("is_active", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "category",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="failure_modes",
                        to="maintenance.failurecategory",
                    ),
                ),
            ],
            options={
                "ordering": ["code"],
            },
        ),
        # Step 2: Add failure_mode FK to MaintenanceIssue (model already exists in state)
        migrations.AddField(
            model_name="maintenanceissue",
            name="failure_mode",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="issues",
                to="maintenance.FailureMode",
            ),
        ),
        # Step 3: Seed categories
        migrations.RunPython(seed_failure_categories, reverse_failure_categories),
        # Step 4: Seed modes
        migrations.RunPython(seed_failure_modes, reverse_failure_modes),
    ]
