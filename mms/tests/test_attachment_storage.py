"""Regression tests for the Phase 2.5 cloud-media storage fixes.

Covers two production bugs that escaped the M2.5 refactor:

1. ``attachment_upload_pending`` (used by the voice recorder on /issues/new/,
   /work-orders/<id>/, etc.) crashed with ``NameError: name 'MAX_AUDIO_SIZE'
   is not defined`` because the central validator refactor moved the size
   constant from a local ``MAX_AUDIO_SIZE`` symbol to
   ``settings.MAX_AUDIO_SIZE`` — but this view was missed and still
   referenced the old name. The user-facing symptom was a 500 with
   ``Failed to load resource`` in the browser console.

2. ``Attachment._generate_thumbnail`` previously used ``self.file.path``
   directly with ``os.makedirs`` and ``os.path.exists``. With S3 storage
   (the production backend), ``self.file.path`` returns the remote URL,
   so every save silently dropped the thumbnail — the upload itself
   succeeded but no thumbnail file was written to the bucket.

These tests guard both fixes so the bugs cannot regress.
"""
from __future__ import annotations

import io
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings

from accounts.models import User
from inventory.models import SparePart
from maintenance.models import Attachment


def _make_wav_bytes() -> bytes:
    """Build a minimal valid RIFF/WAVE header + a short silent body.

    Long enough for the validator's audio check (header sniff) and to make
    the file feel real to MMS — but tiny so tests stay fast.
    """
    sample_rate = 8000
    bits_per_sample = 8
    num_channels = 1
    duration_seconds = 1
    num_samples = sample_rate * duration_seconds
    data_size = num_samples * num_channels * (bits_per_sample // 8)

    import struct

    header = b"RIFF"
    header += struct.pack("<I", 36 + data_size)  # file size - 8
    header += b"WAVE"
    header += b"fmt "
    header += struct.pack("<I", 16)  # fmt chunk size
    header += struct.pack("<H", 1)   # PCM
    header += struct.pack("<H", num_channels)
    header += struct.pack("<I", sample_rate)
    header += struct.pack("<I", sample_rate * num_channels * (bits_per_sample // 8))
    header += struct.pack("<H", num_channels * (bits_per_sample // 8))
    header += struct.pack("<H", bits_per_sample)
    header += b"data"
    header += struct.pack("<I", data_size)
    return header + b"\x80" * data_size


def _make_png_bytes(width: int = 10, height: int = 10, color: str = "red") -> bytes:
    from PIL import Image

    img = Image.new("RGB", (width, height), color=color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _make_mp4_audio_bytes() -> bytes:
    """Build a minimal ISO BMFF (mp4/m4a) header — what iOS Safari MediaRecorder
    produces. Bytes 4..7 are 'ftyp', bytes 8..11 are the major brand.
    A real iOS recording is much larger and has mdat+moov boxes; the validator
    only inspects the first few bytes so a tiny stub is enough."""
    ftyp_box = b"\x00\x00\x00\x20"  # box size = 32 bytes
    ftyp_box += b"ftyp"
    ftyp_box += b"iso5"             # major brand = ISO Base Media 5 (m4a/mp4)
    ftyp_box += b"\x00\x00\x00\x01"  # minor version
    ftyp_box += b"isom"             # compatible brand
    # Tiny body to satisfy the audio sniff in mms.utils.uploads._looks_like_audio_video
    return ftyp_box + b"\x00" * 64


def _make_webm_audio_bytes() -> bytes:
    """Build a minimal Matroska/WebM header — what Chrome MediaRecorder produces.
    Real EBML header starts with 0x1A 0x45 0xDF 0xA3 followed by DocType='webm'.
    """
    ebml = b"\x1a\x45\xdf\xa3"  # EBML magic
    # DocType element: id=0x4282 size=4 data='webm'
    ebml += b"\x42\x82\x84"     # 0x4282 with size=4
    ebml += b"webm"
    return ebml + b"\x00" * 64


def _make_ogg_audio_bytes() -> bytes:
    """Build a minimal Ogg container header — what Firefox MediaRecorder produces."""
    return b"OggS" + b"\x00" * 100


class AttachmentUploadPendingTests(TestCase):
    """Regression tests for the NameError on /attachments/upload-pending/."""

    def setUp(self):
        self.manager = User.objects.create_user(
            username="manager_pending",
            password="pass1234",
            role=User.Role.MANAGER,
        )
        self.client.force_login(self.manager)

    def test_upload_pending_accepts_valid_audio_returns_json(self):
        """The fix: this endpoint no longer 500s on the missing constant.

        Before the fix the view referenced ``MAX_AUDIO_SIZE`` (a name from
        the pre-M2.5 code path that was no longer imported), which raised
        ``NameError`` and returned a 500 with HTML body — the AJAX caller
        in ``_voice_recorder.html`` saw ``r.json()`` throw and surfaced
        'Upload failed' to the user.
        """
        f = SimpleUploadedFile(
            "voice.webm",
            _make_wav_bytes(),
            content_type="audio/wav",
        )
        response = self.client.post("/attachments/upload-pending/", {"file": f})

        self.assertEqual(response.status_code, 200, response.content[:500])
        data = response.json()
        self.assertIn("id", data)
        self.assertIn("url", data)

        # The pending attachment is created with entity_type='pending_voice'
        # and entity_id=0 so the parent form can re-link it after save.
        att = Attachment.objects.get(pk=data["id"])
        self.assertEqual(att.entity_type, "pending_voice")
        self.assertEqual(att.entity_id, 0)
        self.assertEqual(att.uploaded_by, self.manager)
        # Validator now drives category; view uses "OTHER" for pending.
        self.assertEqual(att.category, "OTHER")

    def test_upload_pending_rejects_missing_file(self):
        response = self.client.post("/attachments/upload-pending/", {})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json(), {"error": "No file"})

    def test_upload_pending_rejects_oversized_audio(self):
        """Uses settings.MAX_AUDIO_SIZE via the central validator."""
        from django.conf import settings

        too_big = b"\x00" * (settings.MAX_AUDIO_SIZE + 1)
        f = SimpleUploadedFile(
            "voice.webm",
            too_big,
            content_type="audio/wav",
        )
        response = self.client.post("/attachments/upload-pending/", {"file": f})

        self.assertEqual(response.status_code, 400)
        self.assertIn("error", response.json())
        self.assertIn("25 MB", response.json()["error"])

    def test_upload_pending_rejects_disallowed_mime(self):
        """Central validator catches renamed executables."""
        f = SimpleUploadedFile(
            "malware.png",
            b"X5O!P%@AP[4\\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*",
            content_type="image/png",
        )
        response = self.client.post("/attachments/upload-pending/", {"file": f})
        self.assertEqual(response.status_code, 400)
        self.assertIn("error", response.json())

    def test_upload_pending_returns_json_on_storage_failure(self):
        """If ``Attachment.objects.create`` raises, the view must return
        JSON (not the Django 500 HTML page). Otherwise the client's
        ``r.json()`` throws and the user sees a generic 'Upload failed'
        with no actionable info.
        """
        f = SimpleUploadedFile(
            "voice.webm",
            _make_wav_bytes(),
            content_type="audio/wav",
        )
        with mock.patch(
            "maintenance.models.Attachment.objects.create",
            side_effect=RuntimeError("S3 is on fire"),
        ):
            response = self.client.post("/attachments/upload-pending/", {"file": f})

        self.assertEqual(response.status_code, 500)
        body = response.json()
        self.assertIn("error", body)
        # No traceback leaked to client.
        self.assertNotIn("Traceback", body["error"])

    def test_upload_pending_accepts_ios_safari_mp4_recording(self):
        """iOS Safari MediaRecorder produces audio/mp4 (ISO BMFF). The
        recorder's Blob type was previously hardcoded to audio/webm, so
        an iOS recording would be uploaded with the wrong MIME and
        silently fail to play back. This test asserts the server accepts
        audio/mp4 and stores the mime_type verbatim so playback templates
        can render an <audio> element.
        """
        f = SimpleUploadedFile(
            "voice-note.m4a",
            _make_mp4_audio_bytes(),
            content_type="audio/mp4",
        )
        response = self.client.post("/attachments/upload-pending/", {"file": f})

        self.assertEqual(response.status_code, 200, response.content[:500])
        data = response.json()
        self.assertIn("id", data)
        self.assertIn("url", data)

        att = Attachment.objects.get(pk=data["id"])
        self.assertEqual(
            att.mime_type,
            "audio/mp4",
            "server must preserve the audio/mp4 MIME so playback can match it",
        )

    def test_upload_pending_accepts_chrome_webm_recording(self):
        """Chrome MediaRecorder produces audio/webm (Matroska/EBML).
        Same as the iOS test but for Chrome's default format."""
        f = SimpleUploadedFile(
            "voice-note.webm",
            _make_webm_audio_bytes(),
            content_type="audio/webm",
        )
        response = self.client.post("/attachments/upload-pending/", {"file": f})

        self.assertEqual(response.status_code, 200, response.content[:500])
        att = Attachment.objects.get(pk=response.json()["id"])
        self.assertEqual(att.mime_type, "audio/webm")

    def test_upload_pending_accepts_firefox_ogg_recording(self):
        """Firefox MediaRecorder produces audio/ogg."""
        f = SimpleUploadedFile(
            "voice-note.ogg",
            _make_ogg_audio_bytes(),
            content_type="audio/ogg",
        )
        response = self.client.post("/attachments/upload-pending/", {"file": f})

        self.assertEqual(response.status_code, 200, response.content[:500])
        att = Attachment.objects.get(pk=response.json()["id"])
        self.assertEqual(att.mime_type, "audio/ogg")


class AttachmentUploadJsonFallbackTests(TestCase):
    """Defensive JSON-500 fallback for the main /attachments/upload/ endpoint."""

    def setUp(self):
        self.manager = User.objects.create_user(
            username="manager_json_fallback",
            password="pass1234",
            role=User.Role.MANAGER,
        )
        self.part1 = SparePart.objects.create(sku="BRG-FBK-01", name="Bearing 6201")
        self.client.force_login(self.manager)

    def test_attachment_upload_returns_json_500_on_create_failure(self):
        """Same hardening as the pending endpoint: unexpected exceptions
        must be surfaced as JSON 500, not Django's debug HTML page."""
        f = SimpleUploadedFile(
            "test.png",
            _make_png_bytes(),
            content_type="image/png",
        )
        with mock.patch(
            "maintenance.models.Attachment.objects.create",
            side_effect=RuntimeError("DB is down"),
        ):
            response = self.client.post(
                "/attachments/upload/",
                {
                    "entity_type": "spare_part",
                    "entity_id": self.part1.pk,
                    "file": f,
                },
            )

        self.assertEqual(response.status_code, 500)
        body = response.json()
        self.assertIn("error", body)
        self.assertNotIn("Traceback", body["error"])


class AttachmentThumbnailStorageTests(TestCase):
    """Regression: thumbnails must be written through default_storage
    so S3 production works. Previously the code called ``os.path.exists``
    and ``os.makedirs`` on a URL string and silently dropped the
    thumbnail for every uploaded image.
    """

    def setUp(self):
        self.manager = User.objects.create_user(
            username="manager_thumb",
            password="pass1234",
            role=User.Role.MANAGER,
        )
        self.part1 = SparePart.objects.create(sku="BRG-THMB-01", name="Bearing 6201")
        self.client.force_login(self.manager)

    def test_thumbnail_path_is_written_via_default_storage(self):
        """After uploading, the thumbnail field should be a relative
        storage path (NOT a URL like ``https://...``) and
        ``default_storage.exists`` should be True."""
        f = SimpleUploadedFile(
            "test.png",
            _make_png_bytes(),
            content_type="image/png",
        )
        self.client.force_login(self.manager)
        response = self.client.post(
            "/attachments/upload/",
            {
                "entity_type": "spare_part",
                "entity_id": self.part1.pk,
                "file": f,
            },
        )
        self.assertEqual(response.status_code, 200, response.content[:500])

        att = Attachment.objects.get(pk=response.json()["id"])
        # The thumbnail field is the storage-relative path; default_storage.url()
        # resolves it to the CDN URL when S3 is configured.
        thumb_name = att.thumbnail.name if att.thumbnail else ""
        self.assertTrue(thumb_name, "thumbnail should be set after image upload")
        self.assertFalse(
            thumb_name.startswith("http"),
            f"thumbnail should be a relative path, got {thumb_name!r}",
        )
        self.assertIn("_300.jpg", thumb_name)

        # Confirm default_storage knows about the file (FS in tests).
        from django.core.files.storage import default_storage
        self.assertTrue(default_storage.exists(thumb_name))

    def test_thumbnail_skipped_for_non_image(self):
        """PDF content with PNG MIME — Pillow can't decode, the
        upload must still succeed (200) and thumbnail must remain
        empty. No traceback must leak to the user.
        """
        # Build a real PDF: %PDF-1.4 header + minimal body.
        pdf_bytes = b"%PDF-1.4\n%fake\n1 0 obj\n<<>>\nendobj\n"
        f = SimpleUploadedFile(
            "doc.pdf",
            pdf_bytes,
            content_type="application/pdf",
        )
        self.client.force_login(self.manager)
        response = self.client.post(
            "/attachments/upload/",
            {
                "entity_type": "spare_part",
                "entity_id": self.part1.pk,
                "file": f,
            },
        )
        self.assertEqual(response.status_code, 200, response.content[:500])

        att = Attachment.objects.get(pk=response.json()["id"])
        # ImageFieldFile is truthy if it has a name set; check name explicitly.
        self.assertFalse(att.thumbnail.name, "PDF must not produce a thumbnail")
