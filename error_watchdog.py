"""B031: Auto-incident response watchdog.

Catches handler exceptions across all 6 bots, manages maintenance mode,
notifies users with friendly "под техобслуживание" message + admin alert,
auto-recovers when errors stop, and flags long incidents (>60min) for redeploy.

Schema:
    bot_errors             — log of every exception
    bot_maintenance_state  — per-bot maintenance flag + tracked users

Usage (PTB v20):
    from error_watchdog import ptb_error_handler, watch_errors
    app.add_error_handler(ptb_error_handler)

Usage (aiogram v3) — wire inside existing @dp.errors():
    from error_watchdog import handle_aiogram_error
    @dp.errors()
    async def _global(event):
        await handle_aiogram_error(event)
        return True

Usage (raw requests sync loop):
    from error_watchdog import sync_log_error, is_maintenance, get_maintenance_message
    try:
        handle_update(...)
        record_success(uid)
    except Exception as e:
        sync_log_error(uid, type(e).__name__, str(e), traceback.format_exc(), "handle_update", str(upd)[:200])
        # send maintenance msg to user via raw _api()

Auto-recovery (called from staging-processor cron / standalone service):
    from error_watchdog import recovery_tick
    while True: recovery_tick(); time.sleep(120)
"""
from __future__ import annotations

import os
import json
import threading
import traceback
from typing import Optional, Any

DSN = (
    os.environ.get("RESALE_DATABASE_URL_PUBLIC")
    or os.environ.get("RESALE_DATABASE_URL")
    or os.environ.get("DATABASE_URL")
)
BOT_NAME = os.environ.get("BOT_NAME", "unknown")
ADMIN_CHAT_ID = int(os.environ.get("ADMIN_CHAT_ID", "353806371"))
# Use the bot's own token to message its own users; HUB token only for admin alert.
SELF_BOT_TOKEN = (
    os.environ.get("TELEGRAM_BOT_TOKEN")
    or os.environ.get("RESALE_BOT_TOKEN")
    or os.environ.get("BOT_TOKEN")
    or ""
)
ADMIN_BOT_TOKEN = os.environ.get("HUB_BOT_TOKEN") or SELF_BOT_TOKEN

_RECOVERY_NO_ERROR_WINDOW_MIN = 5
_AUTO_RESTART_AFTER_MIN = 60
_ALERT_REPEAT_MIN = 60
_maintenance_alerted: dict = {}


def _get_conn():
    import psycopg2  # lazy
    return psycopg2.connect(DSN, connect_timeout=5)


def _ensure_tables():
    if not DSN:
        return
    try:
        conn = _get_conn()
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS bot_errors (
                id BIGSERIAL PRIMARY KEY,
                bot_name TEXT NOT NULL,
                user_id BIGINT,
                error_class TEXT NOT NULL,
                error_message TEXT,
                stacktrace TEXT,
                handler_name TEXT,
                payload TEXT,
                occurred_at TIMESTAMPTZ DEFAULT NOW()
            );
            CREATE INDEX IF NOT EXISTS idx_bot_errors_occurred ON bot_errors(occurred_at DESC);
            CREATE INDEX IF NOT EXISTS idx_bot_errors_bot ON bot_errors(bot_name, occurred_at DESC);
            CREATE TABLE IF NOT EXISTS bot_maintenance_state (
                bot_name TEXT PRIMARY KEY,
                is_maintenance BOOLEAN DEFAULT false,
                started_at TIMESTAMPTZ,
                reason TEXT,
                consecutive_errors INT DEFAULT 0,
                last_recovery_check_at TIMESTAMPTZ,
                notified_users JSONB DEFAULT '[]'::jsonb
            );
            """
        )
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print(f"[watchdog] init err: {e}", flush=True)


def sync_log_error(
    user_id: Optional[int],
    error_class: str,
    error_message: str,
    stacktrace: str,
    handler_name: Optional[str] = None,
    payload: Any = None,
) -> None:
    """Log + maintenance update + admin alert. Fire-and-forget thread."""

    def _do():
        if not DSN:
            return
        try:
            conn = _get_conn()
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO bot_errors
                  (bot_name, user_id, error_class, error_message, stacktrace, handler_name, payload)
                VALUES (%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    BOT_NAME,
                    user_id,
                    (error_class or "")[:80],
                    (error_message or "")[:500],
                    (stacktrace or "")[:2000],
                    (handler_name or "")[:100],
                    str(payload or "")[:300],
                ),
            )
            cur.execute(
                """
                INSERT INTO bot_maintenance_state
                  (bot_name, is_maintenance, started_at, reason, consecutive_errors)
                VALUES (%s, true, NOW(), %s, 1)
                ON CONFLICT (bot_name) DO UPDATE SET
                  is_maintenance = true,
                  started_at = COALESCE(bot_maintenance_state.started_at, NOW()),
                  reason = EXCLUDED.reason,
                  consecutive_errors = bot_maintenance_state.consecutive_errors + 1
                """,
                (BOT_NAME, (error_message or "")[:300]),
            )
            conn.commit()
            cur.close()
            conn.close()
        except Exception as e:
            print(f"[watchdog] log_error fail: {e}", flush=True)
        # Admin alert outside DB tx (best-effort)
        try:
            _send_admin_alert(error_class, error_message, handler_name, user_id)
        except Exception:
            pass

    threading.Thread(target=_do, daemon=True).start()


def is_maintenance() -> bool:
    if not DSN:
        return False
    try:
        conn = _get_conn()
        cur = conn.cursor()
        cur.execute(
            "SELECT is_maintenance FROM bot_maintenance_state WHERE bot_name=%s",
            (BOT_NAME,),
        )
        row = cur.fetchone()
        cur.close()
        conn.close()
        return bool(row and row[0])
    except Exception:
        return False


def track_user_notified(user_id: Optional[int]) -> None:
    if not DSN or user_id is None:
        return

    def _do():
        try:
            conn = _get_conn()
            cur = conn.cursor()
            cur.execute(
                """
                UPDATE bot_maintenance_state
                   SET notified_users = COALESCE(notified_users,'[]'::jsonb) || %s::jsonb
                 WHERE bot_name = %s
                   AND NOT (COALESCE(notified_users,'[]'::jsonb) @> %s::jsonb)
                """,
                (json.dumps([user_id]), BOT_NAME, json.dumps([user_id])),
            )
            conn.commit()
            cur.close()
            conn.close()
        except Exception:
            pass

    threading.Thread(target=_do, daemon=True).start()


def record_success(user_id: Optional[int] = None) -> None:
    """A handler completed without exception. If we're in maintenance and
    last 5 min had zero errors -> exit maintenance and notify users."""
    if not DSN:
        return

    def _do():
        try:
            conn = _get_conn()
            cur = conn.cursor()
            cur.execute(
                "UPDATE bot_maintenance_state SET consecutive_errors=0 "
                "WHERE bot_name=%s",
                (BOT_NAME,),
            )
            cur.execute(
                """
                SELECT is_maintenance, started_at, notified_users
                  FROM bot_maintenance_state
                 WHERE bot_name=%s AND is_maintenance=true
                """,
                (BOT_NAME,),
            )
            row = cur.fetchone()
            if row:
                _is_m, _started, notified = row
                cur.execute(
                    """
                    SELECT COUNT(*) FROM bot_errors
                     WHERE bot_name=%s
                       AND occurred_at > NOW() - INTERVAL '%s minutes'
                    """,
                    (BOT_NAME, _RECOVERY_NO_ERROR_WINDOW_MIN),
                )
                err_count = cur.fetchone()[0]
                if err_count == 0:
                    cur.execute(
                        """
                        UPDATE bot_maintenance_state
                           SET is_maintenance=false,
                               started_at=NULL,
                               notified_users='[]'::jsonb,
                               last_recovery_check_at=NOW()
                         WHERE bot_name=%s
                        """,
                        (BOT_NAME,),
                    )
                    conn.commit()
                    _notify_recovery(notified or [])
            conn.commit()
            cur.close()
            conn.close()
        except Exception as e:
            print(f"[watchdog] record_success fail: {e}", flush=True)

    threading.Thread(target=_do, daemon=True).start()


def _send_admin_alert(error_class, msg, handler, user_id):
    if not ADMIN_BOT_TOKEN or not ADMIN_CHAT_ID:
        return
    try:
        import requests
        text = (
            f"\U0001F534 [{BOT_NAME}] {error_class}\n"
            f"user={user_id} handler={handler}\n"
            f"{(msg or '')[:300]}"
        )
        requests.post(
            f"https://api.telegram.org/bot{ADMIN_BOT_TOKEN}/sendMessage",
            json={"chat_id": ADMIN_CHAT_ID, "text": text},
            timeout=5,
        )
    except Exception:
        pass


def _notify_recovery(user_ids):
    if not user_ids or not SELF_BOT_TOKEN:
        return
    try:
        import requests
        for uid in user_ids:
            try:
                requests.post(
                    f"https://api.telegram.org/bot{SELF_BOT_TOKEN}/sendMessage",
                    json={
                        "chat_id": uid,
                        "text": "✅ Работа восстановлена! Можете пользоваться ботом.",
                    },
                    timeout=5,
                )
            except Exception:
                continue
    except Exception:
        pass


def get_maintenance_message(lang: str = "ru") -> str:
    msgs = {
        "ru": (
            "\U0001F6E0 *Технические работы*\n\n"
            "Небольшой сбой, восстановим за 5–10 минут.\n"
            "Ничего нажимать не нужно — мы сами пришлём уведомление, когда заработает. ✅"
        ),
        "en": (
            "\U0001F6E0 *Maintenance*\n\n"
            "Minor issue, fixing in 5–10 min.\n"
            "No action needed — we'll notify you when ready. ✅"
        ),
        "ar": (
            "\U0001F6E0 *أعمال صيانة*\n\n"
            "سنحل المشكلة خلال 5–10 دقائق.\n"
            "سنُعلمك عند الجاهزية. ✅"
        ),
    }
    return msgs.get(lang, msgs["en"])


# ─────────────────────────────────────────────────────────────────────────────
# PTB v20 integration
# ─────────────────────────────────────────────────────────────────────────────

async def ptb_error_handler(update, context):
    """python-telegram-bot global error handler.

    Wire via:  app.add_error_handler(ptb_error_handler)
    """
    err = getattr(context, "error", None)
    if err is None:
        return
    user_id = None
    payload = ""
    try:
        if update is not None and hasattr(update, "effective_user") and update.effective_user:
            user_id = update.effective_user.id
        if update is not None and hasattr(update, "effective_message") and update.effective_message:
            payload = (update.effective_message.text or "")[:200]
        elif update is not None and hasattr(update, "callback_query") and update.callback_query:
            payload = (update.callback_query.data or "")[:200]
    except Exception:
        pass
    tb = "".join(traceback.format_exception(type(err), err, err.__traceback__))
    sync_log_error(user_id, type(err).__name__, str(err), tb, "ptb_handler", payload)
    # Friendly reply
    try:
        if update is not None and getattr(update, "effective_message", None) is not None:
            await update.effective_message.reply_text(
                get_maintenance_message("ru"), parse_mode="Markdown"
            )
        elif update is not None and getattr(update, "callback_query", None) is not None:
            await update.callback_query.message.reply_text(
                get_maintenance_message("ru"), parse_mode="Markdown"
            )
        track_user_notified(user_id)
    except Exception:
        pass


def watch_errors(handler_fn):
    """Decorator (PTB v20 async) — wraps a single handler with error capture
    + record_success on clean exit. Optional; ptb_error_handler covers globals."""
    import functools
    import asyncio

    @functools.wraps(handler_fn)
    async def _aw(update, context, *args, **kwargs):
        user_id = None
        try:
            if hasattr(update, "effective_user") and update.effective_user:
                user_id = update.effective_user.id
        except Exception:
            pass
        try:
            result = await handler_fn(update, context, *args, **kwargs)
            record_success(user_id)
            return result
        except Exception as e:
            tb = traceback.format_exc()
            sync_log_error(
                user_id, type(e).__name__, str(e), tb,
                handler_fn.__name__, str(update)[:200],
            )
            try:
                if hasattr(update, "effective_message") and update.effective_message:
                    await update.effective_message.reply_text(
                        get_maintenance_message("ru"), parse_mode="Markdown"
                    )
                track_user_notified(user_id)
            except Exception:
                pass
            raise

    if asyncio.iscoroutinefunction(handler_fn):
        return _aw

    @functools.wraps(handler_fn)
    def _sw(*args, **kwargs):
        user_id = kwargs.get("user_id")
        try:
            result = handler_fn(*args, **kwargs)
            record_success(user_id)
            return result
        except Exception as e:
            tb = traceback.format_exc()
            sync_log_error(
                user_id, type(e).__name__, str(e), tb,
                handler_fn.__name__, str(args)[:200],
            )
            raise

    return _sw


# ─────────────────────────────────────────────────────────────────────────────
# aiogram v3 integration
# ─────────────────────────────────────────────────────────────────────────────

async def handle_aiogram_error(event):
    """Call inside your existing @dp.errors() handler.

    Logs to bot_errors, sets maintenance, sends admin alert, replies to user."""
    try:
        exc = getattr(event, "exception", None)
        upd = getattr(event, "update", None)
        user_id = None
        handler = "aiogram"
        payload = ""
        target = None
        if upd is not None:
            if getattr(upd, "message", None) is not None:
                m = upd.message
                user_id = m.from_user.id if m.from_user else None
                payload = (m.text or "")[:200]
                handler = "message_handler"
                target = m
            elif getattr(upd, "callback_query", None) is not None:
                cq = upd.callback_query
                user_id = cq.from_user.id if cq.from_user else None
                payload = (cq.data or "")[:200]
                handler = "callback_handler"
                target = cq.message
        tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        sync_log_error(
            user_id,
            type(exc).__name__ if exc else "UnknownError",
            str(exc) if exc else "",
            tb,
            handler,
            payload,
        )
        if target is not None:
            try:
                await target.answer(get_maintenance_message("ru"), parse_mode="Markdown")
            except Exception:
                try:
                    await target.reply(get_maintenance_message("ru"), parse_mode="Markdown")
                except Exception:
                    pass
            track_user_notified(user_id)
    except Exception as e:
        print(f"[watchdog] handle_aiogram_error fail: {e}", flush=True)


# ─────────────────────────────────────────────────────────────────────────────
# Auto-recovery cron tick (run from staging-processor or standalone)
# ─────────────────────────────────────────────────────────────────────────────

def recovery_tick():
    """Per-tick: flag bots stuck >60min for redeploy. Logs hint to stdout.

    Actual redeploy command must be wired in the host service (Railway CLI
    or webhook), since this module doesn't know service IDs.
    """
    if not DSN:
        return []
    out = []
    try:
        conn = _get_conn()
        cur = conn.cursor()
        cur.execute(
            f"""
            SELECT bot_name, started_at FROM bot_maintenance_state
             WHERE is_maintenance=true
               AND started_at < NOW() - INTERVAL '{_AUTO_RESTART_AFTER_MIN} minutes'
            """
        )
        rows = cur.fetchall() or []
        for bot, started in rows:
            msg = (
                f"[recovery] {bot} stuck >{_AUTO_RESTART_AFTER_MIN} min "
                f"(since {started}) — Railway redeploy recommended"
            )
            print(msg, flush=True)
            out.append((bot, started))
            # Best-effort admin alert — не чаще 1 раза в час на бота
            import time as _time
            now_ts = _time.time()
            last_sent = _maintenance_alerted.get(bot, 0)
            if now_ts - last_sent >= _ALERT_REPEAT_MIN * 60:
                _maintenance_alerted[bot] = now_ts
                try:
                    import requests
                    if ADMIN_BOT_TOKEN and ADMIN_CHAT_ID:
                        requests.post(
                            f"https://api.telegram.org/bot{ADMIN_BOT_TOKEN}/sendMessage",
                            json={
                                "chat_id": ADMIN_CHAT_ID,
                                "text": f"⚠️ {bot} в maintenance >{_AUTO_RESTART_AFTER_MIN} мин. Нужен redeploy.",
                            },
                            timeout=5,
                        )
                except Exception:
                    pass
        cur.close()
        conn.close()
    except Exception as e:
        print(f"[watchdog] recovery_tick fail: {e}", flush=True)
    return out


# Init schema on first import
_ensure_tables()
