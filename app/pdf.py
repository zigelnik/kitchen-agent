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


# Watermark tuning. The logo is a light wordmark, so it needs to be faint
# enough not to fight the table text but visible enough to read as branding.
WATERMARK_ALPHA = 0.10
WATERMARK_WIDTH_FRAC = 0.85


def _draw_watermark(canvas, doc) -> None:
    """Paint the logo as an enlarged, faint background on every page.

    Drawn on the canvas rather than added to the story so it sits behind the
    content and repeats per page without affecting layout.
    """
    if not LOGO_PATH.exists():
        return
    try:
        image = ImageReader(str(LOGO_PATH))
        iw, ih = image.getSize()
    except Exception:
        log.warning("could not read logo at %s; skipping watermark", LOGO_PATH)
        return

    page_w, page_h = A4
    target_w = page_w * WATERMARK_WIDTH_FRAC
    target_h = target_w * (ih / iw)

    canvas.saveState()
    try:
        # setFillAlpha needs a PDF 1.4+ transparency group; guard for older
        # ReportLab builds that lack it.
        if hasattr(canvas, "setFillAlpha"):
            canvas.setFillAlpha(WATERMARK_ALPHA)
        canvas.drawImage(
            image,
            (page_w - target_w) / 2,
            (page_h - target_h) / 2,
            width=target_w,
            height=target_h,
            mask="auto",
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
