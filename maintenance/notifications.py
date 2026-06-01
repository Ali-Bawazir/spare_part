"""
In-app notifications (scope / PDF Section I — operational alerts).

Who gets what (high level):
- New issue → managers, supervisors, super admins (+ Django superusers).
- Issue validated → managers + super admins (work can be planned).
- Emergency WO created → managers, supervisors, procurement, super admins.
- WO pending manager review → managers + super admins.
- WO assigned → assigned technician only.
- Low stock (at/below min) → managers + super admins (after stock moves).
- Purchase request created / updated flow → procurement + super admins, and
  managers are copied so store leadership sees demand.
- PM overdue (active schedule, next_due in the past) → managers + super admins,
  deduped per schedule for 48h (see sync_pm_overdue_notifications).
- External repair returned → managers + super admins (accept / close loop).

Call sync_pm_overdue_notifications() from dashboard / PM list, or run
`python manage.py send_scheduled_notifications` on a schedule in production.
"""
from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import timedelta

from django.urls import reverse
from django.utils import timezone

from accounts.models import User

from .models import ExternalRepairOrder, MaintenanceIssue, Notification, PMSchedule


def _send_email_if_configured(recipient, subject, body):
    """
    Placeholder for email sending. In Phase 2, this would send real emails.
    Currently logs to console/audit.
    """
    logger = logging.getLogger(__name__)

    from django.conf import settings
    if not getattr(settings, 'EMAIL_BACKEND', None):
        logger.debug(f"Email not configured — would send to {recipient.email}: {subject}")
        return False

    # Phase 2: actually send email
    # from django.core.mail import send_mail
    # send_mail(subject, body, settings.DEFAULT_FROM_EMAIL, [recipient.email])

    return True


def _unique_users(users: Iterable[User]) -> list[User]:
    seen: set[int] = set()
    out: list[User] = []
    for u in users:
        if u.pk not in seen:
            seen.add(u.pk)
            out.append(u)
    return out


def _notify_users(users: Iterable[User], *, kind: str, title: str, body: str = "", link: str = "", is_critical: bool = False) -> None:
    for u in _unique_users(users):
        Notification.objects.create(
            recipient=u,
            kind=kind,
            title=title[:255],
            body=body[:2000],
            link=link[:500],
            is_critical=is_critical,
        )


def _managers_supervisors_supers() -> list[User]:
    qs = User.objects.filter(
        is_active=True,
        role__in=[User.Role.MANAGER, User.Role.SUPERVISOR, User.Role.SUPER_ADMIN],
    ) | User.objects.filter(is_active=True, is_superuser=True)
    return list(qs.distinct())


def _managers_supers() -> list[User]:
    qs = User.objects.filter(
        is_active=True,
        role__in=[User.Role.MANAGER, User.Role.SUPER_ADMIN],
    ) | User.objects.filter(is_active=True, is_superuser=True)
    return list(qs.distinct())


def _procurement_supers() -> list[User]:
    qs = User.objects.filter(
        is_active=True,
        role__in=[User.Role.PROCUREMENT, User.Role.SUPER_ADMIN],
    ) | User.objects.filter(is_active=True, is_superuser=True)
    return list(qs.distinct())


def _emergency_wo_recipients() -> list[User]:
    """Managers + supervisors + procurement + super admins (PDF: cross-team visibility)."""
    return _unique_users(_managers_supervisors_supers() + _procurement_supers())


def _procurement_request_recipients() -> list[User]:
    """Procurement acts; managers stay informed for stock / WO alignment."""
    return _unique_users(_procurement_supers() + _managers_supers())


def notify_new_issue(issue: MaintenanceIssue) -> None:
    title = f"New issue #{issue.pk} — {issue.machine.name}"
    body = (issue.description or "")[:500]
    _notify_users(
        _managers_supervisors_supers(),
        kind=Notification.Kind.ISSUE_NEW,
        title=title,
        body=body,
        link=reverse("issue_list"),
    )


def notify_issue_validated(issue: MaintenanceIssue) -> None:
    title = f"Issue #{issue.pk} validated ({issue.get_priority_display()})"
    body = f"{issue.machine.name} — ready for work order."
    _notify_users(
        _managers_supers(),
        kind=Notification.Kind.ISSUE_VALIDATED,
        title=title,
        body=body,
        link=reverse("issue_list"),
    )


def notify_emergency_work_order(wo) -> None:
    title = f"Emergency WO: WO-{wo.number}"
    notes = (wo.notes or "").strip()
    body = f"{wo.machine.name}. {notes[:500]}" if notes else f"{wo.machine.name}."
    recipients = _emergency_wo_recipients()
    _notify_users(
        recipients,
        kind=Notification.Kind.WO_EMERGENCY,
        title=title,
        body=body,
        link=reverse("work_order_detail", kwargs={"pk": wo.pk}),
        is_critical=True,
    )
    for recipient in recipients:
        try:
            _send_email_if_configured(recipient, title, body)
        except Exception as e:
            logging.getLogger(__name__).warning(f"Email notification failed: {e}")


def notify_wo_pending_review(wo) -> None:
    title = f"WO-{wo.number} pending your review"
    body = f"{wo.machine.name} — technician submitted."
    _notify_users(
        _managers_supers(),
        kind=Notification.Kind.WO_PENDING_REVIEW,
        title=title,
        body=body,
        link=reverse("work_order_detail", kwargs={"pk": wo.pk}),
    )


def notify_wo_assigned(wo) -> None:
    if not wo.assigned_technician_id:
        return
    tech = wo.assigned_technician
    title = f"Assigned: WO-{wo.number}"
    body = f"{wo.machine.name} — {wo.get_category_display()}."
    _notify_users(
        [tech],
        kind=Notification.Kind.WO_ASSIGNED,
        title=title,
        body=body,
        link=reverse("work_order_detail", kwargs={"pk": wo.pk}),
    )


def notify_low_stock(part, *, sku: str, qty) -> None:
    title = f"Low stock: {sku}"
    body = f"Quantity on hand is now {qty} (at or below minimum)."
    _notify_users(
        _managers_supers(),
        kind=Notification.Kind.LOW_STOCK,
        title=title,
        body=body,
        link=reverse("stock_dashboard"),
        is_critical=(qty == 0),
    )


def notify_procurement_request(pr) -> None:
    title = f"Purchase request #{pr.pk}"
    body = f"{pr.part.sku} × {pr.quantity} — {pr.get_status_display()}."
    recipients = _procurement_request_recipients()
    _notify_users(
        recipients,
        kind=Notification.Kind.PROCUREMENT,
        title=title,
        body=body,
        link=reverse("purchase_list"),
        is_critical=getattr(pr, 'is_emergency', False),
    )
    for recipient in recipients:
        try:
            _send_email_if_configured(recipient, title, body)
        except Exception as e:
            logging.getLogger(__name__).warning(f"Email notification failed: {e}")


def notify_repair_returned(rwo: ExternalRepairOrder) -> None:
    title = f"Repair returned: {rwo.title}"
    body = "Verify repair quality and cost, then accept (UC-20)."
    recipients = _managers_supers()
    _notify_users(
        recipients,
        kind=Notification.Kind.REPAIR_RETURNED,
        title=title,
        body=body,
        link=reverse("repair_manager_accept", kwargs={"pk": rwo.pk}),
    )
    for recipient in recipients:
        try:
            _send_email_if_configured(recipient, title, body)
        except Exception as e:
            logging.getLogger(__name__).warning(f"Email notification failed: {e}")


def sync_pm_overdue_notifications() -> int:
    """
    For each active PM schedule past next_due_at, notify managers once per 48h
    per schedule (dedupe via body tag).
    """
    now = timezone.now()
    created = 0
    overdue = PMSchedule.objects.filter(is_active=True, next_due_at__lt=now).select_related("machine")
    for sched in overdue:
        tag = f"[pm_sched_id:{sched.pk}]"
        if Notification.objects.filter(
            kind=Notification.Kind.PM_OVERDUE,
            body__contains=tag,
            created_at__gte=now - timedelta(hours=48),
        ).exists():
            continue
        title = f"PM overdue: {sched.title}"
        body = f"{tag} Machine {sched.machine.name} — due {sched.next_due_at.strftime('%Y-%m-%d %H:%M')}."
        _notify_users(
            _managers_supers(),
            kind=Notification.Kind.PM_OVERDUE,
            title=title[:255],
            body=body[:2000],
            link=reverse("pm_list"),
        )
        created += 1
    return created
