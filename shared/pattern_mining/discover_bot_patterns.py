"""
discover_bot_patterns — daily cron entry (03:30 Asia/Dubai).

Reads `bot_interactions` (Level 1 behavior_tracking) for the last 7 days and
extracts CLUSTERS of bad outcomes. For each cluster, calls LLM (Cerebras chain)
to propose a structured fix and sends an approval card to admin Telegram.

Algorithm:
  1. ensure_schema (idempotent)
  2. For each issue_type:
       - empty_repeated  (outcome='empty', same normalized message >= MIN_REPEATS)
       - error_repeated  (outcome='error', repeated error_msg)
       - slow_queries    (latency_ms > P95 threshold, repeated)
       - bad_responses   (rating=-1 from bot_feedback)
       - abandoned       (empty + no follow-up within 60s)
  3. Group by normalized user_message → cluster
  4. For each cluster with size >= MIN_SAMPLES_PER_CLUSTER:
       a. Embedding clustering (when pgvector available — TODO Level 5 wiring)
       b. LLM analyze → suggested_fix
       c. INSERT bot_pattern_discoveries (status=pending) with fingerprint dedup
       d. Send Telegram approval card

Manual:
  py C:/Projects/shared/pattern_mining/discover_bot_patterns.py
  py C:/Projects/shared/pattern_mining/discover_bot_patterns.py --days 14 --min 3
  py C:/Projects/shared/pattern_mining/discover_bot_patterns.py --dry-run
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import sys
from collections import defaultdict
from datetime import datetime

if __name__ == "__main__" and __package__ in (None, ""):
    import os as _os
    import sys as _sys
    _here = _os.path.dirname(_os.path.abspath(__file__))
    _parent = _os.path.dirname(_here)
    if _parent not in _sys.path:
        _sys.path.insert(0, _parent)
    __package__ = "pattern_mining"  # noqa: F841

from ._db import conn_cursor, ensure_schema  # type: ignore
from .llm_cluster_analyzer import propose_fix  # type: ignore
from .bot_admin_notifier import send_discovery, send_summary  # type: ignore

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s :: %(message)s",
)
log = logging.getLogger("bpm.discover")


MIN_SAMPLES_PER_CLUSTER = 5
MIN_CONFIDENCE = 0.65
SLOW_LATENCY_MS = 2000  # P95 threshold for slow queries


def _normalize(text: str) -> str:
    if not text:
        return ""
    t = text.lower().strip()
    t = re.sub(r"\s+", " ", t)
    t = re.sub(r"[^\w\sЀ-ӿ؀-ۿ]", "", t)  # keep RU/AR
    return t


def _fingerprint(issue_type: str, cluster_key: str) -> str:
    return hashlib.sha1(f"{issue_type}|{cluster_key}".encode("utf-8")).hexdigest()


def _fetch_empty_clusters(days: int) -> dict[str, dict]:
    """Group empty-outcome interactions by normalized user_message."""
    out: dict[str, dict] = defaultdict(lambda: {"samples": [], "users": set(), "bots": set()})
    with conn_cursor() as (_, cur):
        cur.execute(f"""
            SELECT user_message, user_id, bot_name
            FROM bot_interactions
            WHERE outcome = 'empty'
              AND ts >= NOW() - INTERVAL '{int(days)} days'
              AND user_message IS NOT NULL
              AND LENGTH(user_message) > 1
            ORDER BY ts DESC
            LIMIT 5000
        """)
        for msg, uid, bot in cur.fetchall():
            key = _normalize(msg)[:80]
            if not key:
                continue
            d = out[key]
            d["samples"].append(msg)
            if uid:
                d["users"].add(uid)
            if bot:
                d["bots"].add(bot)
    return out


def _fetch_error_clusters(days: int) -> dict[str, dict]:
    out: dict[str, dict] = defaultdict(lambda: {"samples": [], "users": set(), "bots": set()})
    with conn_cursor() as (_, cur):
        cur.execute(f"""
            SELECT user_message, user_id, bot_name, error_msg
            FROM bot_interactions
            WHERE outcome = 'error'
              AND ts >= NOW() - INTERVAL '{int(days)} days'
              AND error_msg IS NOT NULL
            ORDER BY ts DESC
            LIMIT 3000
        """)
        for msg, uid, bot, err in cur.fetchall():
            key = _normalize(err or "")[:80]
            if not key:
                continue
            d = out[key]
            d["samples"].append(msg or err)
            if uid:
                d["users"].add(uid)
            if bot:
                d["bots"].add(bot)
    return out


def _fetch_slow_clusters(days: int) -> dict[str, dict]:
    out: dict[str, dict] = defaultdict(lambda: {"samples": [], "users": set(), "bots": set()})
    with conn_cursor() as (_, cur):
        cur.execute(f"""
            SELECT user_message, user_id, bot_name, query_type
            FROM bot_interactions
            WHERE latency_ms > {SLOW_LATENCY_MS}
              AND ts >= NOW() - INTERVAL '{int(days)} days'
              AND outcome = 'success'
            ORDER BY ts DESC
            LIMIT 3000
        """)
        for msg, uid, bot, qt in cur.fetchall():
            key = (qt or _normalize(msg or ""))[:80]
            if not key:
                continue
            d = out[key]
            d["samples"].append(msg or qt or "")
            if uid:
                d["users"].add(uid)
            if bot:
                d["bots"].add(bot)
    return out


def _fetch_negative_feedback_clusters(days: int) -> dict[str, dict]:
    out: dict[str, dict] = defaultdict(lambda: {"samples": [], "users": set(), "bots": set()})
    with conn_cursor() as (_, cur):
        cur.execute(f"""
            SELECT bi.user_message, bf.user_id, bf.bot_name
            FROM bot_feedback bf
            JOIN bot_interactions bi ON bi.id = bf.interaction_id
            WHERE bf.rating = -1
              AND bf.ts >= NOW() - INTERVAL '{int(days)} days'
            LIMIT 1000
        """)
        for msg, uid, bot in cur.fetchall():
            key = _normalize(msg or "")[:80]
            if not key:
                continue
            d = out[key]
            d["samples"].append(msg or "")
            if uid:
                d["users"].add(uid)
            if bot:
                d["bots"].add(bot)
    return out


def _severity(affected: int, issue_type: str) -> str:
    if issue_type == "bad_responses":
        if affected >= 10:
            return "high"
        return "medium"
    if affected >= 50:
        return "high"
    if affected >= 15:
        return "medium"
    return "low"


def _insert_discovery(
    bot_name: str,
    issue_type: str,
    affected_users: int,
    severity: str,
    evidence: list[str],
    fix: dict,
    confidence: float,
    fingerprint: str,
) -> int | None:
    """Insert discovery row, dedup by fingerprint (pending/approved). Returns id or None."""
    with conn_cursor() as (_, cur):
        cur.execute("""
            SELECT id FROM bot_pattern_discoveries
            WHERE fingerprint = %s AND status IN ('pending', 'approved')
            LIMIT 1
        """, (fingerprint,))
        existing = cur.fetchone()
        if existing:
            return None
        cur.execute("""
            INSERT INTO bot_pattern_discoveries
              (bot_name, issue_type, affected_users, severity, evidence_json,
               suggested_fix_json, confidence, status, fingerprint)
            VALUES (%s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, 'pending', %s)
            RETURNING id
        """, (
            bot_name, issue_type, affected_users, severity,
            json.dumps(evidence[:15], ensure_ascii=False),
            json.dumps(fix, ensure_ascii=False, default=str),
            confidence, fingerprint,
        ))
        return cur.fetchone()[0]


def _process_issue_type(issue_type: str, clusters: dict[str, dict], dry_run: bool) -> dict:
    """For each cluster: LLM analyze → insert discovery → notify."""
    report = {"clusters": 0, "proposed": 0, "saved": 0, "notified": 0, "deduped": 0, "low_conf": 0}
    for cluster_key, data in sorted(clusters.items(), key=lambda kv: -len(kv[1]["users"])):
        affected = len(data["users"])
        if affected < MIN_SAMPLES_PER_CLUSTER:
            continue
        report["clusters"] += 1
        bot_name = sorted(data["bots"])[0] if data["bots"] else "unknown"
        samples = data["samples"][:15]

        log.info("[%s] cluster '%s' bot=%s affected=%d", issue_type, cluster_key[:40], bot_name, affected)

        proposal = propose_fix(issue_type, bot_name, samples)
        if not proposal:
            log.info("  LLM returned None — skip")
            continue
        report["proposed"] += 1

        confidence = float(proposal.get("confidence") or 0)
        fix = proposal.get("suggested_fix") or {}
        fix["_provider"] = proposal.get("_provider")
        fix["_issue_summary"] = proposal.get("issue_summary")

        if confidence < MIN_CONFIDENCE:
            report["low_conf"] += 1
            log.info("  confidence %.2f < %.2f — skip", confidence, MIN_CONFIDENCE)
            continue

        severity = _severity(affected, issue_type)
        fp = _fingerprint(issue_type, cluster_key)  # safety: ignore — fp = fingerprint hash, not a secret

        if dry_run:
            log.info("  [dry-run] would insert: kind=%s key=%s conf=%.2f",
                     fix.get("kind"), fix.get("key"), confidence)
            report["saved"] += 1
            continue

        try:
            discovery_id = _insert_discovery(
                bot_name=bot_name,
                issue_type=issue_type,
                affected_users=affected,
                severity=severity,
                evidence=samples,
                fix=fix,
                confidence=confidence,
                fingerprint=fp,
            )
        except Exception as exc:
            log.warning("  insert failed: %s", exc)
            continue
        if not discovery_id:
            report["deduped"] += 1
            log.info("  duplicate fingerprint — skipped")
            continue
        report["saved"] += 1

        # Notify
        row = {
            "bot_name": bot_name,
            "issue_type": issue_type,
            "affected_users": affected,
            "severity": severity,
            "confidence": confidence,
            "suggested_fix_json": fix,
            "evidence_json": samples,
        }
        msg_id = send_discovery(discovery_id, row)
        if msg_id:
            with conn_cursor() as (_, cur):
                cur.execute(
                    "UPDATE bot_pattern_discoveries SET admin_message_id=%s WHERE id=%s",
                    (msg_id, discovery_id),
                )
            report["notified"] += 1
    return report


def run(days: int = 7, dry_run: bool = False) -> dict:
    ensure_schema()

    log.info("=== Pattern Mining run (days=%d, dry_run=%s) ===", days, dry_run)

    fetchers = [
        ("empty_repeated", _fetch_empty_clusters),
        ("error_repeated", _fetch_error_clusters),
        ("slow_queries", _fetch_slow_clusters),
        ("bad_responses", _fetch_negative_feedback_clusters),
    ]

    grand = {"status": "ok", "by_type": {}}
    for issue_type, fn in fetchers:
        try:
            clusters = fn(days)
        except Exception as exc:
            log.warning("fetch %s failed: %s", issue_type, exc)
            grand["by_type"][issue_type] = {"error": str(exc)}
            continue
        log.info("[%s] %d candidate clusters", issue_type, len(clusters))
        if not clusters:
            grand["by_type"][issue_type] = {"clusters": 0}
            continue
        grand["by_type"][issue_type] = _process_issue_type(issue_type, clusters, dry_run)

    # summary
    total_saved = sum(v.get("saved", 0) for v in grand["by_type"].values() if isinstance(v, dict))
    total_notified = sum(v.get("notified", 0) for v in grand["by_type"].values() if isinstance(v, dict))
    grand["total_saved"] = total_saved
    grand["total_notified"] = total_notified

    log.info("Done: %s", grand)
    return grand


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    report = run(days=args.days, dry_run=args.dry_run)
    print(json.dumps(report, indent=2, ensure_ascii=False, default=str))

    if not args.dry_run and report.get("total_saved"):
        by = report["by_type"]
        lines = [f"  {k}: saved={v.get('saved', 0)} notified={v.get('notified', 0)}"
                 for k, v in by.items() if isinstance(v, dict)]
        send_summary(
            f"🧠 <b>Bot Pattern Mining (daily)</b>\n"
            f"saved=<b>{report['total_saved']}</b> · notified={report['total_notified']}\n"
            + "\n".join(lines)
            + f"\n<i>{datetime.utcnow().isoformat(timespec='seconds')}Z</i>"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
