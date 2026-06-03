"""auto_discover — daily cron to mine new knowledge from bot_interactions.

Looks for:
  - queries with outcome='empty' that repeated >5 times in last 7 days
  - clusters them via embeddings
  - asks LLM to propose alias mapping
  - inserts as status='pending' (admin approval)

Safe to run when bot_interactions doesn't exist yet (Level 1 still being built):
    will log "no source table" and exit cleanly.
"""
from __future__ import annotations

import json
import logging
import os
from collections import defaultdict
from typing import List

from ._db import conn_cursor
from .client import KG
from .embeddings import embed_text

log = logging.getLogger(__name__)


def _source_table_exists() -> bool:
    try:
        with conn_cursor() as (_, cur):
            cur.execute("SELECT to_regclass('public.bot_interactions')")
            return cur.fetchone()[0] is not None
    except Exception:
        return False


def _detect_columns() -> tuple[str, str] | None:
    """Probe bot_interactions for (query_col, ts_col). Returns None if unusable."""
    try:
        with conn_cursor() as (_, cur):
            cur.execute(
                """SELECT column_name FROM information_schema.columns
                   WHERE table_name='bot_interactions'"""
            )
            cols = {r[0] for r in cur.fetchall()}
    except Exception:
        return None
    query_col = next((c for c in ("query", "user_message", "message", "text") if c in cols), None)
    ts_col = next((c for c in ("created_at", "ts", "timestamp", "at") if c in cols), None)
    if not (query_col and ts_col):
        return None
    return query_col, ts_col


def _gather_empty_queries(min_count: int = 5, days: int = 7) -> List[dict]:
    """Group bot_interactions queries with outcome='empty' by normalised key."""
    if not _source_table_exists():
        log.info("auto_discover: bot_interactions not present, nothing to mine")
        return []
    cols = _detect_columns()
    if not cols:
        log.info("auto_discover: bot_interactions exists but no query/ts column found")
        return []
    qcol, tscol = cols
    try:
        with conn_cursor(dict_cursor=True) as (_, cur):
            cur.execute(
                f"""
                SELECT LOWER(TRIM({qcol})) AS q, COUNT(*) AS n,
                       array_agg(DISTINCT bot_name) AS bots
                FROM bot_interactions
                WHERE outcome = 'empty'
                  AND {tscol} >= NOW() - (%s || ' days')::interval
                  AND {qcol} IS NOT NULL
                  AND LENGTH({qcol}) BETWEEN 3 AND 80
                GROUP BY 1
                HAVING COUNT(*) >= %s
                ORDER BY n DESC
                LIMIT 100
                """,
                (str(days), min_count),
            )
            return [dict(r) for r in cur.fetchall()]
    except Exception as exc:  # noqa: BLE001
        log.warning("auto_discover: gather failed: %s", exc)
        return []


def _llm_propose_alias(query: str) -> dict | None:
    """Ask LLM to map an empty-result query to canonical building/area.

    Uses cerebras/groq via shared.llm_providers if available — falls back to
    a no-op if no LLM provider is configured (we still surface the empty
    query for manual review).
    """
    try:
        import sys
        sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
        from llm_providers import call_llm  # type: ignore
    except Exception:
        log.debug("auto_discover: no llm_providers — skipping LLM proposal")
        return None

    prompt = (
        "You are a Dubai real-estate knowledge assistant. A user query returned 0 results.\n"
        f"Query: \"{query}\"\n"
        "Decide: is this a building name, area name, or unknown?\n"
        "If building/area, propose the canonical Dubai name (as DLD would store it),\n"
        "plus 3-5 alias variants users might type.\n"
        "Reply STRICT JSON: {\"category\":\"building_alias|area_alias|unknown\","
        "\"canonical\":\"...\",\"aliases\":[\"...\"],\"confidence\":0.0-1.0}"
    )
    try:
        raw = call_llm(prompt, max_tokens=200)
        data = json.loads(raw[raw.find("{"):raw.rfind("}") + 1])
        if data.get("category") in ("building_alias", "area_alias") and data.get("confidence", 0) >= 0.6:
            return data
    except Exception as exc:  # noqa: BLE001
        log.debug("auto_discover: LLM call failed for %s: %s", query, exc)
    return None


def run_once(min_count: int = 5, days: int = 7, max_proposals: int = 20) -> dict:
    """Main entry: discover, propose, insert pending knowledge."""
    found = _gather_empty_queries(min_count=min_count, days=days)
    counters = {"queries_seen": len(found), "proposed": 0, "skipped": 0}
    if not found:
        return counters

    seen_keys: set[str] = set()
    for item in found[:max_proposals]:
        q = item["q"]
        if q in seen_keys:
            continue
        seen_keys.add(q)

        # Skip if already in KG (any status)
        try:
            with conn_cursor() as (_, cur):
                cur.execute(
                    """SELECT 1 FROM knowledge_graph
                       WHERE category IN ('building_alias','area_alias')
                         AND payload->>'key' = %s LIMIT 1""",
                    (q,),
                )
                if cur.fetchone():
                    counters["skipped"] += 1
                    continue
        except Exception:
            pass

        proposal = _llm_propose_alias(q)
        if not proposal:
            # still emit a low-confidence pending edge_case for visibility
            KG.add(
                "edge_case",
                {"key": f"empty-query-{q[:32]}", "query": q, "count": item["n"], "bots": item.get("bots") or []},
                description=f"Empty-result query repeated {item['n']}x: '{q}' — no auto-proposal, manual review.",
                source_bot="auto_discover",
            )
            counters["skipped"] += 1
            continue

        payload = {
            "key": q,
            "aliases": proposal.get("aliases", []),
            "canonical": proposal.get("canonical"),
            "discovered_from": {"query_count": item["n"], "bots": item.get("bots") or []},
            "confidence": proposal.get("confidence", 0),
        }
        desc = (
            f"Users searching '{q}' got 0 results ({item['n']} times in {days}d). "
            f"Suggested map → '{proposal.get('canonical')}'."
        )
        kid = KG.add(proposal["category"], payload, desc, source_bot="auto_discover")
        if kid:
            counters["proposed"] += 1

    return counters


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    print("auto_discover result:", run_once())
