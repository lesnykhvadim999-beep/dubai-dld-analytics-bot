"""circuit_breaker.py — persistent CLOSED/OPEN/HALF_OPEN breaker.

Usage:
    from shared.safety_nets import with_circuit_breaker

    @with_circuit_breaker("cerebras")
    def call_cerebras(...): ...

State table `circuit_breakers` (PK name) is the single source of truth so
state survives bot restarts. If Postgres is unreachable the breaker
fails open (calls pass through) so we never block production on
infrastructure issues.

Defaults:
  failure_threshold = 5
  recovery_timeout  = 60 sec
  half_open_max     = 3 calls
"""
from __future__ import annotations
import os
import time
import logging
import functools
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from .bootstrap import get_conn

log = logging.getLogger("safety_nets.circuit_breaker")

STATE_CLOSED = "CLOSED"
STATE_OPEN = "OPEN"
STATE_HALF_OPEN = "HALF_OPEN"


class CircuitOpenError(RuntimeError):
    """Raised when a call is rejected because the breaker is OPEN."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


class CircuitBreaker:
    def __init__(
        self,
        name: str,
        failure_threshold: int = 5,
        recovery_timeout: int = 60,
        half_open_max: int = 3,
    ):
        self.name = name
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.half_open_max = half_open_max
        self._ensure_row()

    # ---- DB helpers (fail-open) ---------------------------------------

    def _ensure_row(self) -> None:
        conn = get_conn()
        if conn is None:
            return
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO circuit_breakers (name, state, failure_count)
                        VALUES (%s, %s, 0)
                        ON CONFLICT (name) DO NOTHING
                        """,
                        (self.name, STATE_CLOSED),
                    )
        except Exception as e:
            log.warning("cb[%s]: ensure_row failed: %s", self.name, e)
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def _load(self) -> dict:
        conn = get_conn()
        if conn is None:
            return {
                "state": STATE_CLOSED,
                "failure_count": 0,
                "opened_at": None,
                "half_open_attempts": 0,
            }
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT state, failure_count, opened_at, half_open_attempts
                    FROM circuit_breakers WHERE name=%s
                    """,
                    (self.name,),
                )
                row = cur.fetchone()
                if not row:
                    return {
                        "state": STATE_CLOSED,
                        "failure_count": 0,
                        "opened_at": None,
                        "half_open_attempts": 0,
                    }
                return {
                    "state": row[0],
                    "failure_count": row[1],
                    "opened_at": row[2],
                    "half_open_attempts": row[3],
                }
        except Exception as e:
            log.warning("cb[%s]: load failed: %s", self.name, e)
            return {
                "state": STATE_CLOSED,
                "failure_count": 0,
                "opened_at": None,
                "half_open_attempts": 0,
            }
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def _save(self, **fields: Any) -> None:
        if not fields:
            return
        conn = get_conn()
        if conn is None:
            return
        try:
            cols = list(fields.keys())
            setters = ", ".join(f"{c}=%s" for c in cols) + ", updated_at=now()"
            vals = [fields[c] for c in cols] + [self.name]
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        f"UPDATE circuit_breakers SET {setters} WHERE name=%s",
                        vals,
                    )
        except Exception as e:
            log.warning("cb[%s]: save failed: %s", self.name, e)
        finally:
            try:
                conn.close()
            except Exception:
                pass

    # ---- State transitions --------------------------------------------

    def _trip(self) -> None:
        log.warning("cb[%s]: tripping OPEN", self.name)
        self._save(
            state=STATE_OPEN,
            opened_at=_now(),
            half_open_attempts=0,
        )
        # Critical alert (fire-and-forget).
        try:
            from shared.admin_notify import admin_notify
            admin_notify(
                f"[Circuit Breaker] <b>{self.name}</b> tripped to OPEN\n"
                f"failures &gt;= {self.failure_threshold}\n"
                f"Vadim Realty | RERA BRN 65011",
                priority="critical",
            )
        except Exception:
            pass

    def _close(self) -> None:
        log.info("cb[%s]: closing", self.name)
        self._save(
            state=STATE_CLOSED,
            failure_count=0,
            opened_at=None,
            half_open_attempts=0,
        )

    def _half_open(self) -> None:
        log.info("cb[%s]: HALF_OPEN", self.name)
        self._save(state=STATE_HALF_OPEN, half_open_attempts=0)

    # ---- Public API ---------------------------------------------------

    def allow(self) -> bool:
        """Decide whether the next call should proceed."""
        s = self._load()
        state = s["state"]
        if state == STATE_CLOSED:
            return True
        if state == STATE_OPEN:
            opened = s["opened_at"]
            if opened is None:
                self._close()
                return True
            age = (_now() - opened).total_seconds()
            if age >= self.recovery_timeout:
                self._half_open()
                return True
            return False
        # HALF_OPEN
        if s["half_open_attempts"] >= self.half_open_max:
            return False
        # reserve a probe slot
        self._save(half_open_attempts=s["half_open_attempts"] + 1)
        return True

    def record_success(self) -> None:
        s = self._load()
        if s["state"] == STATE_HALF_OPEN:
            self._close()
        elif s["state"] == STATE_CLOSED and s["failure_count"] > 0:
            self._save(failure_count=0)

    def record_failure(self) -> None:
        s = self._load()
        if s["state"] == STATE_HALF_OPEN:
            self._trip()
            return
        new_count = s["failure_count"] + 1
        if new_count >= self.failure_threshold:
            self._save(failure_count=new_count)
            self._trip()
        else:
            self._save(failure_count=new_count, last_failure_at=_now())

    def call(self, fn: Callable, *args, **kwargs):
        if not self.allow():
            raise CircuitOpenError(f"circuit '{self.name}' is OPEN")
        try:
            out = fn(*args, **kwargs)
            self.record_success()
            return out
        except CircuitOpenError:
            raise
        except Exception:
            self.record_failure()
            raise


# In-process registry so repeated decorators reuse the same breaker.
_REGISTRY: dict[str, CircuitBreaker] = {}


def get_breaker(
    name: str,
    failure_threshold: int = 5,
    recovery_timeout: int = 60,
    half_open_max: int = 3,
) -> CircuitBreaker:
    cb = _REGISTRY.get(name)
    if cb is None:
        cb = CircuitBreaker(
            name=name,
            failure_threshold=failure_threshold,
            recovery_timeout=recovery_timeout,
            half_open_max=half_open_max,
        )
        _REGISTRY[name] = cb
    return cb


def with_circuit_breaker(
    name: str,
    failure_threshold: int = 5,
    recovery_timeout: int = 60,
    half_open_max: int = 3,
):
    """Decorator: wraps a callable in a persistent circuit breaker."""

    def deco(fn: Callable) -> Callable:
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            cb = get_breaker(
                name,
                failure_threshold=failure_threshold,
                recovery_timeout=recovery_timeout,
                half_open_max=half_open_max,
            )
            return cb.call(fn, *args, **kwargs)

        return wrapper

    return deco
