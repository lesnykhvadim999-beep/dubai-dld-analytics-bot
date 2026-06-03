"""
discover_patterns — weekly cron entry.

Algorithm:
1. Ensure schema (idempotent CREATE TABLE).
2. SELECT failed_log где cluster_id IS NULL AND resolved=false за последние 7 дней.
3. Если меньше MIN_SAMPLES → silent exit.
4. Embed texts (Gemini → TF-IDF fallback).
5. k-means k=10.
6. Для каждого cluster size >= 5:
     a) Cerebras → suggested rule
     b) validate_proposed_regex (match_rate + fp_rate + test_cases)
     c) Если valid=True → INSERT pending + Telegram notify
        Если valid=False → INSERT auto_rejected, причины в suggested_rule_json
     d) Если cluster уже 3 раза подряд auto_rejected — пометить как 'hard'
     e) UPDATE failed_log.cluster_id

7. Регрессия: re-process recent confidence 0.5-0.7 (calibration) — TODO Phase 2.

Manual run:
    py C:/Projects/shared/parser_self_improve/discover_patterns.py
    py C:/Projects/shared/parser_self_improve/discover_patterns.py --backfill 1000
    py C:/Projects/shared/parser_self_improve/discover_patterns.py --days 30 --min 5
    py C:/Projects/shared/parser_self_improve/discover_patterns.py --dry-run
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
from collections import defaultdict
from datetime import datetime

# Поддержка запуска как `python discover_patterns.py` (без пакета)
if __name__ == "__main__" and __package__ in (None, ""):
    import os as _os
    import sys as _sys

    _here = _os.path.dirname(_os.path.abspath(__file__))
    _parent = _os.path.dirname(_here)
    if _parent not in _sys.path:
        _sys.path.insert(0, _parent)
    __package__ = "parser_self_improve"  # noqa: F841

from ._db import conn_cursor, ensure_schema  # type: ignore
from .cluster_engine import cluster_texts  # type: ignore
from .llm_pattern_proposer import propose_pattern  # type: ignore
from .admin_notifier import send_pattern_proposal, send_summary  # type: ignore
from .regex_validator import validate_proposed_regex  # type: ignore

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s :: %(message)s",
)
log = logging.getLogger("psi.discover")


MIN_SAMPLES_PER_RUN = 20
MIN_SAMPLES_PER_CLUSTER = 5
DEFAULT_K = 10
HARD_CLUSTER_REJECT_THRESHOLD = 3  # consecutive auto-rejects → 'hard'


def _fetch_unclustered(days: int) -> list[tuple[int, str]]:
    with conn_cursor() as (_, cur):
        cur.execute(
            """
            SELECT id, original_text
            FROM parser_failed_log
            WHERE cluster_id IS NULL
              AND resolved = false
              AND created_at >= NOW() - INTERVAL '%s days'
            ORDER BY id DESC
            LIMIT 1000
            """ % int(days),
        )
        return [(r[0], r[1]) for r in cur.fetchall() if r[1]]


def _cluster_signature(sample_texts: list[str]) -> str:
    """Стабильный отпечаток кластера — sha1 от sorted shorthashes первых 5 текстов.

    Используется чтобы трекать «один и тот же» проблемный cluster между
    запусками cron'а (для hard-cluster detection).
    """
    parts = sorted(hashlib.sha1((t or "")[:200].encode("utf-8", "ignore")).hexdigest()[:12] for t in sample_texts[:5])
    return hashlib.sha1("|".join(parts).encode()).hexdigest()[:16]


def _consecutive_auto_rejects(signature: str) -> int:
    with conn_cursor() as (_, cur):
        cur.execute(
            """
            SELECT COUNT(*) FROM parser_pattern_clusters
            WHERE status = 'auto_rejected'
              AND suggested_rule_json->>'_signature' = %s
            """,
            (signature,),
        )
        return int(cur.fetchone()[0] or 0)


def _create_cluster_row(size: int, rule: dict, status: str) -> int:
    rejected_clause = ", rejected_at=NOW()" if status in ("auto_rejected", "hard") else ""
    with conn_cursor() as (_, cur):
        cur.execute(
            f"""
            INSERT INTO parser_pattern_clusters (size, suggested_rule_json, status, discovered_at{', rejected_at' if rejected_clause else ''})
            VALUES (%s, %s::jsonb, %s, NOW(){', NOW()' if rejected_clause else ''})
            RETURNING id
            """,
            (size, json.dumps(rule, ensure_ascii=False, default=str), status),
        )
        return cur.fetchone()[0]


def _assign_cluster_ids(row_ids: list[int], cluster_db_id: int) -> None:
    if not row_ids:
        return
    with conn_cursor() as (_, cur):
        cur.execute(
            "UPDATE parser_failed_log SET cluster_id=%s WHERE id = ANY(%s)",
            (cluster_db_id, row_ids),
        )


def _update_cluster_admin_msg(cluster_db_id: int, message_id: int) -> None:
    with conn_cursor() as (_, cur):
        cur.execute(
            "UPDATE parser_pattern_clusters SET admin_message_id=%s WHERE id=%s",
            (message_id, cluster_db_id),
        )


def run(days: int = 7, min_samples: int = MIN_SAMPLES_PER_RUN, k: int = DEFAULT_K,
        dry_run: bool = False) -> dict:
    ensure_schema()
    rows = _fetch_unclustered(days)
    log.info("Fetched %s unclustered failed records (last %sd)", len(rows), days)
    if len(rows) < min_samples:
        log.info("Not enough samples (%d < %d) — silent exit", len(rows), min_samples)
        return {"status": "skipped", "samples": len(rows)}

    ids, texts = zip(*rows)
    assignments = cluster_texts(list(texts), k=k)

    groups: dict[int, list[int]] = defaultdict(list)
    for idx, cid in enumerate(assignments):
        groups[cid].append(idx)

    report = {
        "status": "ok",
        "samples": len(rows),
        "clusters_total": len(groups),
        "clusters_processed": 0,
        "patterns_proposed": 0,
        "patterns_validated": 0,
        "patterns_auto_rejected": 0,
        "hard_clusters": 0,
        "admin_notifications": 0,
        "details": [],
    }

    for cid, indices in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        size = len(indices)
        if size < MIN_SAMPLES_PER_CLUSTER:
            continue
        report["clusters_processed"] += 1
        cluster_texts_sample = [texts[i] for i in indices[:10]]
        signature = _cluster_signature(cluster_texts_sample)
        log.info("Cluster %s (sig=%s): size=%d — calling LLM", cid, signature, size)

        rule = propose_pattern(cluster_texts_sample) if not dry_run else propose_pattern(cluster_texts_sample)
        if not rule:
            log.info("Cluster %s: no rule proposed (LLM unavailable / parse fail)", cid)
            continue
        report["patterns_proposed"] += 1

        # ── Validation ──
        field = rule.get("field", "unknown")
        validation = validate_proposed_regex(rule, cluster_texts_sample, field)
        rule["_signature"] = signature
        rule["_validation"] = validation
        log.info(
            "Cluster %s validation: valid=%s match_rate=%.0f%% fp_rate=%.1f%% reasons=%s",
            cid, validation["valid"],
            (validation.get("match_rate", 0) or 0) * 100,
            (validation.get("fp_rate", 0) or 0) * 100,
            validation.get("reasons"),
        )

        if dry_run:
            report["details"].append({
                "cluster": cid,
                "size": size,
                "field": field,
                "rule": rule,
                "validation": validation,
                "dry_run": True,
            })
            if validation["valid"]:
                report["patterns_validated"] += 1
            else:
                report["patterns_auto_rejected"] += 1
            continue

        if not validation["valid"]:
            # auto-reject
            rule["reject_reason"] = "validator: " + "; ".join(validation["reasons"][:3])
            report["patterns_auto_rejected"] += 1

            prior_rejects = _consecutive_auto_rejects(signature)
            new_status = "auto_rejected"
            if prior_rejects + 1 >= HARD_CLUSTER_REJECT_THRESHOLD:
                new_status = "hard"
                report["hard_clusters"] += 1
                log.warning(
                    "Cluster sig=%s now HARD (%d consecutive auto-rejects)",
                    signature, prior_rejects + 1,
                )

            db_cluster_id = _create_cluster_row(size, rule, status=new_status)
            row_ids = [ids[i] for i in indices]
            _assign_cluster_ids(row_ids, db_cluster_id)
            report["details"].append({
                "cluster_db_id": db_cluster_id,
                "size": size,
                "field": field,
                "status": new_status,
                "validation": validation,
            })
            continue

        # ── Valid → pending + notify ──
        report["patterns_validated"] += 1
        db_cluster_id = _create_cluster_row(size, rule, status="pending")
        row_ids = [ids[i] for i in indices]
        _assign_cluster_ids(row_ids, db_cluster_id)

        msg_id = send_pattern_proposal(
            db_cluster_id, size, rule, cluster_texts_sample, validation=validation,
        )
        if msg_id:
            _update_cluster_admin_msg(db_cluster_id, msg_id)
            report["admin_notifications"] += 1
        report["details"].append({
            "cluster_db_id": db_cluster_id,
            "size": size,
            "field": field,
            "status": "pending",
            "rule": rule,
            "validation": validation,
            "admin_message_id": msg_id,
        })

    log.info("Done: %s", {k: v for k, v in report.items() if k != "details"})
    return report


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--min", type=int, default=MIN_SAMPLES_PER_RUN)
    ap.add_argument("--k", type=int, default=DEFAULT_K)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument(
        "--backfill",
        type=int,
        default=0,
        help="Перед запуском прогнать backfill из listings (N max records)",
    )
    args = ap.parse_args(argv)

    ensure_schema()
    if args.backfill:
        from .failed_collector import backfill_from_listings
        n = backfill_from_listings(limit=args.backfill)
        log.info("Backfilled %d records into parser_failed_log", n)

    report = run(days=args.days, min_samples=args.min, k=args.k, dry_run=args.dry_run)
    print(json.dumps({k: v for k, v in report.items() if k != "details"}, indent=2))

    if args.dry_run:
        # печатаем коротко details — что бы предложили
        for d in report.get("details", []):
            v = d.get("validation", {})
            log.info(
                "DRY cluster=%s field=%s valid=%s mr=%.0f%% fpr=%.1f%% pattern=%s",
                d.get("cluster"),
                d.get("field"),
                v.get("valid"),
                (v.get("match_rate") or 0) * 100,
                (v.get("fp_rate") or 0) * 100,
                (d.get("rule") or {}).get("pattern", "")[:120],
            )

    # Сводное admin сообщение (если что-то нашли)
    if not args.dry_run and (report.get("patterns_validated") or report.get("hard_clusters")):
        send_summary(
            f"🧠 <b>Parser self-improve weekly</b>\n"
            f"samples={report['samples']} · "
            f"clusters={report['clusters_processed']} · "
            f"proposed={report['patterns_proposed']} · "
            f"<b>validated={report['patterns_validated']}</b> · "
            f"auto_rejected={report['patterns_auto_rejected']} · "
            f"hard={report['hard_clusters']} · "
            f"notified={report['admin_notifications']}\n"
            f"<i>{datetime.utcnow().isoformat(timespec='seconds')}Z</i>"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
