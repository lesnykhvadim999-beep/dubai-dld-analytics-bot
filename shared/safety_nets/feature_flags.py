"""feature_flags.py — Postgres-backed boolean flags with auto-disable.

API:
    register_feature(name, default_enabled=True)
    is_feature_enabled(name) -> bool
    report_failure(name)   # 3 consecutive → auto-disable + admin alert
    report_success(name)   # reset consecutive_failures

If DB is unreachable we fail-open (return True) for is_feature_enabled
so production is never silently darkened by infrastructure outage.
"""
from __future__ import annotations
import logging
from datetime import datetime, timezone

from .bootstrap import get_conn

log = logging.getLogger("safety_nets.feature_flags")

AUTO_DISABLE_THRESHOLD = 3


def _now():
    return datetime.now(timezone.utc)


def register_feature(name: str, default_enabled: bool = True) -> None:
    conn = get_conn()
    if conn is None:
        return
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO feature_flags (name, enabled)
                    VALUES (%s, %s)
                    ON CONFLICT (name) DO NOTHING
                    """,
                    (name, default_enabled),
                )
    except Exception as e:
        log.warning("ff[%s]: register failed: %s", name, e)
    finally:
        try:
            conn.close()
        except Exception:
            pass


def is_feature_enabled(name: str) -> bool:
    conn = get_conn()
    if conn is None:
        return True  # fail-open
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT enabled FROM feature_flags WHERE name=%s", (name,))
            row = cur.fetchone()
            if row is None:
                # auto-register on first read
                with conn:
                    with conn.cursor() as cur2:
                        cur2.execute(
                            "INSERT INTO feature_flags(name, enabled) VALUES (%s, TRUE) ON CONFLICT DO NOTHING",
                            (name,),
                        )
                return True
            return bool(row[0])
    except Exception as e:
        log.warning("ff[%s]: read failed: %s", name, e)
        return True
    finally:
        try:
            conn.close()
        except Exception:
            pass


def report_failure(name: str, reason: str = "") -> None:
    conn = get_conn()
    if conn is None:
        return
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO feature_flags (name, enabled, consecutive_failures, last_failure_at)
                    VALUES (%s, TRUE, 1, now())
                    ON CONFLICT (name) DO UPDATE SET
                        consecutive_failures = feature_flags.consecutive_failures + 1,
                        last_failure_at = now(),
                        updated_at = now()
                    RETURNING consecutive_failures, enabled
                    """,
                    (name,),
                )
                row = cur.fetchone()
                if not row:
                    return
                fails, enabled = row[0], row[1]
                if fails >= AUTO_DISABLE_THRESHOLD and enabled:
                    cur.execute(
                        """
                        UPDATE feature_flags
                           SET enabled = FALSE,
                               auto_disabled_at = now(),
                               note = %s,
                               updated_at = now()
                         WHERE name=%s
                        """,
                        (reason[:500], name),
                    )
                    _alert_disabled(name, fails, reason)
    except Exception as e:
        log.warning("ff[%s]: report_failure failed: %s", name, e)
    finally:
        try:
            conn.close()
        except Exception:
            pass


def report_success(name: str) -> None:
    conn = get_conn()
    if conn is None:
        return
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE feature_flags
                       SET consecutive_failures = 0,
                           updated_at = now()
                     WHERE name=%s
                    """,
                    (name,),
                )
    except Exception as e:
        log.warning("ff[%s]: report_success failed: %s", name, e)
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _alert_disabled(name: str, fails: int, reason: str) -> None:
    try:
        from shared.admin_notify import admin_notify
        admin_notify(
            f"[Feature Flag] <b>{name}</b> AUTO-DISABLED\n"
            f"consecutive_failures={fails} (threshold={AUTO_DISABLE_THRESHOLD})\n"
            f"reason: {reason[:300] or 'n/a'}\n\n"
            f"Vadim Realty | RERA BRN 65011",
            priority="critical",
        )
    except Exception:
        pass
