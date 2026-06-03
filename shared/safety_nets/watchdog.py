"""watchdog.py — bot heartbeats + stale detection.

API:
    heartbeat(bot_name)              # call at boot + every 30 sec
    check_stale_bots() -> list[dict] # bots with last_beat older than threshold
    start_heartbeat_thread(name)     # background thread for convenience

Stale > 5 min → admin alert (rate-limited per bot to once per 30 min).

We NEVER auto-restart bots — only notify. Restarts are Railway's job.
"""
from __future__ import annotations
import os
import time
import logging
import threading
from datetime import datetime, timezone, timedelta
from typing import List, Dict

from .bootstrap import get_conn

log = logging.getLogger("safety_nets.watchdog")

STALE_THRESHOLD_SEC = int(os.environ.get("WATCHDOG_STALE_SEC", "300"))   # 5 min
HEARTBEAT_INTERVAL_SEC = int(os.environ.get("WATCHDOG_INTERVAL_SEC", "30"))
ALERT_COOLDOWN_SEC = 1800  # 30 min between repeat alerts for the same bot

_threads: dict[str, threading.Thread] = {}
_start_times: dict[str, float] = {}


def _now():
    return datetime.now(timezone.utc)


def heartbeat(bot_name: str) -> bool:
    """Record a heartbeat for bot_name. Returns True on success."""
    started = _start_times.setdefault(bot_name, time.time())
    uptime = int(time.time() - started)
    conn = get_conn()
    if conn is None:
        return False
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO bot_heartbeats (bot_name, last_beat_at, uptime_sec)
                    VALUES (%s, now(), %s)
                    ON CONFLICT (bot_name) DO UPDATE SET
                        last_beat_at = now(),
                        uptime_sec   = EXCLUDED.uptime_sec
                    """,
                    (bot_name, uptime),
                )
        return True
    except Exception as e:
        log.warning("watchdog[%s]: heartbeat failed: %s", bot_name, e)
        return False
    finally:
        try:
            conn.close()
        except Exception:
            pass


def check_stale_bots(threshold_sec: int = STALE_THRESHOLD_SEC) -> List[Dict]:
    """Return bots whose last_beat_at is older than threshold_sec. Also fires
    admin alerts (rate-limited to once per 30 min per bot).
    """
    conn = get_conn()
    if conn is None:
        return []
    stale: List[Dict] = []
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT bot_name,
                           last_beat_at,
                           uptime_sec,
                           last_alert_at,
                           EXTRACT(EPOCH FROM (now() - last_beat_at))::INT AS age_sec
                      FROM bot_heartbeats
                     WHERE last_beat_at < now() - (%s || ' seconds')::INTERVAL
                    """,
                    (threshold_sec,),
                )
                rows = cur.fetchall()
                for r in rows:
                    name, last_beat, uptime, last_alert, age = r
                    stale.append({
                        "bot_name": name,
                        "last_beat_at": last_beat,
                        "uptime_sec": uptime,
                        "age_sec": age,
                    })
                    cooldown_ok = (
                        last_alert is None
                        or (_now() - last_alert).total_seconds() > ALERT_COOLDOWN_SEC
                    )
                    if cooldown_ok:
                        _alert_stale(name, age)
                        cur.execute(
                            "UPDATE bot_heartbeats SET last_alert_at=now() WHERE bot_name=%s",
                            (name,),
                        )
    except Exception as e:
        log.warning("watchdog: check_stale_bots failed: %s", e)
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return stale


def _alert_stale(name: str, age_sec: int) -> None:
    try:
        from shared.admin_notify import admin_notify
        admin_notify(
            f"[Watchdog] Bot <b>{name}</b> is STALE\n"
            f"last heartbeat: {age_sec} sec ago (threshold {STALE_THRESHOLD_SEC}s)\n"
            f"NOTE: no auto-restart — please check Railway logs.\n\n"
            f"Vadim Realty | RERA BRN 65011",
            priority="critical",
        )
    except Exception:
        pass


def start_heartbeat_thread(bot_name: str, interval_sec: int = HEARTBEAT_INTERVAL_SEC) -> None:
    """Spawn a daemon thread that calls heartbeat(bot_name) periodically.

    Idempotent — multiple calls for the same bot_name only spawn one thread.
    """
    if bot_name in _threads and _threads[bot_name].is_alive():
        return

    def _loop():
        # first beat immediately so the row exists
        try:
            heartbeat(bot_name)
        except Exception:
            pass
        while True:
            try:
                time.sleep(interval_sec)
                heartbeat(bot_name)
            except Exception as e:
                log.warning("watchdog[%s] loop error: %s", bot_name, e)
                time.sleep(interval_sec)

    t = threading.Thread(target=_loop, daemon=True, name=f"safety_watchdog_{bot_name}")
    t.start()
    _threads[bot_name] = t
    log.info("watchdog: heartbeat thread started for %s", bot_name)
