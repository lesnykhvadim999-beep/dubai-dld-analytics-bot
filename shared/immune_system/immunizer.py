"""Immunizer — turn diagnosed incidents into PR-ready patch + regression test.

Never auto-applies anything. Output lives in immunization_proposals and must
be reviewed by admin before merge.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional

from . import _db
from .diagnosis import _llm_complete  # reuse free-chain caller

log = logging.getLogger("immune_system.immunizer")

_IMMUNIZE_PROMPT = """You are a senior Python engineer writing a defensive patch
for a recurring exception in a Telegram bot ecosystem.

Produce:
  1. patch_diff   — a UNIFIED DIFF (---/+++/@@) adding try/except, input
                    validation, or a guard at the failure site. Keep it small.
  2. regression_test — a self-contained pytest function that REPRODUCES the
                    bug (asserts the OLD code path raises) so it locks the fix.
  3. rationale    — 1-2 sentences why this fixes the root cause.

Reply in strict JSON only:
{{
  "patch_diff": "...",
  "regression_test": "...",
  "rationale": "..."
}}

Exception class : {exc_class}
File : {file_path}:{line_no}
Root cause : {root_cause}
Suggested fix : {suggested_fix}
Stack (tail) :
{stack}
"""


def _parse_json_response(text: str) -> dict[str, Any]:
    if not text:
        return {}
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return {}
    try:
        return json.loads(m.group(0))
    except Exception:
        s = re.sub(r",\s*([}\]])", r"\1", m.group(0))
        try:
            return json.loads(s)
        except Exception:
            return {}


def immunize_diagnosed(limit: int = 5) -> int:
    """Pick up to `limit` diagnosed incidents without a proposal yet and
    generate one. Returns count of new proposals."""
    conn = _db.connect()
    if conn is None:
        log.warning("immunize_diagnosed: no DB connection")
        return 0
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT i.id, i.exception_class, i.stack_trace,
                       i.file_path, i.line_no, i.diagnosis
                FROM immune_incidents i
                LEFT JOIN immunization_proposals p ON p.incident_id = i.id
                WHERE i.status = 'diagnosed' AND p.id IS NULL
                ORDER BY i.last_seen DESC
                LIMIT %s
                """,
                (limit,),
            )
            rows = cur.fetchall()

        created = 0
        for row in rows:
            iid, exc_class, stack, file_path, line_no, diagnosis = row
            diag = diagnosis if isinstance(diagnosis, dict) else (
                json.loads(diagnosis) if diagnosis else {}
            )
            tail = (stack or "")[-1500:]
            prompt = _IMMUNIZE_PROMPT.format(
                exc_class=exc_class or "Exception",
                file_path=file_path or "?",
                line_no=line_no or 0,
                root_cause=diag.get("root_cause", "?"),
                suggested_fix=diag.get("suggested_fix", "?"),
                stack=tail,
            )
            text, provider = _llm_complete(prompt, max_tokens=900)
            parsed = _parse_json_response(text)
            patch = (parsed.get("patch_diff") or "").strip()
            test_code = (parsed.get("regression_test") or "").strip()
            rationale = (parsed.get("rationale") or "").strip()
            if not patch and not test_code:
                log.info("incident %s: immunizer LLM returned empty payload "
                         "(provider=%s)", iid, provider)
                continue
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO immunization_proposals
                            (incident_id, patch_diff, regression_test_code,
                             rationale, llm_provider, status)
                        VALUES (%s, %s, %s, %s, %s, 'pending')
                        """,
                        (iid, patch, test_code, rationale, provider),
                    )
                    cur.execute(
                        "UPDATE immune_incidents SET status='proposed' WHERE id=%s",
                        (iid,),
                    )
                created += 1
            except Exception as e:
                log.warning("proposal insert failed for %s: %s", iid, e)
        return created
    finally:
        try:
            conn.close()
        except Exception:
            pass


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=5)
    args = ap.parse_args()
    n = immunize_diagnosed(limit=args.limit)
    print(f"proposed={n}")
