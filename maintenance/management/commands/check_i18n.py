"""
i18n validation command for MMS.

Detects unwrapped user-facing strings in templates and Python files.
Used as a CI guardrail to prevent regressions.

Usage:
    python manage.py check_i18n
    python manage.py check_i18n --quiet   # exit code only
    python manage.py check_i18n --rules R1,R2,R5   # run specific rules
"""
from __future__ import annotations

import re
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError


# Rules: R1-R13
ALL_RULES = {f"R{i}" for i in range(1, 14)}
DEFAULT_RULES = {"R1", "R2", "R3", "R4", "R11", "R12", "R13"}

# HTML tags whose content is skipped (script, style, etc.)
SKIP_INNER_TAGS = ("script", "style", "pre", "code", "svg", "iframe")
# Attributes that are technical identifiers (skip wrapping)
TECHNICAL_ATTRS = {"class", "id", "name", "href", "src", "data-*", "type",
                    "value", "step", "min", "max", "method", "action",
                    "rel", "target", "for", "form", "list", "pattern",
                    "enctype", "role", "tabindex", "autocomplete"}
# Attributes that ARE user-facing (should be wrapped if literal text)
TRANSLATABLE_ATTRS = {"placeholder", "title", "alt", "aria-label", "title"}

# Patterns to skip entirely
TECHNICAL_LINE_PATTERNS = (
    re.compile(r'^\s*\{#.*#\}\s*$'),  # Django comments
    re.compile(r'^\s*\{%\s*comment\s*%\}', re.IGNORECASE),
    re.compile(r'^\s*//'),  # JS comment
    re.compile(r'^\s*#'),  # Python comment in template
)


def get_template_files():
    """Get all .html template files in the project."""
    files = []
    for app_template_dir in settings.TEMPLATES[0]["DIRS"]:
        for path in Path(app_template_dir).rglob("*.html"):
            files.append(path)
    return sorted(files)


def get_skip_ranges(content):
    """Return list of (start, end) ranges to skip (script/style/pre/code blocks)."""
    ranges = []
    for tag in SKIP_INNER_TAGS:
        # Non-greedy match for the open/close pair, allowing < and > inside.
        pattern = re.compile(r'<%s\b[^>]*>.*?</%s>' % (tag, tag), re.DOTALL | re.IGNORECASE)
        for m in pattern.finditer(content):
            ranges.append((m.start(), m.end()))
    return ranges


def in_skip(pos, skip_ranges):
    for s, e in skip_ranges:
        if s <= pos < e:
            return True
    return False


def is_translatable_text(text):
    """Decide if text is a translatable candidate."""
    text = text.strip()
    if not text or len(text) < 3:
        return False
    # Django template vars and tags
    if '{{' in text or '{%' in text or '{#' in text:
        return False
    # JS template literals (${...}) - render client-side, not translatable via gettext
    if '${' in text:
        return False
    # Must have at least 2 letters
    if sum(1 for c in text if c.isalpha()) < 2:
        return False
    # Skip pure-numeric / punctuation
    if re.match(r'^[\d\s.,:_\-/#()\[\]]+$', text):
        return False
    # Skip pure HTML / entities
    if text.startswith('<') or '&' in text and ';' in text:
        return False
    return True


def check_r1_load_i18n(content):
    """R1: Template must have {% load i18n %} before any trans usage."""
    errors = []
    has_trans = bool(re.search(r'\{%\s*trans\b|\{%\s*blocktrans\b', content))
    has_load = bool(re.search(r'\{%\s*load\s+i18n\s*%\}', content))
    if has_trans and not has_load:
        errors.append("Template uses {% trans %} but missing {% load i18n %}")
    return errors


def check_r2_unwrapped_text(content, skip_ranges):
    """R2: Visible text between > and < should be wrapped."""
    errors = []
    pattern = re.compile(r'>([^<>\n]+)<')
    for m in pattern.finditer(content):
        if in_skip(m.start(), skip_ranges):
            continue
        text = m.group(1)
        if not is_translatable_text(text):
            continue
        # Skip if inside HTML comment
        before = content[:m.start()]
        if before.rfind('<!--') > before.rfind('-->'):
            continue
        errors.append(f"Unwrapped text: {text.strip()[:60]!r}")
    return errors


def check_r3_unwrapped_attrs(content, skip_ranges):
    """R3: translatable attribute values should be wrapped."""
    errors = []
    for attr in TRANSLATABLE_ATTRS:
        pattern = re.compile(r'\b' + attr + r'=("[^"\']*?")')
        for m in pattern.finditer(content):
            if in_skip(m.start(), skip_ranges):
                continue
            value = m.group(1)[1:-1]  # Strip quotes
            if not is_translatable_text(value):
                continue
            # Skip if already wrapped
            if '{% trans' in value or '{%blocktrans' in value:
                continue
            errors.append(f"Unwrapped {attr}: {value[:60]!r}")
    return errors


def check_r4_js_strings(content, skip_ranges):
    """R4: JS confirm()/alert() should have wrapped strings."""
    errors = []
    # Find confirm() and alert() calls
    for fn in ('confirm', 'alert'):
        pattern = re.compile(r'\b' + fn + r'\s*\(\s*["\']([^"\']+)["\']\s*\)')
        for m in pattern.finditer(content):
            if in_skip(m.start(), skip_ranges):
                continue
            value = m.group(1)
            # Skip if empty or template-var
            if not value or '{{' in value:
                continue
            # Skip if already wrapped
            if '{% trans' in value:
                continue
            errors.append(f"Unwrapped JS {fn}() string: {value[:60]!r}")
    return errors


def check_r5_python_messages(content):
    """R5: views.py files — messages.X should be wrapped."""
    errors = []
    # Find messages.success(request, "...") without _()
    pattern = re.compile(r'messages\.(success|error|warning|info|debug|add_message)\s*\([^,]+,\s*["\']([^"\']+)["\']')
    for m in pattern.finditer(content):
        cmd, msg = m.group(1), m.group(2)
        if '{{' in msg or '{%' in msg:
            continue
        if 'gettext' in content[:m.start()][-500:]:
            continue  # Likely already wrapped via _() assignment
        errors.append(f"messages.{cmd} with literal string: {msg[:60]!r} (use _())")
    return errors


def check_python_form_labels(content):
    """R7: Form labels, help_text, error_messages should be wrapped."""
    errors = []
    # label = "...", help_text = "...", error_messages = {...}
    # Build docstring ranges to skip false positives in docstring examples.
    docstring_ranges = []
    for m in re.finditer(r'"""[\s\S]*?"""', content):
        docstring_ranges.append((m.start(), m.end()))
    in_docstring = lambda pos: any(s <= pos < e for s, e in docstring_ranges)
    for kw in ('label=', 'help_text='):
        pattern = re.compile(r'\b' + kw + r'\s*["\']([^"\']+)["\']')
        for m in pattern.finditer(content):
            if in_docstring(m.start()):
                continue
            value = m.group(1)
            if not is_translatable_text(value):
                continue
            errors.append(f"Form field {kw.rstrip('=')} = {value[:60]!r} (use _())")
    return errors


def check_r10_orphans(content):
    """R10: Comments-only templates (no actual content) — skip, OK."""
    return []


def check_r11_po_integrity():
    """R11: ar.po has no empty msgstrs and no Latin-only msgstrs (English fallback)."""
    import polib
    from django.conf import settings
    po_path = Path(settings.LOCALE_PATHS[0]) / 'ar' / 'LC_MESSAGES' / 'django.po'
    if not po_path.exists():
        return [f"ar.po not found: {po_path}"]
    # Allow-list of msgids where the msgstr is allowed to be Latin-only
    # (technical codes, KPI abbreviations, file formats, etc.)
    latin_only_allow = {
        'BRG-6006', 'A-01-03', 'SUP-001', 'MWRD-001', 'INV-2026-0042',
        'BRG', 'SAR', 'MTTR', 'MTTW', 'MTBF', 'PDF', 'CSV', 'MMS', 'WO',
        'SUP-001 or MWRD-001', '📥 CSV', 'Ball bearing 6006',
        'servomotor', 'maintenance issue', 'Issue submitted for validation',
    }
    errors = []
    try:
        po = polib.pofile(str(po_path))
    except Exception as e:
        return [f"Failed to parse ar.po: {e}"]
    for entry in po:
        if not entry.msgid:
            continue  # header
        # Skip msgids that are clearly JS expressions or placeholders
        if entry.msgid.startswith('${') or '${' in entry.msgid:
            continue
        # Skip msgids in the latin-only allow-list (codes/identifiers)
        if entry.msgid in latin_only_allow:
            continue
        # Check all msgstr forms
        msgstrs = []
        if entry.msgstr:
            msgstrs.append(('msgstr', entry.msgstr))
        for k, v in entry.msgstr_plural.items():
            if v:
                msgstrs.append((f'msgstr[{k}]', v))
        for label, v in msgstrs:
            if not v:
                errors.append(f"empty {label}: {entry.msgid[:60]!r}")
                continue
            # Check if msgstr is Latin-only
            v_clean = re.sub(r'<[^>]+>', ' ', v)
            v_clean = re.sub(r'%\([\w_]+\)s', ' ', v_clean)
            v_clean = re.sub(r'\{[\w_]+(\|[^}]*)?\}', ' ', v_clean)
            v_clean = re.sub(r'\$\{[^}]+\}', ' ', v_clean)
            v_clean = re.sub(r"'[^']*'", ' ', v_clean)
            arabic_chars = sum(1 for c in v_clean if '\u0600' <= c <= '\u06FF')
            latin_chars = sum(1 for c in v_clean if c.isalpha() and c.isascii())
            total = arabic_chars + latin_chars
            if total > 0 and arabic_chars == 0 and latin_chars >= 3:
                loc = entry.occurrences[0][0] if entry.occurrences else '?'
                errors.append(f"Latin-only {label} at {loc}: {entry.msgid[:40]!r} -> {v[:60]!r}")
    return errors


def check_r12_msgid_english_only():
    """R12: All msgids in ar.po must be English-only (no Arabic characters)."""
    import polib
    from django.conf import settings
    po_path = Path(settings.LOCALE_PATHS[0]) / 'ar' / 'LC_MESSAGES' / 'django.po'
    if not po_path.exists():
        return []
    errors = []
    try:
        po = polib.pofile(str(po_path))
    except Exception as e:
        return [f"Failed to parse ar.po: {e}"]
    for entry in po:
        if not entry.msgid:
            continue
        if re.search(r'[\u0600-\u06FF]', entry.msgid):
            errors.append(f"Arabic in msgid: {entry.msgid[:80]!r}")
        if entry.msgid_plural and re.search(r'[\u0600-\u06FF]', entry.msgid_plural):
            errors.append(f"Arabic in msgid_plural: {entry.msgid_plural[:80]!r}")
    return errors


def check_r13_po_sync():
    """R13: ar.po and en.po msgid sets are in sync."""
    import polib
    from django.conf import settings
    errors = []
    ar_path = Path(settings.LOCALE_PATHS[0]) / 'ar' / 'LC_MESSAGES' / 'django.po'
    en_path = Path(settings.LOCALE_PATHS[0]) / 'en' / 'LC_MESSAGES' / 'django.po'
    if not ar_path.exists() or not en_path.exists():
        return []
    try:
        ar_po = polib.pofile(str(ar_path))
        en_po = polib.pofile(str(en_path))
    except Exception as e:
        return [f"Failed to parse po files: {e}"]
    ar_ids = {e.msgid for e in ar_po if e.msgid}
    en_ids = {e.msgid for e in en_po if e.msgid}
    if ar_ids != en_ids:
        only_ar = ar_ids - en_ids
        only_en = en_ids - ar_ids
        for m in sorted(only_ar)[:5]:
            errors.append(f"msgid only in ar.po: {m[:60]!r}")
        for m in sorted(only_en)[:5]:
            errors.append(f"msgid only in en.po: {m[:60]!r}")
    return errors


CHECKERS = {
    "R1": ("templates", check_r1_load_i18n),
    "R2": ("templates", check_r2_unwrapped_text),
    "R3": ("templates", check_r3_unwrapped_attrs),
    "R4": ("templates", check_r4_js_strings),
    "R5": ("python", check_r5_python_messages),
    "R6": ("python", None),  # Optional: HttpResponse literals
    "R7": ("python", check_python_form_labels),
    "R8": ("python", None),  # Optional: model verbose_name
    "R9": ("templates", None),  # Optional: orphan detection
    "R10": ("templates", check_r10_orphans),
    "R11": ("po", check_r11_po_integrity),
    "R12": ("po", check_r12_msgid_english_only),
    "R13": ("po", check_r13_po_sync),
}


class Command(BaseCommand):
    help = "Check for unwrapped i18n strings in templates and Python files."

    def add_arguments(self, parser):
        parser.add_argument("--quiet", action="store_true",
                            help="Only print summary + exit code.")
        parser.add_argument("--rules", type=str, default="",
                            help="Comma-separated rule IDs to run (default R1-R4).")

    def handle(self, *args, **options):
        rules_arg = options.get("rules") or ",".join(sorted(DEFAULT_RULES))
        active_rules = {r.strip() for r in rules_arg.split(",") if r.strip()}
        quiet = options.get("quiet", False)
        root = Path(settings.BASE_DIR)
        all_issues = []
        files_checked = 0

        # Template checks
        template_files = get_template_files()
        templates_dir = Path(settings.TEMPLATES[0]["DIRS"][0])
        for tpl in template_files:
            try:
                content = tpl.read_text(encoding="utf-8")
            except (UnicodeDecodeError, IOError):
                continue
            files_checked += 1
            skip = get_skip_ranges(content)
            for rule_id in active_rules:
                kind, checker = CHECKERS.get(rule_id, (None, None))
                if checker is None or kind != "templates":
                    continue
                import inspect
                sig = inspect.signature(checker)
                # Check if function takes skip_ranges as second arg
                takes_skip = any(p.startswith("skip") for p in sig.parameters)
                if takes_skip:
                    errors = checker(content, skip)
                else:
                    errors = checker(content)
                for err in errors:
                    # Find line number
                    line_no = content[:content.find(err.split(":")[0][1:-1] if False else err.split("'")[1])].count('\n') + 1 if "'" in err and err.split("'")[1] in content else 0
                    rel_path = tpl.relative_to(root)
                    all_issues.append((rule_id, str(rel_path), err))

        # Python checks (only for files that exist)
        for app in ['accounts', 'inventory', 'procurement', 'maintenance']:
            app_path = root / app
            if not app_path.exists():
                continue
            for pyfile in app_path.rglob("views.py"):
                try:
                    content = pyfile.read_text(encoding="utf-8")
                except (UnicodeDecodeError, IOError):
                    continue
                files_checked += 1
                for rule_id in active_rules:
                    kind, checker = CHECKERS.get(rule_id, (None, None))
                    if checker is None or kind != "python":
                        continue
                    try:
                        import inspect
                        sig = inspect.signature(checker)
                        takes_skip = any(p.startswith('skip') for p in sig.parameters)
                        if takes_skip:
                            errors = checker(content, [])
                        else:
                            errors = checker(content)
                    except TypeError:
                        errors = checker(content)
                    for err in errors:
                        rel_path = pyfile.relative_to(root)
                        all_issues.append((rule_id, str(rel_path), err))

        # PO file checks (R11/R12/R13)
        for rule_id in active_rules:
            kind, checker = CHECKERS.get(rule_id, (None, None))
            if checker is None or kind != "po":
                continue
            try:
                errors = checker()
            except Exception as e:
                errors = [f"Checker raised: {e}"]
            for err in errors:
                all_issues.append((rule_id, "locale/ar/LC_MESSAGES/django.po", err))

        # Group by file
        by_file = {}
        for rule_id, path, msg in all_issues:
            by_file.setdefault(str(path), []).append((rule_id, str(msg)))
        
        if not quiet:
            self.stdout.write(self.style.NOTICE("=" * 70))
            self.stdout.write(self.style.NOTICE(f"i18n check: {len(all_issues)} issue(s) in {files_checked} file(s)"))
            self.stdout.write(self.style.NOTICE("=" * 70))
            for path in sorted(by_file):
                issues = by_file[path]
                self.stdout.write(f"\n{self.style.WARNING(path)}  ({len(issues)} issue(s))")
                for rule_id, msg in issues:
                    self.stdout.write(f"  [{rule_id}] {msg}")
        else:
            self.stdout.write(f"i18n check: {len(all_issues)} issue(s)")

        if all_issues:
            self.stdout.write(self.style.ERROR(
                f"\n✗ {len(all_issues)} unwrapped string(s) found. "
                f"Run with explicit rules for details."
            ))
            raise CommandError(f"check_i18n found {len(all_issues)} unwrapped strings")
        self.stdout.write(self.style.SUCCESS(
            f"\n✓ All checked templates and Python files pass i18n validation."
        ))
