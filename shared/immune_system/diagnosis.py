"""Root-cause diagnoser for pending incidents.

Reads N pending rows from immune_incidents, asks the free LLM chain
(Cerebras → Groq → Gemini fallback) to classify and explain, then updates
status='diagnosed' + diagnosis jsonb. No paid services. DSN only via env.
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Optional

from . import _db

log = logging.getLogger("immune_system.diagnosis")

_CLASSES = (
    "NPE",
    "contract_drift",
    "external_api",
    "data_corruption",
    "logic_bug",
    "race_condition",
)


def _llm_complete(prompt: str, max_tokens: int = 600) -> tuple[str, str]:
    """Free-only LLM chain. Returns (text, provider). Empty string on failure."""
    # 1. Preferred: shared.llm_router (handles full Cerebras→Groq→Gemini chain)
    try:
        from shared import llm_router  # type: ignore
        text, provider = llm_router.complete(prompt, max_tokens=max_tokens)
        if text:
            return text, provider or "llm_router"
    except Exception:
        pass

    # 2. Direct Cerebras fallback
    try:
        import requests
        ck = os.environ.get("CEREBRAS_API_KEY")
        if ck:
            r = requests.post(
                "https://api.cerebras.ai/v1/chat/completions",
                headers={"Authorization": f"Bearer {ck}"},
                json={
                    "model": "llama3.1-8b",
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": max_tokens,
                    "temperature": 0.2,
                },
                timeout=30,
            )
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"], "cerebras"
    except Exception as e:
        log.debug("cerebras failed: %s", e)

    # 3. Direct Groq fallback
    try:
        import requests
        gk = os.environ.get("GROQ_API_KEY")
        if gk:
            r = requests.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {gk}"},
                json={
                    "model": "llama-3.1-8b-instant",
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": max_tokens,
                    "temperature": 0.2,
                },
                timeout=30,
            )
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"], "groq"
    except Exception as e:
        log.debug("groq failed: %s", e)

    return "", "none"


_DIAG_PROMPT = """You are a senior backend engineer doing root-cause analysis on a
Python bot exception.

Classify, explain, and propose a fix. Reply in strict JSON only:
{{
  "class": "one of: {classes}",
  "root_cause": "short explanation (<= 200 chars)",
  "suggested_fix": "concrete change (<= 250 chars)",
  "confidence": 0.0..1.0
}}

Exception class : {exc_class}
File : {file_path}:{line_no}
Bot  : {bot_name}
Occurrences : {occ}
Stack (tail) :
{stack}
"""


def _parse_json_response(text: str) -> dict[str, Any]:
    """Extract the first JSON object from an LLM response. Permissive."""
    if not text:
        return {}
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return {}
    try:
        return json.loads(m.group(0))
    except Exception:
        # try to clean trailing commas
        s = re.sub(r",\s*([}\]])", r"\1", m.group(0))
        try:
            return json.loads(s)
        except Exception:
            return {}


def _match_bug_kb(conn, exc_class: str, file_path: str) -> Optional[dict]:
    """Best-effort similarity match against bug_kb. Falls back to LIKE on file
    path + exception class when pgvector or table absent."""
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT to_regclass('public.bug_kb') IS NOT NULL""",
            )
            has_table = bool(cur.fetchone()[0])
            if not has_table:
                return None
            cur.execute(
                """
                SELECT id, code, symptom, fix
                FROM bug_kb
                WHERE symptom ILIKE %s OR fix ILIKE %s
                ORDER BY id DESC
                LIMIT 1
                """,
                (f"%{exc_class}%", f"%{os.path.basename(file_path or '')}%"),
            )
            row = cur.fetchone()
            if not row:
                return None
            return {"bug_id": row[0], "code": row[1], "symptom": row[2], "fix": row[3]}
    except Exception:
        return None


def diagnose_pending(limit: int = 10) -> int:
    """Diagnose up to `limit` pending incidents. Returns count diagnosed."""
    conn = _db.connect()
    if conn is None:
        log.warning("diagnose_pending: no DB connection (DSN empty?)")
        return 0
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, bot_name, exception_class, stack_trace,
                       file_path, line_no, occurrence_count
                FROM immune_incidents
                WHERE status = 'pending'
                ORDER BY last_seen DESC
                LIMIT %s
                """,
                (limit,),
            )
            rows = cur.fetchall()

        diagnosed = 0
        for row in rows:
            iid, bot, exc_class, stack, file_path, line_no, occ = row
            tail = (stack or "")[-1500:]
            prompt = _DIAG_PROMPT.format(
                classes=", ".join(_CLASSES),
                exc_class=exc_class or "Exception",
                file_path=file_path or "?",
                line_no=line_no or 0,
                bot_name=bot or "?",
                occ=occ or 1,
                stack=tail,
            )
            text, provider = _llm_complete(prompt, max_tokens=600)
            parsed = _parse_json_response(text)
            if not parsed:
                log.info("incident %s: LLM diagnosis empty (provider=%s)",
                         iid, provider)
                continue
            kb = _match_bug_kb(conn, exc_class or "", file_path or "")
            diagnosis = {
                "class": parsed.get("class") or "unknown",
                "root_cause": parsed.get("root_cause") or "",
                "suggested_fix": parsed.get("suggested_fix") or "",
                "confidence": parsed.get("confidence"),
                "provider": provider,
                "bug_kb_match": kb,
            }
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE immune_incidents
                           SET diagnosis = %s::jsonb,
                               status    = 'diagnosed'
                         WHERE id = %s
                        """,
                        (json.dumps(diagnosis), iid),
                    )
                diagnosed += 1
            except Exception as e:
                log.warning("update diagnosis failed for %s: %s", iid, e)
        return diagnosed
    finally:
        try:
            conn.close()
        except Exception:
            pass


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=10)
    args = ap.parse_args()
    n = diagnose_pending(limit=args.limit)
    print(f"diagnosed={n}")
