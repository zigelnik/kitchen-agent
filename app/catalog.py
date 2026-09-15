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

# Words that carry no distinguishing information and would otherwise create
# false overlap between unrelated items.
_STOPWORDS = {"מ", "מם", "עם", "ו", "של", "מ\"מ", "ס\"מ", "18"}

# Hebrew plural/feminine suffixes, longest first. Stripping these makes
# speech ("ידיות שחורות") match the catalog ("ידית שחורה"). Deliberately
# crude -- a real stemmer is overkill for a ~20-row catalog, and over-
# stripping is harmless here because matching is by overlap, not equality.
_SUFFIXES = ("ניות", "יות", "ים", "ות", "ה", "ת", "י")

# Item kinds, so a lookup for a drawer can never return a hinge. Inferred
# from the item name rather than a new CSV column the carpenter would have
# to maintain by hand.
_KIND_MARKERS = (
    ("drawer", ("מגיר", "drawer")),
    ("hinge", ("ציר", "hinge")),
    ("handle", ("ידית", "ידיות", "handle", "knob")),
    ("countertop", ("שיש", "פורמייקה", "למינציה", "גרניט", "קוורץ",
                    "countertop", "top", "quartz", "granite", "laminate")),
    ("carcass", ("מלמין", "פורניר", "לכה", "mdf", "סנדוויץ", "דיקט",
                 "melamine", "veneer", "oak", "lacquer", "plywood")),
    ("transport", ("הובלה", "התקנה", "transport", "delivery", "install")),
)

# A match must cover this fraction of the SPOKEN words. Coverage of the query
# -- not Jaccard -- is the right measure: "בלום" is one word against the
# four-word "ציר בלום סגירה שקטה" and is a perfect match, while
# "מגירת מותג שלא קיים" shares only 1 of 4 words with "מגירה רגילה" and
# should be rejected so the carpenter learns the brand was unrecognized.
MIN_QUERY_COVERAGE = 0.5


def _stem(word: str) -> str:
    """Strip one Hebrew plural/feminine suffix, keeping the word substantial."""
    for suffix in _SUFFIXES:
        if len(word) > len(suffix) + 1 and word.endswith(suffix):
            return word[: -len(suffix)]
    return word


def _stems(text: str) -> set[str]:
    """Normalize a phrase into a set of comparable word stems."""
    cleaned = text.lower().replace("×", " ").replace(",", " ").replace("'", "")
    out = set()
    for raw in cleaned.split():
        word = raw.strip("\".()")
        if not word or word in _STOPWORDS:
            continue
        out.add(_stem(word))
    return out


def _infer_kind(item_name: str) -> str:
    low = item_name.lower()
    for kind, markers in _KIND_MARKERS:
        if any(m in low for m in markers):
            return kind
    return "other"


@dataclass(frozen=True)
class CatalogItem:
    item_name: str
    category: str
    unit: str
    unit_price_ils: float
    notes: str = ""

    @property
    def item_kind(self) -> str:
        return _infer_kind(self.item_name)


class Catalog:
    def __init__(self, items: list[CatalogItem]) -> None:
        self.items = items
        self._by_name = {i.item_name.strip().lower(): i for i in items}

    def __len__(self) -> int:
        return len(self.items)

    def by_category(self, category: str) -> list[CatalogItem]:
        return [i for i in self.items if i.category == category]

    def find(
        self,
        query: str | None,
        category: str | None = None,
        kind: str | None = None,
    ) -> CatalogItem | None:
        """Look up an item by loose name match, scored on shared word stems.

        Speech and catalog rarely agree on exact wording: the carpenter says
        "ידיות שחורות" (plural), the catalog says "ידית שחורה" (singular); he
        says "בלום" and means "מגירת בלום". Substring matching handled neither
        and silently picked wrong items across kinds -- "בלום" matched a hinge
        before a drawer -- so matching is by normalized token overlap, and
        `kind` hard-restricts the pool when the caller knows what it wants.
        """
        if not query:
            return None
        q = query.strip().lower()
        if not q:
            return None

        pool = self.items if category is None else self.by_category(category)
        if kind is not None:
            pool = [i for i in pool if i.item_kind == kind]
        if not pool:
            return None

        exact = self._by_name.get(q)
        if exact is not None and exact in pool:
            return exact

        q_tokens = _stems(q)
        if not q_tokens:
            return None

        best: tuple[float, int, CatalogItem] | None = None
        for item in pool:
            i_tokens = _stems(item.item_name)
            if not i_tokens:
                continue
            shared = q_tokens & i_tokens
            if not shared:
                continue
            coverage = len(shared) / len(q_tokens)
            if coverage < MIN_QUERY_COVERAGE:
                continue
            # Tie-break on how much of the item name was also matched, so
            # "מגירת בלום" beats a bare "מגירה" when both are covered.
            specificity = len(shared) / len(i_tokens)
            candidate = (coverage, specificity, item)
            if best is None or candidate[:2] > best[:2]:
                best = candidate

        return best[2] if best is not None else None

    def cheapest(self, category: str, unit: str | None = None) -> CatalogItem | None:
        """Fallback pricing when the spec names something not in the catalog."""
        pool = self.by_category(category)
        if unit is not None:
            pool = [i for i in pool if i.unit == unit]
        return min(pool, key=lambda i: i.unit_price_ils) if pool else None

    def cheapest_of_kind(self, category: str, kind: str) -> CatalogItem | None:
        """Cheapest item of a given kind -- the fallback for an unmatched
        drawer/hinge/handle, so the default is never a different kind of part.
        """
        pool = [i for i in self.by_category(category) if i.item_kind == kind]
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
