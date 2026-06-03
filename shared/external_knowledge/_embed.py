"""Tiny deterministic 384-dim embedding using hashing trick.

We do NOT depend on sentence-transformers (heavy + paid hosting on Railway).
This is a feature-hash bag-of-tokens vector, which is sufficient for
cosine-based nearest neighbour over short Dubai property news snippets.

For production-grade semantic search swap in `sentence-transformers`
`all-MiniLM-L6-v2` (384 dim) — the rest of the pipeline is unchanged.
"""
from __future__ import annotations

import hashlib
import math
import re
from typing import List

_DIM = 384
_TOKEN_RE = re.compile(r"[A-Za-zА-Яа-яЁё0-9]+")


def embed(text: str) -> List[float]:
    vec = [0.0] * _DIM
    if not text:
        return vec
    toks = _TOKEN_RE.findall(text.lower())
    if not toks:
        return vec
    for t in toks:
        h = hashlib.md5(t.encode("utf-8")).digest()
        idx = int.from_bytes(h[:4], "little") % _DIM
        sign = 1.0 if (h[4] & 1) else -1.0
        vec[idx] += sign
    # L2 normalize
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


def cosine(a: List[float], b: List[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    # both already normalized → dot product
    return sum(x * y for x, y in zip(a, b))
