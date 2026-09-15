"""Hebrew RTL quote PDF via ReportLab.

Why ReportLab and not WeasyPrint: WeasyPrint needs native GTK/Pango libraries
that are absent on stock Windows, so it cannot even import here. ReportLab is
pure Python and works anywhere.

Note on RTL: ReportLab does NOT implement the bidirectional algorithm -- it
draws glyphs in the order given -- so every Hebrew string must be reordered
into visual order with python-bidi before it reaches a Paragraph. Verified by
rendering the same strings both ways (see tests/test_pdf.py).

Caveat when testing this by eye: prefixing a sample with ASCII ("LABEL: ...")
changes the paragraph's resolved base direction and inverts which variant
looks correct. Always eyeball bare Hebrew strings.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from pathlib import Path

import re
import unicodedata

from bidi.algorithm import get_display
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from app.config import LOGO_PATH, QUOTES_DIR, BusinessConfig, ensure_dirs
from app.models import KitchenSpec, QuoteBreakdown

log = logging.getLogger(__name__)

FONT_CANDIDATES = [
    (Path("C:/Windows/Fonts/arial.ttf"), Path("C:/Windows/Fonts/arialbd.ttf")),
    (Path("C:/Windows/Fonts/david.ttf"), Path("C:/Windows/Fonts/davidbd.ttf")),
    (
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    ),
]

_fonts_ready: tuple[str, str] | None = None


def _register_fonts() -> tuple[str, str]:
    """Register a Hebrew-capable TTF pair, falling back to built-ins."""
    global _fonts_ready
    if _fonts_ready is not None:
        return _fonts_ready
    for reg_path, bold_path in FONT_CANDIDATES:
        if reg_path.exists():
            pdfmetrics.registerFont(TTFont("HebReg", str(reg_path)))
            bold_src = bold_path if bold_path.exists() else reg_path
            pdfmetrics.registerFont(TTFont("HebBold", str(bold_src)))
            _fonts_ready = ("HebReg", "HebBold")
            return _fonts_ready
    log.warning("no Hebrew TTF found; Hebrew text will not render correctly")
    _fonts_ready = ("Helvetica", "Helvetica-Bold")
    return _fonts_ready


def rtl(text: str) -> str:
    """Reorder logical-order Hebrew into visual order for ReportLab.

    Numbers and Latin runs embedded in Hebrew stay left-to-right, which is
    what the bidi algorithm is for. See the module docstring.
    """
    if not text:
        return ""
    return get_display(str(text))


def _money(amount: float, symbol: str) -> str:
    return f"{symbol}{amount:,.0f}"


def _safe_filename_part(text: str) -> str:
    """Make a client name safe for a filename while keeping Hebrew readable.

    Hebrew is preserved (the carpenter recognizes the file by it); only the
    characters Windows and Telegram actually reject are stripped.
    """
    cleaned = unicodedata.normalize("NFC", text).strip()
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", cleaned)
    cleaned = re.sub(r"\s+", "-", cleaned)
    cleaned = cleaned.strip(".-")
    return cleaned[:60]


def quote_filename(
    spec: KitchenSpec, quote_id: int, on: date | None = None
) -> str:
    """Build the PDF filename: client name + date, per the carpenter's request.

    Falls back to the quote id when no client name was captured, so the file
    is never just a bare date.
    """
    stamp = (on or date.today()).strftime("%Y-%m-%d")
    client = _safe_filename_part(spec.client_name or "")
    if client:
        return f"{client}_{stamp}.pdf"
    return f"quote-{quote_id:05d}_{stamp}.pdf"


# Watermark tuning. Large enough to read as branding, faint enough that the
# line-item table stays legible on top of it.
WATERMARK_STRENGTH = 0.13
WATERMARK_WIDTH_FRAC = 0.80

# The watermark sits as a footer mark in the bottom third of the page. Its
# centre is placed this far up from the page bottom, as a fraction of page
# height -- i.e. the middle of the lower third.
WATERMARK_CENTER_Y_FRAC = 0.17

# Pixels differing from the sampled background by more than this count as
# content when cropping away the logo's empty margin.
_CONTENT_THRESHOLD = 12

_watermark_cache: ImageReader | None = None
_watermark_key: tuple[str, float, int] | None = None


def _prepare_watermark() -> ImageReader | None:
    """Load the logo and fade it toward white for use as a background.

    The supplied logo is an OPAQUE image (a .png-named JPEG with no alpha
    channel), so its cream background would paint over the whole page and
    canvas alpha alone cannot rescue it. Instead the pixels are blended
    toward white here, which both removes the background block and produces
    the faint wash a watermark needs. Cached, since this runs per page.
    """
    global _watermark_cache, _watermark_key

    if not LOGO_PATH.exists():
        return None

    try:
        stat = LOGO_PATH.stat()
        key = (str(LOGO_PATH), stat.st_mtime, stat.st_size)
        if _watermark_cache is not None and _watermark_key == key:
            return _watermark_cache

        from PIL import Image, ImageChops

        with Image.open(LOGO_PATH) as src:
            logo = src.convert("RGB")

        # Crop away the logo's empty margin. The supplied file is a 1600x1600
        # square whose wordmark covers only ~70% x 22% of it; placing the
        # whole square as a footer mark would put the visible letters in the
        # middle of a large dead box instead of where they were aimed.
        margin_bg = logo.getpixel((2, 2))
        mask = ImageChops.difference(
            logo, Image.new("RGB", logo.size, margin_bg)
        ).convert("L").point(lambda v: 255 if v > _CONTENT_THRESHOLD else 0)
        content = mask.getbbox()
        if content is not None:
            logo = logo.crop(content)

        # The logo sits on a cream field, not white. Blending straight to
        # white keeps that field as a visible tinted rectangle on the page,
        # so normalize the background up to pure white by scaling each channel
        # so it maps to 255. The dark wordmark is far from that value and
        # survives. Note the background colour is sampled BEFORE the crop --
        # after cropping, the corner pixel may be part of a letter.
        bg = margin_bg
        scales = [255.0 / max(c, 1) for c in bg]
        normalized = Image.merge("RGB", [
            # round(), not int(): truncation leaves the background a channel
            # short of pure white, which still reads as a faint rectangle.
            channel.point(lambda v, s=s: min(255, round(v * s)))
            for channel, s in zip(logo.split(), scales)
        ])
        # Guard against a logo whose corner is not background: if that made
        # the image essentially blank, fall back to the raw image.
        if ImageChops.difference(
            normalized, Image.new("RGB", logo.size, (255, 255, 255))
        ).getbbox() is None:
            normalized = logo

        white = Image.new("RGB", normalized.size, (255, 255, 255))
        faded = Image.blend(white, normalized, WATERMARK_STRENGTH)

        _watermark_cache = ImageReader(faded)
        _watermark_key = key
        return _watermark_cache
    except Exception:
        log.warning("could not prepare logo at %s; skipping watermark",
                    LOGO_PATH, exc_info=True)
        return None


def _draw_watermark(canvas, doc) -> None:
    """Paint the faded logo as a footer mark in the bottom third of the page.

    Drawn on the canvas rather than added to the story so it sits behind the
    content and repeats per page without affecting layout.
    """
    image = _prepare_watermark()
    if image is None:
        return

    iw, ih = image.getSize()
    page_w, page_h = A4
    target_w = page_w * WATERMARK_WIDTH_FRAC
    target_h = target_w * (ih / iw)

    x = (page_w - target_w) / 2
    # Centre it in the lower third, then clamp so a tall logo can neither run
    # off the bottom edge nor climb out of the bottom third of the page.
    y = page_h * WATERMARK_CENTER_Y_FRAC - target_h / 2
    y = max(0.0, min(y, page_h / 3.0 - target_h))
    if y < 0.0:
        # Taller than the band: sit it on the bottom margin instead.
        y = 0.0

    canvas.saveState()
    try:
        canvas.drawImage(
            image,
            x,
            y,
            width=target_w,
            height=target_h,
            preserveAspectRatio=True,
        )
    except Exception:
        log.exception("watermark draw failed; continuing without it")
    finally:
        canvas.restoreState()


def render_quote_pdf(
    spec: KitchenSpec,
    breakdown: QuoteBreakdown,
    business: BusinessConfig,
    quote_id: int,
) -> Path:
    ensure_dirs()
    regular, bold = _register_fonts()
    out_path = QUOTES_DIR / quote_filename(spec, quote_id)

    doc = SimpleDocTemplate(
        str(out_path),
        pagesize=A4,
        rightMargin=18 * mm,
        leftMargin=18 * mm,
        topMargin=18 * mm,
        bottomMargin=18 * mm,
        title=f"Quote {quote_id}",
    )

    h_title = ParagraphStyle(
        "title", fontName=bold, fontSize=20, alignment=TA_CENTER,
        spaceAfter=2 * mm, textColor=colors.HexColor("#1a1a1a"),
    )
    h_sub = ParagraphStyle(
        "sub", fontName=regular, fontSize=10, alignment=TA_CENTER,
        textColor=colors.HexColor("#666666"), spaceAfter=6 * mm,
    )
    h_right = ParagraphStyle(
        "right", fontName=regular, fontSize=10.5, alignment=TA_RIGHT, leading=16,
    )
    h_sec = ParagraphStyle(
        "sec", fontName=bold, fontSize=12, alignment=TA_RIGHT,
        spaceBefore=5 * mm, spaceAfter=2 * mm, textColor=colors.HexColor("#1a1a1a"),
    )
    h_small = ParagraphStyle(
        "small", fontName=regular, fontSize=8.5, alignment=TA_RIGHT,
        textColor=colors.HexColor("#777777"), leading=12,
    )

    story: list = []
    story.append(Paragraph(rtl(business.business_name), h_title))
    story.append(Paragraph(rtl("הצעת מחיר"), h_sub))

    today = date.today()
    valid_until = today + timedelta(days=business.quote_valid_days)
    meta_rows = []
    if spec.client_name:
        meta_rows.append([Paragraph(rtl(f"לכבוד: {spec.client_name}"), h_right)])
    meta_rows.append([Paragraph(rtl(f"מספר הצעה: {quote_id}"), h_right)])
    meta_rows.append(
        [Paragraph(rtl(f"תאריך: {today.strftime('%d/%m/%Y')}"), h_right)]
    )
    meta_rows.append(
        [Paragraph(rtl(f"בתוקף עד: {valid_until.strftime('%d/%m/%Y')}"), h_right)]
    )

    meta = Table(meta_rows, colWidths=[doc.width])
    meta.setStyle(
        TableStyle([
            ("ALIGN", (0, 0), (-1, -1), "RIGHT"),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
            ("TOPPADDING", (0, 0), (-1, -1), 1),
        ])
    )
    story.append(meta)

    spec_bits = []
    if spec.layout:
        spec_bits.append(f"צורה: {spec.layout}")
    if spec.dimensions_m:
        spec_bits.append(f"מידות: {spec.dimensions_m}")
    if spec.material:
        spec_bits.append(f"חומר: {spec.material}")
    if spec.countertop:
        spec_bits.append(f"משטח: {spec.countertop}")
    if spec_bits:
        story.append(Paragraph(rtl("מפרט"), h_sec))
        story.append(Paragraph(rtl(" | ".join(spec_bits)), h_right))

    story.append(Paragraph(rtl("פירוט"), h_sec))
    cell = ParagraphStyle(
        "cell", fontName=regular, fontSize=9.5, alignment=TA_RIGHT, leading=13,
    )
    head = ParagraphStyle(
        "head", fontName=bold, fontSize=9.5, alignment=TA_RIGHT,
        textColor=colors.white,
    )

    # RTL table: description column sits rightmost.
    data = [[
        Paragraph(rtl('סה"כ'), head),
        Paragraph(rtl("מחיר יח'"), head),
        Paragraph(rtl("תיאור"), head),
    ]]
    for item in breakdown.line_items:
        data.append([
            Paragraph(rtl(_money(item.subtotal, breakdown.currency_symbol)), cell),
            Paragraph(rtl(_money(item.unit_price, breakdown.currency_symbol)), cell),
            Paragraph(rtl(item.label), cell),
        ])

    col_w = [doc.width * 0.22, doc.width * 0.22, doc.width * 0.56]
    table = Table(data, colWidths=col_w, repeatRows=1)
    table.setStyle(
        TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2c3e50")),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1),
             [colors.white, colors.HexColor("#f5f6f7")]),
            ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#d8dbde")),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ])
    )
    story.append(table)

    story.append(Spacer(1, 4 * mm))
    tot_label = ParagraphStyle("tl", fontName=regular, fontSize=10, alignment=TA_RIGHT)
    tot_bold = ParagraphStyle("tb", fontName=bold, fontSize=13, alignment=TA_RIGHT)

    # Always show the pre-VAT subtotal, the VAT line, and the final total, so
    # the client can see both numbers rather than inferring one from the other.
    sym = breakdown.currency_symbol
    rows = [
        [
            Paragraph(rtl(_money(breakdown.subtotal, sym)), tot_label),
            Paragraph(rtl('סה"כ לפני מע"מ'), tot_label),
        ],
        [
            Paragraph(rtl(_money(breakdown.vat_amount, sym)), tot_label),
            Paragraph(rtl(f'מע"מ {business.vat_pct:g}%'), tot_label),
        ],
        [
            Paragraph(rtl(_money(breakdown.total, sym)), tot_bold),
            Paragraph(rtl('סה"כ לתשלום כולל מע"מ'), tot_bold),
        ],
    ]

    totals = Table(rows, colWidths=[doc.width * 0.3, doc.width * 0.3], hAlign="RIGHT")
    totals.setStyle(
        TableStyle([
            ("LINEABOVE", (0, -1), (-1, -1), 1.0, colors.HexColor("#2c3e50")),
            ("TOPPADDING", (0, -1), (-1, -1), 5),
            ("ALIGN", (0, 0), (-1, -1), "RIGHT"),
        ])
    )
    story.append(totals)

    if business.payment_terms:
        story.append(Paragraph(rtl("תנאי תשלום"), h_sec))
        story.append(Paragraph(rtl(business.payment_terms), h_right))

    story.append(Spacer(1, 6 * mm))
    story.append(
        Paragraph(
            rtl(
                f"הצעה זו בתוקף {business.quote_valid_days} ימים. "
                "המחירים אינם כוללים שינויים שלא צוינו במפרט."
            ),
            h_small,
        )
    )

    doc.build(story, onFirstPage=_draw_watermark, onLaterPages=_draw_watermark)
    log.info("rendered quote PDF -> %s", out_path)
    return out_path
