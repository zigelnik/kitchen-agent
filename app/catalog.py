"""Price catalog loader.

MVP reads a local CSV so there is no cloud dependency to get a working quote.
`load_catalog()` is the single seam to swap for a Google Sheets read later
(PLAN.md Phase 3) — nothing downstream knows where the rows came from.
"""
from __future__ import annotations

import csv
import time
from dataclasses import dataclass

from app.config import CATALOG_PATH

CACHE_TTL_SECONDS = 300

_cache: tuple[float, "Catalog"] | None = None


@dataclass(frozen=True)
class CatalogItem:
    item_name: str
    category: str
    unit: str
    unit_price_ils: float
    notes: str = ""


class Catalog:
    def __init__(self, items: list[CatalogItem]) -> None:
        self.items = items
        self._by_name = {i.item_name.strip().lower(): i for i in items}

    def __len__(self) -> int:
        return len(self.items)

    def by_category(self, category: str) -> list[CatalogItem]:
        return [i for i in self.items if i.category == category]

    def find(self, query: str | None, category: str | None = None) -> CatalogItem | None:
        """Look up an item by loose name match.

        The LLM returns free text ("ידית שחורה", "black handles"), so exact
        keys are not enough: fall back to substring matching in both
        directions before giving up.
        """
        if not query:
            return None
        q = query.strip().lower()
        if not q:
            return None

        pool = self.items if category is None else self.by_category(category)

        exact = self._by_name.get(q)
        if exact is not None and (category is None or exact.category == category):
            return exact

        for item in pool:
            if item.item_name.strip().lower() == q:
                return item
        # Prefer the longest containment match so "מגירת בלום" doesn't lose
        # to a shorter generic "מגירה".
        candidates = [
            item
            for item in pool
            if q in item.item_name.strip().lower() or item.item_name.strip().lower() in q
        ]
        if candidates:
            return max(candidates, key=lambda i: len(i.item_name))
        return None

    def cheapest(self, category: str, unit: str | None = None) -> CatalogItem | None:
        """Fallback pricing when the spec names something not in the catalog."""
        pool = self.by_category(category)
        if unit is not None:
            pool = [i for i in pool if i.unit == unit]
        return min(pool, key=lambda i: i.unit_price_ils) if pool else None


def _parse_rows(rows: list[dict[str, str]]) -> list[CatalogItem]:
    items: list[CatalogItem] = []
    for row in rows:
        name = (row.get("item_name") or "").strip()
        if not name:
            continue
        try:
            price = float((row.get("unit_price_ils") or "0").strip() or 0)
        except ValueError:
            continue
        items.append(
            CatalogItem(
                item_name=name,
                category=(row.get("category") or "").strip(),
                unit=(row.get("unit") or "").strip(),
                unit_price_ils=price,
                notes=(row.get("notes") or "").strip(),
            )
        )
    return items


def load_catalog(force: bool = False) -> Catalog:
    """Load the catalog, cached for CACHE_TTL_SECONDS so edits appear without
    a restart."""
    global _cache
    now = time.monotonic()
    if not force and _cache is not None and now - _cache[0] < CACHE_TTL_SECONDS:
        return _cache[1]

    if not CATALOG_PATH.exists():
        catalog = Catalog([])
    else:
        with CATALOG_PATH.open(encoding="utf-8-sig", newline="") as fh:
            catalog = Catalog(_parse_rows(list(csv.DictReader(fh))))

    _cache = (now, catalog)
    return catalog
