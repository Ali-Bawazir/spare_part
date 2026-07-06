# Arabic Font

PDFs in Arabic require an Arabic-capable TTF.

## Install

Download `NotoSansArabic-Regular.ttf` (or `NotoNaskhArabic-Regular.ttf`) and
place it in this directory:

  static/fonts/arabic/NotoSansArabic-Regular.ttf

The `pdf_utils._register_arabic_font()` function looks here as a fallback after
the standard OS paths.

Sources:
- Google Fonts: https://fonts.google.com/noto/specimen/Noto+Sans+Arabic
- GitHub: https://github.com/googlefonts/noto-fonts

After installing, run `python manage.py compilemessages` (no-op for fonts but
forces settings reload) and re-render a PDF in Arabic locale.

If no font is present, PDFs will fall back to Helvetica which renders Arabic as
boxes but does not crash.
