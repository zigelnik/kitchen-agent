# Kitchen Cabinetry Quote Agent — MVP Build Plan

## 0. Context & Goal

Build a Telegram-bot-based assistant for a solo kitchen carpenter (Hebrew-speaking,
first user is a specific person — "the carpenter") that turns a voice note
describing a kitchen job into a priced, professional PDF quote, with the
carpenter approving/correcting the draft before it goes out.

**Non-goals for MVP:**
- No multi-tenant support, no auth system, no billing.
- No auto-sending quotes to end clients — the bot sends the PDF back to the
  carpenter, who forwards it himself.
- No LangGraph, no MCP, no LangSmith/LangFuse. This is a straight-line
  pipeline with one correction loop, not a branching multi-agent workflow.
  Revisit these tools only in Phase 3+ if real complexity demands it.

**Primary success metric:** the carpenter uses this on a real job instead of
doing the quote by hand, and trusts the number enough to send it.

---

## 1. Architecture (MVP)

```
Telegram (voice note / text, Hebrew)
        |
        v
FastAPI app (single service, single process is fine)
        |
        1. Transcribe: Whisper API, Hebrew, with a jargon-biasing prompt
        |
        2. Parse: one Claude API call -> structured JSON (KitchenSpec)
        |    includes a missing_fields / low_confidence_fields list
        |
        3. If missing_fields non-empty -> send ONE clarifying question,
        |    wait for reply, go back to step 2 with the answer appended
        |
        4. Price lookup: plain Python function reads the catalog
        |    (Google Sheet via Sheets API, or local SQLite table)
        |
        5. Deterministic calculation: pure Python, no LLM math
        |    total = materials + hardware + labor_hours*rate + transport
        |    total *= (1 + margin)
        |
        6. Send draft summary to Telegram with inline buttons:
        |    [Approve] [Correct]
        |    (state = one row per chat_id in SQLite holding the draft JSON)
        |
        7a. Approve -> render PDF (WeasyPrint, Hebrew RTL template)
        |     -> send PDF back to carpenter in Telegram
        |
        7b. Correction reply (free text, e.g. "change handles to black alloy")
              -> re-run step 2 on just the delta, merge into existing spec,
                 loop back to step 4
```

### Why these choices
- **Telegram, not WhatsApp, for MVP.** Telegram Bot API is free, has no
  business-verification process, supports voice notes and inline buttons
  natively, and the carpenter only needs to install one ordinary app once.
  WhatsApp Business API is the right choice later when onboarding other
  carpenters (WhatsApp is the default messaging app in Israel), but the
  approval/registration overhead isn't worth it for a single test user.
- **SQLite, not Postgres.** One user, low write volume, zero ops burden.
  Migrate to Postgres only when there are multiple concurrent workshops.
- **Google Sheet as the price catalog (recommended default).** The carpenter
  or family can edit prices directly in a spreadsheet they already
  understand, with zero admin UI to build. Read-only access via the Sheets
  API on the backend. (Alternative: a local SQLite `catalog` table if he'd
  never touch a spreadsheet himself — pick this only if he explicitly wants
  no editing responsibility at all.)
- **No LLM math, ever.** All arithmetic is plain Python. The LLM's only job
  is turning speech into structured data and asking clarifying questions.
- **No LangGraph/MCP/LangSmith.** This flow is linear with a single
  loop-back on correction. A plain Python state dict handles that fine.
  Logging goes to a simple SQLite table, not an observability platform.

---

## 2. Data Model

### `KitchenSpec` (Pydantic model — the structured parse output)
```python
class KitchenSpec(BaseModel):
    layout: str | None            # e.g. "L-shape", "U-shape", "straight"
    dimensions_m: str | None      # free text is fine for MVP, e.g. "4x3"
    material: str | None          # e.g. "Oak veneer", "melamine 18mm"
    cabinet_count: int | None
    drawer_count: int | None
    drawer_type: str | None       # e.g. brand/model if mentioned
    hinge_type: str | None
    handle_type: str | None
    countertop: str | None
    notes: str | None             # anything that doesn't fit a field
    missing_fields: list[str]     # fields the LLM couldn't confidently fill
    low_confidence_fields: list[str]
```
Keep this permissive (mostly `str | None`) for MVP — don't over-constrain
enums yet. Tighten types once real transcripts show the actual value space.

### `quotes` table (SQLite)
`id, chat_id, client_name, spec_json, total_ils, status (draft/approved/sent),
created_at, updated_at`

### `pending_state` table (SQLite)
`chat_id (PK), spec_json, awaiting_field (nullable), updated_at`
— one row per active conversation; cleared on approve or cancel.

### `transcripts_log` table (SQLite)
`id, chat_id, raw_audio_transcript, parsed_json, correction_text (nullable),
created_at`
— this is the data that will later drive jargon-glossary tuning and few-shot
examples. Log every single interaction from day one, even in MVP.

### Catalog schema (Google Sheet columns)
`item_name, category (material/hardware/labor/transport), unit, unit_price_ils,
notes`

---

## 3. Build Order (suggested milestones for Claude Code)

### Milestone 1 — Skeleton & Telegram round-trip
- FastAPI app with a Telegram webhook endpoint (use `python-telegram-bot`
  or raw webhook + `httpx`).
- Bot receives a text message and echoes it back. Confirms the wiring works
  before adding any AI.
- SQLite setup (`quotes`, `pending_state`, `transcripts_log` tables).

### Milestone 2 — Transcription
- Handle incoming voice notes: download the `.ogg` file from Telegram,
  send to Whisper API with `language="he"` and an `initial_prompt` containing
  a starter Hebrew carpentry glossary (see Phase 1 below — start with a
  reasonable manual list of ~30 terms).
- Log every transcript to `transcripts_log`.

### Milestone 3 — Structured parsing
- One Claude API call (tool use / structured output) that takes the
  transcript and returns a `KitchenSpec` JSON, including `missing_fields`.
- If `missing_fields` is non-empty, send back one short Hebrew clarifying
  question for the single most important missing field, and store
  `awaiting_field` in `pending_state`. On reply, re-parse with the answer
  appended to context and merge.

### Milestone 4 — Pricing engine
- Google Sheets API read (service account credentials) pulling the catalog
  into memory (cache with a short TTL, e.g. 5 minutes, so sheet edits show
  up without a redeploy).
- Pure Python `calculate_quote(spec: KitchenSpec, catalog: dict) -> QuoteBreakdown`
  function — deterministic, unit-tested with a handful of fixed examples.
- Config values (default margin %, hourly labor rate, transport flat fee)
  live in a small `config.yaml` or the same Sheet on a second tab — carpenter-
  editable, not hardcoded.

### Milestone 5 — Approval loop
- Send the draft total + breakdown to Telegram with inline `Approve`/`Correct`
  buttons.
- `Approve` -> move to Milestone 6.
- `Correct` -> prompt for free-text correction, re-run parse on the delta
  (merge into existing spec, don't restart from scratch), recalculate, show
  the new draft again.

### Milestone 6 — PDF generation
- WeasyPrint (or ReportLab if RTL handling proves easier there — test both
  with real Hebrew text and Hebrew digits/currency formatting early) with
  an HTML/CSS template: header with carpenter's business name/logo, line-item
  breakdown table, total, payment terms footer.
- Render to PDF, send back to the carpenter via Telegram as a document.
- Mark `quotes.status = approved` and store the final PDF path or bytes.

### Milestone 7 — Real-world dry run
- Carpenter uses it on 3-5 real jobs.
- Review `transcripts_log` for every case where parsing was wrong or a
  correction was needed — this directly feeds Phase 1 (jargon tuning) below.

---

## 4. Post-MVP Phases (do not build now, listed for context)

- **Phase 1 — Parsing reliability:** build a Hebrew carpentry glossary and
  few-shot example set from real logged transcripts + corrections. Add
  "like the last kitchen" retrieval (pull a past `quotes` row as a starting
  template).
- **Phase 2 — Business memory:** quote history with won/lost tracking,
  simple margin analytics.
- **Phase 3 — Multi-carpenter readiness:** move catalog to a real DB with a
  minimal admin UI, switch messaging to WhatsApp Business API, add
  per-workshop config.
- **Phase 4 — Commercialization:** multi-tenant auth, billing, self-serve
  onboarding. Reconsider LangGraph/observability tooling only if the
  workflow has grown genuinely branching by this point.

---

## 5. Tech Stack (MVP)

| Concern | Choice |
|---|---|
| Language | Python 3.11+ |
| API framework | FastAPI + Uvicorn |
| Messaging | Telegram Bot API (`python-telegram-bot`) |
| Speech-to-text | OpenAI Whisper API |
| LLM parsing | Anthropic Claude API (Sonnet) |
| Validation | Pydantic v2 |
| Storage | SQLite (`sqlite3` / SQLAlchemy, no async needed at this scale) |
| Catalog | Google Sheets API (read-only) |
| PDF generation | WeasyPrint (try first) or ReportLab (fallback for RTL issues) |
| Hosting (MVP) | Single small VM / Fly.io / Railway — anything that can hold a webhook + SQLite file |

---

## 6. Accounts & API Keys Needed

Set these up before starting, or as you hit each milestone:

1. **Telegram Bot Token**
   - Open Telegram, message **@BotFather**, run `/newbot`, follow prompts.
   - Free, instant, no business verification needed.
   - Gives you a `TELEGRAM_BOT_TOKEN`.

2. **Anthropic API key** (for the parsing step)
   - Register at https://console.anthropic.com
   - Create an API key, gives you `ANTHROPIC_API_KEY`.
   - This is pay-as-you-go; MVP usage volume (one carpenter, a few quotes a
     day) will be very cheap.

3. **OpenAI API key** (for Whisper transcription)
   - Register at https://platform.openai.com
   - Create an API key, gives you `OPENAI_API_KEY`.
   - Whisper is billed per audio minute — cheap at this volume.
   - (Alternative if you want to avoid a second AI vendor: self-hosted
     `faster-whisper` running locally/on your VM, no API key needed, but
     more ops work. Recommend starting with the hosted API for MVP speed.)

4. **Google Cloud service account (Sheets API access)**
   - Create a project at https://console.cloud.google.com
   - Enable the "Google Sheets API".
   - Create a Service Account, download its JSON credentials file.
   - Share the actual price-catalog Google Sheet with the service account's
     email address (read access is enough).
   - No cost at this usage level (well within free quota).

5. **Hosting account** (pick one)
   - Fly.io, Railway, or Render all support "small always-on Python service
     + persistent disk for SQLite" cheaply (free/low tier is enough for MVP).
   - Needed mainly so the Telegram webhook has a stable public HTTPS URL.

**Not needed for MVP** (deliberately deferred): WhatsApp Business API /
Twilio account, Postgres hosting, LangSmith/LangFuse account, any
multi-tenant auth provider.

---

## 7. Open Questions to Resolve Before/During Build

- Does the carpenter have (or want) a business name/logo for the PDF header,
  or keep it plain for now?
- What's his current default margin % and hourly labor rate — needed to
  seed the config/catalog with real starting numbers rather than guesses.
- Confirm he's comfortable using Telegram (vs. wanting WhatsApp from day
  one) — if WhatsApp is a hard requirement even for the prototype, swap
  Milestone 1 to use the WhatsApp Cloud API (Meta's own API, not Twilio,
  has a simpler personal-testing path via a test business number) instead
  of Telegram, accepting the extra setup step.
