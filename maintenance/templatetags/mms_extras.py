from django import template

from accounts.utils import role_display_name

from maintenance.models import Attachment

register = template.Library()

WO_STATUS_BADGE = {
    "approved": "mms-badge mms-badge--muted",
    "assigned": "mms-badge mms-badge--info",
    "in_progress": "mms-badge mms-badge--primary",
    "paused": "mms-badge mms-badge--warning",
    "waiting_vendor": "mms-badge mms-badge--warning",
    "pending_parts": "mms-badge mms-badge--warning",
    "pending_review": "mms-badge mms-badge--accent",
    "closed": "mms-badge mms-badge--success",
}


@register.filter
def wo_status_badge_class(value):
    return WO_STATUS_BADGE.get(value, "mms-badge mms-badge--muted")


@register.filter
def dict_get(d, key):
    """Get a value from a dict by key, returning None if missing or non-dict."""
    if isinstance(d, dict):
        return d.get(key)
    return None


ISSUE_STATUS_BADGE = {
    "new": "mms-badge mms-badge--accent",
    "validated": "mms-badge mms-badge--info",
    "converted": "mms-badge mms-badge--success",
}


@register.filter
def issue_status_badge_class(value):
    return ISSUE_STATUS_BADGE.get(value, "mms-badge mms-badge--muted")


PRIORITY_BADGE = {
    "critical": "mms-badge mms-badge--danger",
    "high": "mms-badge mms-badge--warning",
    "medium": "mms-badge mms-badge--info",
    "low": "mms-badge mms-badge--muted",
}


@register.filter
def priority_badge_class(value):
    return PRIORITY_BADGE.get(value, "mms-badge mms-badge--muted")


PART_STATUS_BADGE = {
    "pending": "mms-badge mms-badge--warning",
    "approved": "mms-badge mms-badge--success",
    "rejected": "mms-badge mms-badge--muted",
}


@register.filter
def part_status_badge_class(value):
    return PART_STATUS_BADGE.get(value, "mms-badge mms-badge--muted")


ERR_STATUS_BADGE = {
    "pending": "mms-badge mms-badge--warning",
    "approved": "mms-badge mms-badge--success",
    "rejected": "mms-badge mms-badge--muted",
}


@register.filter
def err_status_badge_class(value):
    return ERR_STATUS_BADGE.get(value, "mms-badge mms-badge--muted")


ERO_STATUS_BADGE = {
    "draft": "mms-badge mms-badge--muted",
    "pending_vendor": "mms-badge mms-badge--warning",
    "sent_to_vendor": "mms-badge mms-badge--info",
    "returned": "mms-badge mms-badge--accent",
    "closed": "mms-badge mms-badge--success",
    "rejected": "mms-badge mms-badge--danger",
}


@register.filter
def ero_status_badge_class(value):
    return ERO_STATUS_BADGE.get(value, "mms-badge mms-badge--muted")


PR_STATUS_BADGE = {
    "pending": "mms-badge mms-badge--warning",
    "converted_to_po": "mms-badge mms-badge--info",
    "partially_fulfilled": "mms-badge mms-badge--accent",
    "fulfilled": "mms-badge mms-badge--success",
    "cancelled": "mms-badge mms-badge--danger",
}


@register.filter
def pr_status_badge_class(value):
    return PR_STATUS_BADGE.get(value, "mms-badge mms-badge--muted")


PO_STATUS_BADGE = {
    "draft": "mms-badge mms-badge--muted",
    "sent": "mms-badge mms-badge--info",
    "partial": "mms-badge mms-badge--accent",
    "received": "mms-badge mms-badge--success",
    "closed_short": "mms-badge mms-badge--danger",
    "cancelled": "mms-badge mms-badge--danger",
}


@register.filter
def po_status_badge_class(value):
    return PO_STATUS_BADGE.get(value, "mms-badge mms-badge--muted")


@register.filter
def role_name(value):
    """Display a role code as its friendly name (e.g. 'procurement' -> 'Maintenance Supply Officer')."""
    return role_display_name(value)


@register.filter
def user_role(value):
    """Display the friendly role name for a User object (or its .role)."""
    if hasattr(value, "role"):
        return role_display_name(getattr(value, "role", "") or "")
    return role_display_name(value or "")


@register.simple_tag
def part_primary_image(part):
    """Return the primary image URL for a spare part, or None."""
    if not part or not part.pk:
        return None
    att = Attachment.objects.filter(
        entity_type='spare_part',
        entity_id=part.pk,
        is_primary=True
    ).first()
    if att:
        if att.thumbnail:
            return att.thumbnail.url
        return att.file.url
    return None
