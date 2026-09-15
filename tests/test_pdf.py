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
