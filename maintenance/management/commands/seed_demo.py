"""Load demo users, master data, and optional transactional sample data."""

from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from accounts.staff_admin import assign_staff_model_permissions
from inventory.models import SparePart
from maintenance.models import (
    ExternalRepairOrder,
    Machine,
    MaintenanceIssue,
    Notification,
    PMTemplate,
    PMSchedule,
    QuickMaintenanceLog,
    Tool,
    WorkOrder,
)
from maintenance.services import transition_work_order, validate_issue
from procurement.models import PurchaseRequest, Supplier


DEMO_ISSUE_PREFIX = "[MMS-DEMO]"


def _seed_transactional_demo(stdout, style) -> None:
    if MaintenanceIssue.objects.filter(description__startswith=DEMO_ISSUE_PREFIX).exists():
        stdout.write("Demo issues already exist (description starts with [MMS-DEMO]). Skipping transactional seed.")
        return

    User = get_user_model()
    operator = User.objects.filter(username="operator").first()
    manager = User.objects.filter(username="manager").first()
    technician = User.objects.filter(username="technician").first()
    if not all([operator, manager, technician]):
        stdout.write(style.WARNING("Need users operator, manager, technician. Run seed without --full first."))
        return

    m1 = Machine.objects.order_by("pk").first()
    m2 = Machine.objects.order_by("pk").last()
    if not m1:
        stdout.write(style.WARNING("No machines; skipping transactional seed."))
        return

    belt = SparePart.objects.filter(sku="BELT-100").first()
    grease = SparePart.objects.filter(sku="GREASE-5L").first()

    from maintenance.notifications import notify_emergency_work_order, notify_procurement_request, notify_wo_assigned

    with transaction.atomic():
        MaintenanceIssue.objects.create(
            machine=m1,
            reported_by=operator,
            description=f"{DEMO_ISSUE_PREFIX} Unusual vibration on main bearing (operator walk-by).",
            status=MaintenanceIssue.Status.NEW,
        )
        i_val = MaintenanceIssue.objects.create(
            machine=m2 or m1,
            reported_by=operator,
            description=f"{DEMO_ISSUE_PREFIX} Grease line seep at coupling guard.",
            status=MaintenanceIssue.Status.NEW,
        )
        validate_issue(i_val, actor=manager, priority=MaintenanceIssue.Priority.HIGH)

        wo = WorkOrder.objects.create(
            category=WorkOrder.Category.BREAKDOWN,
            issue=i_val,
            machine=i_val.machine,
            lifecycle_status=WorkOrder.LifecycleStatus.ASSIGNED,
            operational_status=WorkOrder.OperationalStatus.PAUSED,
            created_by=manager,
            notes=f"{DEMO_ISSUE_PREFIX} Created from validated issue.",
        )
        i_val.status = MaintenanceIssue.Status.CONVERTED
        i_val.save(update_fields=["status"])
        transition_work_order(wo, WorkOrder.LifecycleStatus.ASSIGNED, actor=manager, note="Demo: created from issue")

        wo.assigned_technician = technician
        wo.save(update_fields=["assigned_technician", "updated_at"])
        transition_work_order(wo, WorkOrder.LifecycleStatus.ASSIGNED, actor=manager, note="Demo assign")
        notify_wo_assigned(wo)

        wo_em = WorkOrder.objects.create(
            category=WorkOrder.Category.EMERGENCY,
            is_emergency=True,
            machine=m1,
            lifecycle_status=WorkOrder.LifecycleStatus.ASSIGNED,
            operational_status=WorkOrder.OperationalStatus.PAUSED,
            created_by=manager,
            notes=f"{DEMO_ISSUE_PREFIX} Line stop — motor overtemp trip.",
        )
        transition_work_order(wo_em, WorkOrder.LifecycleStatus.ASSIGNED, actor=manager, note="Demo emergency WO")
        notify_emergency_work_order(wo_em)

        t0 = timezone.now() - timedelta(days=3)
        WorkOrder.objects.create(
            category=WorkOrder.Category.BREAKDOWN,
            machine=m2 or m1,
            lifecycle_status=WorkOrder.LifecycleStatus.CLOSED,
            operational_status=WorkOrder.OperationalStatus.PAUSED,
            created_by=manager,
            notes=f"{DEMO_ISSUE_PREFIX} Historical WO for KPIs / reports.",
            downtime_started_at=t0,
            downtime_ended_at=t0 + timedelta(hours=4),
        )

        if belt:
            pr = PurchaseRequest.objects.create(
                part=belt,
                work_order=wo,
                quantity=Decimal("2"),
                urgency="normal",
                is_emergency=False,
                notes=f"{DEMO_ISSUE_PREFIX} Belt stock replenish.",
                status=PurchaseRequest.Status.PENDING,
                created_by=manager,
            )
            notify_procurement_request(pr)

        pm_template, _ = PMTemplate.objects.get_or_create(
            code="PMT-MONTHLY-PRESS-01",
            defaults={
                "title": "Monthly press inspection",
                "description": "Monthly visual and lubrication inspection of the press line.",
                "priority": PMTemplate.Priority.MEDIUM,
                "estimated_duration_minutes": 30,
                "is_active": True,
            },
        )

        PMSchedule.objects.create(
            machine=m1,
            template=pm_template,
            frequency_type=PMSchedule.FrequencyType.MONTHLY,
            interval=1,
            start_date=timezone.now().date(),
            next_due_at=timezone.now() - timedelta(days=2),
            is_active=True,
            created_by=manager,
        )

        QuickMaintenanceLog.objects.create(
            machine=m1,
            author=technician,
            summary=f"{DEMO_ISSUE_PREFIX} Greased slides — 10 min.",
            details="No further issues observed.",
        )

        ExternalRepairOrder.objects.create(
            work_order=wo,
            title=f"{DEMO_ISSUE_PREFIX} Spindle encoder",
            description="Vendor calibration after crash; demo repair line.",
            vendor_name="Local Parts Co",
            estimated_cost=Decimal("450.00"),
            status=ExternalRepairOrder.Status.SENT_TO_VENDOR,
            created_by=manager,
            sent_at=timezone.now() - timedelta(days=1),
        )

        Notification.objects.create(
            recipient=manager,
            kind=Notification.Kind.ISSUE_NEW,
            title="(Demo) Sample unread notification",
            body="You can mark notifications read from the Notifications page.",
            link="",
        )

        if grease and grease.is_consumable:
            grease.quantity_on_hand = max(Decimal("0"), grease.quantity_on_hand - Decimal("1"))
            grease.save(update_fields=["quantity_on_hand"])

    stdout.write(style.SUCCESS("Transactional demo: issues, WOs (assigned + emergency + closed), PR, PM schedule, quick log, external repair, sample notification."))


class Command(BaseCommand):
    help = "Load demo users, machines, parts, tools, suppliers. Use --full for issues/WOs/PR/PM sample rows."

    def add_arguments(self, parser):
        parser.add_argument("--password", default="demo123", help="Password for demo users (default: demo123 — used by all 8 demo users)")
        parser.add_argument(
            "--full",
            action="store_true",
            help="Also create demo issues, work orders, purchase request, PM schedule, quick log, repair order, notification.",
        )

    def handle(self, *args, **options):
        pwd = options["password"]
        User = get_user_model()

        roles = [
            ("operator", User.Role.OPERATOR),
            ("supervisor", User.Role.SUPERVISOR),
            ("technician", User.Role.TECHNICIAN),
            ("manager", User.Role.MANAGER),
            ("procurement", User.Role.PROCUREMENT),
        ]
        for username, role in roles:
            u, _created = User.objects.get_or_create(username=username, defaults={"email": f"{username}@local", "role": role})
            u.role = role
            u.is_staff = username in ("manager", "procurement")
            u.set_password(pwd)
            u.save()
            self.stdout.write(self.style.SUCCESS(f"User {username} / {pwd} ({role})"))

        assign_staff_model_permissions(User)

        if not Machine.objects.exists():
            Machine.objects.create(name="Line A — Press 1", qr_code="PRESS-01", location="Hall A")
            Machine.objects.create(name="Line B — Conveyor 2", qr_code="CONV-02", location="Hall B")
            self.stdout.write("Created demo machines (QR: PRESS-01, CONV-02).")

        if not SparePart.objects.exists():
            SparePart.objects.create(
                sku="BELT-100",
                name="Drive belt 100",
                quantity_on_hand=Decimal("5"),
                min_stock_level=Decimal("2"),
                is_consumable=False,
            )
            SparePart.objects.create(
                sku="GREASE-5L",
                name="Grease 5L",
                quantity_on_hand=Decimal("10"),
                min_stock_level=Decimal("2"),
                is_consumable=True,
            )
            self.stdout.write("Created demo spare parts.")

        if options["full"] and not SparePart.objects.filter(sku="FILTER-A1").exists():
            SparePart.objects.create(
                sku="FILTER-A1",
                name="Air filter A1",
                quantity_on_hand=Decimal("4"),
                min_stock_level=Decimal("5"),
                is_consumable=False,
            )
            self.stdout.write("Added extra part FILTER-A1 (below min for low-stock demos).")

        if not Tool.objects.exists():
            Tool.objects.create(code="TORQUE-01", name="Torque wrench 01")
            Tool.objects.create(code="MULTI-02", name="Multimeter 02")
            self.stdout.write("Created demo tools.")

        if not Supplier.objects.exists():
            Supplier.objects.create(name="Local Parts Co", contact="555-0100")
            self.stdout.write("Created demo supplier.")

        if options["full"]:
            _seed_transactional_demo(self.stdout, self.style)

        self.stdout.write(
            self.style.SUCCESS(
                "Done. Staff users received Django model permissions so /admin/ lists apps. "
                "Super Admin: python manage.py create_mms_user NAME --role super_admin (or createsuperuser). "
                "Full sample data: python manage.py seed_demo --full"
            )
        )
