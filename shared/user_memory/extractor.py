"""
shared.user_memory.extractor — pulls atomic facts from a user's recent
conversation via the shared LLM chain (Cerebras → Groq → ... → Anthropic).

Usage:
    extract_facts_for_user(user_id=123, bot_name="resale-bot", lookback_n=20)

After each significant dialog turn, a bot can call this. It is safe to call
from a background thread / asyncio.create_task — DB ops fail-open.
"""
from __future__ import annotations
import json
import os
import re
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

from ._db import get_conn
from ._embed import embed, embed_batch, EMBED_DIM, to_pgvector_literal

# -------- LLM hookup (reuse shared/auto_docs llm_client wrapper) -------
_llm_call = None


def _load_llm():
    global _llm_call
    if _llm_call is not None:
        return
    try:
        # Prefer the canonical shared wrapper.
        from shared.auto_docs.llm_client import llm_call as _ll  # type: ignore
        _llm_call = _ll
        return
    except Exception:
        pass
    # Fallback: direct import via importlib.
    import importlib.util
    candidates = [
        r"C:\Projects\shared\auto_docs\llm_client.py",
    ]
    for path in candidates:
        if not os.path.isfile(path):
            continue
        try:
            spec = importlib.util.spec_from_file_location("um_llm_client", path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)  # type: ignore
            if hasattr(mod, "llm_call"):
                _llm_call = mod.llm_call
                return
        except Exception as e:
            sys.stderr.write(f"[user_memory.extractor] LLM load failed {path}: {e}\n")


FACT_TYPES = {"preference", "budget", "family", "timeline",
              "objection", "area", "property_type", "contact", "other"}


_PROMPT_TEMPLATE = """You are extracting durable, user-specific facts from a chat
log between a real-estate bot and a user. Output ONLY a JSON array (no prose,
no markdown fences). Each item:
  {"fact": "<short factual statement, English or Russian>",
   "type": "preference|budget|family|timeline|objection|area|property_type|contact|other",
   "confidence": 0.0-1.0}

Rules:
- Extract only stable facts the user revealed about themselves
  (budget, family size, preferred areas, timeline, must-haves, objections,
  bedroom count, etc.). Skip generic chitchat.
- Do NOT invent facts. If unsure, lower confidence.
- Keep each fact under 140 chars.
- Output [] if nothing usable.
- Never include phone numbers or emails verbatim.

Conversation (most recent last):
{conv}

JSON array:"""


def _build_prompt(messages: List[Dict[str, Any]]) -> str:
    lines = []
    for m in messages:
        role = "USER" if (m.get("role") == "user" or m.get("user_message")) else "BOT"
        text = m.get("text") or m.get("user_message") or m.get("bot_response_preview") or ""
        text = text.replace("\n", " ").strip()
        if not text:
            continue
        if len(text) > 320:
            text = text[:320] + "…"
        lines.append(f"{role}: {text}")
    conv = "\n".join(lines[-40:])
    return _PROMPT_TEMPLATE.replace("{conv}", conv)


def _strip_code_fences(s: str) -> str:
    s = s.strip()
    s = re.sub(r"^```(?:json)?", "", s).strip()
    s = re.sub(r"```$", "", s).strip()
    return s


def _parse_facts(raw: str) -> List[Dict[str, Any]]:
    if not raw:
        return []
    s = _strip_code_fences(raw)
    # Find the first JSON array in the output
    m = re.search(r"\[.*\]", s, flags=re.DOTALL)
    candidate = m.group(0) if m else s
    try:
        arr = json.loads(candidate)
    except Exception:
        return []
    out = []
    if not isinstance(arr, list):
        return []
    for item in arr:
        if not isinstance(item, dict):
            continue
        fact = (item.get("fact") or "").strip()
        ftype = (item.get("type") or "other").strip().lower()
        if ftype not in FACT_TYPES:
            ftype = "other"
        try:
            conf = float(item.get("confidence", 0.5))
        except Exception:
            conf = 0.5
        conf = max(0.0, min(1.0, conf))
        if not fact:
            continue
        if len(fact) > 280:
            fact = fact[:280]
        out.append({"fact": fact, "type": ftype, "confidence": conf})
    return out


def _fetch_recent_messages(user_id: int, bot_name: Optional[str],
                           lookback_n: int) -> List[Dict[str, Any]]:
    """Pull last N interactions for this user from bot_interactions (BL)."""
    conn = get_conn()
    if not conn:
        return []
    try:
        with conn.cursor() as cur:
            if bot_name:
                cur.execute("""
                    SELECT id, user_message, bot_response_preview, ts
                    FROM bot_interactions
                    WHERE user_id = %s AND bot_name = %s
                    ORDER BY ts DESC
                    LIMIT %s
                """, (user_id, bot_name, lookback_n))
            else:
                cur.execute("""
                    SELECT id, user_message, bot_response_preview, ts
                    FROM bot_interactions
                    WHERE user_id = %s
                    ORDER BY ts DESC
                    LIMIT %s
                """, (user_id, lookback_n))
            rows = cur.fetchall()
        msgs = []
        for r in reversed(rows):  # oldest → newest
            msgs.append({"id": r[0], "role": "user",
                         "user_message": r[1] or "", "ts": r[3]})
            if r[2]:
                msgs.append({"id": r[0], "role": "bot",
                             "bot_response_preview": r[2], "ts": r[3]})
        return msgs
    except Exception as e:
        sys.stderr.write(f"[user_memory.extractor] fetch err: {e}\n")
        return []


def _fact_exists_similar(cur, user_id: int, fact_text: str, fact_type: str) -> Optional[int]:
    """Check for a near-duplicate active fact (trigram similarity)."""
    try:
        cur.execute("""
            SELECT id FROM user_memory_facts
            WHERE user_id = %s
              AND fact_type = %s
              AND superseded_by IS NULL
              AND similarity(fact_text, %s) > 0.55
            ORDER BY similarity(fact_text, %s) DESC
            LIMIT 1
        """, (user_id, fact_type, fact_text, fact_text))
        row = cur.fetchone()
        return row[0] if row else None
    except Exception:
        return None


def upsert_user_profile(user_id: int, bot_name: str,
                        language: Optional[str] = None,
                        recent_text: Optional[str] = None) -> None:
    """Increment counters + refresh centroid embedding for a profile row."""
    conn = get_conn()
    if not conn:
        return
    try:
        emb_literal = None
        if recent_text:
            v = embed(recent_text)
            emb_literal = to_pgvector_literal(v)
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO user_profiles
                  (user_id, bot_name, embedding, language, last_active, total_interactions)
                VALUES (%s, %s, %s::vector, %s, NOW(), 1)
                ON CONFLICT (user_id, bot_name) DO UPDATE
                SET last_active = NOW(),
                    total_interactions = user_profiles.total_interactions + 1,
                    language = COALESCE(EXCLUDED.language, user_profiles.language),
                    embedding = CASE
                      WHEN EXCLUDED.embedding IS NOT NULL THEN EXCLUDED.embedding
                      ELSE user_profiles.embedding END,
                    updated_at = NOW()
            """, (user_id, bot_name, emb_literal, language))
    except Exception as e:
        sys.stderr.write(f"[user_memory.extractor] upsert_profile err: {e}\n")


def extract_facts_for_user(user_id: int, bot_name: Optional[str] = None,
                           lookback_n: int = 20,
                           min_messages: int = 3) -> Dict[str, Any]:
    """Run LLM extraction on the user's recent dialog and persist facts.

    Returns a dict summary. Always returns; never raises.
    """
    summary = {"user_id": user_id, "bot_name": bot_name,
               "messages_scanned": 0, "facts_extracted": 0,
               "facts_merged": 0, "error": None}
    t0 = time.time()
    try:
        msgs = _fetch_recent_messages(user_id, bot_name, lookback_n)
        summary["messages_scanned"] = len(msgs)
        if len(msgs) < min_messages:
            summary["error"] = "not_enough_messages"
            _log_run(summary, t0)
            return summary

        _load_llm()
        if _llm_call is None:
            summary["error"] = "llm_unavailable"
            _log_run(summary, t0)
            return summary

        prompt = _build_prompt(msgs)
        raw = _llm_call(prompt, max_tokens=600, timeout=20) or ""
        facts = _parse_facts(raw)
        if not facts:
            summary["error"] = "no_facts_parsed"
            _log_run(summary, t0)
            return summary

        texts = [f["fact"] for f in facts]
        embs = embed_batch(texts)

        conn = get_conn()
        if not conn:
            summary["error"] = "no_db"
            _log_run(summary, t0)
            return summary

        merged = 0
        inserted = 0
        last_msg_id = msgs[-1].get("id") if msgs else None
        with conn.cursor() as cur:
            for f, vec in zip(facts, embs):
                existing = _fact_exists_similar(cur, user_id, f["fact"], f["type"])
                emb_lit = to_pgvector_literal(vec)
                if existing:
                    # Bump confidence and reference_count
                    cur.execute("""
                        UPDATE user_memory_facts
                        SET reference_count = reference_count + 1,
                            confidence = LEAST(1.0,
                              GREATEST(confidence, %s) + 0.05),
                            last_referenced = NOW(),
                            embedding = COALESCE(embedding, %s::vector)
                        WHERE id = %s
                    """, (f["confidence"], emb_lit, existing))
                    merged += 1
                else:
                    cur.execute("""
                        INSERT INTO user_memory_facts
                          (user_id, bot_name, fact_text, fact_type, confidence,
                           source_message_id, embedding, metadata)
                        VALUES (%s, %s, %s, %s, %s, %s, %s::vector, %s::jsonb)
                    """, (user_id, bot_name, f["fact"], f["type"],
                          f["confidence"], last_msg_id, emb_lit,
                          json.dumps({"src": "extractor"})))
                    inserted += 1

        summary["facts_extracted"] = inserted
        summary["facts_merged"] = merged
        _log_run(summary, t0)
        # Refresh profile centroid using last user message
        last_user_text = next((m.get("user_message") for m in reversed(msgs)
                               if m.get("user_message")), None)
        upsert_user_profile(user_id, bot_name or "unknown",
                            recent_text=last_user_text)
        return summary
    except Exception as e:
        summary["error"] = f"exception:{e}"
        _log_run(summary, t0)
        return summary


def _log_run(summary: Dict[str, Any], t0: float) -> None:
    conn = get_conn()
    if not conn:
        return
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO user_memory_runs
                  (bot_name, user_id, messages_scanned, facts_extracted,
                   facts_merged, llm_ms, error)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
            """, (summary.get("bot_name"), summary.get("user_id"),
                  summary.get("messages_scanned", 0),
                  summary.get("facts_extracted", 0),
                  summary.get("facts_merged", 0),
                  int((time.time() - t0) * 1000),
                  summary.get("error")))
    except Exception:
        pass
