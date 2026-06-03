"""Smoke fixture runner — execute all FIXTURES, alert on failures.

How to run
----------
  py C:\\Projects\\shared\\smoke_fixture_runner.py             # all fixtures
  py C:\\Projects\\shared\\smoke_fixture_runner.py --id <fid>  # one fixture
  py C:\\Projects\\shared\\smoke_fixture_runner.py --bot <bot> # one bot
  py C:\\Projects\\shared\\smoke_fixture_runner.py --json      # output JSON only

Exit codes
----------
  0  — all hard fixtures passed (soft failures ignored)
  1  — at least one hard fixture failed
  2  — runner itself crashed

Side effects
------------
  - On hard failure: admin_notify + append to claude_autoheal_queue.txt
  - On success of all: silent (only success log written to SUCCESS_LOG)

Cron entry (Windows Task Scheduler) — schedule every 6h:
  Program: py
  Args:    C:\\Projects\\shared\\smoke_fixture_runner.py
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Make shared importable
THIS_DIR = Path(__file__).parent
if str(THIS_DIR) not in sys.path:
    sys.path.insert(0, str(THIS_DIR))

from smoke_fixtures import FIXTURES, by_id, Fixture  # noqa: E402

# admin_notify is local to /shared
try:
    from admin_notify import admin_notify  # type: ignore
except Exception:
    def admin_notify(text, **kw):  # type: ignore
        print(f"[admin_notify-fallback] {text}", file=sys.stderr)
        return False

CLAUDE_QUEUE_PATH = Path(os.environ.get(
    "CLAUDE_AUTOHEAL_QUEUE",
    str(Path.home() / ".claude" / "claude_autoheal_queue.txt"),
))
SUCCESS_LOG = Path(os.environ.get(
    "SMOKE_SUCCESS_LOG",
    str(Path.home() / ".claude" / "smoke_fixture_history.jsonl"),
))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connect(dsn: str):
    import psycopg2
    return psycopg2.connect(dsn, connect_timeout=8)


def run_fixture(f: Fixture) -> dict:
    dsn = os.environ.get(f.dsn_env, "").strip()
    if not dsn:
        return {
            "id": f.id, "bot": f.bot, "ok": False, "skipped": True,
            "reason": f"env {f.dsn_env} not set", "count": None,
            "expect_min": f.expect_min, "soft": f.soft, "dt_ms": 0,
        }
    t0 = time.time()
    try:
        with _connect(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(f.sql)
                row = cur.fetchone()
                n = int(row[0]) if row and row[0] is not None else 0
        ok = n >= f.expect_min
        return {
            "id": f.id, "bot": f.bot, "ok": ok, "count": n,
            "expect_min": f.expect_min, "soft": f.soft,
            "dt_ms": int((time.time() - t0) * 1000),
            "description": f.description,
        }
    except Exception as e:
        return {
            "id": f.id, "bot": f.bot, "ok": False, "error": str(e)[:300],
            "count": None, "expect_min": f.expect_min, "soft": f.soft,
            "dt_ms": int((time.time() - t0) * 1000),
            "description": f.description,
        }


def _write_success_log(results: list[dict]) -> None:
    try:
        SUCCESS_LOG.parent.mkdir(parents=True, exist_ok=True)
        with SUCCESS_LOG.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "ts": _now_iso(),
                "summary": _summary(results),
                "results": results,
            }, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"[smoke] success log write fail: {e}", file=sys.stderr)


def _enqueue_autoheal(payload: dict) -> None:
    """Append to claude_autoheal_queue.txt in the JSON-array format used by
    log_sentinel.py.
    """
    try:
        CLAUDE_QUEUE_PATH.parent.mkdir(parents=True, exist_ok=True)
        items = []
        if CLAUDE_QUEUE_PATH.exists():
            try:
                items = json.loads(CLAUDE_QUEUE_PATH.read_text(encoding="utf-8"))
                if not isinstance(items, list):
                    items = []
            except Exception:
                items = []
        items.append(payload)
        if len(items) > 50:
            items = items[-50:]
        CLAUDE_QUEUE_PATH.write_text(
            json.dumps(items, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as e:
        print(f"[smoke] queue write fail: {e}", file=sys.stderr)


def _summary(results: list[dict]) -> dict:
    total = len(results)
    skipped = sum(1 for r in results if r.get("skipped"))
    passed = sum(1 for r in results if r.get("ok"))
    failed_hard = sum(1 for r in results
                      if not r.get("ok") and not r.get("soft")
                      and not r.get("skipped"))
    failed_soft = sum(1 for r in results
                      if not r.get("ok") and r.get("soft")
                      and not r.get("skipped"))
    return {
        "total": total, "passed": passed, "failed_hard": failed_hard,
        "failed_soft": failed_soft, "skipped": skipped,
    }


def report_failures(results: list[dict]) -> None:
    hard_fails = [r for r in results
                  if not r.get("ok") and not r.get("soft")
                  and not r.get("skipped")]
    soft_fails = [r for r in results
                  if not r.get("ok") and r.get("soft")
                  and not r.get("skipped")]
    if not hard_fails and not soft_fails:
        return
    lines = ["🚨 <b>Smoke fixtures failed</b>"]
    for r in hard_fails:
        kind = "ERROR" if r.get("error") else "EMPTY"
        lines.append(
            f"❌ <code>{r['id']}</code> ({r['bot']}): {kind} "
            f"got={r.get('count')} expect>={r['expect_min']}"
        )
        if r.get("error"):
            lines.append(f"   <i>{r['error'][:160]}</i>")
    for r in soft_fails:
        lines.append(f"⚠️ soft <code>{r['id']}</code>: got={r.get('count')}")
    msg = "\n".join(lines)[:4090]
    admin_notify(msg, parse_mode="HTML", priority="critical" if hard_fails else "normal")

    # auto-heal queue entry per hard failure
    for r in hard_fails:
        _enqueue_autoheal({
            "ts": _now_iso(),
            "bot": r["bot"],
            "severity": "CRITICAL",
            "category": "smoke_fixture",
            "error_line": f"smoke fixture {r['id']} failed: "
                          f"got={r.get('count')} expect>={r['expect_min']} "
                          f"err={r.get('error', '')}",
            "context_tail": r.get("description", ""),
            "instruction": (
                f"Investigate smoke fixture '{r['id']}' failure "
                f"(bot={r['bot']}). Description: {r.get('description', '')}. "
                f"Check whether the underlying data exists, whether schema "
                f"changed, and whether parser/filter logic regressed. "
                f"See C:\\Projects\\shared\\smoke_fixtures.py for the SQL. "
                f"Fix root cause, redeploy, notify @vadim_admin_bot when done."
            ),
        })


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", help="run a single fixture by id")
    ap.add_argument("--bot", help="run only fixtures for this bot")
    ap.add_argument("--json", action="store_true",
                     help="print JSON output only, no human summary")
    ap.add_argument("--quiet", action="store_true",
                     help="suppress admin alerts (useful for /smoke testing)")
    args = ap.parse_args()

    if args.id:
        f = by_id(args.id)
        if not f:
            print(f"unknown fixture id: {args.id}", file=sys.stderr)
            return 2
        fixtures = [f]
    elif args.bot:
        fixtures = [f for f in FIXTURES if f.bot == args.bot]
        if not fixtures:
            print(f"no fixtures for bot: {args.bot}", file=sys.stderr)
            return 2
    else:
        fixtures = FIXTURES

    results = []
    for f in fixtures:
        r = run_fixture(f)
        results.append(r)
        if not args.json:
            status = "OK   " if r.get("ok") else (
                "SKIP " if r.get("skipped") else ("FAIL " if not r.get("soft") else "SOFT "))
            print(f"  [{status}] {r['id']:<45} count={r.get('count')} "
                  f"expect>={r['expect_min']} dt={r.get('dt_ms')}ms"
                  + (f"  reason={r['reason']}" if r.get("skipped") else "")
                  + (f"  err={r['error'][:120]}" if r.get("error") else ""))

    summary = _summary(results)
    _write_success_log(results)
    if not args.quiet:
        try:
            report_failures(results)
        except Exception as e:
            print(f"[smoke] report fail: {e}", file=sys.stderr)

    if args.json:
        print(json.dumps({"summary": summary, "results": results},
                          ensure_ascii=False, indent=2))
    else:
        print()
        print(f"Summary: {summary['passed']}/{summary['total']} passed, "
              f"hard_fail={summary['failed_hard']}, "
              f"soft_fail={summary['failed_soft']}, "
              f"skipped={summary['skipped']}")

    return 1 if summary["failed_hard"] > 0 else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nInterrupted", file=sys.stderr)
        sys.exit(2)
    except Exception as e:
        import traceback
        traceback.print_exc()
        try:
            admin_notify(f"🚨 smoke_fixture_runner crashed: {e}",
                          parse_mode="HTML", priority="critical")
        except Exception:
            pass
        sys.exit(2)
