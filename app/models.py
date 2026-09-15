"""Pydantic models: the parsed kitchen spec and the priced breakdown."""
from __future__ import annotations

from pydantic import BaseModel, Field


class KitchenSpec(BaseModel):
    """Structured output of the transcript-parsing step.

    Deliberately permissive (mostly `str | None`) for MVP. Tighten into enums
    once real transcripts show the actual value space.
    """

    client_name: str | None = None
    layout: str | None = None
    dimensions_m: str | None = None
    material: str | None = None
    cabinet_count: int | None = None
    drawer_count: int | None = None
    drawer_type: str | None = None
    hinge_type: str | None = None
    handle_type: str | None = None
    countertop: str | None = None
    countertop_length_m: float | None = None
    labor_hours: float | None = None
    notes: str | None = None

    missing_fields: list[str] = Field(default_factory=list)
    low_confidence_fields: list[str] = Field(default_factory=list)

    def merge(self, other: KitchenSpec) -> KitchenSpec:
        """Overlay non-null values from `other` onto self.

        Used on the correction loop so a "change the handles" reply doesn't
        wipe every other field back to null.
        """
        merged = self.model_dump()
        for key, value in other.model_dump().items():
            if key in ("missing_fields", "low_confidence_fields"):
                continue
            if value is not None:
                merged[key] = value
        merged["missing_fields"] = other.missing_fields
        merged["low_confidence_fields"] = other.low_confidence_fields
        return KitchenSpec(**merged)


class LineItem(BaseModel):
    label: str
    category: str
    quantity: float
    unit: str
    unit_price: float
    subtotal: float


class QuoteBreakdown(BaseModel):
    """Result of the deterministic pricing step. No LLM touches these numbers."""

    line_items: list[LineItem] = Field(default_factory=list)
    materials_cost: float = 0.0
    hardware_cost: float = 0.0
    labor_cost: float = 0.0
    transport: float = 0.0
    margin_amount: float = 0.0
    subtotal: float = 0.0
    vat_amount: float = 0.0
    total: float = 0.0
    currency_symbol: str = "₪"
    warnings: list[str] = Field(default_factory=list)

    # Values outside plausible bounds (a 40m countertop, 200 cabinets). Kept
    # separate from `warnings` because these block one-tap approval: a wrong
    # dimension reaching a client's PDF is worse than an extra confirmation.
    sanity_alerts: list[str] = Field(default_factory=list)

    @property
    def needs_confirmation(self) -> bool:
        return bool(self.sanity_alerts)
