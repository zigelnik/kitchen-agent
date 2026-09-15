"""Telegram bot: the whole conversation flow.

Voice note or text -> transcribe -> parse -> (clarify) -> price -> draft with
Approve/Correct buttons -> PDF.

State lives in the `pending_state` table keyed by chat_id, so a restart does
not lose an in-progress quote.
"""
from __future__ import annotations

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from app import db
from app.catalog import load_catalog
from app.config import AUDIO_DIR, ensure_dirs, get_settings
from app.models import KitchenSpec, QuoteBreakdown
from app.parser import (
    next_clarifying_question,
    parse_clarification,
    parse_correction,
    parse_transcript,
)
from app.pdf import render_quote_pdf
from app.pricing import calculate_quote
from app.transcribe import transcribe

log = logging.getLogger(__name__)

# Awaiting kinds stored in pending_state.awaiting_kind
AWAIT_CLARIFY = "clarify"
AWAIT_CORRECTION = "correction"

CB_APPROVE = "approve"
CB_CORRECT = "correct"
CB_CANCEL = "cancel"

WELCOME = (
    "שלום! שלח לי הקלטה קולית או הודעת טקסט עם תיאור המטבח, "
    "ואני אכין הצעת מחיר.\n\n"
    "לדוגמה: \"מטבח בצורת L ללקוח דני, 12 ארונות פורניר אלון, "
    "6 מגירות בלום, שיש אבן קיסר 4 מטר, ידיות שחורות\"\n\n"
    "פקודות: /new להתחלה מחדש, /last להצעה האחרונה"
)


# --- access control --------------------------------------------------------

def _is_allowed(update: Update) -> bool:
    """Single-user bot: refuse anyone but the configured carpenter.

    With ALLOWED_TELEGRAM_USER_ID unset the bot answers anyone, which is fine
    for first-run setup (it reports the id to paste into .env) but should be
    locked down before the token is in the wild.
    """
    settings = get_settings()
    if settings.allowed_telegram_user_id is None:
        return True
    user = update.effective_user
    return user is not None and user.id == settings.allowed_telegram_user_id


# --- message formatting ----------------------------------------------------

def _format_draft(spec: KitchenSpec, breakdown: QuoteBreakdown) -> str:
    sym = breakdown.currency_symbol
    lines = ["*הצעת מחיר — טיוטה*", ""]

    if spec.client_name:
        lines.append(f"לקוח: {spec.client_name}")
    details = []
    if spec.layout:
        details.append(f"צורה: {spec.layout}")
    if spec.cabinet_count:
        details.append(f"{spec.cabinet_count} ארונות")
    if spec.drawer_count:
        details.append(f"{spec.drawer_count} מגירות")
    if details:
        lines.append(" · ".join(details))
    lines.append("")

    for item in breakdown.line_items:
        lines.append(f"• {item.label} — {sym}{item.subtotal:,.0f}")

    lines.append("")
    if breakdown.margin_amount:
        lines.append(f"רווח: {sym}{breakdown.margin_amount:,.0f}")
    if breakdown.vat_amount:
        lines.append(f"לפני מע\"מ: {sym}{breakdown.subtotal:,.0f}")
        lines.append(f"מע\"מ: {sym}{breakdown.vat_amount:,.0f}")
    lines.append(f"*סה\"כ: {sym}{breakdown.total:,.0f}*")

    if breakdown.warnings:
        lines.append("")
        lines.append("⚠️ הנחות שהמערכת עשתה:")
        for warning in breakdown.warnings:
            lines.append(f"  · {warning}")

    # The parser flags genuine ambiguities here (a number it could not place,
    # an implausible measurement). That is exactly what to check before
    # sending a quote to a client, so it belongs in the draft.
    if spec.notes:
        lines.append("")
        lines.append(f"📝 הערות לבדיקה: {spec.notes}")

    if spec.low_confidence_fields:
        lines.append("")
        lines.append(
            "❓ שדות בניחוש: " + ", ".join(spec.low_confidence_fields)
        )

    return "\n".join(lines)


def _draft_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ אישור", callback_data=CB_APPROVE),
        InlineKeyboardButton("✏️ תיקון", callback_data=CB_CORRECT),
        InlineKeyboardButton("🗑 ביטול", callback_data=CB_CANCEL),
    ]])


# --- core pipeline ---------------------------------------------------------

async def _price_and_show_draft(
    update: Update, chat_id: int, spec: KitchenSpec
) -> None:
    """Price the spec, persist it, and show the draft with action buttons."""
    breakdown = calculate_quote(spec, load_catalog(), get_settings().business)
    db.save_pending(chat_id, spec.model_dump(), breakdown.model_dump())

    target = update.effective_message
    await target.reply_text(
        _format_draft(spec, breakdown),
        parse_mode="Markdown",
        reply_markup=_draft_keyboard(),
    )


MIN_DESCRIPTION_CHARS = 12
MIN_DESCRIPTION_WORDS = 3


def _looks_like_a_description(text: str) -> bool:
    """Cheap sanity check before spending an API call.

    A kitchen description is a sentence. A stray one-word message -- a typo, a
    forwarded token, an accidental tap -- is not, and parsing it costs money
    and leaves confusing pending state behind.
    """
    stripped = text.strip()
    if len(stripped) < MIN_DESCRIPTION_CHARS:
        return False
    if len(stripped.split()) < MIN_DESCRIPTION_WORDS:
        return False
    # Require at least one Hebrew letter or digit: a bare Latin token is never
    # a Hebrew kitchen description.
    return any("֐" <= ch <= "׿" or ch.isdigit() for ch in stripped)


async def _handle_new_description(
    update: Update, chat_id: int, text: str, was_voice: bool
) -> None:
    """Parse a fresh description, then either clarify or show a draft."""
    settings = get_settings()
    if not settings.has_anthropic:
        await update.effective_message.reply_text(
            "⚠️ חסר ANTHROPIC_API_KEY בהגדרות — לא ניתן לנתח את התיאור."
        )
        return

    if not _looks_like_a_description(text):
        await update.effective_message.reply_text(
            "לא זיהיתי תיאור מטבח בהודעה. שלח הקלטה או תיאור עם פרטי העבודה — "
            "למשל מספר ארונות, חומר, משטח וידיות."
        )
        return

    try:
        spec = parse_transcript(text)
    except Exception:
        log.exception("parse failed")
        await update.effective_message.reply_text(
            "לא הצלחתי לנתח את התיאור. אפשר לנסות שוב בבקשה?"
        )
        return

    db.log_interaction(chat_id, transcript=text, parsed=spec.model_dump())

    question = next_clarifying_question(spec)
    if question is not None:
        field, text_q = question
        db.save_pending(
            chat_id, spec.model_dump(), None, awaiting_field=field,
            awaiting_kind=AWAIT_CLARIFY,
        )
        await update.effective_message.reply_text(f"❓ {text_q}")
        return

    await _price_and_show_draft(update, chat_id, spec)


# --- handlers --------------------------------------------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    # Logged so the carpenter's id can be pasted into ALLOWED_TELEGRAM_USER_ID.
    log.info("start from telegram user id=%s username=%s",
             user.id if user else "?", user.username if user else "?")
    if not _is_allowed(update):
        await update.effective_message.reply_text("מצטער, הבוט הזה פרטי.")
        return
    await update.effective_message.reply_text(WELCOME)


async def cmd_new(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update):
        return
    db.clear_pending(update.effective_chat.id)
    await update.effective_message.reply_text("התחלנו מחדש. שלח תיאור מטבח.")


async def cmd_last(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update):
        return
    row = db.latest_quote(update.effective_chat.id)
    if row is None:
        await update.effective_message.reply_text("אין עדיין הצעות.")
        return
    await update.effective_message.reply_text(
        f"הצעה #{row['id']} · {row['status']} · "
        f"{get_settings().business.currency_symbol}{row['total_ils']:,.0f}"
    )


async def on_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update):
        return
    chat_id = update.effective_chat.id
    message = update.effective_message
    voice = message.voice or message.audio

    await context.bot.send_chat_action(chat_id, ChatAction.TYPING)
    ensure_dirs()
    audio_path = AUDIO_DIR / f"{chat_id}-{message.message_id}.ogg"

    try:
        tg_file = await context.bot.get_file(voice.file_id)
        await tg_file.download_to_drive(str(audio_path))
    except Exception:
        log.exception("voice download failed")
        await message.reply_text("לא הצלחתי להוריד את ההקלטה.")
        return

    await message.reply_text("🎧 מתמלל...")
    try:
        text = transcribe(audio_path)
    except Exception:
        log.exception("transcription failed")
        await message.reply_text("התמלול נכשל. אפשר לשלוח את התיאור כטקסט?")
        return

    if not text:
        await message.reply_text("לא זיהיתי דיבור בהקלטה.")
        return

    await message.reply_text(f"📝 _{text}_", parse_mode="Markdown")
    await _route_text(update, context, chat_id, text, was_voice=True)


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update):
        return
    chat_id = update.effective_chat.id
    text = (update.effective_message.text or "").strip()
    if not text:
        return
    await _route_text(update, context, chat_id, text, was_voice=False)


async def _route_text(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    text: str,
    was_voice: bool,
) -> None:
    """Send the message down the right branch based on what we're awaiting."""
    pending = db.get_pending(chat_id)

    if pending is None:
        await _handle_new_description(update, chat_id, text, was_voice)
        return

    spec = KitchenSpec(**pending["spec"])
    kind = pending["awaiting_kind"]

    if kind == AWAIT_CLARIFY and pending["awaiting_field"]:
        field = pending["awaiting_field"]
        try:
            spec = parse_clarification(spec, field, text)
        except Exception:
            log.exception("clarification parse failed")
            await update.effective_message.reply_text("לא הבנתי, אפשר לנסח שוב?")
            return
        db.log_interaction(chat_id, transcript=text, parsed=spec.model_dump())

        # Ask at most one more question, then price regardless so the
        # carpenter is never stuck in an interrogation loop.
        question = next_clarifying_question(spec)
        if question is not None and question[0] != field:
            nxt_field, nxt_text = question
            db.save_pending(chat_id, spec.model_dump(), None,
                            awaiting_field=nxt_field, awaiting_kind=AWAIT_CLARIFY)
            await update.effective_message.reply_text(f"❓ {nxt_text}")
            return

        await _price_and_show_draft(update, chat_id, spec)
        return

    if kind == AWAIT_CORRECTION:
        try:
            spec = parse_correction(spec, text)
        except Exception:
            log.exception("correction parse failed")
            await update.effective_message.reply_text("לא הצלחתי להחיל את התיקון.")
            return
        db.log_interaction(chat_id, parsed=spec.model_dump(), correction_text=text)
        await _price_and_show_draft(update, chat_id, spec)
        return

    # A draft is on screen and the carpenter typed instead of tapping a
    # button: treat it as a correction, which is what they almost always mean.
    try:
        spec = parse_correction(spec, text)
    except Exception:
        log.exception("implicit correction failed")
        await _handle_new_description(update, chat_id, text, was_voice)
        return
    db.log_interaction(chat_id, parsed=spec.model_dump(), correction_text=text)
    await _price_and_show_draft(update, chat_id, spec)


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update):
        return
    query = update.callback_query
    await query.answer()

    chat_id = update.effective_chat.id
    pending = db.get_pending(chat_id)
    if pending is None:
        await query.edit_message_text("הטיוטה פגה. שלח תיאור חדש.")
        return

    spec = KitchenSpec(**pending["spec"])

    if query.data == CB_CANCEL:
        db.clear_pending(chat_id)
        await query.edit_message_text("בוטל.")
        return

    if query.data == CB_CORRECT:
        db.save_pending(chat_id, pending["spec"], pending["breakdown"],
                        awaiting_kind=AWAIT_CORRECTION)
        await query.edit_message_text(
            "מה לתקן? כתוב או הקלט את התיקון.\n"
            "לדוגמה: \"תחליף לידיות שחורות\" או \"14 ארונות\""
        )
        return

    if query.data == CB_APPROVE:
        if not pending["breakdown"]:
            await query.edit_message_text("חסר חישוב. שלח תיאור מחדש.")
            return
        breakdown = QuoteBreakdown(**pending["breakdown"])

        quote_id = db.create_quote(
            chat_id, spec.client_name, spec.model_dump(),
            breakdown.model_dump(), breakdown.total, status="draft",
        )
        await query.edit_message_text("✅ מאושר. מכין PDF...")

        try:
            pdf_path = render_quote_pdf(
                spec, breakdown, get_settings().business, quote_id
            )
        except Exception:
            log.exception("pdf render failed")
            await context.bot.send_message(chat_id, "יצירת ה-PDF נכשלה.")
            return

        db.mark_quote_approved(quote_id, str(pdf_path))
        db.clear_pending(chat_id)

        with pdf_path.open("rb") as fh:
            await context.bot.send_document(
                chat_id,
                document=fh,
                filename=pdf_path.name,
                caption=f"הצעה #{quote_id} · "
                        f"{breakdown.currency_symbol}{breakdown.total:,.0f}",
            )


def build_application() -> Application:
    settings = get_settings()
    if not settings.has_telegram:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")

    app = Application.builder().token(settings.telegram_bot_token).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_start))
    app.add_handler(CommandHandler("new", cmd_new))
    app.add_handler(CommandHandler("last", cmd_last))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, on_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(CallbackQueryHandler(on_callback))
    return app
