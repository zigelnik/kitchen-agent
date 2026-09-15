"""Transcript -> KitchenSpec via one Claude call using a tool schema.

The LLM's only jobs are (a) extracting structure from speech and (b) saying
what it could not determine. It never computes prices.
"""
from __future__ import annotations

import json
import logging

from app.config import get_settings
from app.models import KitchenSpec

log = logging.getLogger(__name__)

MODEL = "claude-sonnet-5"

# Fields worth asking a clarifying question about, most price-sensitive first.
CLARIFY_PRIORITY = [
    "cabinet_count",
    "material",
    "countertop",
    "drawer_count",
    "handle_type",
    "dimensions_m",
]

FIELD_QUESTIONS_HE = {
    "cabinet_count": "כמה ארונות יש במטבח?",
    "material": "מאיזה חומר החזיתות? (מלמין / פורניר / לכה)",
    "countertop": "איזה משטח עבודה? (אבן קיסר / גרניט / למינציה)",
    "countertop_length_m": "מה אורך משטח העבודה במטרים?",
    "drawer_count": "כמה מגירות?",
    "drawer_type": "איזה סוג מגירות? (רגילות / בלום / האפלה)",
    "handle_type": "איזה ידיות? (כרום / שחורות / נירוסטה)",
    "hinge_type": "איזה צירים? (רגילים / סגירה שקטה)",
    "dimensions_m": "מה מידות המטבח במטרים?",
    "layout": "מה צורת המטבח? (ישר / L / U)",
    "labor_hours": "כמה שעות עבודה אתה מעריך?",
    "client_name": "מה שם הלקוח?",
}

SYSTEM_PROMPT = """You extract structured kitchen-cabinetry job specs from a \
Hebrew carpenter's spoken notes.

Rules:
- Record ONLY what the carpenter actually said. Never invent a value.
- If something was not mentioned or you are unsure, OMIT the field entirely \
and list its name in missing_fields. Do not send an empty string or "null".
- If a value was implied but ambiguous, fill your best reading AND list the \
field in low_confidence_fields.
- Keep Hebrew terms in Hebrew — do not translate material or hardware names, \
because they are matched against a Hebrew price catalog.
- Never estimate prices, costs, or totals. That is not your job.
- Put anything that does not fit a field into `notes`."""

# Optional fields are plain types and simply omitted when unknown, rather than
# ["string", "null"] unions: union types are not reliably honoured in tool
# schemas, and KitchenSpec already defaults every field to None.
SPEC_TOOL = {
    "name": "record_kitchen_spec",
    "description": "Record the kitchen job spec extracted from the transcript.",
    "input_schema": {
        "type": "object",
        "properties": {
            "client_name": {"type": "string", "description": "Client name if mentioned"},
            "layout": {"type": "string", "description": "e.g. ישר, L, U"},
            "dimensions_m": {"type": "string", "description": "Free text, e.g. 4x3"},
            "material": {
                "type": "string",
                "description": "Carcass/front material, in Hebrew",
            },
            "cabinet_count": {"type": "integer"},
            "drawer_count": {"type": "integer"},
            "drawer_type": {"type": "string"},
            "hinge_type": {"type": "string"},
            "handle_type": {"type": "string"},
            "countertop": {
                "type": "string",
                "description": "Countertop material, in Hebrew",
            },
            "countertop_length_m": {"type": "number"},
            "labor_hours": {
                "type": "number",
                "description": "Only if the carpenter stated it",
            },
            "notes": {"type": "string"},
            "missing_fields": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Names of fields not determinable from the transcript. "
                               "Omit a field from the output entirely and list it here.",
            },
            "low_confidence_fields": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Names of fields filled by inference rather than statement",
            },
        },
        "required": ["missing_fields", "low_confidence_fields"],
    },
}
# Note: deliberately NOT strict. Strict mode requires every property to appear
# in `required`, but a partial spec -- most fields absent -- is the normal case
# here. `_coerce_spec` below does the validation instead.


def _client():
    import anthropic

    settings = get_settings()
    if not settings.anthropic_api_key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY is not set — add it to .env before parsing transcripts."
        )
    return anthropic.Anthropic(api_key=settings.anthropic_api_key)


_ALLOWED_KEYS = set(KitchenSpec.model_fields)


def _coerce_spec(raw: dict) -> KitchenSpec:
    """Build a KitchenSpec from tool output, tolerating model sloppiness.

    Guards against the three things that actually happen in practice: an
    unexpected key, the string "null"/"" where a value should be, and a
    numeric field returned as a string.
    """
    cleaned: dict = {}
    for key, value in raw.items():
        if key not in _ALLOWED_KEYS:
            log.warning("dropping unexpected field from tool output: %s", key)
            continue
        if isinstance(value, str) and value.strip().lower() in ("", "null", "none"):
            continue
        cleaned[key] = value

    for list_field in ("missing_fields", "low_confidence_fields"):
        val = cleaned.get(list_field)
        if not isinstance(val, list):
            cleaned[list_field] = []
        else:
            cleaned[list_field] = [str(x) for x in val]

    for num_field in ("cabinet_count", "drawer_count"):
        if isinstance(cleaned.get(num_field), str):
            try:
                cleaned[num_field] = int(float(cleaned[num_field]))
            except ValueError:
                cleaned.pop(num_field)
    for num_field in ("countertop_length_m", "labor_hours"):
        if isinstance(cleaned.get(num_field), str):
            try:
                cleaned[num_field] = float(cleaned[num_field])
            except ValueError:
                cleaned.pop(num_field)

    return KitchenSpec(**cleaned)


def _call_claude(user_content: str) -> dict:
    response = _client().messages.create(
        model=MODEL,
        max_tokens=1500,
        system=SYSTEM_PROMPT,
        tools=[SPEC_TOOL],
        tool_choice={"type": "tool", "name": "record_kitchen_spec"},
        messages=[{"role": "user", "content": user_content}],
    )
    # A refusal returns HTTP 200 with no tool_use block; surface it clearly
    # rather than as a confusing "no tool_use" error.
    if response.stop_reason == "refusal":
        raise RuntimeError(f"model declined the request: {response.stop_details}")
    for block in response.content:
        if getattr(block, "type", None) == "tool_use":
            return dict(block.input)
    raise RuntimeError(f"Claude returned no tool_use block: {response.content!r}")


def parse_transcript(transcript: str) -> KitchenSpec:
    """Initial parse of a fresh voice note or text description."""
    raw = _call_claude(f"תמלול הקלטה של הנגר:\n\n{transcript}")
    return _coerce_spec(raw)


def parse_correction(existing: KitchenSpec, correction: str) -> KitchenSpec:
    """Re-parse only the delta, then merge onto the existing spec.

    The prompt shows the current spec so Claude returns just what changed
    rather than re-deriving the whole kitchen from a one-line correction.
    """
    current = existing.model_dump(
        exclude={"missing_fields", "low_confidence_fields"}, exclude_none=True
    )
    prompt = (
        "המפרט הקיים של המטבח:\n"
        f"{json.dumps(current, ensure_ascii=False, indent=2)}\n\n"
        f"תיקון מהנגר:\n{correction}\n\n"
        "Return ONLY the fields this correction changes or adds. Leave every "
        "field the correction does not touch as null — do not restate them."
    )
    delta = _coerce_spec(_call_claude(prompt))
    return existing.merge(delta)


def parse_clarification(existing: KitchenSpec, field: str, answer: str) -> KitchenSpec:
    """Fold a one-field answer to a clarifying question back into the spec."""
    prompt = (
        "המפרט הקיים:\n"
        f"{json.dumps(existing.model_dump(exclude_none=True), ensure_ascii=False, indent=2)}\n\n"
        f"נשאלה שאלה על השדה `{field}`. תשובת הנגר:\n{answer}\n\n"
        f"Return the value for `{field}` (plus anything else the answer "
        "revealed). Leave untouched fields null."
    )
    delta = _coerce_spec(_call_claude(prompt))
    return existing.merge(delta)


def next_clarifying_question(spec: KitchenSpec) -> tuple[str, str] | None:
    """Pick the single most price-sensitive missing field to ask about.

    Returns (field_name, hebrew_question) or None if nothing worth asking.
    """
    missing = set(spec.missing_fields)
    for field in CLARIFY_PRIORITY:
        if field in missing and field in FIELD_QUESTIONS_HE:
            return field, FIELD_QUESTIONS_HE[field]
    return None
