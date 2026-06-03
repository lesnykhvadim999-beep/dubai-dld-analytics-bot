"""
PHASE BM Master Cron Worker
===========================

Один долгоживущий процесс, который раз в минуту смотрит расписание и запускает
PHASE BM jobs (L9-L22) — агентов J, K, L, G, H + market_world_model (I).

Запуск:
  • standalone (Railway worker / Windows Scheduled Task):
        python -m shared.scripts.phase_bm_master_cron
  • как daemon-thread изнутри другого процесса (analytics, hub):
        from shared.scripts.phase_bm_master_cron import start_thread
        start_thread()

Дизайн:
  • Каждые 60 секунд проверяем все таблицы расписаний.
  • Для каждого job вычисляем "should-run-now" через croniter (если установлен)
    или через ручной cron matcher (fallback — никаких новых зависимостей).
  • Анти-двойной-запуск: state-файл `phase_bm_cron_state.json` хранит
    last_run_ts по каждому job; если задача уже стартовала в эту минуту —
    пропускаем.
  • Каждая задача исполняется в отдельной daemon-thread с timeout (через
    subprocess timeout). Падение одной задачи не валит scheduler.

Schedules (UTC):
  agent_j_rss            "0 */3 * * *"       every 3h
  agent_j_telegram       "0 */2 * * *"       every 2h
  agent_j_meta           "0 3 * * 0"         Sun 03:00
  agent_j_digest         "0 8 * * *"         daily 08:00
  agent_l_daily_content  "0 8 * * *"         daily 08:00
  agent_l_weekly_top     "0 9 * * 1"         Mon 09:00
  agent_l_monthly_report "0 10 1 * *"        1st 10:00
  agent_l_self_modify    "0 2 * * *"         daily 02:00
  agent_l_followups      "*/5 * * * *"       every 5m
  agent_g_weekly_learner "0 6 * * 0"         Sun 06:00
  agent_i_mwm_weekly     "0 2 * * 0"         Sun 02:00
  agent_k_causal_monthly "0 4 1 * *"         1st 04:00
  agent_h_subscriber     "* * * * *"         every minute
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

log = logging.getLogger("phase_bm_cron")

STATE_FILE = Path(
    os.environ.get("PHASE_BM_CRON_STATE",
                   str(Path(__file__).resolve().parent / "phase_bm_cron_state.json"))
)
PROJECT_ROOT = Path(__file__).resolve().parents[2]  # C:/Projects
PYTHON = sys.executable or "python"

# Advisory lock + cron_run_log are best-effort: never block scheduler on DB hiccup.
try:
    import psycopg2  # type: ignore
    _PG_OK = True
except Exception:
    _PG_OK = False


def _dsn() -> str | None:
    for k in ("INTELLIGENCE_DATABASE_URL", "RESALE_DATABASE_URL",
              "DATABASE_URL", "BEHAVIOR_DATABASE_URL"):
        v = os.environ.get(k)
        if v:
            return v
    return None


def _job_lock_key(name: str) -> int:
    """Stable 31-bit int key per job for pg_try_advisory_lock."""
    h = 0
    for c in name:
        h = (h * 131 + ord(c)) & 0xFFFFFFFF
    return h % (2 ** 31)


def _try_acquire_lock(name: str):
    """Returns (conn, locked: bool). conn kept open while job runs."""
    if not _PG_OK:
        return None, True  # no DB → degrade to state-file only (still safe in single-replica)
    dsn = _dsn()
    if not dsn:
        return None, True
    try:
        conn = psycopg2.connect(dsn, connect_timeout=5)
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(%s)", (_job_lock_key(name),))
            ok = bool(cur.fetchone()[0])
        if not ok:
            try: conn.close()
            except Exception: pass
            return None, False
        return conn, True
    except Exception as e:
        log.warning("[phase_bm_cron] advisory_lock conn failed for %s: %s", name, e)
        return None, True  # degrade open


def _release_lock(conn, name: str) -> None:
    if conn is None:
        return
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_unlock(%s)", (_job_lock_key(name),))
    except Exception:
        pass
    try:
        conn.close()
    except Exception:
        pass


def _log_run(job_name: str, cron_expr: str, started_at, finished_at, rc, elapsed_ms, note):
    """Best-effort INSERT into cron_run_log; swallow errors."""
    if not _PG_OK:
        return
    dsn = _dsn()
    if not dsn:
        return
    host = os.environ.get("RAILWAY_SERVICE_NAME") or os.environ.get("HOSTNAME") or "local"
    try:
        with psycopg2.connect(dsn, connect_timeout=5) as conn:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO cron_run_log
                       (job_name, cron_expr, started_at, finished_at, rc, elapsed_ms, host, note)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (job_name, cron_expr, started_at, finished_at, rc, elapsed_ms, host, (note or "")[:500]),
                )
    except Exception:
        pass  # cron_run_log table may not exist on a fresh DB; ignored


# ── job registry ──────────────────────────────────────────────────────────
JOBS: list[dict] = [
    # Agent J — Meta-Learning + External Knowledge
    {"name": "agent_j_rss",            "cron": "0 */3 * * *",
     "argv": [PYTHON, "-m", "shared.scripts.cron_phase_bm_j", "rss_poll"]},
    {"name": "agent_j_telegram",       "cron": "0 */2 * * *",
     "argv": [PYTHON, "-m", "shared.scripts.cron_phase_bm_j", "telegram_scrape"]},
    {"name": "agent_j_meta",           "cron": "0 3 * * 0",
     "argv": [PYTHON, "-m", "shared.scripts.cron_phase_bm_j", "meta_optimize", "14"]},
    {"name": "agent_j_digest",         "cron": "0 8 * * *",
     "argv": [PYTHON, "-m", "shared.scripts.cron_phase_bm_j", "daily_digest"]},

    # Agent L — Content Pipeline + Self-Modify + Continuous Reasoning
    {"name": "agent_l_daily_content",  "cron": "0 8 * * *",
     "argv": [PYTHON, "-m", "shared.scripts.cron_phase_bm_l", "daily_content"]},
    {"name": "agent_l_weekly_top",     "cron": "0 9 * * 1",
     "argv": [PYTHON, "-m", "shared.scripts.cron_phase_bm_l", "weekly_top"]},
    {"name": "agent_l_monthly_report", "cron": "0 10 1 * *",
     "argv": [PYTHON, "-m", "shared.scripts.cron_phase_bm_l", "monthly_report"]},
    {"name": "agent_l_self_modify",    "cron": "0 2 * * *",
     "argv": [PYTHON, "-m", "shared.scripts.cron_phase_bm_l", "self_modify_scan"]},
    {"name": "agent_l_followups",      "cron": "*/5 * * * *",
     "argv": [PYTHON, "-m", "shared.scripts.cron_phase_bm_l", "followups"]},

    # Agent G — Causal Engine weekly learner
    {"name": "agent_g_weekly_learner", "cron": "0 6 * * 0",
     "argv": [PYTHON, "-m", "shared.causal_engine.cron_weekly_learner"]},

    # Agent I — Market World Model weekly retrain
    {"name": "agent_i_mwm_weekly",     "cron": "0 2 * * 0",
     "argv": [PYTHON, "-m", "shared.market_world_model.builder", "--weekly"]},

    # Agent K — Causal Inference monthly refresh
    {"name": "agent_k_causal_monthly", "cron": "0 4 1 * *",
     "argv": [PYTHON, "-m", "shared.causal_inference.cron_monthly_refresh"]},
]

# Optional: subscriber daemon (Agent H) — every minute. Wrap as a periodic job;
# expects module shared.subscriber_daemon.run_once to exist. Skipped silently
# if module missing (fail-soft).
JOBS.append(
    {"name": "agent_h_subscriber", "cron": "* * * * *",
     "argv": [PYTHON, "-m", "shared.agent_bus.daemon",
              "--agent", "admin-notifier", "--once", "--batch", "50"]}
)

# ── PHASE BO O1: Disaster Recovery jobs ───────────────────────────────────
JOBS.extend([
    {"name": "dr_backup_daily",   "cron": "0 1 * * *",
     "argv": [PYTHON, "-m", "shared.disaster_recovery.backup",
              "--db", "intelligence"]},
    {"name": "dr_verify_weekly",  "cron": "30 2 * * 0",
     "argv": [PYTHON, "-m", "shared.disaster_recovery.verify",
              "--db", "intelligence", "--bucket", "daily"]},
    {"name": "dr_retention_prune","cron": "0 4 * * *",
     "argv": [PYTHON, "-m", "shared.disaster_recovery.retention", "--apply"]},
])

# ── PHASE BO O3: UX metrics + auto-rollback ───────────────────────────────
JOBS.extend([
    # daily 06:00 UTC — compute funnels (logs results, used for heatmap reports)
    {"name": "ux_metrics_daily_funnels", "cron": "0 6 * * *",
     "argv": [PYTHON, "-m", "shared.ux_metrics.analyzer",
              "funnel", "resale", "default", "1d"]},
    # daily 07:00 UTC — auto-rollback check (2-day worse streak → disable flag)
    {"name": "ux_rollback_check",        "cron": "0 7 * * *",
     "argv": [PYTHON, "-m", "shared.ux_metrics.rollback"]},
])


# ── PHASE BN N2: Immune System ────────────────────────────────────────────
JOBS.extend([
    {"name": "immune_diagnose_hourly", "cron": "0 * * * *",
     "argv": [PYTHON, "-m", "shared.immune_system.diagnosis", "--limit", "10"]},
    {"name": "immune_immunize_daily",  "cron": "0 2 * * *",
     "argv": [PYTHON, "-m", "shared.immune_system.immunizer", "--limit", "5"]},
    {"name": "immune_verify_weekly",   "cron": "0 3 * * 0",
     "argv": [PYTHON, "-m", "shared.immune_system.registry"]},
])

# ── PHASE BN N4: Autonomous Audit Loop ────────────────────────────────────
JOBS.extend([
    {"name": "hourly_audit",       "cron": "0 * * * *",
     "argv": [PYTHON, "-m", "shared.autonomous_audit.runner",
              "--type", "hourly"]},
    {"name": "daily_audit",        "cron": "0 3 * * *",
     "argv": [PYTHON, "-m", "shared.autonomous_audit.runner",
              "--type", "daily"]},
    {"name": "weekly_audit",       "cron": "0 4 * * 0",
     "argv": [PYTHON, "-m", "shared.autonomous_audit.runner",
              "--type", "weekly"]},
    {"name": "audit_daily_digest", "cron": "30 8 * * *",
     "argv": [PYTHON, "-m", "shared.autonomous_audit.reporter"]},
])

# ── PHASE BN N5: Continuous Performance Optimizer ─────────────────────────
JOBS.extend([
    {"name": "optimizer_query_perf",  "cron": "0 2 * * *",
     "argv": [PYTHON, "-m", "shared.optimizer.query_perf"]},
    {"name": "optimizer_dead_code",   "cron": "0 5 * * 0",
     "argv": [PYTHON, "-m", "shared.optimizer.dead_code"]},
    {"name": "optimizer_dep_scan",    "cron": "0 6 * * 0",
     "argv": [PYTHON, "-m", "shared.optimizer.dep_scan"]},
])

# ── PHASE BO O2: Unified Observability — hourly system pulse digest ───────
JOBS.append(
    {"name": "obs_system_pulse_hourly", "cron": "0 * * * *",
     "argv": [PYTHON, "-m", "shared.observability.digest", "send"]}
)

# ── PHASE BO O4: auto_docs_v2 — /help + runbook + quickstart ──────────────
JOBS.extend([
    {"name": "auto_docs_help_daily",       "cron": "0 7 * * *",
     "argv": [PYTHON, "-m", "shared.auto_docs_v2.help_generator"]},
    {"name": "auto_docs_runbook_weekly",   "cron": "0 8 * * 0",
     "argv": [PYTHON, "-m", "shared.auto_docs_v2.runbook_generator"]},
    {"name": "auto_docs_quickstart_weekly","cron": "5 8 * * 0",
     "argv": [PYTHON, "-m", "shared.auto_docs_v2.quickstart"]},
])


# ── tiny cron matcher (no external dep) ───────────────────────────────────
def _expand_field(expr: str, lo: int, hi: int) -> set[int]:
    """Parse one cron field into the set of allowed integers."""
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


def _cron_match(cron: str, now: datetime) -> bool:
    """Returns True if `now` (UTC) falls on `cron` boundary (minute-precision)."""
    fields = cron.split()
    if len(fields) != 5:
        return False
    minute, hour, dom, month, dow = fields
    try:
        if now.minute not in _expand_field(minute, 0, 59): return False
        if now.hour   not in _expand_field(hour,   0, 23): return False
        if now.day    not in _expand_field(dom,    1, 31): return False
        if now.month  not in _expand_field(month,  1, 12): return False
        # cron dow: 0 = Sunday; Python: weekday() Mon=0, Sun=6
        py_dow = now.weekday()
        cron_dow = (py_dow + 1) % 7  # Mon=1 ... Sat=6, Sun=0
        if cron_dow not in _expand_field(dow, 0, 6): return False
        return True
    except Exception:
        return False


# ── state persistence ─────────────────────────────────────────────────────
def _load_state() -> dict:
    try:
        if STATE_FILE.exists():
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning("[phase_bm_cron] state load failed: %s", e)
    return {}


def _save_state(state: dict) -> None:
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(state), encoding="utf-8")
    except Exception as e:
        log.warning("[phase_bm_cron] state save failed: %s", e)


# ── job runner ────────────────────────────────────────────────────────────
def _run_job(job: dict, timeout: int = 600) -> None:
    """Spawn subprocess in PROJECT_ROOT, log result. Wrapped in pg advisory lock
    so concurrent master_cron instances (numReplicas>1, accidental dev run) don't
    fire the same job twice in the same minute."""
    name = job["name"]
    cron_expr = job.get("cron", "")
    argv = job["argv"]
    started_ts = time.time()
    started_iso = datetime.now(timezone.utc)
    rc = None
    note_parts = []

    lock_conn, got_lock = _try_acquire_lock(name)
    if not got_lock:
        log.info("[phase_bm_cron] %s skipped — advisory lock held by another instance",
                 name)
        _log_run(name, cron_expr, started_iso, datetime.now(timezone.utc),
                 None, 0, "skipped:lock_held")
        return
    try:
        env = os.environ.copy()
        env.setdefault("PYTHONIOENCODING", "utf-8")
        # ensure shared.* importable from PROJECT_ROOT
        env["PYTHONPATH"] = str(PROJECT_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
        r = subprocess.run(argv, cwd=str(PROJECT_ROOT), env=env,
                           capture_output=True, text=True, timeout=timeout)
        rc = r.returncode
        out = (r.stdout or "")[-500:]
        err = (r.stderr or "")[-500:]
        elapsed = round(time.time() - started_ts, 1)
        log.info("[phase_bm_cron] %s done rc=%s in %ss :: %s",
                 name, rc, elapsed, out.strip().replace("\n", " | "))
        if rc != 0 and err.strip():
            log.warning("[phase_bm_cron] %s stderr: %s", name, err.strip()[-300:])
            note_parts.append("err:" + err.strip()[-200:])
        else:
            note_parts.append(out.strip()[-200:])
    except subprocess.TimeoutExpired:
        rc = -9
        log.error("[phase_bm_cron] %s TIMEOUT after %ss", name, timeout)
        note_parts.append("TIMEOUT")
    except Exception as e:
        rc = -1
        log.error("[phase_bm_cron] %s crashed: %s\n%s",
                  name, e, traceback.format_exc()[-500:])
        note_parts.append("crash:" + str(e)[:200])
    finally:
        finished_iso = datetime.now(timezone.utc)
        elapsed_ms = int((time.time() - started_ts) * 1000)
        _log_run(name, cron_expr, started_iso, finished_iso, rc, elapsed_ms,
                 " | ".join(note_parts))
        _release_lock(lock_conn, name)


def _tick(state: dict) -> None:
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    key = now.isoformat()
    for job in JOBS:
        try:
            if not _cron_match(job["cron"], now):
                continue
            last = state.get(job["name"])
            if last == key:
                continue  # already started in this minute
            state[job["name"]] = key
            log.info("[phase_bm_cron] launching %s (cron=%s)",
                     job["name"], job["cron"])
            t = threading.Thread(target=_run_job, args=(job,),
                                 name=f"phasebm:{job['name']}", daemon=True)
            t.start()
        except Exception as e:
            log.error("[phase_bm_cron] tick error for %s: %s", job.get("name"), e)
    _save_state(state)


def loop() -> None:
    """Main scheduler loop. Sleeps until ~top of next minute, runs _tick."""
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO,
                            format="%(asctime)s %(levelname)s %(name)s %(message)s")
    log.info("[phase_bm_cron] master scheduler started "
             "(jobs=%d, state=%s)", len(JOBS), STATE_FILE)
    state = _load_state()
    # initial tick (so we don't wait 60s on start)
    _tick(state)
    while True:
        # Sleep until just after next minute boundary
        now = time.time()
        sleep_for = max(5, 60 - int(now % 60) + 1)
        try:
            time.sleep(sleep_for)
        except KeyboardInterrupt:
            log.info("[phase_bm_cron] stopped by SIGINT")
            return
        try:
            _tick(state)
        except Exception as e:
            log.error("[phase_bm_cron] tick crashed: %s", e)


_thread_singleton: threading.Thread | None = None


def start_thread() -> threading.Thread | None:
    """Launch scheduler as daemon thread (idempotent)."""
    global _thread_singleton
    if _thread_singleton and _thread_singleton.is_alive():
        return _thread_singleton
    if os.environ.get("PHASE_BM_CRON_DISABLED") == "1":
        log.info("[phase_bm_cron] disabled by env PHASE_BM_CRON_DISABLED=1")
        return None
    t = threading.Thread(target=loop, name="phase_bm_master_cron", daemon=True)
    t.start()
    _thread_singleton = t
    log.info("[phase_bm_cron] daemon thread launched")
    return t


if __name__ == "__main__":
    loop()
