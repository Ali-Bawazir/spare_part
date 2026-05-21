"""
QR code generation for PART and INV codes.
Format: PART:{sku}   e.g. PART:BRG-6006
"""
import qrcode
import os
from io import BytesIO
from django.conf import settings

def generate_part_qr(sku: str) -> bytes:
    """Generate QR PNG bytes for a PART code."""
    qr = qrcode.QRCode(version=1, box_size=10, border=4)
    qr.add_data(f"PART:{sku}")
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()

def save_part_qr(sku: str, destination: str = None) -> str:
    """Save PART QR to media/qr/parts/ and return the file path."""
    if destination is None:
        qr_dir = os.path.join(settings.MEDIA_ROOT, "qr", "parts")
        os.makedirs(qr_dir, exist_ok=True)
        destination = os.path.join(qr_dir, f"PART_{sku}.png")

    png_bytes = generate_part_qr(sku)
    with open(destination, "wb") as f:
        f.write(png_bytes)
    return destination

def get_part_qr_url(sku: str) -> str:
    """Return the URL path to the QR PNG for a part."""
    return f"/media/qr/parts/PART_{sku}.png"


def qr_scan_decode(raw: str) -> dict:
    """
    Decode a scanned QR raw string.
    Handles: PART:{sku}   → returns {"type": "part", "sku": "..."}
    Handles: INV:{part_id} → returns {"type": "inventory", "part_id": ...}
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
    return {"type": "unknown", "raw": raw}