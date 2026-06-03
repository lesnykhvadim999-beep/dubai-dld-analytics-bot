"""Shared helpers for auto_audit cron jobs (daily + realtime).

Provides:
  * DSN map for the 4 Railway Postgres DBs (resale, live, archive, intelligence)
  * `audit_history` table bootstrap + `record_metric()` writer
  * `should_alert(key, ttl_seconds)` throttle to avoid spam
  * Bot URL / token registries (no secrets hard-coded — env first)
"""
from __future__ import annotations
import os
import sys
import json
import time
import tempfile
from pathlib import Path
from typing import Any, Optional

# Make sibling shared utilities importable.
_SHARED = Path(r"C:\Projects\shared")
if str(_SHARED) not in sys.path:
    sys.path.insert(0, str(_SHARED))

try:
    from admin_notify import admin_notify  # type: ignore
except Exception:  # pragma: no cover
    def admin_notify(text: str, **kw) -> bool:  # type: ignore
        print(f"[admin_notify-stub] {text}")
        return False

try:
    import psycopg2  # type: ignore
    import psycopg2.extras  # type: ignore
except ImportError as e:  # pragma: no cover
    print(f"[auto_audit] psycopg2 missing: {e}", file=sys.stderr)
    raise

# ────────────────────────── DSN registry ──────────────────────────
def _dsn(name: str) -> str:
    """Prefer the *_PUBLIC variant so we can hit Railway from local host."""
    upper = name.upper()
    return (
        os.environ.get(f"{upper}_DATABASE_URL_PUBLIC", "")
        or os.environ.get(f"{upper}_DATABASE_URL", "")
        or ""
    )

DSNS = {
    "resale":       _dsn("resale"),
    "live":         _dsn("live"),
    "archive":      _dsn("archive"),
    "intelligence": _dsn("intelligence"),
}

# ────────────────────────── Bot registry ──────────────────────────
# Source: C:\Projects\shared\UPTIMEROBOT_SETUP.md (2026-05-30 inventory).
BOTS = [
    ("resale-bot",     "https://resale-bot-production.up.railway.app/health"),
    ("analytics-bot",  "https://dubai-dld-analytics-bot-production.up.railway.app/health"),
    ("hub-bot",        "https://hub-bot-production.up.railway.app/health"),
    ("lead-bot",       "https://telegram-bot-production-a608.up.railway.app/health"),
    ("roi-bot",        "https://dubai-roi-bot-production.up.railway.app/health"),
    ("channel-bot",    "https://channel-bot-new-production-9184.up.railway.app/health"),
    ("currency-bot",   "https://currency-bot-production-38d9.up.railway.app/health"),
    ("cloud-watchdog", "https://cloud-watchdog-production.up.railway.app/health"),
]

# ────────────────────────── LLM provider registry ──────────────────────────
# (name, env_key, ping_url, ping_method).  Free-tier first.
LLM_PROVIDERS = [
    ("cerebras",   "CEREBRAS_API_KEY",   "https://api.cerebras.ai/v1/models"),
    ("groq",       "GROQ_API_KEY",       "https://api.groq.com/openai/v1/models"),
    ("gemini",     "GEMINI_API_KEY",     "https://generativelanguage.googleapis.com/v1beta/models"),
    ("sambanova",  "SAMBANOVA_API_KEY",  "https://api.sambanova.ai/v1/models"),
    ("mistral",    "MISTRAL_API_KEY",    "https://api.mistral.ai/v1/models"),
    ("openrouter", "OPENROUTER_API_KEY", "https://openrouter.ai/api/v1/models"),
    ("hf",         "HF_API_KEY",         "https://api-inference.huggingface.co/models"),
    ("anthropic",  "ANTHROPIC_API_KEY",  "https://api.anthropic.com/v1/models"),
]

# ────────────────────────── audit_history bootstrap ──────────────────────────
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS audit_history (
  id      BIGSERIAL PRIMARY KEY,
  ts      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  metric  TEXT        NOT NULL,
  value   NUMERIC,
  meta    JSONB
);
CREATE INDEX IF NOT EXISTS audit_history_metric_ts_idx
  ON audit_history(metric, ts DESC);
"""


def ensure_schema(dsn: Optional[str] = None) -> bool:
    dsn = dsn or DSNS["resale"]
    if not dsn:
        return False
    try:
        with psycopg2.connect(dsn, connect_timeout=8) as conn:
            with conn.cursor() as cur:
                cur.execute(SCHEMA_SQL)
            conn.commit()
        return True
    except Exception as e:
        print(f"[auto_audit] ensure_schema failed: {e}")
        return False


def record_metric(metric: str, value: float | int | None,
                   meta: dict | None = None) -> bool:
    """Insert one metric row into `audit_history` (resale DB).

    `value=None` is allowed (e.g. for events with only meta).
    """
    dsn = DSNS["resale"]
    if not dsn:
        return False
    try:
        with psycopg2.connect(dsn, connect_timeout=8) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO audit_history(metric, value, meta) "
                    "VALUES (%s, %s, %s::jsonb)",
                    (metric, value, json.dumps(meta or {})),
                )
            conn.commit()
        return True
    except Exception as e:
        print(f"[auto_audit] record_metric({metric}) failed: {e}")
        return False


def last_metric(metric: str) -> dict | None:
    """Return last recorded row for the metric (or None)."""
    dsn = DSNS["resale"]
    if not dsn:
        return None
    try:
        with psycopg2.connect(dsn, connect_timeout=8) as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    "SELECT ts, value, meta FROM audit_history "
                    "WHERE metric=%s ORDER BY ts DESC LIMIT 1",
                    (metric,),
                )
                row = cur.fetchone()
                return dict(row) if row else None
    except Exception as e:
        print(f"[auto_audit] last_metric({metric}) failed: {e}")
        return None


def metric_at_offset(metric: str, days_ago: int) -> dict | None:
    """Return the recorded row for `metric` closest to NOW - days_ago days.

    Uses ± 12h window around the target timestamp; returns the row whose
    ts is nearest to the target. Returns None if no data in window.
    """
    dsn = DSNS["resale"]
    if not dsn:
        return None
    try:
        with psycopg2.connect(dsn, connect_timeout=8) as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    """
                    SELECT ts, value, meta
                    FROM audit_history
                    WHERE metric = %s
                      AND ts BETWEEN NOW() - (%s::int * INTERVAL '1 day') - INTERVAL '12 hours'
                                 AND NOW() - (%s::int * INTERVAL '1 day') + INTERVAL '12 hours'
                    ORDER BY ABS(EXTRACT(EPOCH FROM (ts - (NOW() - (%s::int * INTERVAL '1 day')))))
                    LIMIT 1
                    """,
                    (metric, days_ago, days_ago, days_ago),
                )
                row = cur.fetchone()
                return dict(row) if row else None
    except Exception as e:
        print(f"[auto_audit] metric_at_offset({metric}, {days_ago}d) failed: {e}")
        return None


# ────────────────────────── Throttle ──────────────────────────
_THROTTLE_DIR = Path(tempfile.gettempdir()) / "auto_audit_throttle"
_THROTTLE_DIR.mkdir(exist_ok=True)


def should_alert(key: str, ttl_seconds: int = 3600) -> bool:
    """Return True if we haven't alerted for `key` in the last `ttl_seconds`.

    Side-effect: if True, stamps `key` so the next call within TTL returns False.
    """
    fp = _THROTTLE_DIR / f"{key}.ts"
    now = time.time()
    if fp.exists():
        try:
            last = float(fp.read_text(encoding="utf-8").strip() or "0")
            if now - last < ttl_seconds:
                return False
        except Exception:
            pass
    try:
        fp.write_text(str(now), encoding="utf-8")
    except Exception:
        pass
    return True


# ────────────────────────── Query helper ──────────────────────────
def q(dsn: str, sql: str, params: tuple = (), *, timeout: int = 10) -> list[dict]:
    """Lightweight RealDictCursor query; returns [] on error."""
    if not dsn:
        return []
    try:
        with psycopg2.connect(dsn, connect_timeout=timeout) as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(sql, params)
                if cur.description:
                    return [dict(r) for r in cur.fetchall()]
                return []
    except Exception as e:
        print(f"[auto_audit] q failed: {e}")
        return [{"_error": str(e)}]


# ────────────────────────── HTTP probe ──────────────────────────
def http_probe(url: str, *, timeout: int = 10) -> tuple[int, float, str]:
    """Returns (status_code, latency_ms, error_or_body_prefix).

    status_code = 0 on connection error.
    """
    import urllib.request
    import urllib.error
    t0 = time.time()
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read(512).decode("utf-8", errors="replace")
            return r.status, (time.time() - t0) * 1000, body
    except urllib.error.HTTPError as e:
        return e.code, (time.time() - t0) * 1000, str(e)
    except Exception as e:
        return 0, (time.time() - t0) * 1000, f"{type(e).__name__}: {e}"


# ────────────────────────── LLM probe ──────────────────────────
def llm_probe(name: str, env_key: str, url: str,
              *, timeout: int = 8) -> tuple[bool, float, str]:
    """Lightweight key-validation probe. Free providers: GET /models with bearer."""
    import urllib.request
    import urllib.error
    key = os.environ.get(env_key, "").strip()
    if not key:
        return False, 0.0, "no key"
    t0 = time.time()
    try:
        # gemini uses x-goog-api-key, everyone else uses Authorization
        headers = {}
        u = url
        if name == "gemini":
            u = f"{url}?key={key}"
        else:
            headers["Authorization"] = f"Bearer {key}"
        req = urllib.request.Request(u, headers=headers, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status < 400, (time.time() - t0) * 1000, str(r.status)
    except urllib.error.HTTPError as e:
        # 401 = expired key, still gives us a signal
        return False, (time.time() - t0) * 1000, f"HTTP {e.code}"
    except Exception as e:
        return False, (time.time() - t0) * 1000, f"{type(e).__name__}"


__all__ = [
    "DSNS", "BOTS", "LLM_PROVIDERS",
    "admin_notify", "ensure_schema", "record_metric", "last_metric",
    "should_alert", "q", "http_probe", "llm_probe",
]
