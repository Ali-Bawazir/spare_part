from datetime import timedelta

from django.utils import timezone

from accounts.capabilities import get_mms_capabilities
from accounts.models import User
from maintenance.models import ExternalRepairOrder, MaintenanceIssue, Notification, WorkOrder
from inventory.models import PartShortageReport
from procurement.models import PurchaseOrder, PurchaseRequest


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
            "nav_po_open": 0,
            "nav_wo_overdue": 0,
            "nav_shortage_pending": 0,
            "nav_shortage_in_fulfillment": 0,
            "nav_shortage_blocked": 0,
            "nav_shortage_overdue": 0,
            "nav_my_issues_30d": 0,
            "nav_my_issues_unresolved": 0,
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
        "nav_po_open": 0,
        "nav_wo_overdue": 0,
        "nav_shortage_pending": 0,
        "nav_shortage_in_fulfillment": 0,
        "nav_shortage_blocked": 0,
        "nav_shortage_overdue": 0,
        "nav_my_issues_30d": 0,
        "nav_my_issues_unresolved": 0,
        "nav_notif_unread": Notification.objects.filter(recipient=u, read_at__isnull=True).count(),
        **perm,
    }

    issue_staff = role in (User.Role.SUPERVISOR, User.Role.MANAGER, User.Role.SUPER_ADMIN) or u.is_superuser
    if issue_staff:
        ctx["nav_issues_new"] = MaintenanceIssue.objects.filter(status=MaintenanceIssue.Status.NEW).count()

    if caps.get("close_or_review_wo") or caps.get("repair_officer"):
        ctx["nav_ero_returned"] = ExternalRepairOrder.objects.filter(
            status=ExternalRepairOrder.Status.RETURNED
        ).count()
    if caps.get("close_or_review_wo"):
        ctx["nav_wo_review"] = WorkOrder.objects.filter(lifecycle_status=WorkOrder.LifecycleStatus.PENDING_REVIEW).count()
        seven_days_ago = timezone.now() - timedelta(days=7)
        ctx["nav_wo_overdue"] = WorkOrder.objects.filter(
            created_at__lt=seven_days_ago,
        ).exclude(
            lifecycle_status=WorkOrder.LifecycleStatus.CLOSED,
        ).count()

    if caps.get("view_procurement_requests"):
        ctx["nav_pr_pending"] = PurchaseRequest.objects.filter(status=PurchaseRequest.Status.PENDING).count()
        ctx["nav_ero_draft"] = ExternalRepairOrder.objects.filter(
            status=ExternalRepairOrder.Status.DRAFT
        ).count()

    if caps.get("view_purchase_orders"):
        ctx["nav_po_open"] = PurchaseOrder.objects.filter(
            status__in=[
                PurchaseOrder.Status.DRAFT,
                PurchaseOrder.Status.SENT,
                PurchaseOrder.Status.PARTIAL_RECEIVED,
            ]
        ).count()

    if role == User.Role.TECHNICIAN:
        ctx["nav_my_open_wo"] = WorkOrder.objects.filter(assigned_technician=u).exclude(
            lifecycle_status=WorkOrder.LifecycleStatus.CLOSED
        ).count()

    # Phase 6: per-user reporting counters. Operators and technicians can
    # both report issues from the field — surface a 30-day and unresolved
    # count as nav badges so they can see "how many have I reported?".
    # "Unresolved" here = not yet converted to a work order (still NEW or
    # VALIDATED awaiting conversion).
    if role in (User.Role.OPERATOR, User.Role.TECHNICIAN):
        my_issues_qs = MaintenanceIssue.objects.filter(reported_by=u)
        ctx["nav_my_issues_30d"] = my_issues_qs.filter(
            created_at__gte=timezone.now() - timedelta(days=30),
        ).count()
        ctx["nav_my_issues_unresolved"] = my_issues_qs.exclude(
            status=MaintenanceIssue.Status.CONVERTED,
        ).count()

    # v4.8 shortage counters (for users who can decide shortage reports)
    if caps.get("decide_part_shortage_report") or caps.get("close_or_review_wo"):
        from datetime import timedelta as _td
        ctx["nav_shortage_pending"] = PartShortageReport.objects.filter(
            status=PartShortageReport.Status.PENDING_REVIEW,
        ).count()
        ctx["nav_shortage_in_fulfillment"] = PartShortageReport.objects.filter(
            status=PartShortageReport.Status.IN_FULFILLMENT,
        ).count()
        ctx["nav_shortage_blocked"] = PartShortageReport.objects.filter(
            status=PartShortageReport.Status.BLOCKED,
        ).count()
        # Overdue = IN_FULFILLMENT for more than 7 days (Sprint 4 will refine)
        seven_days_ago = timezone.now() - _td(days=7)
        ctx["nav_shortage_overdue"] = PartShortageReport.objects.filter(
            status=PartShortageReport.Status.IN_FULFILLMENT,
            reviewed_at__lt=seven_days_ago,
        ).count()

    return ctx
