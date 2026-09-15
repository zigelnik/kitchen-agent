"""FastAPI app exposing the Telegram webhook, plus a polling entry point.

Two ways to run:

  Local dev (no public URL needed):
      python -m app.main

  Deployed (stable HTTPS URL, webhook):
      uvicorn app.main:api --host 0.0.0.0 --port 8000
  then register the webhook once:
      curl -F "url=https://YOUR_HOST/telegram/webhook/<SECRET>" \
           https://api.telegram.org/bot<TOKEN>/setWebhook
"""
from __future__ import annotations

import logging

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from telegram import Update

from app import db
from app.bot import build_application
from app.config import get_settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger(__name__)

_application = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _application
    db.init_db()
    settings = get_settings()
    if settings.has_telegram:
        _application = build_application()
        await _application.initialize()
        log.info("telegram application ready")
    else:
        log.warning("TELEGRAM_BOT_TOKEN unset — webhook will reject updates")
    yield
    if _application is not None:
        await _application.shutdown()


api = FastAPI(title="Kitchen Quote Agent", lifespan=lifespan)


@api.get("/health")
async def health() -> dict:
    settings = get_settings()
    return {
        "status": "ok",
        "telegram_configured": settings.has_telegram,
        "anthropic_configured": settings.has_anthropic,
        "transcribe_backend": settings.transcribe_backend,
    }


@api.post("/telegram/webhook/{secret}")
async def telegram_webhook(secret: str, request: Request) -> dict:
    settings = get_settings()
    if secret != settings.telegram_webhook_secret:
        raise HTTPException(status_code=403, detail="bad webhook secret")
    if _application is None:
        raise HTTPException(status_code=503, detail="bot not configured")

    payload = await request.json()
    update = Update.de_json(payload, _application.bot)
    await _application.process_update(update)
    return {"ok": True}


def run_polling() -> None:
    """Local development runner — no public URL required."""
    db.init_db()
    application = build_application()
    log.info("starting polling; press Ctrl+C to stop")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    run_polling()
