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


# Rules: R1-R10
ALL_RULES = {f"R{i}" for i in range(1, 11)}
DEFAULT_RULES = {"R1", "R2", "R3", "R4"}

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
        pattern = re.compile(r'<%s[\s>][^<]*</%s>' % (tag, tag), re.DOTALL | re.IGNORECASE)
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
    for kw in ('label=', 'help_text='):
        pattern = re.compile(r'\b' + kw + r'\s*["\']([^"\']+)["\']')
        for m in pattern.finditer(content):
            value = m.group(1)
            if not is_translatable_text(value):
                continue
            errors.append(f"Form field {kw.rstrip('=')} = {value[:60]!r} (use _())")
    return errors


def check_r10_orphans(content):
    """R10: Comments-only templates (no actual content) — skip, OK."""
    return []


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
