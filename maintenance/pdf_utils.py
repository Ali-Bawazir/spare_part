from __future__ import annotations

from io import BytesIO
from typing import Optional

from django.http import HttpResponse
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm, cm
from reportlab.platypus import (
    BaseDocTemplate,
    Frame,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)


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
    t = Table(
        [[Paragraph(f"<b>{k}</b>", getSampleStyleSheet()["Normal"]), Paragraph(str(v), getSampleStyleSheet()["Normal"])] for k, v in rows],
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
