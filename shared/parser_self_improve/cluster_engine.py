"""
cluster_engine — k-means clustering текстов через Gemini text-embedding-004 (free).

Fallback при недоступности embeddings: TF-IDF + cosine k-means через sklearn,
а если и sklearn нет — простой shingle-hash bucketing.

Quota: Gemini text-embedding-004 free tier ~1500 req/day.
Мы передаём батчами по 100 текстов → max 100 запросов на цикл.
"""
from __future__ import annotations

import logging
import os
import random
import time
from typing import Sequence

log = logging.getLogger(__name__)

GEMINI_KEY = os.environ.get("GEMINI_API_KEY")
EMBED_MODEL = "models/gemini-embedding-001"


# ──────────────────────────── Gemini embeddings (REST) ─────────────────────
def _embed_gemini(texts: Sequence[str]) -> list[list[float]] | None:
    """REST call к Gemini embeddings — без зависимости от google-generativeai."""
    if not GEMINI_KEY:
        return None
    try:
        import requests  # стандартный depends

        out: list[list[float]] = []
        url = (
            "https://generativelanguage.googleapis.com/v1beta/"
            f"{EMBED_MODEL}:embedContent?key={GEMINI_KEY}"
        )
        for t in texts:
            payload = {
                "model": EMBED_MODEL,
                "content": {"parts": [{"text": t[:3000]}]},
            }
            r = requests.post(url, json=payload, timeout=20)
            if r.status_code != 200:
                # rate-limit / quota — выходим, dispatch на fallback
                log.warning(
                    "Gemini embed %s — falling back to ngram", r.status_code
                )
                return None
            emb = r.json().get("embedding", {}).get("values")
            if not emb:
                return None
            out.append(emb)
            time.sleep(0.05)  # 1500/day → 1 req/0.05s ОК
        return out
    except Exception as exc:
        log.warning("Gemini embeddings failed: %s — fallback ngram", exc)
        return None


# ──────────────────────────── Character n-gram embedder (no deps) ──────────
def _embed_ngram(texts: Sequence[str], dim: int = 256, n: int = 3) -> list[list[float]]:
    """Hashed character n-gram bag-of-features. Без sklearn/numpy."""
    import math

    vectors: list[list[float]] = []
    for t in texts:
        lo = (t or "").lower()[:3000]
        vec = [0.0] * dim
        for i in range(max(0, len(lo) - n + 1)):
            gram = lo[i : i + n]
            h = hash(gram) % dim
            vec[h] += 1.0
        # L2 normalise
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        vectors.append([v / norm for v in vec])
    return vectors


# ──────────────────────────── TF-IDF fallback ──────────────────────────────
def _embed_tfidf(texts: Sequence[str]) -> list[list[float]] | None:
    try:
        from sklearn.feature_extraction.text import TfidfVectorizer  # type: ignore

        vec = TfidfVectorizer(
            max_features=512, ngram_range=(1, 2), stop_words="english"
        )
        mat = vec.fit_transform([t[:3000] for t in texts]).toarray()
        return mat.tolist()
    except Exception as exc:
        log.warning("TF-IDF fallback failed: %s", exc)
        return None


def embed_texts(texts: Sequence[str]) -> list[list[float]] | None:
    """Order: Gemini → TF-IDF (sklearn) → char n-gram (no deps)."""
    vecs = _embed_gemini(texts)
    if vecs is not None:
        log.info("embed: gemini OK (n=%d, dim=%d)", len(vecs), len(vecs[0]) if vecs else 0)
        return vecs
    vecs = _embed_tfidf(texts)
    if vecs is not None:
        log.info("embed: tfidf OK (n=%d)", len(vecs))
        return vecs
    log.info("embed: char-ngram fallback (no external deps)")
    return _embed_ngram(texts)


# ──────────────────────────── k-means ──────────────────────────────────────
def _euclid(a: list[float], b: list[float]) -> float:
    return sum((x - y) ** 2 for x, y in zip(a, b))


def _kmeans(vectors: list[list[float]], k: int, max_iter: int = 25) -> list[int]:
    if len(vectors) <= k:
        return list(range(len(vectors)))
    random.seed(42)
    # init: k-means++ light
    centroids = [vectors[random.randrange(len(vectors))]]
    while len(centroids) < k:
        dists = [min(_euclid(v, c) for c in centroids) for v in vectors]
        total = sum(dists) or 1.0
        r = random.uniform(0, total)
        acc = 0.0
        for v, d in zip(vectors, dists):
            acc += d
            if acc >= r:
                centroids.append(v)
                break
    assignments = [0] * len(vectors)
    for _ in range(max_iter):
        changed = False
        for i, v in enumerate(vectors):
            best = min(range(k), key=lambda j: _euclid(v, centroids[j]))
            if best != assignments[i]:
                assignments[i] = best
                changed = True
        # recompute centroids
        new_centroids: list[list[float]] = []
        dim = len(vectors[0])
        for j in range(k):
            members = [vectors[i] for i in range(len(vectors)) if assignments[i] == j]
            if not members:
                new_centroids.append(centroids[j])
                continue
            new_centroids.append(
                [sum(m[d] for m in members) / len(members) for d in range(dim)]
            )
        centroids = new_centroids
        if not changed:
            break
    return assignments


def cluster_texts(texts: Sequence[str], k: int = 10) -> list[int]:
    """Возвращает список cluster_id той же длины что texts. -1 если кластеризация невозможна."""
    if len(texts) < 2:
        return [0] * len(texts)
    vectors = embed_texts(texts)
    if vectors is None:
        log.warning("No embeddings available — single-bucket fallback")
        return [0] * len(texts)
    effective_k = min(k, max(2, len(texts) // 4))
    try:
        return _kmeans(vectors, effective_k)
    except Exception as exc:
        log.warning("k-means failed: %s — single-bucket fallback", exc)
        return [0] * len(texts)
