"""One-shot migration of legacy local media to cloud object storage.

Scans ``MEDIA_ROOT`` (local FileSystemStorage) for files that were
uploaded before cloud storage was enabled, and re-uploads them through
the active default storage (which will be S3 in production).

Use cases:
- First-time switch from local media to cloud (no longer needed on a
  fresh prod DB, but ships for parity with the deployment history).
- Re-runs after a bucket migration or storage-class change.

Idempotent: existing files in the bucket with the same key are not
overwritten by default (pass ``--force`` to overwrite).

Examples:
    python manage.py migrate_media_to_cloud --dry-run
    python manage.py migrate_media_to_cloud
    python manage.py migrate_media_to_cloud --prefix attachments/_thumbs --force
"""
from __future__ import annotations

import os

from django.conf import settings
from django.core.files.storage import default_storage
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = (
        "Re-upload local MEDIA_ROOT files into the configured cloud "
        "storage (S3). Idempotent by default; --force overwrites."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="List files that would be uploaded without actually uploading.",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Overwrite existing files in cloud storage (default: skip).",
        )
        parser.add_argument(
            "--prefix",
            default=None,
            help=(
                "Only migrate files whose storage-relative path starts with "
                "this prefix. Defaults to MEDIA_ROOT_PREFIX ('attachments') or "
                "'attachments/_thumbs' when migrating thumbnails."
            ),
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        force = options["force"]
        prefix = options["prefix"] or getattr(settings, "MEDIA_ROOT_PREFIX", "attachments")

        # If default_storage is the same class as our local "from" storage,
        # there's nothing to migrate (dev mode where both are FileSystemStorage).
        from django.core.files.storage import FileSystemStorage
        if isinstance(default_storage, FileSystemStorage) and not getattr(settings, "AWS_STORAGE_BUCKET_NAME", None):
            self.stdout.write(
                self.style.WARNING(
                    "MMS_MEDIA_BUCKET not set; default storage is "
                    "FileSystemStorage. Nothing to migrate in dev mode."
                )
            )
            self.stdout.write("0 files migrated")
            return

        local_root = settings.MEDIA_ROOT
        if not os.path.isdir(local_root):
            self.stdout.write(f"MEDIA_ROOT does not exist: {local_root}")
            self.stdout.write("0 files migrated")
            return

        uploaded = 0
        skipped = 0
        ignored = 0
        errors = 0

        for dirpath, _dirnames, filenames in os.walk(local_root):
            for name in filenames:
                abs_path = os.path.join(dirpath, name)
                # Compute storage-relative key (forward slashes, relative
                # to MEDIA_ROOT).
                rel = os.path.relpath(abs_path, local_root).replace(os.sep, "/")
                if prefix and not rel.startswith(prefix + "/") and rel != prefix:
                    ignored += 1
                    continue
                try:
                    if default_storage.exists(rel) and not force:
                        skipped += 1
                        continue
                    with open(abs_path, "rb") as fh:
                        if dry_run:
                            self.stdout.write(f"would upload {rel}")
                        else:
                            default_storage.save(rel, fh)
                            self.stdout.write(self.style.SUCCESS(f"uploaded {rel}"))
                    uploaded += 1
                except Exception as exc:  # noqa: BLE001 — surface and continue
                    errors += 1
                    self.stdout.write(self.style.ERROR(f"failed {rel}: {exc}"))

        summary = (
            f"\n{('Would upload' if dry_run else 'Uploaded')}: {uploaded}, "
            f"Skipped (already in cloud): {skipped}, "
            f"Ignored (outside prefix): {ignored}, "
            f"Errors: {errors}"
        )
        self.stdout.write(self.style.SUCCESS(summary) if errors == 0 else self.style.WARNING(summary))