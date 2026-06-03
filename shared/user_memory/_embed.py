"""
shared.user_memory._embed — embeddings (sentence-transformers or hash fallback).

Free, CPU-only. Model: paraphrase-multilingual-MiniLM-L12-v2 (384 dims).
If the package is missing or model load fails, falls back to a deterministic
hash-bag embedding so the rest of the pipeline still works.
"""
from __future__ import annotations
import os
import hashlib
import math
import threading
from typing import List, Optional

EMBED_DIM = 384
_MODEL_NAME = os.environ.get(
    "USER_MEMORY_EMBED_MODEL",
    "paraphrase-multilingual-MiniLM-L12-v2",
)

_model = None
_load_lock = threading.Lock()
_load_failed = False


def _try_load_model():
    global _model, _load_failed
    if _model is not None or _load_failed:
        return
    if os.environ.get("USER_MEMORY_DISABLE_ST", "0") == "1":
        _load_failed = True
        return
    with _load_lock:
        if _model is not None or _load_failed:
            return
        try:
            from sentence_transformers import SentenceTransformer
            _model = SentenceTransformer(_MODEL_NAME)
        except Exception:
            _load_failed = True
            _model = None


def _hash_embed(text: str) -> List[float]:
    """Deterministic hash-bag embedding (free, no deps).
    Not semantically rich, but stable for fallback cosine ops.
    """
    vec = [0.0] * EMBED_DIM
    tokens = [t for t in (text or "").lower().split() if t]
    if not tokens:
        return vec
    for tok in tokens:
        h = hashlib.sha256(tok.encode("utf-8")).digest()
        # 384 dims, take 2 bytes each, sign by bit
        for i in range(EMBED_DIM):
            b = h[i % len(h)]
            sign = 1.0 if (b & 0x80) else -1.0
            vec[i] += sign * ((b & 0x7F) / 127.0)
    # L2 normalize
    n = math.sqrt(sum(x * x for x in vec))
    if n > 0:
        vec = [x / n for x in vec]
    return vec


def embed(text: Optional[str]) -> List[float]:
    if not text:
        return [0.0] * EMBED_DIM
    _try_load_model()
    if _model is not None:
        try:
            v = _model.encode(text, normalize_embeddings=True)
            return [float(x) for x in v]
        except Exception:
            pass
    return _hash_embed(text)


def embed_batch(texts: List[str]) -> List[List[float]]:
    _try_load_model()
    if _model is not None:
        try:
            arr = _model.encode(texts, normalize_embeddings=True, batch_size=16)
            return [[float(x) for x in row] for row in arr]
        except Exception:
            pass
    return [_hash_embed(t) for t in texts]


def to_pgvector_literal(vec: List[float]) -> str:
    """Format a Python list as a pgvector literal string '[x,y,...]'."""
    return "[" + ",".join(f"{float(x):.6f}" for x in vec) + "]"
