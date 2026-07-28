"""Backfill thumbnails for any Attachment missing one.

Usage:
    python manage.py backfill_thumbnails              # process all missing
    python manage.py backfill_thumbnails --dry-run   # preview only
    python manage.py backfill_thumbnails --limit 50  # first 50 only
"""
import logging

from django.core.management.base import BaseCommand

from maintenance.models import Attachment

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Generate thumbnails for attachments that are missing one."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true",
                            help="Show what would be processed; don't generate.")
        parser.add_argument("--limit", type=int, default=None,
                            help="Only process the first N attachments.")

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        limit = options["limit"]

        qs = Attachment.objects.filter(thumbnail="").order_by("pk")
        if limit is not None:
            qs = qs[:limit]

        total = 0
        generated = 0
        skipped = 0
        for att in qs:
            total += 1
            self.stdout.write(f"[{att.pk}] {att.filename} ({att.entity_type}:{att.entity_id})")
            if dry_run:
                continue
            try:
                att._generate_thumbnail()
                att.refresh_from_db()
                if att.thumbnail:
                    generated += 1
                    self.stdout.write(self.style.SUCCESS(f"  -> generated"))
                else:
                    skipped += 1
                    self.stdout.write(self.style.WARNING(f"  -> skipped (thumbnail still empty)"))
            except Exception as e:
                skipped += 1
                self.stdout.write(self.style.ERROR(f"  -> error: {e}"))

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS(
            f"Processed {total} | Generated {generated} | Skipped {skipped}"
        ))
        if dry_run:
            self.stdout.write(self.style.WARNING("(dry-run — no changes made)"))