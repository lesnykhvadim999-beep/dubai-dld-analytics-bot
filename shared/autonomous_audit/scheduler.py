"""PHASE BN N4 — Audit scheduler.

Cron-based dispatcher for the three audit tiers:
  hourly  '0 * * * *'   → lightweight (heartbeats, breakers, queues)
  daily   '0 3 * * *'   → medium     (parser, contract drift, dead links)
  weekly  '0 4 * * 0'   → full       (schema, security scan, brand)

Two usage modes:

  1. Library mode — `should_run(audit_type, now)` for embedding into the
     master cron tick. Returns True iff `now` matches the schedule.

  2. Standalone — `python -m shared.autonomous_audit.scheduler` runs a
     blocking loop, sleeping until each next boundary and invoking
     `runner.run_audit(audit_type)`. Used when deployed as its own Railway
     worker (optional — preferred path is integration into
     shared/scripts/phase_bm_master_cron.py).

Backend: tries to use croniter if available; otherwise uses the same tiny
manual matcher as phase_bm_master_cron.py.
"""
from __future__ import annotations

import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_SHARED = _HERE.parent
_PROJECT = _SHARED.parent
for p in (str(_PROJECT), str(_SHARED)):
    if p not in sys.path:
        sys.path.insert(0, p)

log = logging.getLogger("autonomous_audit.scheduler")

SCHEDULES: dict[str, str] = {
    "hourly":  "0 * * * *",
    "daily":   "0 3 * * *",
    "weekly":  "0 4 * * 0",
}

try:
    from croniter import croniter  # type: ignore
    _HAS_CRONITER = True
except Exception:
    _HAS_CRONITER = False


# ── manual matcher (matches phase_bm_master_cron behaviour) ──────────────
def _expand_field(expr: str, lo: int, hi: int) -> set[int]:
    out: set[int] = set()
    for chunk in expr.split(","):
        step = 1
        if "/" in chunk:
            chunk, step_s = chunk.split("/", 1)
            step = max(1, int(step_s))
        if chunk in ("*", ""):
            start, end = lo, hi
        elif "-" in chunk:
            a, b = chunk.split("-", 1)
            start, end = int(a), int(b)
        else:
            start = end = int(chunk)
        for v in range(start, end + 1, step):
            if lo <= v <= hi:
                out.add(v)
    return out


def _cron_match_manual(cron: str, now: datetime) -> bool:
    fields = cron.split()
    if len(fields) != 5:
        return False
    minute, hour, dom, month, dow = fields
    try:
        if now.minute not in _expand_field(minute, 0, 59): return False
        if now.hour   not in _expand_field(hour,   0, 23): return False
        if now.day    not in _expand_field(dom,    1, 31): return False
        if now.month  not in _expand_field(month,  1, 12): return False
        py_dow = now.weekday()
        cron_dow = (py_dow + 1) % 7  # Sun=0
        if cron_dow not in _expand_field(dow, 0, 6): return False
        return True
    except Exception:
        return False


def should_run(audit_type: str, now: datetime | None = None) -> bool:
    """True iff `now` (UTC) matches the schedule for `audit_type`."""
    cron = SCHEDULES.get(audit_type)
    if not cron:
        return False
    now = now or datetime.now(timezone.utc)
    # truncate to minute precision
    now = now.replace(second=0, microsecond=0)
    if _HAS_CRONITER:
        try:
            # croniter.match exists in modern versions; fall back to is_valid+get_prev
            if hasattr(croniter, "match"):
                return bool(croniter.match(cron, now))
            it = croniter(cron, now - timedelta(seconds=1))
            return it.get_next(datetime).replace(tzinfo=now.tzinfo or timezone.utc) == now
        except Exception:
            pass
    return _cron_match_manual(cron, now)


def next_run(audit_type: str, after: datetime | None = None) -> datetime | None:
    cron = SCHEDULES.get(audit_type)
    if not cron:
        return None
    base = after or datetime.now(timezone.utc)
    if _HAS_CRONITER:
        try:
            it = croniter(cron, base)
            return it.get_next(datetime)
        except Exception:
            pass
    # manual: walk forward minute-by-minute, max 8 days lookahead.
    t = base.replace(second=0, microsecond=0) + timedelta(minutes=1)
    for _ in range(60 * 24 * 8):
        if _cron_match_manual(cron, t):
            return t
        t += timedelta(minutes=1)
    return None


# ── standalone loop ───────────────────────────────────────────────────────
def loop() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    from autonomous_audit.runner import run_audit  # noqa: E402

    log.info("[autonomous_audit] scheduler started; tiers=%s", list(SCHEDULES))
    last_fired: dict[str, str] = {}
    while True:
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        key = now.isoformat()
        for tier in SCHEDULES:
            if not should_run(tier, now):
                continue
            if last_fired.get(tier) == key:
                continue
            last_fired[tier] = key
            log.info("[autonomous_audit] firing %s audit", tier)
            try:
                summary = run_audit(tier)
                log.info("[autonomous_audit] %s done :: %s", tier, summary)
            except Exception as e:
                log.error("[autonomous_audit] %s crashed: %s", tier, e)
        # sleep until just after next minute boundary
        nowt = time.time()
        sleep_for = max(5, 60 - int(nowt % 60) + 1)
        try:
            time.sleep(sleep_for)
        except KeyboardInterrupt:
            log.info("[autonomous_audit] stopped")
            return


if __name__ == "__main__":
    loop()
