"""Conversation-flow tests with the LLM mocked out.

These cover the state machine in bot.py — clarify, correct, approve — without
needing an Anthropic key, so the loop logic stays verified offline.
"""
from __future__ import annotations

import pytest

from app import db
from app.models import KitchenSpec
from app.parser import next_clarifying_question


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """Point the DB at a temp file so tests never touch real data."""
    monkeypatch.setattr("app.config.DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr("app.db.DB_PATH", tmp_path / "test.db")
    db.init_db()
    yield


def test_pending_state_round_trip():
    spec = KitchenSpec(layout="L", cabinet_count=10, handle_type="ידית כרום")
    db.save_pending(1, spec.model_dump(), None, "countertop", "clarify")

    pending = db.get_pending(1)
    assert pending is not None
    assert pending["awaiting_field"] == "countertop"
    assert pending["awaiting_kind"] == "clarify"
    restored = KitchenSpec(**pending["spec"])
    assert restored.cabinet_count == 10
    assert restored.handle_type == "ידית כרום"


def test_clearing_pending_removes_it():
    db.save_pending(2, KitchenSpec().model_dump())
    assert db.get_pending(2) is not None
    db.clear_pending(2)
    assert db.get_pending(2) is None


def test_pending_is_per_chat():
    db.save_pending(10, KitchenSpec(cabinet_count=5).model_dump())
    db.save_pending(20, KitchenSpec(cabinet_count=9).model_dump())
    assert KitchenSpec(**db.get_pending(10)["spec"]).cabinet_count == 5
    assert KitchenSpec(**db.get_pending(20)["spec"]).cabinet_count == 9


def test_quote_lifecycle_draft_to_approved():
    spec = KitchenSpec(client_name="דני", cabinet_count=8)
    qid = db.create_quote(5, "דני", spec.model_dump(), {"total": 20000}, 20000.0)

    row = db.latest_quote(5)
    assert row["status"] == "draft"
    assert row["client_name"] == "דני"

    db.mark_quote_approved(qid, "data/quotes/quote-00001.pdf")
    row = db.latest_quote(5)
    assert row["status"] == "approved"
    assert row["pdf_path"].endswith(".pdf")


def test_latest_quote_returns_most_recent():
    db.create_quote(7, "first", KitchenSpec().model_dump(), {}, 100.0)
    db.create_quote(7, "second", KitchenSpec().model_dump(), {}, 200.0)
    assert db.latest_quote(7)["client_name"] == "second"


def test_every_interaction_is_logged():
    db.log_interaction(3, transcript="מטבח בצורת L", parsed={"layout": "L"})
    db.log_interaction(3, correction_text="ידיות שחורות")

    with db.connect() as conn:
        rows = conn.execute(
            "SELECT * FROM transcripts_log WHERE chat_id=3 ORDER BY id"
        ).fetchall()

    assert len(rows) == 2
    assert rows[0]["raw_audio_transcript"] == "מטבח בצורת L"
    assert rows[1]["correction_text"] == "ידיות שחורות"


def test_clarify_priority_is_price_sensitive_first():
    # cabinet_count moves the total far more than handle_type, so it is asked
    # first even though handle_type appears earlier in the list.
    spec = KitchenSpec(missing_fields=["handle_type", "cabinet_count"])
    field, question = next_clarifying_question(spec)
    assert field == "cabinet_count"
    assert question


def test_no_question_when_nothing_important_is_missing():
    assert next_clarifying_question(KitchenSpec()) is None
    # `notes` is not worth interrupting the carpenter for.
    assert next_clarifying_question(KitchenSpec(missing_fields=["notes"])) is None


def test_correction_merge_keeps_prior_answers():
    """The bug this guards: a one-word correction wiping the whole spec."""
    original = KitchenSpec(client_name="דני", layout="L", material="פורניר אלון",
                           cabinet_count=12, drawer_count=6,
                           handle_type="ידית כרום")
    delta = KitchenSpec(handle_type="ידית שחורה")
    merged = original.merge(delta)

    assert merged.handle_type == "ידית שחורה"
    assert merged.client_name == "דני"
    assert merged.material == "פורניר אלון"
    assert merged.cabinet_count == 12
    assert merged.drawer_count == 6


def test_junk_text_is_rejected_before_any_api_call():
    """Guards the bug that burned a real API request on the string 'getUpdates'."""
    from app.bot import _looks_like_a_description as looks

    for junk in ("getUpdates", "start", "ok", "/x", "שלום", "hi there", "a b c"):
        assert not looks(junk), f"{junk!r} should not reach the parser"


def test_real_descriptions_pass_the_guard():
    from app.bot import _looks_like_a_description as looks

    for real in (
        "מטבח בצורת L ללקוח דני, 12 ארונות פורניר אלון, ידיות שחורות",
        "12 ארונות מלמין עם שיש אבן קיסר 4 מטר",
        "מטבח ישר 6 ארונות, 4 מגירות בלום, משטח למינציה",
    ):
        assert looks(real), f"{real!r} should reach the parser"


def test_guard_only_gates_new_descriptions_not_corrections():
    """Short replies are valid mid-conversation, so the guard must not be
    applied to clarification answers or corrections."""
    import inspect

    from app import bot

    # The guard is called in _handle_new_description and nowhere else.
    assert "_looks_like_a_description" in inspect.getsource(
        bot._handle_new_description
    )
    for fn in (bot._route_text, bot.on_callback):
        assert "_looks_like_a_description" not in inspect.getsource(fn)
