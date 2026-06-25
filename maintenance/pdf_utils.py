from __future__ import annotations

import logging
import os
from io import BytesIO
from typing import Optional

from django.http import HttpResponse
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm, cm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    BaseDocTemplate,
    Frame,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)

logger = logging.getLogger(__name__)


def _register_arabic_font() -> bool:
    """Register an Arabic-capable TTF with ReportLab under two names.

    Idempotent: the second and subsequent calls return the cached result
    without re-scanning the filesystem. Returns True if a font was found
    and registered, False otherwise.
    """
    if hasattr(_register_arabic_font, "_registered"):
        return _register_arabic_font._registered

    candidates = [
        "/System/Library/Fonts/SFArabic.ttf",
        "/usr/share/fonts/truetype/noto/NotoSansArabic-Regular.ttf",
        "/usr/share/fonts/truetype/noto/NotoNaskhArabic-Regular.ttf",
        "/Library/Fonts/NotoSansArabic-Regular.ttf",
    ]
    try:
        from django.conf import settings
        static_root = str(settings.STATIC_ROOT) if settings.STATIC_ROOT else ""
        if static_root:
            candidates.append(os.path.join(static_root, "fonts", "arabic", "NotoSansArabic-Regular.ttf"))
    except Exception:
        pass

    for path in candidates:
        if not os.path.exists(path):
            continue
        try:
            pdfmetrics.registerFont(TTFont("Arabic", path))
            pdfmetrics.registerFont(TTFont("Arabic-Bold", path))
            _register_arabic_font._registered = True
            return True
        except Exception as e:
            logger.warning(f"Failed to register Arabic font {path}: {e}")
            continue

    _register_arabic_font._registered = False
    logger.warning(
        "No Arabic font found. Install fonts-noto (Linux) or drop a TTF in static/fonts/arabic/. "
        "Arabic text in PDFs will show as boxes until then."
    )
    return False


def _shape_text(text: str) -> str:
    """Reshape + reorder Arabic text for ReportLab rendering.

    Latin-only text is passed through unchanged. Mixed text is reshaped
    (Arabic letters get contextual forms) and reordered via the Unicode
    Bidirectional Algorithm so ReportLab can render it correctly in a
    left-to-right paragraph.
    """
    if not text:
        return text
    has_arabic = any("\u0600" <= c <= "\u06FF" for c in text)
    if not has_arabic:
        return text
    try:
        import arabic_reshaper
        from bidi.algorithm import get_display
        try:
            reshaper = arabic_reshaper.ArabicReshaper()
        except TypeError:
            # Older API took a config arg; keep as fallback.
            reshaper = arabic_reshaper.ArabicReshaper(
                arabic_reshaper.config_for_default_arabic_languages()
            )
        reshaped = reshaper.reshape(text)
        return get_display(reshaped)
    except Exception as e:
        logger.warning(f"Arabic shaping failed for {text!r}: {e}")
        return text


def _safe_paragraph(text: str, style):
    """Render a Paragraph with Arabic font auto-detection.

    Latin text: standard Paragraph. Arabic text: reshape + bidi + <font>
    tag pointing at the registered Arabic font. If no Arabic font has
    been registered, the <font> tag references a missing font and
    ReportLab falls back to Helvetica (which renders Arabic as boxes,
    but does not crash).
    """
    if text is None:
        return Paragraph("", style)
    text_str = str(text)
    if not text_str:
        return Paragraph("", style)
    has_arabic = any("\u0600" <= c <= "\u06FF" for c in text_str)
    if has_arabic:
        _register_arabic_font()
        shaped = _shape_text(text_str)
        shaped_xml = (shaped.replace("&", "&amp;")
                          .replace("<", "&lt;")
                          .replace(">", "&gt;"))
        return Paragraph(f'<font name="Arabic">{shaped_xml}</font>', style)
    safe = (text_str.replace("&", "&amp;")
                    .replace("<", "&lt;")
                    .replace(">", "&gt;"))
    return Paragraph(safe, style)


def build_pdf_response(filename: str) -> BytesIO:
    buf = BytesIO()
    doc = BaseDocTemplate(
        buf,
        pagesize=A4,
        topMargin=20 * mm,
        bottomMargin=20 * mm,
        leftMargin=20 * mm,
        rightMargin=20 * mm,
        title=filename,
    )
    frame = Frame(
        doc.leftMargin,
        doc.bottomMargin,
        doc.width,
        doc.height,
        id="main",
    )
    doc.addPageTemplates([PageTemplate(id="main", frames=[frame])])
    return buf, doc


def _header_table(brand: str = "Bawazir Maintenance") -> Table:
    return Table(
        [[brand, "Maintenance & Spare Parts Management"]],
        colWidths=[80 * mm, 90 * mm],
        style=TableStyle([
            ("FONTNAME", (0, 0), (0, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (0, 0), 14),
            ("FONTNAME", (1, 0), (1, 0), "Helvetica"),
            ("FONTSIZE", (1, 0), (1, 0), 8),
            ("TEXTCOLOR", (1, 0), (1, 0), colors.gray),
            ("ALIGN", (1, 0), (1, 0), "RIGHT"),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ]),
    )


def _section(title: str) -> list:
    return [
        Spacer(1, 4 * mm),
        Table(
            [[Paragraph(f"<b>{title}</b>", getSampleStyleSheet()["Normal"])]],
            colWidths=[170 * mm],
            style=TableStyle([
                ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#1e3a5f")),
                ("TEXTCOLOR", (0, 0), (-1, -1), colors.white),
                ("FONTSIZE", (0, 0), (-1, -1), 10),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]),
        ),
        Spacer(1, 2 * mm),
    ]


def _field_table(rows: list[tuple[str, str]]) -> Table:
    """Render a key-value pair table for document fields."""
    normal_style = getSampleStyleSheet()["Normal"]
    processed = []
    for k, v in rows:
        if v is None or v == "":
            v = "—"
        processed.append([
            Paragraph(f"<b>{k}</b>", normal_style),
            _safe_paragraph(v, normal_style),
        ])
    t = Table(
        processed,
        colWidths=[55 * mm, 115 * mm],
        style=TableStyle([
            ("FONTSIZE", (0, 0), (-1, -1), 9),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("TOPPADDING", (0, 0), (-1, -1), 2),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.lightgrey),
        ]),
    )
    return t


def signature_block() -> Table:
    return Table(
        [["", ""], ["", ""], ["", ""]],
        colWidths=[80 * mm, 80 * mm],
        style=TableStyle([
            ("LINEABOVE", (0, 2), (-1, 2), 1, colors.black),
            ("TOPPADDING", (0, 2), (-1, 2), 20),
            ("FONTSIZE", (0, 0), (-1, -1), 9),
            ("ALIGN", (0, 2), (-1, 2), "CENTER"),
        ]),
    )
