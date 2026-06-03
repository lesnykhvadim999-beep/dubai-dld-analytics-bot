"""shared.external_knowledge — Phase BM Layer 16.

Public API:
    search_external_news(query, top_k, min_relevance, max_age_days) -> list
    top_recent_signals(top_k, max_age_hours) -> list
    render_daily_digest_md(top_k) -> str
"""
from .integration import (
    search_external_news,
    top_recent_signals,
    render_daily_digest_md,
)

__all__ = [
    "search_external_news",
    "top_recent_signals",
    "render_daily_digest_md",
]
