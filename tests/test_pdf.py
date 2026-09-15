"""PDF rendering tests, including the RTL regression that cost real debugging.

ReportLab does not implement the bidirectional algorithm, so Hebrew must be
reordered with python-bidi before it reaches a Paragraph. These tests pin that
down: if someone "simplifies" rtl() into a pass-through, the first test fails.
"""
from __future__ import annotations

import pymupdf
import pytest

from app.catalog import Catalog, CatalogItem
from app.config import BusinessConfig
from app.models import KitchenSpec
from app.pdf import render_quote_pdf, rtl
from app.pricing import calculate_quote


def test_rtl_reorders_hebrew_into_visual_order():
    # A pure-Hebrew word must come back reversed; that IS visual order for a
    # renderer that draws glyphs left to right.
    assert rtl("מפרט") == "מפרט"[::-1]


def test_rtl_keeps_embedded_numbers_left_to_right():
    # The digits must survive as "12", not "21" — the whole point of using the
    # bidi algorithm instead of a plain string reverse.
    assert "12" in rtl("פורניר אלון × 12 ארונות")


def test_rtl_is_not_a_plain_reverse_for_mixed_text():
    mixed = "עבודה × 28.0 שעות"
    assert rtl(mixed) != mixed[::-1]


def test_rtl_handles_empty():
    assert rtl("") == ""


@pytest.fixture
def business() -> BusinessConfig:
    return BusinessConfig(payment_terms="50% מקדמה, 50% בסיום")


@pytest.fixture
def catalog() -> Catalog:
    return Catalog([
        CatalogItem("מלמין 18", "material", "cabinet", 320.0),
        CatalogItem("שיש אבן קיסר", "material", "meter", 1450.0),
        CatalogItem("מגירה רגילה", "hardware", "unit", 120.0),
        CatalogItem("ציר רגיל", "hardware", "unit", 18.0),
        CatalogItem("ידית כרום", "hardware", "unit", 35.0),
        CatalogItem("הובלה והתקנה", "transport", "flat", 250.0),
    ])


def test_renders_a_readable_hebrew_pdf(catalog, business, tmp_path, monkeypatch):
    monkeypatch.setattr("app.pdf.QUOTES_DIR", tmp_path)
    spec = KitchenSpec(
        client_name="דני כהן",
        layout="L",
        material="מלמין 18",
        cabinet_count=10,
        drawer_count=4,
        countertop="שיש אבן קיסר",
        countertop_length_m=3.0,
        labor_hours=20.0,
    )
    breakdown = calculate_quote(spec, catalog, business)
    out = render_quote_pdf(spec, breakdown, business, 1)

    assert out.exists()
    doc = pymupdf.open(str(out))
    assert doc.page_count == 1
    text = doc[0].get_text()

    # The client name and the business name must both appear.
    assert "כהן" in text
    assert business.business_name in text
    # The total must be printed with thousands separators.
    assert f"{breakdown.total:,.0f}" in text
    doc.close()


def test_vat_rows_appear_only_when_vat_configured(catalog, business, tmp_path,
                                                  monkeypatch):
    monkeypatch.setattr("app.pdf.QUOTES_DIR", tmp_path)
    spec = KitchenSpec(material="מלמין 18", cabinet_count=4, drawer_count=0,
                       labor_hours=5.0)

    no_vat = calculate_quote(spec, catalog, business)
    p1 = render_quote_pdf(spec, no_vat, business, 11)
    t1 = pymupdf.open(str(p1))[0].get_text()

    vat_biz = business.model_copy(update={"vat_pct": 18.0})
    with_vat = calculate_quote(spec, catalog, vat_biz)
    p2 = render_quote_pdf(spec, with_vat, vat_biz, 12)
    t2 = pymupdf.open(str(p2))[0].get_text()

    assert "18" in t2
    assert len(t2) > len(t1)


# --- filename, VAT display, watermark ------------------------------------

def test_filename_uses_client_name_and_date():
    from datetime import date

    from app.pdf import quote_filename

    name = quote_filename(KitchenSpec(client_name="דני כהן"), 1, date(2026, 9, 15))
    assert name == "דני-כהן_2026-09-15.pdf"


def test_filename_falls_back_to_quote_id_without_a_client():
    from datetime import date

    from app.pdf import quote_filename

    name = quote_filename(KitchenSpec(), 7, date(2026, 9, 15))
    assert name == "quote-00007_2026-09-15.pdf"


def test_filename_strips_characters_windows_rejects():
    from datetime import date

    from app.pdf import quote_filename

    name = quote_filename(
        KitchenSpec(client_name='דני/כהן: בע"מ <x>'), 2, date(2026, 9, 15)
    )
    for bad in r'<>:"/\|?*':
        assert bad not in name
    assert name.endswith("_2026-09-15.pdf")
    # Hebrew must survive -- the carpenter recognizes the file by it.
    assert "דני" in name


def test_pdf_shows_subtotal_vat_and_inclusive_total(catalog, tmp_path, monkeypatch):
    """The carpenter asked for the price both before and after VAT."""
    monkeypatch.setattr("app.pdf.QUOTES_DIR", tmp_path)
    biz = BusinessConfig(vat_pct=18.0)
    spec = KitchenSpec(client_name="דני", material="מלמין 18",
                       cabinet_count=10, drawer_count=4, labor_hours=20.0)
    breakdown = calculate_quote(spec, catalog, biz)
    out = render_quote_pdf(spec, breakdown, biz, 1)

    text = pymupdf.open(str(out))[0].get_text()
    assert f"{breakdown.subtotal:,.0f}" in text
    assert f"{breakdown.vat_amount:,.0f}" in text
    assert f"{breakdown.total:,.0f}" in text
    assert "18" in text


def test_vat_rows_are_present_even_at_zero_vat(catalog, tmp_path, monkeypatch):
    """Both figures are always shown, so the layout must not depend on VAT."""
    monkeypatch.setattr("app.pdf.QUOTES_DIR", tmp_path)
    biz = BusinessConfig(vat_pct=0.0)
    spec = KitchenSpec(material="מלמין 18", cabinet_count=4, drawer_count=0,
                       labor_hours=5.0)
    breakdown = calculate_quote(spec, catalog, biz)
    out = render_quote_pdf(spec, breakdown, biz, 2)
    text = pymupdf.open(str(out))[0].get_text()
    assert f"{breakdown.total:,.0f}" in text


def test_render_succeeds_when_no_logo_file_exists(catalog, tmp_path, monkeypatch):
    """The watermark is optional: a missing logo must not break rendering."""
    monkeypatch.setattr("app.pdf.QUOTES_DIR", tmp_path)
    monkeypatch.setattr("app.pdf.LOGO_PATH", tmp_path / "definitely-absent.png")
    biz = BusinessConfig()
    spec = KitchenSpec(material="מלמין 18", cabinet_count=4, labor_hours=5.0)
    breakdown = calculate_quote(spec, catalog, biz)
    out = render_quote_pdf(spec, breakdown, biz, 3)
    assert out.exists()


def test_watermark_is_drawn_when_a_logo_exists(catalog, tmp_path, monkeypatch):
    """A real logo must land in the PDF as an embedded image."""
    logo = tmp_path / "logo.png"
    try:
        from PIL import Image

        Image.new("RGB", (400, 200), (200, 170, 60)).save(logo)
    except ImportError:
        pytest.skip("PIL not available to synthesize a logo")

    monkeypatch.setattr("app.pdf.QUOTES_DIR", tmp_path)
    monkeypatch.setattr("app.pdf.LOGO_PATH", logo)
    biz = BusinessConfig()
    spec = KitchenSpec(material="מלמין 18", cabinet_count=4, labor_hours=5.0)
    breakdown = calculate_quote(spec, catalog, biz)
    out = render_quote_pdf(spec, breakdown, biz, 4)

    doc = pymupdf.open(str(out))
    assert len(doc[0].get_images()) >= 1, "logo was not embedded in the page"
    doc.close()
