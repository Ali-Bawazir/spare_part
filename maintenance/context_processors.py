from datetime import timedelta

from django.utils import timezone

from accounts.capabilities import get_mms_capabilities
from accounts.models import User
from maintenance.models import ExternalRepairOrder, MaintenanceIssue, Notification, WorkOrder
from procurement.models import PurchaseRequest


def mms_nav(request):
    caps = get_mms_capabilities(getattr(request, "user", None))
    perm = {f"perm_{k}": v for k, v in caps.items()}

    if not getattr(request, "user", None) or not request.user.is_authenticated:
        return {
            "current_url_name": "",
            "nav_issues_new": 0,
            "nav_wo_review": 0,
            "nav_pr_pending": 0,
            "nav_my_open_wo": 0,
            "nav_notif_unread": 0,
            "nav_ero_returned": 0,
            "nav_ero_draft": 0,
            "nav_wo_overdue": 0,
            **perm,
        }

    u = request.user
    role = getattr(u, "role", "")
    ctx = {
        "current_url_name": getattr(request.resolver_match, "url_name", "") or "",
        "nav_issues_new": 0,
        "nav_wo_review": 0,
        "nav_pr_pending": 0,
        "nav_my_open_wo": 0,
        "nav_ero_returned": 0,
        "nav_ero_draft": 0,
        "nav_wo_overdue": 0,
        "nav_notif_unread": Notification.objects.filter(recipient=u, read_at__isnull=True).count(),
        **perm,
    }

    issue_staff = role in (User.Role.SUPERVISOR, User.Role.MANAGER, User.Role.SUPER_ADMIN) or u.is_superuser
    if issue_staff:
        ctx["nav_issues_new"] = MaintenanceIssue.objects.filter(status=MaintenanceIssue.Status.NEW).count()

    if caps.get("close_or_review_wo"):
        ctx["nav_wo_review"] = WorkOrder.objects.filter(status=WorkOrder.Status.PENDING_REVIEW).count()
        ctx["nav_ero_returned"] = ExternalRepairOrder.objects.filter(
            status=ExternalRepairOrder.Status.RETURNED
        ).count()
        seven_days_ago = timezone.now() - timedelta(days=7)
        ctx["nav_wo_overdue"] = WorkOrder.objects.filter(
            status__in=[
                WorkOrder.Status.APPROVED,
                WorkOrder.Status.ASSIGNED,
                WorkOrder.Status.IN_PROGRESS,
                WorkOrder.Status.PAUSED,
                WorkOrder.Status.PENDING_PARTS,
                WorkOrder.Status.WAITING_FOR_VENDOR,
            ],
            created_at__lt=seven_days_ago,
        ).count()

    if caps.get("view_procurement_requests"):
        ctx["nav_pr_pending"] = PurchaseRequest.objects.filter(status=PurchaseRequest.Status.PENDING).count()
        ctx["nav_ero_draft"] = ExternalRepairOrder.objects.filter(
            status=ExternalRepairOrder.Status.DRAFT
        ).count()

    if role == User.Role.TECHNICIAN:
        ctx["nav_my_open_wo"] = WorkOrder.objects.filter(assigned_technician=u).exclude(
            status=WorkOrder.Status.CLOSED
        ).count()

    return ctx
