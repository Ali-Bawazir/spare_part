from django import template

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
