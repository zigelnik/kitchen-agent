# Kitchen Cabinetry Quote Agent

Telegram bot that turns a Hebrew voice note describing a kitchen job into a
priced PDF quote, with an approve/correct loop before anything goes out.

Built per [PLAN.md](PLAN.md). Milestones 1-6 are implemented.

## How it works

```
voice note (Hebrew)  ->  faster-whisper (local)  ->  transcript
transcript           ->  Claude (tool use)       ->  KitchenSpec JSON
missing fields?      ->  one clarifying question ->  merge answer
spec + catalog.csv   ->  pure-Python pricing     ->  QuoteBreakdown
draft + buttons      ->  [approve] [correct] [cancel]
approve              ->  ReportLab RTL PDF       ->  sent back in Telegram
```

No LLM ever touches the arithmetic. Prices come from `data/catalog.csv` and
rates from `config.yaml`; the calculation is plain Python covered by unit tests.

## Setup

```bash
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt   # Linux/macOS: .venv/bin/pip
cp .env.example .env
```

Then fill in `.env`:

| Variable | Needed for | Where to get it |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | everything | Telegram → @BotFather → `/newbot` |
| `ANTHROPIC_API_KEY` | parsing transcripts | console.anthropic.com |
| `ALLOWED_TELEGRAM_USER_ID` | locking the bot to one user | `/start` the bot, read the id from the log |
| `TRANSCRIBE_BACKEND` | `local` (default) or `openai` | — |
| `OPENAI_API_KEY` | only if backend is `openai` | platform.openai.com |

`ALLOWED_TELEGRAM_USER_ID` left blank means **anyone who finds the bot can use
it** (and spend your Anthropic credit). Set it before the token is anywhere
public.

## Running

Local development, no public URL needed:

```bash
.venv/Scripts/python -m app.main
```

Deployed with a webhook:

```bash
.venv/Scripts/uvicorn app.main:api --host 0.0.0.0 --port 8000
# register once:
curl -F "url=https://YOUR_HOST/telegram/webhook/$TELEGRAM_WEBHOOK_SECRET" \
     "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/setWebhook"
```

`GET /health` reports which keys are configured.

## Tests

```bash
.venv/Scripts/python -m pytest -q
```

24 tests, no API keys or network required.

## Editing prices

- `data/catalog.csv` — item prices. Columns: `item_name, category
  (material/hardware/labor/transport), unit (cabinet/meter/unit/flat),
  unit_price_ils, notes`. Reloaded automatically every 5 minutes.
- `config.yaml` — labor rates, margin, transport fee, payment terms, VAT.

Item names are matched loosely against what the carpenter says, so keep them
in the Hebrew he actually uses. Anything unmatched falls back to the cheapest
item in its category **and adds a visible warning to the draft** rather than
silently guessing.

### Margin policy

`margin_pct` applies to materials + hardware only. Labor is already marked up
through the cost/bill hourly spread (₪50 cost → ₪100 billed), so applying
margin to labor too would double-count it. VAT, if enabled, is applied last.

## Notes and known gaps

- **Google Sheets catalog** (PLAN.md §1) was deferred in favour of a local CSV
  so the bot works with no cloud setup. `app/catalog.py:load_catalog()` is the
  only function that would change.
- **VAT is 0** in `config.yaml`. Israeli VAT is 18%; left off until it's
  confirmed whether quotes should show inclusive or exclusive prices.
- **WeasyPrint is unused.** It needs native GTK/Pango libraries that aren't
  present on Windows, so it cannot even import. `app/pdf.py` uses ReportLab,
  which is pure Python and portable.
- **RTL is hand-applied.** ReportLab doesn't implement the bidirectional
  algorithm, so every Hebrew string passes through `rtl()` (python-bidi)
  before rendering. `tests/test_pdf.py` pins this down — if someone turns
  `rtl()` into a pass-through, the tests fail. When eyeballing RTL output,
  never prefix samples with ASCII labels: it changes the resolved base
  direction and inverts which variant looks correct.
- **First transcription is slow.** faster-whisper downloads the `large-v3`
  weights (~1-3 GB) on first use, then runs ~10-30 s per note on CPU. Set
  `WHISPER_MODEL=small` to trade accuracy for speed while testing.
- **`labor_hours` is estimated** when not stated (4h base + 1.5h/cabinet +
  0.5h/drawer) and the estimate is always shown as a warning in the draft.
  Replace the constants in `app/pricing.py` once real jobs show actual hours.
