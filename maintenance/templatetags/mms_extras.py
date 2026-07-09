from django import template
from django.utils.safestring import mark_safe
from django.utils.translation import gettext

from accounts.utils import role_display_name

from maintenance.models import Attachment

register = template.Library()

WO_STATUS_BADGE = {
    "assigned": "mms-badge mms-badge--info",
    "in_progress": "mms-badge mms-badge--primary",
    "pending_review": "mms-badge mms-badge--accent",
    "closed": "mms-badge mms-badge--success",
    "cancelled": "mms-badge mms-badge--muted",
    "paused": "mms-badge mms-badge--warning",
    "pending_parts": "mms-badge mms-badge--warning",
    "waiting_vendor": "mms-badge mms-badge--warning",
    "active": "mms-badge mms-badge--info",
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
    "allocated": "mms-badge mms-badge--info",
    "issued": "mms-badge mms-badge--primary",
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


@register.filter
def get_item(dictionary, key):
    """Template filter: dict lookup with variable key."""
    if dictionary is None:
        return None
    try:
        return dictionary.get(key)
    except (AttributeError, TypeError):
        return None


@register.filter
def qty_no_zeros(value):
    """Render a Decimal (or numeric) as a clean string without
    trailing zeros: Decimal('2.000') -> '2', Decimal('1.500') -> '1.5'.
    Falls back to str() for non-Decimal inputs.
    """
    from decimal import Decimal
    if value is None or value == "":
        return ""
    if isinstance(value, Decimal):
        if value == 0:
            return "0"
        try:
            return format(value.normalize(), "f")
        except Exception:
            return str(value)
    return str(value)


@register.simple_tag
def i18n_json(*pairs):
    """Render a JSON object containing translated strings for use by embedded JS.

    Usage:
        {% i18n_json "recording" "● recording" "uploading" "uploading…" ... %}
    Renders:
        <script type="application/json" id="i18n-json">{"recording": "● recording", ...}</script>

    The script tag is hidden (no rendering effect) and is read by JS as:
        const T = JSON.parse(document.getElementById('i18n-json').textContent);
    """
    import json
    if len(pairs) % 2 != 0:
        raise template.TemplateSyntaxError(
            "i18n_json requires an even number of arguments (key, value pairs)."
        )
    data = {pairs[i]: gettext(pairs[i + 1]) for i in range(0, len(pairs), 2)}
    return mark_safe(
        f'<script type="application/json" id="i18n-json">'
        f'{json.dumps(data, ensure_ascii=False)}'
        f'</script>'
    )
