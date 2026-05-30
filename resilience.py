"""resilience.py — per-bot process-level resilience helpers.

Drop-in module. Each bot's main file imports `start_resilience(bot_name)`
right at the top of `main()` (before polling starts). It is also exported
via the same name in `shared/` so we can keep one canonical version.

What it provides:
  - self_smoke_test(bot_name) -> bool       — fast startup checks (token,
    DB, env vars). Skipped if SKIP_SMOKE_TEST=1. <30 sec total.
  - start_watchdog_thread(bot_name)         — daemon thread; every 30 sec
    calls getMe, touches /tmp/<bot>.alive. 3 consecutive failures → sys.exit(1)
    so Railway restarts the container.
  - check_crash_loop(bot_name)              — reads /tmp/<bot>_crash_count.txt;
    if >=3 restarts within 5 min, either notifies @vadim_admin_bot or, if
    AUTO_ROLLBACK_ENABLED=1, runs `git revert HEAD --no-edit && git push`.
  - start_resilience(bot_name)              — convenience: runs all three
    in the right order. Returns True if startup is allowed to continue.

All functions are defensive: any internal failure prints a warning and
returns gracefully so they cannot block a healthy bot from starting.

Env flags:
  SKIP_SMOKE_TEST=1            — skip startup smoke test
  SKIP_BOT_WATCHDOG=1          — do not spawn the watchdog thread
  AUTO_ROLLBACK_ENABLED=1      — opt-in to git-revert on crash loop
  BOT_WATCHDOG_INTERVAL_SEC=30 — watchdog poll interval
  BOT_WATCHDOG_FAILURES=3      — consecutive failures before exit(1)
  CRASH_LOOP_WINDOW_SEC=300    — sliding window for crash counter (default 5 min)
  CRASH_LOOP_THRESHOLD=3       — restarts within window that count as a loop
  ADMIN_BOT_TOKEN, ADMIN_CHAT_ID — Telegram alert target
"""
from __future__ import annotations
import os
import sys
import time
import json
import threading
import tempfile
import subprocess
import urllib.request
import urllib.parse


# ── paths ────────────────────────────────────────────────────────────────────

def _state_dir() -> str:
    if os.path.isdir("/tmp"):
        return "/tmp"
    return tempfile.gettempdir()


def heartbeat_path(bot_name: str) -> str:
    return os.path.join(_state_dir(), f"{bot_name}.alive")


def crash_counter_path(bot_name: str) -> str:
    return os.path.join(_state_dir(), f"{bot_name}_crash_count.txt")


# ── token discovery ──────────────────────────────────────────────────────────

_TOKEN_ENV_CANDIDATES = (
    "BOT_TOKEN",
    "TELEGRAM_BOT_TOKEN",
    "RESALE_BOT_TOKEN",
    "HUB_BOT_TOKEN",
    "ROI_BOT_TOKEN",
    "LEAD_BOT_TOKEN",
    "CHANNEL_BOT_TOKEN",
    "ANALYTICS_BOT_TOKEN",
    "CURRENCY_BOT_TOKEN",
)


def _discover_token() -> str:
    for k in _TOKEN_ENV_CANDIDATES:
        v = os.environ.get(k, "").strip()
        if v:
            return v
    return ""


# ── admin notify (urllib only — no external deps) ────────────────────────────

def _notify(text: str) -> bool:
    token = os.environ.get("ADMIN_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("ADMIN_CHAT_ID", "353806371").strip()
    if not token:
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": chat_id,
        "text": text[:4090],
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }).encode("utf-8")
    try:
        req = urllib.request.Request(url, data=data, method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except Exception as e:
        print(f"[resilience] notify failed: {e}", flush=True)
        return False


# ── smoke test ───────────────────────────────────────────────────────────────

def _check_get_me(token: str, timeout: int) -> tuple[bool, str]:
    url = f"https://api.telegram.org/bot{token}/getMe"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            body = json.loads(r.read().decode("utf-8"))
        if body.get("ok"):
            uname = body.get("result", {}).get("username", "?")
            return True, f"getMe OK (@{uname})"
        return False, f"getMe not ok: {body}"
    except Exception as e:
        return False, f"getMe exception: {type(e).__name__}: {e}"


def _check_db(timeout: int) -> tuple[bool, str]:
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


def self_smoke_test(bot_name: str, timeout_sec: int | None = None) -> bool:
    """Run startup smoke test. Returns True if all critical checks passed."""
    if os.environ.get("SKIP_SMOKE_TEST", "0") == "1":
        print(f"[smoke {bot_name}] SKIP_SMOKE_TEST=1 — skipping", flush=True)
        return True

    timeout = timeout_sec if timeout_sec is not None else int(
        os.environ.get("SMOKE_TIMEOUT_SEC", "10")
    )
    token = _discover_token()
    if not token:
        print(f"[smoke {bot_name}] FAIL: no Telegram token env found", flush=True)
        return False

    t0 = time.time()
    me_ok, me_msg = _check_get_me(token, timeout)
    print(f"[smoke {bot_name}] {'OK ' if me_ok else 'FAIL'} getMe: {me_msg}", flush=True)

    db_ok, db_msg = _check_db(timeout)
    print(f"[smoke {bot_name}] {'OK ' if db_ok else 'FAIL'} db: {db_msg}", flush=True)

    elapsed = time.time() - t0
    overall = me_ok and db_ok
    print(f"[smoke {bot_name}] done in {elapsed:.1f}s — {'PASS' if overall else 'FAIL'}",
          flush=True)
    return overall


# ── watchdog thread ──────────────────────────────────────────────────────────

_watchdog_started = False
_watchdog_lock = threading.Lock()


def _watchdog_loop(bot_name: str, token: str, interval: int, max_failures: int) -> None:
    hb_path = heartbeat_path(bot_name)
    failures = 0
    print(f"[watchdog {bot_name}] started — interval={interval}s "
          f"max_failures={max_failures} heartbeat={hb_path}", flush=True)
    while True:
        try:
            url = f"https://api.telegram.org/bot{token}/getMe"
            with urllib.request.urlopen(url, timeout=10) as r:
                body = json.loads(r.read().decode("utf-8"))
            if body.get("ok"):
                failures = 0
                # touch heartbeat
                try:
                    with open(hb_path, "w", encoding="utf-8") as fh:
                        fh.write(str(time.time()))
                except OSError as e:
                    print(f"[watchdog {bot_name}] heartbeat write failed: {e}", flush=True)
            else:
                failures += 1
                print(f"[watchdog {bot_name}] getMe not ok ({failures}/{max_failures}): {body}",
                      flush=True)
        except Exception as e:
            failures += 1
            print(f"[watchdog {bot_name}] getMe error ({failures}/{max_failures}): "
                  f"{type(e).__name__}: {e}", flush=True)

        if failures >= max_failures:
            msg = (f"🚨 <b>Watchdog: bot dead</b>\n"
                   f"bot: <code>{bot_name}</code>\n"
                   f"consecutive getMe failures: {failures}\n"
                   f"exiting (Railway will restart container)")
            print(f"[watchdog {bot_name}] {failures} failures — exiting(1)", flush=True)
            try:
                _notify(msg)
            except Exception:
                pass
            # hard exit — Railway restartPolicy will redeploy
            os._exit(1)

        time.sleep(interval)


def start_watchdog_thread(bot_name: str) -> bool:
    """Start the in-process watchdog daemon thread. Idempotent."""
    global _watchdog_started
    if os.environ.get("SKIP_BOT_WATCHDOG", "0") == "1":
        print(f"[watchdog {bot_name}] SKIP_BOT_WATCHDOG=1 — not starting", flush=True)
        return False
    with _watchdog_lock:
        if _watchdog_started:
            return True
        token = _discover_token()
        if not token:
            print(f"[watchdog {bot_name}] no token env found — not starting", flush=True)
            return False
        interval = int(os.environ.get("BOT_WATCHDOG_INTERVAL_SEC", "30"))
        max_failures = int(os.environ.get("BOT_WATCHDOG_FAILURES", "3"))
        t = threading.Thread(
            target=_watchdog_loop,
            args=(bot_name, token, interval, max_failures),
            name=f"watchdog-{bot_name}",
            daemon=True,
        )
        t.start()
        _watchdog_started = True
        return True


# ── crash-loop counter / auto-rollback ───────────────────────────────────────

def _git_revert_head() -> tuple[bool, str]:
    """Run `git revert HEAD --no-edit && git push origin HEAD`."""
    try:
        env = os.environ.copy()
        env.setdefault("GIT_TERMINAL_PROMPT", "0")
        # Some CI envs lack user identity — set a fallback.
        env.setdefault("GIT_AUTHOR_NAME", "auto-rollback")
        env.setdefault("GIT_AUTHOR_EMAIL", "auto-rollback@vadim-realty.local")
        env.setdefault("GIT_COMMITTER_NAME", "auto-rollback")
        env.setdefault("GIT_COMMITTER_EMAIL", "auto-rollback@vadim-realty.local")

        r1 = subprocess.run(
            ["git", "revert", "HEAD", "--no-edit"],
            capture_output=True, text=True, timeout=60, env=env,
        )
        if r1.returncode != 0:
            return False, f"git revert failed: {r1.stderr.strip()}"
        r2 = subprocess.run(
            ["git", "push", "origin", "HEAD"],
            capture_output=True, text=True, timeout=120, env=env,
        )
        if r2.returncode != 0:
            return False, f"git push failed: {r2.stderr.strip()}"
        return True, "revert pushed"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def check_crash_loop(bot_name: str) -> None:
    """Update crash counter file and react if a crash loop is detected.

    Counter file format (json): {"count": N, "first_at": ts, "last_at": ts}

    Logic:
      - if last_at older than WINDOW_SEC → reset
      - else increment
      - if count >= THRESHOLD:
          AUTO_ROLLBACK_ENABLED=1  → revert + push, reset counter
          else                     → notify admin only
    """
    path = crash_counter_path(bot_name)
    window = int(os.environ.get("CRASH_LOOP_WINDOW_SEC", "300"))
    threshold = int(os.environ.get("CRASH_LOOP_THRESHOLD", "3"))
    auto_rollback = os.environ.get("AUTO_ROLLBACK_ENABLED", "0") == "1"
    now = time.time()

    state = {"count": 0, "first_at": now, "last_at": now}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            prev = json.loads(fh.read() or "{}")
        if isinstance(prev, dict):
            last_at = float(prev.get("last_at", 0))
            if now - last_at <= window:
                state["count"] = int(prev.get("count", 0)) + 1
                state["first_at"] = float(prev.get("first_at", now))
            else:
                # window expired — fresh start
                state["count"] = 1
                state["first_at"] = now
        state["last_at"] = now
    except FileNotFoundError:
        state["count"] = 1
        state["first_at"] = now
        state["last_at"] = now
    except (OSError, ValueError, json.JSONDecodeError) as e:
        print(f"[crashloop {bot_name}] read failed ({e}) — resetting", flush=True)
        state = {"count": 1, "first_at": now, "last_at": now}

    print(f"[crashloop {bot_name}] restart count={state['count']} "
          f"window={int(now - state['first_at'])}s/{window}s "
          f"threshold={threshold} auto_rollback={auto_rollback}", flush=True)

    if state["count"] >= threshold:
        if auto_rollback:
            _notify(f"🚨 <b>Crash loop detected</b>\nbot: <code>{bot_name}</code>\n"
                    f"{state['count']} restarts in {int(now - state['first_at'])}s — "
                    f"reverting last commit")
            ok, msg = _git_revert_head()
            if ok:
                _notify(f"✅ <b>Auto-rollback complete</b>\nbot: <code>{bot_name}</code>\n"
                        f"{msg} — Railway will redeploy the revert")
                # reset counter so the reverted build starts clean
                state = {"count": 0, "first_at": now, "last_at": now}
            else:
                _notify(f"❌ <b>Auto-rollback FAILED</b>\nbot: <code>{bot_name}</code>\n"
                        f"<code>{msg}</code>\nmanual intervention required")
        else:
            _notify(f"🚨 <b>Crash loop detected</b>\nbot: <code>{bot_name}</code>\n"
                    f"{state['count']} restarts in {int(now - state['first_at'])}s\n"
                    f"<i>AUTO_ROLLBACK_ENABLED not set — manual fix required</i>")

    # persist counter
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(state))
    except OSError as e:
        print(f"[crashloop {bot_name}] write failed: {e}", flush=True)


# ── orchestrator ─────────────────────────────────────────────────────────────

def start_resilience(bot_name: str, *, run_smoke: bool = True,
                     run_crash_check: bool = True,
                     run_watchdog: bool = True) -> bool:
    """Run the full resilience sequence at startup.

    Returns False ONLY when the smoke test failed AND it was enabled — the
    caller should `sys.exit(1)` in that case. Watchdog and crash-loop checks
    never block startup.
    """
    print(f"[resilience {bot_name}] starting…", flush=True)
    if run_crash_check:
        try:
            check_crash_loop(bot_name)
        except Exception as e:
            print(f"[resilience {bot_name}] crash_check error: {e}", flush=True)

    smoke_ok = True
    if run_smoke:
        try:
            smoke_ok = self_smoke_test(bot_name)
        except Exception as e:
            print(f"[resilience {bot_name}] smoke_test crashed: {e} — treating as pass",
                  flush=True)
            smoke_ok = True

    if run_watchdog:
        try:
            start_watchdog_thread(bot_name)
        except Exception as e:
            print(f"[resilience {bot_name}] watchdog start error: {e}", flush=True)

    print(f"[resilience {bot_name}] ready (smoke={'OK' if smoke_ok else 'FAIL'})",
          flush=True)
    return smoke_ok


if __name__ == "__main__":
    # CLI sanity test: `python resilience.py <bot_name>`
    name = sys.argv[1] if len(sys.argv) > 1 else "test-bot"
    ok = self_smoke_test(name)
    sys.exit(0 if ok else 1)
