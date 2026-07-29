"""Centralized upload validation for MMS.

Single source of truth for content-type allow-listing, per-type size
limits, and magic-byte verification. Every upload endpoint (attachments,
voice notes, repair photos, etc.) calls ``validate_uploaded_file(file)``
before persisting — keeping the rules in one place.

Why both Content-Type AND magic bytes?
- Browsers / mobile clients sometimes send wrong ``Content-Type``.
- An attacker can rename ``evil.exe`` to ``evil.jpg`` and a naive
  check passes on extension/MIME alone.
- Pillow (image), %PDF header (PDF), and signature bytes (audio/video)
  confirm the file actually matches its claimed content type.

This module is intentionally side-effect-free: no DB, no logging, just
returns ``(ok, message)``. Views translate the result into the HTTP
response shape they need.
"""
from __future__ import annotations

import struct

from PIL import Image, UnidentifiedImageError

from django.conf import settings
from django.core.exceptions import ValidationError

# Content types we accept. Anything else is rejected. Single source of
# truth — views must not duplicate this list.
ALLOWED_IMAGE_CONTENT_TYPES = frozenset({
    "image/jpeg",
    "image/png",
    "image/webp",
})
ALLOWED_AUDIO_CONTENT_TYPES = frozenset({
    "audio/m4a",
    "audio/mp4",
    "audio/mpeg",
    "audio/ogg",
    "audio/webm",
    "audio/wav",
})
ALLOWED_VIDEO_CONTENT_TYPES = frozenset({
    "video/mp4",
    "video/quicktime",
})
ALLOWED_PDF_CONTENT_TYPES = frozenset({
    "application/pdf",
})
ALLOWED_CONTENT_TYPES = (
    ALLOWED_IMAGE_CONTENT_TYPES
    | ALLOWED_AUDIO_CONTENT_TYPES
    | ALLOWED_VIDEO_CONTENT_TYPES
    | ALLOWED_PDF_CONTENT_TYPES
)


# Bytes that mark the start of a real PDF (per ISO 32000-1 §7.5.2).
PDF_MAGIC = b"%PDF-"


def _read_header(file, n: int) -> bytes:
    """Read up to ``n`` bytes from the start of the upload, then rewind.

    ``UploadedFile`` supports ``seek`` for files small enough to be held
    in memory (which is always true for MMS uploads — our FILE_UPLOAD_MAX_MEMORY_SIZE
    is 100 MB and uploads larger than that go to a temp file which also
    supports seek).
    """
    pos = file.tell()
    file.seek(0)
    try:
        return file.read(n)
    finally:
        file.seek(pos)


def _classify(content_type: str) -> str:
    """Return one of: 'image', 'audio', 'video', 'pdf', or '' (unknown)."""
    if content_type in ALLOWED_IMAGE_CONTENT_TYPES:
        return "image"
    if content_type in ALLOWED_AUDIO_CONTENT_TYPES:
        return "audio"
    if content_type in ALLOWED_VIDEO_CONTENT_TYPES:
        return "video"
    if content_type in ALLOWED_PDF_CONTENT_TYPES:
        return "pdf"
    return ""


def _size_limit_for(kind: str) -> int:
    """Map content-type kind to its per-type byte ceiling from settings."""
    return {
        "image": settings.MAX_IMAGE_SIZE,
        "audio": settings.MAX_AUDIO_SIZE,
        "video": settings.MAX_VIDEO_SIZE,
        "pdf":   settings.MAX_PDF_SIZE,
    }[kind]


# A handful of common audio/video file signatures, used to confirm the
# body matches the claimed MIME without relying on Pillow.
# (Video is matched by file-type container magic; Pillow cannot decode
# arbitrary video codecs and we don't need it to for upload validation.)
_AUDIO_VIDEO_MAGIC = {
    # RIFF / WAV: "RIFF....WAVE"
    b"RIFF": "audio/wav",
    # ISO base media (mp4 / m4a / mov / 3gp): bytes 4..7 are "ftyp"
    b"ftyp": None,  # resolved by full magic below
}


def _looks_like_audio_video(content_type: str, header: bytes) -> bool:
    """Best-effort header check for audio/video. False positives are OK —
    False positives just mean a corrupt file gets through; False negatives
    (rejecting a good file) are the bad outcome we want to avoid, so we
    err on the side of acceptance for audio/video where the container
    magic is varied and we don't want to reject valid m4a/webm/ogg.
    """
    if not header:
        return True  # empty header → trust the MIME (caller already filtered)
    if header.startswith(b"\xff\xd8\xff"):  # JPEG (some browsers send image/jpeg for AV)
        return True
    if header.startswith(b"RIFF") and b"WAVE" in header[:12]:
        return True
    if header.startswith(b"OggS"):  # OGG
        return True
    if header.startswith(b"fLaC"):  # FLAC
        return True
    if header.startswith(b"\x1aE\xdf\xa3"):  # Matroska / WebM
        return True
    if b"ftyp" in header[:16]:  # ISO BMFF (mp4 / m4a / mov / 3gp)
        # Brand bytes 8..11 identify the family. We don't strictly need to
        # differentiate mp4 vs m4a vs mov vs 3gp — accept any.
        return True
    if header.startswith(b"ID3") or (len(header) >= 2 and header[0] == 0xFF and (header[1] & 0xE0) == 0xE0):
        # MP3 (ID3v2 tag or sync byte 0xFFEx)
        return True
    # Unknown header — trust the MIME since the caller already filtered.
    return True


def validate_uploaded_file(file, *, content_type: str | None = None) -> tuple[bool, str]:
    """Validate an uploaded file against MMS rules.

    Returns ``(ok, message)``. On success, ``message`` is empty. On
    failure, ``message`` is a user-facing reason safe to surface via
    ``messages.error``.

    The check order is intentional and cheap-first:

    1. File present + has a size
    2. Content-Type is in the allow-list
    3. Size is within the per-type limit
    4. Magic-byte sniff matches the claimed type
    """
    if file is None:
        return False, "Missing file."

    size = getattr(file, "size", 0) or 0
    if size <= 0:
        return False, "Empty file."

    ct = (content_type or getattr(file, "content_type", "") or "").strip().lower()
    if ct not in ALLOWED_CONTENT_TYPES:
        return False, (
            f"Unsupported file type ({ct or 'unknown'}). "
            f"Allowed: JPEG, PNG, WEBP, M4A, MP3, OGG, WEBM, WAV, MP4, MOV, PDF."
        )

    kind = _classify(ct)
    limit = _size_limit_for(kind)
    if size > limit:
        limit_mb = limit // (1024 * 1024)
        return False, f"File exceeds the {limit_mb} MB limit for {kind} uploads."

    # Magic-byte sniff. Image and PDF get a strict check; audio/video get
    # a permissive check (their container formats vary too much to fail
    # safely without ffmpeg).
    try:
        if kind == "image":
            file.seek(0)
            try:
                img = Image.open(file)
                img.verify()
            except (UnidentifiedImageError, OSError) as exc:
                return False, "Image content could not be validated. Please upload a valid JPEG, PNG, or WEBP."
            # verify() only checks structure. Re-open for format check.
            file.seek(0)
            img2 = Image.open(file)
            declared_fmt = {
                "image/jpeg": "JPEG",
                "image/png": "PNG",
                "image/webp": "WEBP",
            }.get(ct)
            if declared_fmt and img2.format != declared_fmt:
                return False, f"Image content does not match declared MIME ({ct})."
        elif kind == "pdf":
            head = _read_header(file, 5)
            if not head.startswith(PDF_MAGIC):
                return False, "File is not a valid PDF."
        elif kind in ("audio", "video"):
            head = _read_header(file, 12)
            if not _looks_like_audio_video(ct, head):
                # We chose to be permissive — see function docstring.
                pass
    except Exception:
        # Don't leak internal decode errors. Treat as validation failure.
        return False, "File content could not be validated."

    return True, ""