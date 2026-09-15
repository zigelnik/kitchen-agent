"""Fixed-example tests for the deterministic pricing engine."""
from __future__ import annotations

import pytest

from app.catalog import Catalog, CatalogItem
from app.config import BusinessConfig
from app.models import KitchenSpec
from app.pricing import calculate_quote, estimate_labor_hours


@pytest.fixture
def business() -> BusinessConfig:
    return BusinessConfig(
        labor_cost_per_hour=50,
        labor_bill_per_hour=100,
        margin_pct=50,
        transport_flat=250,
        vat_pct=0,
    )


@pytest.fixture
def catalog() -> Catalog:
    return Catalog(
        [
            CatalogItem("melamine", "material", "cabinet", 300.0),
            CatalogItem("oak veneer", "material", "cabinet", 800.0),
            CatalogItem("quartz top", "material", "meter", 1000.0),
            CatalogItem("מגירה רגילה", "hardware", "unit", 100.0),
            CatalogItem("ציר רגיל", "hardware", "unit", 20.0),
            CatalogItem("ידית כרום", "hardware", "unit", 30.0),
            CatalogItem("ידית שחורה", "hardware", "unit", 50.0),
            CatalogItem("הובלה והתקנה", "transport", "flat", 250.0),
        ]
    )


def test_fully_specified_quote_is_exact(catalog, business):
    spec = KitchenSpec(
        material="melamine",
        cabinet_count=10,
        drawer_count=4,
        drawer_type="מגירה רגילה",
        hinge_type="ציר רגיל",
        handle_type="ידית כרום",
        countertop="quartz top",
        countertop_length_m=3.0,
        labor_hours=20.0,
    )
    q = calculate_quote(spec, catalog, business)

    # materials: 10 cabinets * 300 = 3000, top: 3m * 1000 = 3000 -> 6000
    assert q.materials_cost == 6000.0
    # hardware: drawers 4*100=400, hinges 20*20=400, handles 14*30=420 -> 1220
    assert q.hardware_cost == 1220.0
    # margin 50% on goods only
    assert q.margin_amount == pytest.approx((6000 + 1220) * 0.5)
    # labor billed at 100/hr, not marked up again
    assert q.labor_cost == 2000.0
    assert q.transport == 250.0
    assert q.total == pytest.approx(6000 + 1220 + 3610 + 2000 + 250)
    assert q.warnings == []


def test_margin_does_not_touch_labor_or_transport(catalog, business):
    spec = KitchenSpec(material="melamine", cabinet_count=1, drawer_count=0,
                       labor_hours=10.0)
    q = calculate_quote(spec, catalog, business)
    goods = q.materials_cost + q.hardware_cost
    assert q.margin_amount == pytest.approx(goods * 0.5)
    # labor and transport pass through untouched
    assert q.labor_cost == 1000.0
    assert q.total == pytest.approx(goods * 1.5 + 1000.0 + 250.0)


def test_missing_counts_fall_back_and_warn(catalog, business):
    q = calculate_quote(KitchenSpec(), catalog, business)
    assert q.total > 0
    assert any("ארונות" in w for w in q.warnings)
    assert any("שעות" in w for w in q.warnings)
    # unspecified material must not silently price as the expensive option
    assert any("חומר" in w for w in q.warnings)


def test_unknown_material_warns_and_uses_cheapest(catalog, business):
    spec = KitchenSpec(material="unobtainium", cabinet_count=2, drawer_count=0,
                       labor_hours=1.0)
    q = calculate_quote(spec, catalog, business)
    assert any("unobtainium" in w for w in q.warnings)
    assert q.materials_cost == 600.0  # 2 * cheapest (300)


def test_handle_change_moves_the_total(catalog, business):
    base = KitchenSpec(material="melamine", cabinet_count=10, drawer_count=0,
                       handle_type="ידית כרום", labor_hours=10.0)
    changed = base.model_copy(update={"handle_type": "ידית שחורה"})
    q1 = calculate_quote(base, catalog, business)
    q2 = calculate_quote(changed, catalog, business)
    # 10 handles, 20 -> 50 price delta, plus 50% margin on the delta
    assert q2.total - q1.total == pytest.approx(10 * 20 * 1.5)


def test_vat_applies_last(catalog, business):
    vat_business = business.model_copy(update={"vat_pct": 18.0})
    spec = KitchenSpec(material="melamine", cabinet_count=4, drawer_count=0,
                       labor_hours=5.0)
    q = calculate_quote(spec, catalog, vat_business)
    assert q.vat_amount == pytest.approx(q.subtotal * 0.18)
    assert q.total == pytest.approx(q.subtotal * 1.18)


def test_labor_estimate_prefers_stated_hours():
    spec = KitchenSpec(labor_hours=12.5)
    assert estimate_labor_hours(spec, 10, 5) == 12.5


def test_labor_estimate_scales_with_size():
    small = estimate_labor_hours(KitchenSpec(), 4, 0)
    large = estimate_labor_hours(KitchenSpec(), 12, 6)
    assert large > small


def test_spec_merge_preserves_unmentioned_fields():
    original = KitchenSpec(layout="L", material="melamine", cabinet_count=8,
                           handle_type="ידית כרום")
    delta = KitchenSpec(handle_type="ידית שחורה")
    merged = original.merge(delta)
    assert merged.handle_type == "ידית שחורה"
    assert merged.layout == "L"
    assert merged.material == "melamine"
    assert merged.cabinet_count == 8
