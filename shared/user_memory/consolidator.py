"""
shared.user_memory.consolidator — nightly memory hygiene job.

Run via Windows Task Scheduler at 05:00 (see install_cron.ps1).

Actions:
1. Merge near-duplicate active facts per user (keep highest confidence).
2. Decay confidence of facts not referenced for > N days.
3. Prune very-low-confidence stale facts.
4. Recompute profile centroid embedding from top-K active facts.
"""
from __future__ import annotations
import json
import sys
import time
from typing import Any, Dict, List

from ._db import get_conn
from ._embed import embed_batch, to_pgvector_literal


def _merge_user(cur, user_id: int) -> Dict[str, int]:
    stats = {"merged": 0, "decayed": 0, "pruned": 0}
    cur.execute("""
        SELECT id, fact_text, fact_type, confidence, reference_count
        FROM user_memory_facts
        WHERE user_id = %s AND superseded_by IS NULL
        ORDER BY id
    """, (user_id,))
    rows = cur.fetchall()
    if not rows:
        return stats

    # Greedy near-duplicate merge within (fact_type)
    by_type: Dict[str, List[tuple]] = {}
    for r in rows:
        by_type.setdefault(r[2], []).append(r)

    for ftype, group in by_type.items():
        # Use trigram similarity in SQL pairwise — keep highest confidence
        kept_ids = []
        for r in group:
            fid, ftext, _, fconf, fref = r
            merged_into = None
            for kid, ktext, kconf, kref in kept_ids:
                cur.execute(
                    "SELECT similarity(%s, %s)", (ftext, ktext))
                sim = cur.fetchone()[0] or 0
                if sim >= 0.60:
                    merged_into = (kid, kconf, kref)
                    break
            if merged_into is not None:
                kid, kconf, kref = merged_into
                new_conf = min(1.0, max(kconf, fconf) + 0.05)
                new_ref = (kref or 0) + (fref or 0)
                cur.execute("""
                    UPDATE user_memory_facts
                    SET superseded_by = %s
                    WHERE id = %s
                """, (kid, fid))
                cur.execute("""
                    UPDATE user_memory_facts
                    SET confidence = %s, reference_count = %s,
                        last_referenced = NOW()
                    WHERE id = %s
                """, (new_conf, new_ref, kid))
                stats["merged"] += 1
            else:
                kept_ids.append((fid, ftext, fconf, fref))

    # Decay stale facts
    cur.execute("""
        UPDATE user_memory_facts
        SET confidence = GREATEST(0.05, confidence - 0.05)
        WHERE user_id = %s
          AND superseded_by IS NULL
          AND last_referenced < NOW() - INTERVAL '30 days'
    """, (user_id,))
    stats["decayed"] = cur.rowcount or 0

    # Prune ultra-low confidence
    cur.execute("""
        DELETE FROM user_memory_facts
        WHERE user_id = %s
          AND superseded_by IS NULL
          AND confidence < 0.1
          AND created_at < NOW() - INTERVAL '60 days'
    """, (user_id,))
    stats["pruned"] = cur.rowcount or 0

    return stats


def _refresh_profile_centroid(cur, user_id: int, bot_name: str) -> None:
    cur.execute("""
        SELECT fact_text FROM user_memory_facts
        WHERE user_id = %s AND superseded_by IS NULL
          AND (bot_name = %s OR bot_name IS NULL)
        ORDER BY confidence DESC, last_referenced DESC
        LIMIT 10
    """, (user_id, bot_name))
    texts = [r[0] for r in cur.fetchall() if r[0]]
    if not texts:
        return
    embs = embed_batch(texts)
    # Mean pool
    dim = len(embs[0])
    centroid = [sum(e[i] for e in embs) / len(embs) for i in range(dim)]
    norm = sum(x * x for x in centroid) ** 0.5
    if norm > 0:
        centroid = [x / norm for x in centroid]
    lit = to_pgvector_literal(centroid)
    cur.execute("""
        UPDATE user_profiles
        SET embedding = %s::vector, updated_at = NOW()
        WHERE user_id = %s AND bot_name = %s
    """, (lit, user_id, bot_name))


def consolidate_user_memory(active_window_days: int = 90) -> Dict[str, Any]:
    t0 = time.time()
    out = {"users_processed": 0, "duplicates_merged": 0,
           "facts_pruned": 0, "decayed": 0, "errors": 0}
    conn = get_conn()
    if not conn:
        out["errors"] = 1
        return out
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT DISTINCT user_id, bot_name FROM user_profiles
                WHERE last_active > NOW() - INTERVAL '%s days'
            """ % int(active_window_days))
            rows = cur.fetchall()
        for user_id, bot_name in rows:
            try:
                with conn.cursor() as cur:
                    stats = _merge_user(cur, user_id)
                    _refresh_profile_centroid(cur, user_id, bot_name)
                out["duplicates_merged"] += stats["merged"]
                out["facts_pruned"] += stats["pruned"]
                out["decayed"] += stats["decayed"]
                out["users_processed"] += 1
            except Exception as e:
                sys.stderr.write(f"[consolidator] user {user_id} err: {e}\n")
                out["errors"] += 1
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO user_memory_consolidation_log
                      (users_processed, duplicates_merged, facts_pruned,
                       duration_ms, details)
                    VALUES (%s, %s, %s, %s, %s::jsonb)
                """, (out["users_processed"], out["duplicates_merged"],
                      out["facts_pruned"], int((time.time() - t0) * 1000),
                      json.dumps(out)))
        except Exception:
            pass
        return out
    except Exception as e:
        out["errors"] += 1
        out["error"] = str(e)
        return out


if __name__ == "__main__":
    result = consolidate_user_memory()
    print(json.dumps(result, default=str, indent=2))
