"""
shared.user_memory.retriever — semantic + recency retrieval over user facts.
"""
from __future__ import annotations
import sys
from typing import Any, Dict, List, Optional

from ._db import get_conn
from ._embed import embed, to_pgvector_literal


def get_user_facts(user_id: int, bot_name: Optional[str] = None,
                   limit: int = 30,
                   min_confidence: float = 0.3) -> List[Dict[str, Any]]:
    """Return all active facts for the user (newest-referenced first)."""
    conn = get_conn()
    if not conn:
        return []
    try:
        with conn.cursor() as cur:
            if bot_name:
                cur.execute("""
                    SELECT id, fact_text, fact_type, confidence,
                           reference_count, last_referenced
                    FROM user_memory_facts
                    WHERE user_id = %s
                      AND (bot_name = %s OR bot_name IS NULL)
                      AND superseded_by IS NULL
                      AND confidence >= %s
                    ORDER BY confidence * 0.4 +
                             LEAST(reference_count, 10)/10.0 * 0.3 +
                             EXTRACT(EPOCH FROM (NOW() - last_referenced)) * -0.0
                             DESC,
                             last_referenced DESC
                    LIMIT %s
                """, (user_id, bot_name, min_confidence, limit))
            else:
                cur.execute("""
                    SELECT id, fact_text, fact_type, confidence,
                           reference_count, last_referenced
                    FROM user_memory_facts
                    WHERE user_id = %s
                      AND superseded_by IS NULL
                      AND confidence >= %s
                    ORDER BY confidence DESC, last_referenced DESC
                    LIMIT %s
                """, (user_id, min_confidence, limit))
            rows = cur.fetchall()
        return [
            {"id": r[0], "fact_text": r[1], "fact_type": r[2],
             "confidence": float(r[3] or 0), "reference_count": r[4],
             "last_referenced": r[5]}
            for r in rows
        ]
    except Exception as e:
        sys.stderr.write(f"[user_memory.retriever] get_user_facts err: {e}\n")
        return []


def semantic_search_facts(user_id: int, query: str,
                          bot_name: Optional[str] = None,
                          top_k: int = 5,
                          min_confidence: float = 0.3) -> List[Dict[str, Any]]:
    """Semantic similarity search over the user's facts. Falls back to
    last-referenced order if vector index is unusable.
    """
    conn = get_conn()
    if not conn:
        return []
    if not query or not query.strip():
        return get_user_facts(user_id, bot_name, limit=top_k,
                              min_confidence=min_confidence)
    try:
        q_emb = embed(query)
        q_lit = to_pgvector_literal(q_emb)
        with conn.cursor() as cur:
            if bot_name:
                cur.execute("""
                    SELECT id, fact_text, fact_type, confidence,
                           reference_count, last_referenced,
                           1 - (embedding <=> %s::vector) AS sim
                    FROM user_memory_facts
                    WHERE user_id = %s
                      AND (bot_name = %s OR bot_name IS NULL)
                      AND superseded_by IS NULL
                      AND confidence >= %s
                      AND embedding IS NOT NULL
                    ORDER BY embedding <=> %s::vector
                    LIMIT %s
                """, (q_lit, user_id, bot_name, min_confidence, q_lit, top_k))
            else:
                cur.execute("""
                    SELECT id, fact_text, fact_type, confidence,
                           reference_count, last_referenced,
                           1 - (embedding <=> %s::vector) AS sim
                    FROM user_memory_facts
                    WHERE user_id = %s
                      AND superseded_by IS NULL
                      AND confidence >= %s
                      AND embedding IS NOT NULL
                    ORDER BY embedding <=> %s::vector
                    LIMIT %s
                """, (q_lit, user_id, min_confidence, q_lit, top_k))
            rows = cur.fetchall()
        results = [
            {"id": r[0], "fact_text": r[1], "fact_type": r[2],
             "confidence": float(r[3] or 0), "reference_count": r[4],
             "last_referenced": r[5], "similarity": float(r[6] or 0)}
            for r in rows
        ]
        # mark as referenced
        if results:
            try:
                ids = tuple(r["id"] for r in results)
                with conn.cursor() as cur2:
                    cur2.execute(
                        "UPDATE user_memory_facts SET last_referenced = NOW() "
                        "WHERE id = ANY(%s)", (list(ids),))
            except Exception:
                pass
        return results
    except Exception as e:
        sys.stderr.write(f"[user_memory.retriever] semantic err: {e}\n")
        return get_user_facts(user_id, bot_name, limit=top_k,
                              min_confidence=min_confidence)


def get_user_profile(user_id: int, bot_name: str) -> Optional[Dict[str, Any]]:
    conn = get_conn()
    if not conn:
        return None
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT user_id, bot_name, preferences, life_events,
                       last_active, total_interactions, language
                FROM user_profiles
                WHERE user_id = %s AND bot_name = %s
            """, (user_id, bot_name))
            r = cur.fetchone()
        if not r:
            return None
        return {
            "user_id": r[0], "bot_name": r[1],
            "preferences": r[2] or {}, "life_events": r[3] or [],
            "last_active": r[4], "total_interactions": r[5], "language": r[6],
        }
    except Exception as e:
        sys.stderr.write(f"[user_memory.retriever] profile err: {e}\n")
        return None
