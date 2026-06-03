"""
Бесплатная генерация ассетов: Pollinations.ai (images) + Edge-TTS (audio).
Не требует API-ключей.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from pathlib import Path
from typing import Optional
from urllib.parse import quote

import requests

log = logging.getLogger("content_pipeline.assets")

CACHE_DIR = Path(os.getenv("CONTENT_CACHE_DIR", "C:/Projects/shared/content_pipeline/_cache"))
CACHE_DIR.mkdir(parents=True, exist_ok=True)


def pollinations_image(
    prompt: str,
    *,
    width: int = 1024,
    height: int = 1024,
    seed: Optional[int] = None,
    out_dir: Path = CACHE_DIR,
) -> Optional[str]:
    """Возвращает локальный путь к PNG. Кэш по hash(prompt+wh+seed)."""
    key = hashlib.sha1(f"{prompt}|{width}x{height}|{seed}".encode()).hexdigest()[:16]
    out = out_dir / f"poll_{key}.png"
    if out.exists():
        return str(out)
    try:
        seed_part = f"&seed={seed}" if seed is not None else ""
        url = (
            f"https://image.pollinations.ai/prompt/{quote(prompt)}"
            f"?width={width}&height={height}&nologo=true{seed_part}"
        )
        r = requests.get(url, timeout=45)
        if r.status_code == 200 and r.content:
            out.write_bytes(r.content)
            return str(out)
        log.warning("pollinations http=%s", r.status_code)
    except Exception as e:  # noqa: BLE001
        log.exception("pollinations_image failed: %s", e)
    return None


def edge_tts_synth(
    text: str,
    *,
    voice: str = "ru-RU-SvetlanaNeural",
    out_dir: Path = CACHE_DIR,
) -> Optional[str]:
    """Возвращает локальный путь MP3."""
    try:
        import edge_tts  # type: ignore
    except ImportError:
        log.warning("edge_tts not installed; pip install edge-tts")
        return None
    key = hashlib.sha1(f"{voice}|{text}".encode("utf-8")).hexdigest()[:16]
    out = out_dir / f"tts_{key}.mp3"
    if out.exists():
        return str(out)

    async def _go():
        comm = edge_tts.Communicate(text, voice=voice)
        await comm.save(str(out))

    try:
        asyncio.run(_go())
        return str(out) if out.exists() else None
    except RuntimeError:
        # уже внутри event loop (например, aiogram handler) — пишем sync через nested loop
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(_go())
            return str(out) if out.exists() else None
        finally:
            loop.close()
    except Exception as e:  # noqa: BLE001
        log.exception("edge_tts failed: %s", e)
        return None
