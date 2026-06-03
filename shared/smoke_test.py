"""Generic smoke test template — bots adapt this for their own startup probe.

Verifies basics before polling starts (fast, <30 sec):
  1. getMe to the Telegram Bot API (token alive, network OK)
  2. DB connectivity if DATABASE_URL is set
  3. Critical env vars present

Exit 0 on success, 1 on failure. Each bot typically embeds an equivalent
`_self_smoke_test()` directly in its main file via `resilience.self_smoke_test`.

Env:
  BOT_TOKEN | TELEGRAM_BOT_TOKEN | RESALE_BOT_TOKEN  — Telegram token (first found)
  DATABASE_URL                                       — optional; verified if present
  SMOKE_REQUIRED_ENVS                                — comma-separated extras
  SMOKE_TIMEOUT_SEC                                  — per-check timeout (default 10)
"""
from __future__ import annotations
import os
import sys
import json
import urllib.request


def _pick_token() -> tuple[str, str]:
    for k in ("BOT_TOKEN", "TELEGRAM_BOT_TOKEN", "RESALE_BOT_TOKEN"):
        v = os.environ.get(k, "").strip()
        if v:
            return k, v
    return "", ""


def check_get_me(token: str, timeout: int) -> tuple[bool, str]:
    url = f"https://api.telegram.org/bot{token}/getMe"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            body = json.loads(r.read().decode("utf-8"))
        if body.get("ok"):
            uname = body.get("result", {}).get("username", "?")
            return True, f"getMe OK ({uname})"
        return False, f"getMe not ok: {body}"
    except Exception as e:
        return False, f"getMe exception: {type(e).__name__}: {e}"


def check_db(timeout: int) -> tuple[bool, str]:
    dsn = os.environ.get("DATABASE_URL", "").strip()
    if not dsn:
        return True, "DATABASE_URL not set — skipped"
    try:
        import psycopg2  # type: ignore
    except Exception:
        return True, "psycopg2 not installed — skipped"
    try:
        conn = psycopg2.connect(dsn, connect_timeout=timeout)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
        finally:
            conn.close()
        return True, "DB ping OK"
    except Exception as e:
        return False, f"DB ping failed: {type(e).__name__}: {e}"


def check_required_envs() -> tuple[bool, str]:
    raw = os.environ.get("SMOKE_REQUIRED_ENVS", "").strip()
    if not raw:
        return True, "no extra envs required"
    missing = [k.strip() for k in raw.split(",") if k.strip() and not os.environ.get(k.strip())]
    if missing:
        return False, f"missing envs: {missing}"
    return True, "all required envs present"


def run(verbose: bool = True) -> bool:
    timeout = int(os.environ.get("SMOKE_TIMEOUT_SEC", "10"))
    tok_name, token = _pick_token()
    if not token:
        if verbose:
            print("[smoke] FAIL: no Telegram token env (BOT_TOKEN / TELEGRAM_BOT_TOKEN / RESALE_BOT_TOKEN)")
        return False
    if verbose:
        print(f"[smoke] using token from {tok_name}")
    checks = [
        ("getMe", check_get_me(token, timeout)),
        ("db", check_db(timeout)),
        ("envs", check_required_envs()),
    ]
    ok = True
    for name, (passed, msg) in checks:
        if verbose:
            mark = "OK " if passed else "FAIL"
            print(f"[smoke] {mark} {name}: {msg}")
        if not passed:
            ok = False
    return ok


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
