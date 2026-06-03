"""
PHASE BM Layer 18 — Multimodal Understanding.

Vadim Realty (RERA BRN 65011). Free APIs only:
- Gemini Flash 2.0 Vision  (фото, документы, floor plans)
- Groq Whisper-v3          (voice -> text)
- Cerebras LLaMa            (intent classifier поверх transcript)
"""

from .photo_understanding import analyze_photo
from .voice_to_intent import voice_to_intent
from .doc_parser import parse_document

__all__ = ["analyze_photo", "voice_to_intent", "parse_document"]
