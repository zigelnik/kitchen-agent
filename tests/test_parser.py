"""Parser tests. No network — these exercise the local coercion and schema.

The point is to prove that malformed tool output can't crash the bot, without
spending an API request to find out.
"""
from __future__ import annotations

import json

from app.models import KitchenSpec
from app.parser import SPEC_TOOL, _coerce_spec, next_clarifying_question


def test_tool_schema_is_serializable_and_well_formed():
    json.dumps(SPEC_TOOL)  # must survive the wire exactly as written
    schema = SPEC_TOOL["input_schema"]
    assert schema["type"] == "object"
    assert set(schema["required"]) == {"missing_fields", "low_confidence_fields"}


def test_schema_declares_no_union_types():
    """Union types like ["string","null"] are not reliably honoured."""
    for name, prop in SPEC_TOOL["input_schema"]["properties"].items():
        assert isinstance(prop["type"], str), f"{name} uses a union type"


def test_schema_fields_all_exist_on_the_model():
    """A property the model doesn't have would be silently dropped."""
    model_fields = set(KitchenSpec.model_fields)
    for name in SPEC_TOOL["input_schema"]["properties"]:
        assert name in model_fields, f"schema has unknown field {name}"


def test_coerce_handles_a_clean_full_response():
    spec = _coerce_spec({
        "client_name": "דני כהן",
        "layout": "L",
        "material": "פורניר אלון",
        "cabinet_count": 12,
        "drawer_count": 6,
        "countertop": "שיש אבן קיסר",
        "countertop_length_m": 4.5,
        "missing_fields": [],
        "low_confidence_fields": [],
    })
    assert spec.client_name == "דני כהן"
    assert spec.cabinet_count == 12
    assert spec.countertop_length_m == 4.5


def test_coerce_drops_unknown_fields():
    spec = _coerce_spec({
        "cabinet_count": 8,
        "sink_brand": "Blanco",  # not in KitchenSpec
        "missing_fields": [],
        "low_confidence_fields": [],
    })
    assert spec.cabinet_count == 8
    assert not hasattr(spec, "sink_brand")


def test_coerce_treats_null_like_strings_as_absent():
    spec = _coerce_spec({
        "material": "null",
        "layout": "",
        "handle_type": "None",
        "missing_fields": [],
        "low_confidence_fields": [],
    })
    assert spec.material is None
    assert spec.layout is None
    assert spec.handle_type is None


def test_coerce_converts_stringified_numbers():
    spec = _coerce_spec({
        "cabinet_count": "12",
        "drawer_count": "6",
        "countertop_length_m": "4.5",
        "labor_hours": "28",
        "missing_fields": [],
        "low_confidence_fields": [],
    })
    assert spec.cabinet_count == 12
    assert spec.drawer_count == 6
    assert spec.countertop_length_m == 4.5
    assert spec.labor_hours == 28.0


def test_coerce_survives_unparseable_numbers():
    spec = _coerce_spec({
        "cabinet_count": "כמה",
        "missing_fields": [],
        "low_confidence_fields": [],
    })
    assert spec.cabinet_count is None


def test_coerce_repairs_missing_or_wrong_typed_lists():
    spec = _coerce_spec({"cabinet_count": 5})
    assert spec.missing_fields == []
    assert spec.low_confidence_fields == []

    spec2 = _coerce_spec({
        "missing_fields": "countertop",       # string, not a list
        "low_confidence_fields": None,
    })
    assert spec2.missing_fields == []
    assert spec2.low_confidence_fields == []


def test_coerce_stringifies_list_entries():
    spec = _coerce_spec({"missing_fields": [1, "material"], "low_confidence_fields": []})
    assert spec.missing_fields == ["1", "material"]


def test_coerce_of_empty_response_yields_empty_spec():
    spec = _coerce_spec({})
    assert spec.cabinet_count is None
    assert spec.missing_fields == []


def test_coerced_spec_then_asks_the_right_question():
    """End to end on the local path: sloppy output still drives the clarify loop."""
    spec = _coerce_spec({
        "material": "פורניר אלון",
        "missing_fields": ["handle_type", "cabinet_count"],
        "low_confidence_fields": [],
    })
    field, question = next_clarifying_question(spec)
    assert field == "cabinet_count"
    assert "ארונות" in question
