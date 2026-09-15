"""Deterministic quote calculation. Pure Python — no LLM, ever.

Margin policy: margin_pct applies to materials + hardware only. Labor is
already marked up through the cost/bill hourly spread (50 -> 100), so applying
margin to labor as well would double-count it.
"""
from __future__ import annotations

import math

from app.catalog import Catalog, CatalogItem
from app.config import BusinessConfig
from app.models import KitchenSpec, LineItem, QuoteBreakdown

# Fallbacks used when the transcript never mentioned a count. Chosen to be
# obviously-a-guess and always surfaced in `warnings` so the carpenter can
# correct them rather than silently accepting a wrong number.
DEFAULT_CABINET_COUNT = 8
DEFAULT_COUNTERTOP_M = 3.0

# Sanity bounds. Speech-to-text and the parser both mis-scale numbers
# ("12 על 20 מטר" for a kitchen), so implausible values are surfaced as
# warnings rather than silently priced.
MAX_PLAUSIBLE_CABINETS = 40
MAX_PLAUSIBLE_COUNTERTOP_M = 15.0
MAX_PLAUSIBLE_LABOR_HOURS = 200.0

# Labor estimate, used only when the carpenter didn't state hours.
HOURS_PER_CABINET = 1.5
HOURS_PER_DRAWER = 0.5
HOURS_BASE = 4.0


def estimate_labor_hours(spec: KitchenSpec, cabinets: int, drawers: int) -> float:
    if spec.labor_hours is not None and spec.labor_hours > 0:
        return float(spec.labor_hours)
    hours = HOURS_BASE + cabinets * HOURS_PER_CABINET + drawers * HOURS_PER_DRAWER
    return round(hours, 1)


def _line(item: CatalogItem, label: str, qty: float) -> LineItem:
    return LineItem(
        label=label,
        category=item.category,
        quantity=qty,
        unit=item.unit,
        unit_price=item.unit_price_ils,
        subtotal=round(item.unit_price_ils * qty, 2),
    )


def calculate_quote(
    spec: KitchenSpec, catalog: Catalog, business: BusinessConfig
) -> QuoteBreakdown:
    warnings: list[str] = []
    # Implausible values go here, not in `warnings`: they block one-tap
    # approval so a bad dimension cannot reach a client's PDF unchallenged.
    sanity_alerts: list[str] = []
    lines: list[LineItem] = []

    # --- quantities ---
    cabinets = spec.cabinet_count
    if cabinets is None or cabinets <= 0:
        cabinets = DEFAULT_CABINET_COUNT
        warnings.append(f"מספר ארונות לא צוין — הונח {cabinets}")

    if cabinets > MAX_PLAUSIBLE_CABINETS:
        sanity_alerts.append(f"{cabinets} ארונות — מספר חריג")

    drawers = spec.drawer_count if spec.drawer_count is not None else 0
    if spec.drawer_count is None:
        warnings.append("מספר מגירות לא צוין — הונח 0")

    if spec.countertop_length_m and spec.countertop_length_m > MAX_PLAUSIBLE_COUNTERTOP_M:
        sanity_alerts.append(
            f"אורך משטח {spec.countertop_length_m} מ' — חריג"
        )
    if spec.labor_hours and spec.labor_hours > MAX_PLAUSIBLE_LABOR_HOURS:
        sanity_alerts.append(f"{spec.labor_hours} שעות עבודה — חריג")

    # --- carcass material, priced per cabinet ---
    mat = catalog.find(spec.material, category="material", kind="carcass")
    if mat is None or mat.unit != "cabinet":
        mat = catalog.cheapest("material", unit="cabinet")
        if spec.material:
            warnings.append(f'חומר "{spec.material}" לא נמצא בקטלוג — חושב לפי {mat.item_name if mat else "?"}')
        else:
            warnings.append(f'חומר לא צוין — חושב לפי {mat.item_name if mat else "?"}')
    if mat is not None:
        lines.append(_line(mat, f"{mat.item_name} × {cabinets} ארונות", cabinets))

    # --- countertop, priced per metre ---
    top = catalog.find(spec.countertop, category="material", kind="countertop")
    if top is not None and top.unit == "meter":
        length = spec.countertop_length_m
        if length is None or length <= 0:
            length = DEFAULT_COUNTERTOP_M
            warnings.append(f"אורך משטח לא צוין — הונח {length} מ'")
        lines.append(_line(top, f"{top.item_name} × {length} מ'", length))
    elif spec.countertop:
        warnings.append(f'משטח "{spec.countertop}" לא נמצא בקטלוג — לא חויב')

    # --- hardware ---
    # `kind` keeps each lookup inside its own family, so a brand name like
    # "בלום" -- which names both a drawer and a hinge -- resolves correctly.
    if drawers > 0:
        drawer = catalog.find(spec.drawer_type, category="hardware", kind="drawer")
        if drawer is None:
            drawer = catalog.cheapest_of_kind("hardware", "drawer")
            if spec.drawer_type:
                warnings.append(
                    f'מגירה "{spec.drawer_type}" לא נמצאה בקטלוג — '
                    f'חושב לפי {drawer.item_name if drawer else "?"}'
                )
        if drawer is not None:
            lines.append(_line(drawer, f"{drawer.item_name} × {drawers}", drawers))

    hinges = cabinets * 2  # two hinges per door, standard
    hinge = catalog.find(spec.hinge_type, category="hardware", kind="hinge")
    if hinge is None:
        hinge = catalog.cheapest_of_kind("hardware", "hinge")
        if spec.hinge_type:
            warnings.append(
                f'ציר "{spec.hinge_type}" לא נמצא — '
                f'חושב לפי {hinge.item_name if hinge else "?"}'
            )
    if hinge is not None:
        lines.append(_line(hinge, f"{hinge.item_name} × {hinges}", hinges))

    handles = cabinets + drawers
    handle = catalog.find(spec.handle_type, category="hardware", kind="handle")
    if handle is None:
        handle = catalog.cheapest_of_kind("hardware", "handle")
        if spec.handle_type:
            warnings.append(
                f'ידית "{spec.handle_type}" לא נמצאה — '
                f'חושב לפי {handle.item_name if handle else "?"}'
            )
    if handle is not None:
        lines.append(_line(handle, f"{handle.item_name} × {handles}", handles))

    # --- labor ---
    hours = estimate_labor_hours(spec, cabinets, drawers)
    if spec.labor_hours is None:
        warnings.append(f"שעות עבודה לא צוינו — הוערכו {hours} שעות")
    labor_cost = round(hours * business.labor_bill_per_hour, 2)
    lines.append(
        LineItem(
            label=f"עבודה והרכבה × {hours} שעות",
            category="labor",
            quantity=hours,
            unit="hour",
            unit_price=business.labor_bill_per_hour,
            subtotal=labor_cost,
        )
    )

    # --- transport ---
    transport_item = catalog.find(None, category="transport") or None
    transport = business.transport_flat
    if transport_item is None:
        tr = catalog.by_category("transport")
        if tr:
            transport = tr[0].unit_price_ils
    lines.append(
        LineItem(
            label="הובלה והתקנה",
            category="transport",
            quantity=1,
            unit="flat",
            unit_price=transport,
            subtotal=transport,
        )
    )

    # --- totals ---
    materials_cost = round(
        sum(l.subtotal for l in lines if l.category == "material"), 2
    )
    hardware_cost = round(
        sum(l.subtotal for l in lines if l.category == "hardware"), 2
    )

    goods = materials_cost + hardware_cost
    margin_amount = round(goods * (business.margin_multiplier - 1.0), 2)

    subtotal = round(goods + margin_amount + labor_cost + transport, 2)
    vat_amount = round(subtotal * business.vat_pct / 100.0, 2)
    total = round(subtotal + vat_amount, 2)

    return QuoteBreakdown(
        line_items=lines,
        materials_cost=materials_cost,
        hardware_cost=hardware_cost,
        labor_cost=labor_cost,
        transport=transport,
        margin_amount=margin_amount,
        subtotal=subtotal,
        vat_amount=vat_amount,
        total=total,
        currency_symbol=business.currency_symbol,
        warnings=warnings,
        sanity_alerts=sanity_alerts,
    )
