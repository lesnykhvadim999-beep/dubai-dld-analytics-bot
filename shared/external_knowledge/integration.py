"""Public API: search_external_news(query, top_k) for all bots.

Semantic search over external_signals using cosine similarity on the
384-dim hashed embedding stored as JSON in `embedding` column.

Falls back to LIKE search if no embeddings present.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from ._db import conn_cursor
from ._embed import embed, cosine

log = logging.getLogger(__name__)


def search_external_news(
    query: str,
    top_k: int = 5,
    min_relevance: float = 0.3,
    max_age_days: int = 14,
) -> list[dict[str, Any]]:
    """Return top_k external signals semantically matching `query`."""
    if not query or not query.strip():
        return []

    qv = embed(query)
    try:
        with conn_cursor(dict_cursor=True) as (_, cur):
            cur.execute(
                """
                SELECT id, source, source_kind, signal_type, url, title,
                       raw_text, extracted_facts, embedding,
                       relevance_score, published_at
                FROM external_signals
                WHERE relevance_score >= %s
                  AND (published_at IS NULL
                       OR published_at >= NOW() - (%s || ' days')::interval)
                ORDER BY published_at DESC NULLS LAST
                LIMIT 500
                """,
                (min_relevance, str(max_age_days)),
            )
            rows = cur.fetchall()
    except Exception as e:
        log.warning("search_external_news db error: %s", e)
        return []

    scored = []
    for r in rows:
        emb_raw = r.get("embedding")
        if not emb_raw:
            sim = 0.0
        else:
            try:
                vec = json.loads(emb_raw) if isinstance(emb_raw, str) else emb_raw
                sim = cosine(qv, vec)
            except Exception:
                sim = 0.0
        combined = 0.7 * sim + 0.3 * float(r.get("relevance_score") or 0.0)
        scored.append((combined, r))

    scored.sort(key=lambda x: x[0], reverse=True)
    out = []
    for combined, r in scored[:top_k]:
        out.append({
            "id": r["id"],
            "source": r["source"],
            "source_kind": r["source_kind"],
            "signal_type": r["signal_type"],
            "url": r["url"],
            "title": r["title"],
            "summary": (r.get("extracted_facts") or {}).get("summary") or (r.get("raw_text") or "")[:300],
            "relevance_score": float(r["relevance_score"] or 0.0),
            "similarity": float(combined),
            "published_at": r["published_at"].isoformat() if r["published_at"] else None,
        })
    return out


def top_recent_signals(top_k: int = 5, max_age_hours: int = 36) -> list[dict[str, Any]]:
    """Used by channel-bot-new daily digest."""
    try:
        with conn_cursor(dict_cursor=True) as (_, cur):
            cur.execute(
                """
                SELECT id, source, signal_type, url, title, extracted_facts,
                       relevance_score, published_at
                FROM external_signals
                WHERE published_at >= NOW() - (%s || ' hours')::interval
                ORDER BY relevance_score DESC, published_at DESC NULLS LAST
                LIMIT %s
                """,
                (str(max_age_hours), top_k),
            )
            rows = cur.fetchall()
    except Exception as e:
        log.warning("top_recent_signals error: %s", e)
        return []

    out = []
    for r in rows:
        facts = r.get("extracted_facts") or {}
        out.append({
            "id": r["id"],
            "source": r["source"],
            "url": r["url"],
            "title": r["title"],
            "summary": facts.get("summary") or "",
            "areas": facts.get("areas") or [],
            "relevance_score": float(r["relevance_score"] or 0.0),
            "published_at": r["published_at"].isoformat() if r["published_at"] else None,
        })
    return out


def render_daily_digest_md(top_k: int = 5) -> str:
    """Markdown digest for channel-bot-new."""
    signals = top_recent_signals(top_k=top_k, max_age_hours=36)
    if not signals:
        return ""

    lines = ["*Top 5 Dubai property news today*", ""]
    for i, s in enumerate(signals, 1):
        title = (s["title"] or "").strip()
        url = s["url"] or ""
        summary = (s["summary"] or "").strip()
        if url:
            lines.append(f"{i}. [{title}]({url})")
        else:
            lines.append(f"{i}. {title}")
        if summary:
            lines.append(f"   _{summary}_")
        lines.append("")
    lines.append("— Vadim Realty · RERA BRN 65011")
    return "\n".join(lines)
