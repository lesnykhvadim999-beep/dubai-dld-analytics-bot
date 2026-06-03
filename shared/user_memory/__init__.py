"""
shared.user_memory — Phase BM, Layer 9: long-term user memory.

Public API:
    from shared.user_memory import (
        extract_facts_for_user,        # batch extractor (LLM)
        get_user_facts,                # raw fetch
        enrich_context,                # ready-to-prompt context string
        upsert_user_profile,           # bookkeeping
        consolidate_user_memory,       # cron entrypoint
    )
"""
from .extractor import extract_facts_for_user, upsert_user_profile
from .retriever import get_user_facts, semantic_search_facts
from .integration import enrich_context
from .consolidator import consolidate_user_memory

__all__ = [
    "extract_facts_for_user",
    "upsert_user_profile",
    "get_user_facts",
    "semantic_search_facts",
    "enrich_context",
    "consolidate_user_memory",
]
