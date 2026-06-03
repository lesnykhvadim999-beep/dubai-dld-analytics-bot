"""
PHASE BM Layer 20 — Virtual Property Tours.

Storytelling tour: Cerebras пишет сценарий по комнатам,
Pollinations.ai генерирует картинки, Edge-TTS озвучивает,
MoviePy склеивает в slideshow video. Кэш по listing_id.
"""
from .tour_generator import generate_tour
from .cache import get_cached_tour, save_tour_cache

__all__ = ["generate_tour", "get_cached_tour", "save_tour_cache"]
