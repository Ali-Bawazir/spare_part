"""
Video compression service (Phase 2D-1).

Stub for video attachment compression. Phase 2 will wire this to ffmpeg
via subprocess; for now, the service detects file size / extension and
returns a deterministic "skipped" CompressionResult so the call sites
can be exercised in tests.

Phase 2 plan: invoke ffmpeg via subprocess with
    -c:v libx264 -crf 28 -preset medium
to produce a smaller MP4 alongside the original. The compressed file
path will be stored in `Attachment.compressed_path` (already present on
the model).

Phase 1.x behavior:
    - If the file doesn't exist: raise FileNotFoundError.
    - If the file is not a video: skipped result, reason "not a video".
    - If the file is < 5MB: skipped result, reason "file too small".
    - If the file is >= 5MB: skipped result with compressed_path ==
      original_path and reason "Phase 2: ffmpeg not yet wired".
      (compressed_size == original_size; ratio_pct == 100)
"""
from __future__ import annotations

import os
from dataclasses import dataclass


_VIDEO_EXTS = {".mp4", ".mov", ".webm", ".avi"}
_SIZE_THRESHOLD_BYTES = 5 * 1024 * 1024  # 5 MB


class VideoCompressionService:
    """Compresses uploaded video attachments (Phase 2 v2 hook).

    Stateless. Call `VideoCompressionService.compress(file_path)` to
    obtain a frozen CompressionResult.
    """

    @dataclass(frozen=True)
    class CompressionResult:
        """Output of `compress`."""
        original_path: str
        compressed_path: str
        original_size: int
        compressed_size: int
        ratio_pct: int      # compressed_size / original_size * 100
        skipped: bool       # True if compression was skipped
        reason: str         # human-readable explanation

    @classmethod
    def compress(cls, file_path: str) -> "VideoCompressionService.CompressionResult":
        """Inspect a video file and return a CompressionResult.

        Phase 1.x: returns a skipped result unless the file is missing
        (raises FileNotFoundError) or non-video. Phase 2 will actually
        invoke ffmpeg for large video files.

        Args:
            file_path: absolute or relative path to the file on disk.

        Returns:
            CompressionResult describing what would happen.

        Raises:
            FileNotFoundError: if `file_path` does not exist.
        """
        if not file_path or not os.path.exists(file_path):
            raise FileNotFoundError(f"Video file not found: {file_path!r}")

        original_size = os.path.getsize(file_path)
        ext = os.path.splitext(file_path)[1].lower()

        if ext not in _VIDEO_EXTS:
            return cls.CompressionResult(
                original_path=file_path,
                compressed_path=file_path,
                original_size=original_size,
                compressed_size=original_size,
                ratio_pct=100,
                skipped=True,
                reason="not a video",
            )

        if original_size < _SIZE_THRESHOLD_BYTES:
            return cls.CompressionResult(
                original_path=file_path,
                compressed_path=file_path,
                original_size=original_size,
                compressed_size=original_size,
                ratio_pct=100,
                skipped=True,
                reason="file too small (<5MB) to benefit from compression",
            )

        # Phase 1.x stub: ffmpeg is not wired yet.
        return cls.CompressionResult(
            original_path=file_path,
            compressed_path=file_path,
            original_size=original_size,
            compressed_size=original_size,
            ratio_pct=100,
            skipped=True,
            reason="Phase 2: ffmpeg not yet wired",
        )


__all__ = ["VideoCompressionService"]
