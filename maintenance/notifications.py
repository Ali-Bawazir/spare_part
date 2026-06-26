"""
In-app notifications (scope / PDF Section I — operational alerts).

Who gets what (high level):
- New issue → managers, supervisors, super admins (+ Django superusers).
- Issue validated → managers + super admins (work can be planned).
- Emergency WO created → managers, supervisors, procurement, super admins.
- WO created from issue → managers, supervisors, super admins.
- WO assigned → assigned technician + managers + supervisors + super admins.
- WO started (IN_PROGRESS) → managers, supervisors, super admins.
- WO paused / waiting → managers, supervisors, super admins.
- WO pending manager review → managers, supervisors, super admins.
- WO closed/rejected → managers, supervisors, super admins.
- Low stock (at/below min) → managers + super admins (after stock moves).
- Purchase request created / updated flow → procurement + super admins, and
  managers are copied so store leadership sees demand.
- PM overdue (active schedule, next_due in the past) → managers + super admins,
  deduped per schedule for 48h (see sync_pm_overdue_notifications).
- External repair requested → managers, supervisors, super admins.
- External repair returned → managers, supervisors, super admins (accept / close loop).
- Stale issue (NEW > threshold) → managers, supervisors, super admins.
"""
from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import timedelta

from django.urls import reverse
from django.utils import timezone
from django.db.models import Q

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


def notify_emergency_issue_reported(issue) -> None:
    """P3.3: notify manager + on-call tech when an emergency issue is reported."""
    title = f"EMERGENCY issue: {issue.machine.name}"
    desc = (issue.description or "")[:500]
    body = f"Reported by {issue.reported_by.username}. {desc}"
    recipients = _emergency_wo_recipients()
    _notify_users(
        recipients,
        kind=Notification.Kind.WO_EMERGENCY,
        title=title,
        body=body,
        link=reverse("issue_detail", kwargs={"pk": issue.pk}),
        is_critical=True,
    )


def notify_wo_pending_review(wo) -> None:
    title = f"WO-{wo.number} pending review"
    body = f"{wo.machine.name} — technician submitted."
    _notify_users(
        _managers_supervisors_supers(),
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
    # Notify technician + supervisors + managers
    _notify_users(
        [tech],
        kind=Notification.Kind.WO_ASSIGNED,
        title=title,
        body=body,
        link=reverse("work_order_detail", kwargs={"pk": wo.pk}),
    )
    _notify_users(
        _managers_supervisors_supers(),
        kind=Notification.Kind.WO_ASSIGNED,
        title=f"WO-{wo.number} assigned to {tech.get_full_name() or tech.username}",
        body=f"{wo.machine.name} — {wo.get_category_display()}.",
        link=reverse("work_order_detail", kwargs={"pk": wo.pk}),
    )


def notify_low_stock(part, *, sku: str, qty) -> None:
    title = f"Low stock: {sku}"
    body = f"Quantity on hand is now {qty} (at or below minimum)."
    _notify_users(
        _unique_users(_procurement_supers() + _managers_supers()),
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
    recipients = _unique_users(_procurement_supers() + _managers_supervisors_supers())
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


def notify_repair_request_created(err) -> None:
    """Technician submitted a PENDING external-repair request → notify managers + supervisors."""
    title = f"External repair requested on WO-{err.work_order.number}"
    body = (
        f"Technician {err.requested_by.get_full_name() or err.requested_by.username} "
        f"requests external repair for: {err.part_description[:120]}"
    )
    recipients = _managers_supervisors_supers()
    _notify_users(
        recipients,
        kind=Notification.Kind.REPAIR_REQUESTED,
        title=title,
        body=body,
        link=reverse("work_order_detail", kwargs={"pk": err.work_order_id}),
        is_critical=False,
    )
    for recipient in recipients:
        try:
            _send_email_if_configured(recipient, title, body)
        except Exception as e:
            logging.getLogger(__name__).warning(f"Email notification failed: {e}")


def notify_repair_draft_created(ero) -> None:
    """P3.7: manager approved the ERR → DRAFT ERO created → notify supply officers.

    The supply officer is responsible for picking a vendor, requesting a quote,
    sending the part out, and recording the return. Without this notification
    they must poll /repairs/.
    """
    title = f"New external repair order: {ero.title}"
    body = (
        f"A new external repair order is awaiting vendor assignment. "
        f"Linked to WO-{ero.work_order.number}. "
        f"Action required: pick a vendor, request a quote, then mark as sent."
    )
    recipients = _procurement_supers()
    _notify_users(
        recipients,
        kind=Notification.Kind.REPAIR_DRAFT,
        title=title,
        body=body,
        link=reverse("repair_officer", kwargs={"pk": ero.pk}),
    )
    for recipient in recipients:
        try:
            _send_email_if_configured(recipient, title, body)
        except Exception as e:
            logging.getLogger(__name__).warning(f"Email notification failed: {e}")


def notify_repair_sent_to_vendor(ero) -> None:
    """P3.7: supply officer sent the part to the vendor → notify managers.

    Gives the manager visibility that the repair actually left the facility
    and is now in the vendor's hands.
    """
    title = f"ERO sent to vendor: {ero.title}"
    body = (
        f"The maintenance supply officer has sent this repair to "
        f"{ero.vendor_name or 'the vendor'}. "
        f"Sent at: "
        f"{ero.sent_at.strftime('%Y-%m-%d %H:%M') if ero.sent_at else 'just now'}. "
        f"Estimated cost: {ero.estimated_cost or 'not set'}. "
        f"Status will move to RETURNED when the part comes back."
    )
    recipients = _managers_supervisors_supers()
    _notify_users(
        recipients,
        kind=Notification.Kind.REPAIR_SENT,
        title=title,
        body=body,
        link=reverse("repair_officer", kwargs={"pk": ero.pk}),
    )
    for recipient in recipients:
        try:
            _send_email_if_configured(recipient, title, body)
        except Exception as e:
            logging.getLogger(__name__).warning(f"Email notification failed: {e}")


def notify_wo_created(wo) -> None:
    """WO created from a validated issue → notify managers, supervisors, super admins."""
    title = f"WO-{wo.number} created"
    body = f"{wo.machine.name} — created from issue #{wo.issue_id}."
    _notify_users(
        _managers_supervisors_supers(),
        kind=Notification.Kind.WO_CREATED,
        title=title,
        body=body,
        link=reverse("work_order_detail", kwargs={"pk": wo.pk}),
    )


def notify_wo_started(wo) -> None:
    """Technician started work → notify managers, supervisors, super admins."""
    title = f"WO-{wo.number} started"
    tech_name = wo.assigned_technician.get_full_name() or wo.assigned_technician.username if wo.assigned_technician else "Technician"
    body = f"{wo.machine.name} — {tech_name} started work."
    _notify_users(
        _managers_supervisors_supers(),
        kind=Notification.Kind.WO_STARTED,
        title=title,
        body=body,
        link=reverse("work_order_detail", kwargs={"pk": wo.pk}),
    )


def notify_wo_paused(wo) -> None:
    """WO paused or moved to waiting status → notify managers, supervisors, super admins."""
    status_label = wo.get_lifecycle_status_display()
    title = f"WO-{wo.number} {status_label}"
    body = f"{wo.machine.name} — lifecycle changed to {status_label}."
    _notify_users(
        _managers_supervisors_supers(),
        kind=Notification.Kind.WO_PAUSED,
        title=title,
        body=body,
        link=reverse("work_order_detail", kwargs={"pk": wo.pk}),
    )


def notify_wo_closed(wo) -> None:
    """WO closed/rejected → notify managers, supervisors, super admins."""
    title = f"WO-{wo.number} closed"
    body = f"{wo.machine.name} — work order closed."
    _notify_users(
        _managers_supervisors_supers(),
        kind=Notification.Kind.WO_CLOSED,
        title=title,
        body=body,
        link=reverse("work_order_detail", kwargs={"pk": wo.pk}),
    )


def notify_stale_issue(issue: MaintenanceIssue) -> None:
    """Issue remains NEW beyond threshold → notify managers, supervisors, super admins."""
    title = f"Stale issue #{issue.pk} — {issue.machine.name}"
    body = f"Priority {issue.get_priority_display()} — not yet validated."
    _notify_users(
        _managers_supervisors_supers(),
        kind=Notification.Kind.ISSUE_STALE,
        title=title,
        body=body,
        link=reverse("issue_detail", kwargs={"pk": issue.pk}),
    )


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
        title = f"PM overdue: {sched.template.code} — {sched.template.title}"
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


def _technicians_active() -> list[User]:
    qs = User.objects.filter(
        is_active=True,
        role=User.Role.TECHNICIAN,
    ) | User.objects.filter(is_active=True, is_superuser=True, role=User.Role.TECHNICIAN)
    return list(qs.distinct())


def notify_pm_upcoming(schedule, *, days_before: int) -> int:
    from django.urls import reverse
    kind_map = {
        7: Notification.Kind.PM_UPCOMING_7D,
        3: Notification.Kind.PM_UPCOMING_3D,
        1: Notification.Kind.PM_UPCOMING_1D,
    }
    if days_before not in kind_map:
        raise ValueError(f"days_before must be 7, 3, or 1 (got {days_before})")
    kind = kind_map[days_before]
    due_date_str = schedule.next_due_at.strftime("%Y-%m-%d")
    tag = f"[pm_sched:{schedule.pk}|stage:UPCOMING_{days_before}D|due:{due_date_str}]"
    if Notification.objects.filter(kind=kind, body__contains=tag).exists():
        return 0
    if days_before == 1:
        recipients = _managers_supervisors_supers()
    else:
        recipients = _managers_supers()
    label = "tomorrow" if days_before == 1 else f"in {days_before} days"
    title = f"PM {label}: {schedule.template.title}"
    body = (
        f"{tag} Machine {schedule.machine.name} — "
        f"due {schedule.next_due_at.strftime('%Y-%m-%d %H:%M')}."
    )
    _notify_users(
        recipients,
        kind=kind,
        title=title[:255],
        body=body[:2000],
        link=reverse("pm_list"),
    )
    return 1


def notify_pm_due_today(schedule) -> int:
    from django.urls import reverse
    due_date_str = schedule.next_due_at.strftime("%Y-%m-%d")
    tag = f"[pm_sched:{schedule.pk}|stage:DUE_TODAY|due:{due_date_str}]"
    if Notification.objects.filter(kind=Notification.Kind.PM_DUE_TODAY, body__contains=tag).exists():
        return 0
    recipients = _unique_users(_managers_supervisors_supers() + _technicians_active())
    title = f"PM due today: {schedule.template.title}"
    body = (
        f"{tag} Machine {schedule.machine.name} — "
        f"due {schedule.next_due_at.strftime('%Y-%m-%d %H:%M')}."
    )
    _notify_users(
        recipients,
        kind=Notification.Kind.PM_DUE_TODAY,
        title=title[:255],
        body=body[:2000],
        link=reverse("pm_list"),
    )
    return 1


def sync_pm_notifications() -> dict:
    from .models import PMSchedule
    counts = {"upcoming_7d": 0, "upcoming_3d": 0, "upcoming_1d": 0, "due_today": 0, "overdue": 0}
    now = timezone.now()
    today = now.date()
    active_pms = PMSchedule.objects.filter(is_active=True).select_related("template", "machine")
    for schedule in active_pms:
        days_until_due = (schedule.next_due_at.date() - today).days
        if days_until_due == 7:
            counts["upcoming_7d"] += notify_pm_upcoming(schedule, days_before=7)
        elif days_until_due == 3:
            counts["upcoming_3d"] += notify_pm_upcoming(schedule, days_before=3)
        elif days_until_due == 1:
            counts["upcoming_1d"] += notify_pm_upcoming(schedule, days_before=1)
        elif days_until_due == 0:
            counts["due_today"] += notify_pm_due_today(schedule)
    counts["overdue"] = sync_pm_overdue_notifications()
    return counts


def notify_part_shortage(wo, part, qty_requested, qty_available, shortage, reported_by):
    """Create in-app notifications for every active Manager + Super Admin
    when a PartShortageReport is raised. The notification links to the WO
    detail page; the manager will see the report in the WO's
    "Parts Waiting Review" panel.

    Args:
        wo: the WorkOrder
        part: the SparePart
        qty_requested: the technician's requested quantity (Decimal)
        qty_available: the on-hand stock at the moment of the report (Decimal)
        shortage: qty_requested - min(qty_requested, qty_available) (Decimal)
        reported_by: the User who raised the report
    """
    from accounts.models import User  # local import to avoid circular
    from django.urls import reverse

    # v4.9 A3: include the reporting tech so they get notified too.
    recipients = User.objects.filter(
        Q(role__in=[User.Role.MANAGER, User.Role.SUPER_ADMIN]) | Q(pk=reported_by.pk),
        is_active=True,
    ).distinct()
    if not recipients.exists():
        return

    shortage_int = int(shortage) if shortage == shortage.to_integral_value() else shortage
    title = f"Part shortage: {part.sku}"
    body = (
        f"{reported_by.username} reported a shortage on WO-{wo.number}: "
        f"need {qty_requested} × {part.name}, "
        f"only {qty_available} in stock, "
        f"short {shortage_int}."
    )
    link = reverse("work_order_detail", kwargs={"pk": wo.pk})

    for recipient in recipients:
        is_reporter = recipient.pk == reported_by.pk
        Notification.objects.create(
            recipient=recipient,
            kind=Notification.Kind.PART_SHORTAGE_REPORTED,
            title=title,
            body=body,
            link=link,
            is_critical=(getattr(wo, "is_emergency", False) or shortage_int >= 10),
        )


def notify_part_request_re_review(new_line, old_line):
    """v4.9 A5: Notify all managers that a tech requested re-review of a refused part line."""
    from accounts.models import User
    from django.urls import reverse

    managers = User.objects.filter(
        role__in=[User.Role.MANAGER, User.Role.SUPER_ADMIN],
        is_active=True,
    )
    if not managers.exists():
        return

    title = f"Re-review requested: {new_line.part.sku}"
    body = (
        f"{new_line.requested_by.get_full_name() or new_line.requested_by.username} "
        f"requested re-review of refused part #{old_line.pk} on WO-{new_line.work_order.number}."
    )
    link = reverse("work_order_detail", kwargs={"pk": new_line.work_order_id})

    for mgr in managers:
        Notification.objects.create(
            recipient=mgr,
            kind=Notification.Kind.PROCUREMENT,
            title=title,
            body=body,
            link=link,
        )


def notify_part_received(po, part, qty, actor):
    """v4.9 B4: Notify manager + procurement officer + actor (tech) when PO line is received."""
    from accounts.models import User
    from django.urls import reverse

    recipients = User.objects.filter(
        Q(role__in=[
            User.Role.MANAGER, User.Role.PROCUREMENT, User.Role.SUPER_ADMIN,
        ]) | Q(pk=actor.pk),
        is_active=True,
    ).distinct()
    if not recipients.exists():
        return

    qty_str = str(qty) if hasattr(qty, "isoformat") is False else str(qty)
    title = f"Part received: {part.sku}"
    po_number = getattr(po, "po_number", None) or getattr(po, "number", None) or str(po.pk)
    body = f"{qty_str} × {part.name} received against PO-{po_number}."
    link = reverse("purchase_order_detail", kwargs={"pk": po.pk})

    for r in recipients:
        Notification.objects.create(
            recipient=r,
            kind=Notification.Kind.PART_RECEIVED,
            title=title, body=body, link=link,
        )


def notify_vendor_return(ero, part, note, actor):
    """v4.9 B4: Notify manager + tech who created the ERO when vendor returns a spare part."""
    from accounts.models import User
    from django.urls import reverse

    recipients = User.objects.filter(
        Q(role__in=[User.Role.MANAGER, User.Role.SUPER_ADMIN]) | Q(pk=ero.created_by_id),
        is_active=True,
    ).distinct()
    if not recipients.exists():
        return

    title = f"Vendor return: {part.sku}"
    body = f"{part.name} returned from vendor: {note}"
    link = reverse("repair_officer", kwargs={"pk": ero.pk})

    for r in recipients:
        Notification.objects.create(
            recipient=r,
            kind=Notification.Kind.VENDOR_RETURN,
            title=title, body=body, link=link,
        )


def notify_wo_part_received(work_order, part, qty, po, actor):
    """v4.9.3: When a PO line is received and linked to a WO, notify the
    assigned technician + manager. The tech needs to know parts are
    physically available so they can pick them up and continue the WO.
    """
    from accounts.models import User
    from django.urls import reverse

    recipients = User.objects.filter(
        Q(role__in=[User.Role.MANAGER, User.Role.SUPER_ADMIN])
        | Q(pk=getattr(work_order, "assigned_technician_id", None))
        | Q(pk=getattr(work_order, "created_by_id", None)),
        is_active=True,
    ).distinct()
    if not recipients.exists():
        return

    title = f"📦 Part received: {part.sku} for WO-{work_order.number}"
    body = (
        f"{qty:g}× {part.name} received against PO-{po.po_number}. "
        f"You can pick it up from the warehouse."
    )
    link = reverse("work_order_detail", kwargs={"pk": work_order.pk})

    for r in recipients:
        Notification.objects.create(
            recipient=r,
            kind=Notification.Kind.WO_PART_RECEIVED,
            title=title, body=body, link=link,
        )


def notify_wo_part_returned(work_order, part, ero, actor):
    """v4.9.3: When an external repair comes back from the vendor and is
    linked to a WO, notify the assigned tech + manager. The tech needs
    to know the part is back so they can re-install it.
    """
    from accounts.models import User
    from django.urls import reverse

    recipients = User.objects.filter(
        Q(role__in=[User.Role.MANAGER, User.Role.SUPER_ADMIN])
        | Q(pk=getattr(work_order, "assigned_technician_id", None))
        | Q(pk=getattr(work_order, "created_by_id", None))
        | Q(pk=getattr(ero, "created_by_id", None)),
        is_active=True,
    ).distinct()
    if not recipients.exists():
        return

    title = f"🔁 Part returned from vendor: {part.sku} for WO-{work_order.number}"
    body = (
        f"{part.name} returned from vendor against ERO #{ero.pk}. "
        f"You can re-install it on the asset."
    )
    link = reverse("work_order_detail", kwargs={"pk": work_order.pk})

    for r in recipients:
        Notification.objects.create(
            recipient=r,
            kind=Notification.Kind.WO_PART_RETURNED,
            title=title, body=body, link=link,
        )


def notify_wo_part_rejected(line, reason, actor):
    """v4.9.3: When a manager rejects a tech's part request on a WO, notify
    the tech so they can edit & re-submit, switch to a different part, or
    use the shortage flow. Recipients: the original requester + WO creator.
    """
    from accounts.models import User
    from django.urls import reverse

    work_order = line.work_order
    recipients = User.objects.filter(
        Q(pk=getattr(line, "requested_by_id", None))
        | Q(pk=getattr(work_order, "created_by_id", None)),
        is_active=True,
    ).distinct()
    if not recipients.exists():
        return

    title = f"❌ Part request rejected: {line.part.sku} on WO-{work_order.number}"
    body = (
        f"Your request for {line.quantity:g}× {line.part.name} was rejected. "
        f"Reason: {reason or 'No reason given'}. "
        f"Edit & re-submit, switch parts, or use the shortage flow."
    )
    link = reverse("work_order_detail", kwargs={"pk": work_order.pk})

    for r in recipients:
        Notification.objects.create(
            recipient=r,
            kind=Notification.Kind.WO_PART_REJECTED,
            title=title, body=body, link=link,
        )
