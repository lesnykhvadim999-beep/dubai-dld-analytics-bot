"""Embeddings provider — Gemini text-embedding-004 (free 1500/day).

Falls back to deterministic hash-based pseudo-embedding when Gemini unavailable
(keeps system usable for seeding/tests even without API key — search quality
degrades but never crashes).
"""
from __future__ import annotations

import hashlib
import logging
import os
import time
from typing import List

log = logging.getLogger(__name__)

EMBED_DIM = 768
_GEMINI_KEY = (
    os.environ.get("GEMINI_API_KEY")
    or os.environ.get("GOOGLE_API_KEY")
    or os.environ.get("GEMINI_KEY")
)

_backoff_until = 0.0


def _hash_embed(text: str) -> List[float]:
    """Deterministic pseudo-embedding — last-resort fallback only."""
    text = (text or "").strip().lower()
    out = []
    seed = text.encode("utf-8") or b"_"
    for i in range(EMBED_DIM // 32):
        h = hashlib.sha256(seed + str(i).encode()).digest()
        for j in range(0, 32, 4):
            v = int.from_bytes(h[j:j + 4], "big") / 2**32
            out.append((v - 0.5) * 0.1)
    # normalise length
    return (out + [0.0] * EMBED_DIM)[:EMBED_DIM]


def embed_text(text: str) -> List[float]:
    global _backoff_until
    text = (text or "").strip()
    if not text:
        return [0.0] * EMBED_DIM

    if _GEMINI_KEY and time.time() > _backoff_until:
        try:
            import google.generativeai as genai  # type: ignore
            genai.configure(api_key=_GEMINI_KEY)
            result = genai.embed_content(
                model="models/text-embedding-004",
                content=text[:8000],
            )
            emb = result["embedding"] if isinstance(result, dict) else result.embedding
            if emb and len(emb) == EMBED_DIM:
                return list(emb)
        except Exception as exc:  # noqa: BLE001
            msg = str(exc).lower()
            if "429" in msg or "quota" in msg or "rate" in msg:
                _backoff_until = time.time() + 300  # 5 min backoff
                log.warning("Gemini embeddings: rate-limited, backoff 5min")
            else:
                log.warning("Gemini embeddings failed: %s", exc)

    return _hash_embed(text)


def to_pgvector_literal(vec: List[float]) -> str:
    """Format vector as PostgreSQL pgvector literal `[v1,v2,...]`."""
    return "[" + ",".join(f"{x:.6f}" for x in vec) + "]"
