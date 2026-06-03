"""
Layer 20 — Virtual tour generator.

Input: dict с описанием листинга (area, bedrooms, view, project и т.д.)
Output: dict {rooms, script_md, audio_path, video_path, cover_image, from_cache}
Кэшируется в virtual_tours.
"""
from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from .cache import get_cached_tour, save_tour_cache

# Используем тот же пул бесплатных ассетов и LLM, что и content_pipeline.
from shared.content_pipeline._assets import pollinations_image, edge_tts_synth, CACHE_DIR
from shared.content_pipeline._llm import chat

log = logging.getLogger("virtual_tours.gen")

VIDEO_DIR = Path(os.getenv("TOUR_VIDEO_DIR", str(CACHE_DIR / "tours")))
VIDEO_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_ROOMS = ["Living room", "Kitchen", "Master bedroom", "Bathroom", "Balcony / view"]


def _plan_rooms(listing: Dict[str, Any]) -> List[Dict[str, str]]:
    """Просим LLM описать 4-5 комнат + промпты под Pollinations."""
    descr = json.dumps({k: listing.get(k) for k in (
        "area", "bedrooms", "view", "project", "size_sqft", "category", "developer",
    )}, ensure_ascii=False)
    prompt = (
        f"Listing: {descr}\n\n"
        "Опиши 4-5 ключевых комнат для виртуального тура. JSON строго: "
        '[{"name":"<RU название>","prompt":"<EN image prompt для photo-realistic Pollinations>",'
        '"narration":"<RU озвучка 2-3 предложения>"}].'
        " Подпись бренда не нужна (добавлю отдельно). Без markdown."
    )
    raw = chat(prompt, max_tokens=900, temperature=0.55)
    if not raw:
        return [{"name": r, "prompt": f"Dubai luxury apartment {r}, photo-realistic", "narration": r} for r in DEFAULT_ROOMS]
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z]*\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw)
    try:
        data = json.loads(raw)
        if isinstance(data, list) and data:
            return data[:5]
    except Exception:
        m = re.search(r"\[.*\]", raw, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))[:5]
            except Exception:
                pass
    return [{"name": r, "prompt": f"Dubai luxury apartment {r}, photo-realistic", "narration": r} for r in DEFAULT_ROOMS]


def _build_video(images: List[str], audio: Optional[str], out_path: Path,
                 per_image_sec: float = 3.5) -> Optional[str]:
    """MoviePy slideshow. Если moviepy не установлен — возвращаем None."""
    if not images:
        return None
    try:
        from moviepy.editor import ImageClip, concatenate_videoclips, AudioFileClip
    except Exception as e:  # noqa: BLE001
        log.warning("moviepy not available (%s), skipping video", e)
        return None
    try:
        clips = [ImageClip(p).set_duration(per_image_sec) for p in images if p]
        if not clips:
            return None
        video = concatenate_videoclips(clips, method="compose")
        if audio and Path(audio).exists():
            a = AudioFileClip(audio)
            video = video.set_audio(a.subclip(0, min(a.duration, video.duration)))
        video.write_videofile(
            str(out_path), fps=24, codec="libx264", audio_codec="aac",
            logger=None, verbose=False,
        )
        return str(out_path)
    except Exception as e:  # noqa: BLE001
        log.exception("video build failed: %s", e)
        return None


def generate_tour(
    listing: Dict[str, Any],
    *,
    bot: str = "resale-bot",
    force: bool = False,
) -> Dict[str, Any]:
    listing_id = str(listing.get("listing_id") or listing.get("id") or "")
    if not listing_id:
        return {"error": "no_listing_id"}

    if not force:
        cached = get_cached_tour(listing_id, bot)
        if cached:
            cached["from_cache"] = True
            return cached

    rooms = _plan_rooms(listing)
    # Картинки
    for i, r in enumerate(rooms):
        img = pollinations_image(
            r.get("prompt") or f"Dubai apartment {r.get('name')}",
            width=1280, height=720, seed=hash(listing_id + str(i)) % 10_000_000,
        )
        r["image_path"] = img

    # Сценарий + озвучка
    script_md = "🎥 *Виртуальный тур*\n\n" + "\n\n".join(
        f"*{r.get('name')}*\n{r.get('narration','')}" for r in rooms
    ) + "\n\n_Vadim Realty · RERA BRN 65011_"
    narration_text = " ".join(r.get("narration", "") for r in rooms)
    audio_path = edge_tts_synth(narration_text or "Виртуальный тур по объекту от Vadim Realty.")

    images = [r["image_path"] for r in rooms if r.get("image_path")]
    video_path = _build_video(images, audio_path, VIDEO_DIR / f"tour_{bot}_{listing_id}.mp4")
    cover = images[0] if images else None

    save_tour_cache(listing_id, bot, rooms, script_md, audio_path, video_path, cover)
    return {
        "listing_id": listing_id, "bot": bot,
        "rooms": rooms, "script_md": script_md,
        "audio_path": audio_path, "video_path": video_path,
        "cover_image": cover, "from_cache": False,
    }
