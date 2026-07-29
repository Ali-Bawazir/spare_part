"""
QR code generation for PART and SUPPLIER codes.
Format: PART:{sku}   e.g. PART:BRG-6006
Format: SUPPLIER:{code}

QR PNGs are stored under MEDIA_ROOT_PREFIX so they share the bucket
layout with attachments (and benefit from the same CDN / Cache-Control).
"""
import qrcode
from io import BytesIO
from django.conf import settings
from django.core.files.storage import default_storage


def _qr_path(model_label: str, key: str) -> str:
    """Cloud-relative path for a QR PNG.

    Format: <MEDIA_ROOT_PREFIX>/_qr/<model_label>/<key>.png
    """
    prefix = getattr(settings, "MEDIA_ROOT_PREFIX", "attachments")
    return f"{prefix}/_qr/{model_label}/{key}.png"


def generate_part_qr(sku: str) -> bytes:
    """Generate QR PNG bytes for a PART code."""
    qr = qrcode.QRCode(version=1, box_size=10, border=4)
    qr.add_data(f"PART:{sku}")
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def save_part_qr(sku: str) -> str:
    """Save PART QR to the configured storage (S3 in prod, FS in dev).

    Returns the storage-relative path. Old on-disk QR files at
    ``media/qr/parts/PART_<sku>.png`` are ignored — the new path is
    authoritative. Empty prod DB means there are no old paths to migrate.
    """
    path = _qr_path("part", f"PART_{sku}")
    default_storage.save(path, BytesIO(generate_part_qr(sku)))
    return path


def get_part_qr_url(sku: str) -> str:
    """Return the public URL for the QR PNG of a part (CDN in prod)."""
    return default_storage.url(_qr_path("part", f"PART_{sku}"))


def generate_supplier_qr(code: str) -> bytes:
    """Generate QR PNG bytes for a SUPPLIER code."""
    qr = qrcode.QRCode(version=1, box_size=10, border=4)
    qr.add_data(f"SUPPLIER:{code}")
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def save_supplier_qr(code: str) -> str:
    """Save SUPPLIER QR to the configured storage."""
    path = _qr_path("supplier", f"SUPPLIER_{code}")
    default_storage.save(path, BytesIO(generate_supplier_qr(code)))
    return path


def get_supplier_qr_url(code: str) -> str:
    """Return the public URL for the QR PNG of a supplier."""
    return default_storage.url(_qr_path("supplier", f"SUPPLIER_{code}"))


def qr_scan_decode(raw: str) -> dict:
    """
    Decode a scanned QR raw string.
    Handles: PART:{sku}   → returns {"type": "part", "sku": "..."}
    Handles: INV:{part_id} → returns {"type": "inventory", "part_id": ...}
    Handles: SUPPLIER:{code} → returns {"type": "supplier", "code": "..."}
    Returns {"type": "unknown", "raw": raw} if format not recognized.
    """
    raw = raw.strip()
    if raw.startswith("PART:"):
        sku = raw[5:]
        return {"type": "part", "sku": sku}
    if raw.startswith("INV:"):
        try:
            part_id = int(raw[4:])
            return {"type": "inventory", "part_id": part_id}
        except ValueError:
            pass
    if raw.startswith("SUPPLIER:"):
        code = raw[9:]
        return {"type": "supplier", "code": code}
    return {"type": "unknown", "raw": raw}