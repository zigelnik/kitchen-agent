"""Speech-to-text. Local faster-whisper by default (no API key, no vendor).

Both backends sit behind `transcribe()` so switching is an env-var change.
"""
from __future__ import annotations

import logging
from pathlib import Path

from app.config import get_settings
from app.glossary import whisper_prompt

log = logging.getLogger(__name__)

_model = None


def _get_local_model():
    """Lazy-load the model. First call downloads weights (~1-3GB) and is slow."""
    global _model
    if _model is None:
        from faster_whisper import WhisperModel

        settings = get_settings()
        log.info("loading faster-whisper model %s", settings.whisper_model)
        # int8 on CPU keeps memory and latency sane on a small VM.
        _model = WhisperModel(settings.whisper_model, device="cpu", compute_type="int8")
    return _model


def _transcribe_local(path: Path) -> str:
    model = _get_local_model()
    segments, _info = model.transcribe(
        str(path),
        language="he",
        initial_prompt=whisper_prompt(),
        vad_filter=True,
        beam_size=5,
    )
    return " ".join(seg.text.strip() for seg in segments).strip()


def _transcribe_openai(path: Path) -> str:
    import httpx

    settings = get_settings()
    if not settings.openai_api_key:
        raise RuntimeError("TRANSCRIBE_BACKEND=openai but OPENAI_API_KEY is unset")

    with path.open("rb") as fh:
        response = httpx.post(
            "https://api.openai.com/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {settings.openai_api_key}"},
            files={"file": (path.name, fh, "audio/ogg")},
            data={
                "model": "whisper-1",
                "language": "he",
                "prompt": whisper_prompt(),
            },
            timeout=120.0,
        )
    response.raise_for_status()
    return (response.json().get("text") or "").strip()


def transcribe(path: Path) -> str:
    """Transcribe Hebrew audio to text."""
    backend = get_settings().transcribe_backend.lower()
    if backend == "openai":
        return _transcribe_openai(path)
    return _transcribe_local(path)
