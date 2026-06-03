"""bot_user_tracker.py — централизованный трекинг пользователей всех ботов.

Записывает в таблицу `bot_users` в общей resale-DB (через RESALE_DATABASE_URL).
Все 6 ботов экосистемы (hub/resale/channel/analytics/roi/lead) пишут сюда —
hub-bot потом отдаёт сводный /stats.

Дизайн:
- UPSERT через ON CONFLICT (idempotent)
- Background thread (daemon) — никогда не блокирует /start
- Никогда не бросает исключения — analytics не должен ронять бота
- Своё psycopg2-соединение на каждый вызов (нет пула — оверкилл при /start)

API:
    from bot_user_tracker import track_user_async
    track_user_async(
        telegram_id=update.effective_user.id,
        username=update.effective_user.username,
        first_name=update.effective_user.first_name,
        language=update.effective_user.language_code,
        action="message"  # или "callback"
    )

ENV vars:
    RESALE_DATABASE_URL (или DATABASE_URL) — DSN общей DB
    BOT_NAME           — имя бота для записи: hub|resale|channel|analytics|roi|lead
"""
import os
import threading


def _get_db_url() -> str:
    return (
        os.environ.get("RESALE_DATABASE_URL")
        or os.environ.get("DATABASE_URL")
        or ""
    )


def _get_bot_name() -> str:
    return os.environ.get("BOT_NAME", "unknown")


def track_user(telegram_id: int,
               username: str = None,
               first_name: str = None,
               language: str = None,
               action: str = "message") -> None:
    """UPSERT user в bot_users. Silent fail."""
    if not telegram_id:
        return
    url = _get_db_url()
    if not url:
        return
    try:
        import psycopg2  # type: ignore
    except Exception:
        return
    bot_name = _get_bot_name()
    counter_col = "total_callbacks" if action == "callback" else "total_messages"
    try:
        conn = psycopg2.connect(url, connect_timeout=5)
        try:
            with conn.cursor() as cur:
                # Создать таблицу если её нет (идемпотентно, копеечно)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS bot_users (
                      id BIGSERIAL PRIMARY KEY,
                      telegram_id BIGINT NOT NULL,
                      bot_name TEXT NOT NULL,
                      first_seen TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                      last_seen TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                      language TEXT,
                      username TEXT,
                      first_name TEXT,
                      is_bot BOOLEAN DEFAULT false,
                      total_messages INT DEFAULT 0,
                      total_callbacks INT DEFAULT 0,
                      UNIQUE(telegram_id, bot_name)
                    )
                """)
                cur.execute(f"""
                    INSERT INTO bot_users (telegram_id, bot_name, username, first_name, language, last_seen, {counter_col})
                    VALUES (%s, %s, %s, %s, %s, NOW(), 1)
                    ON CONFLICT (telegram_id, bot_name) DO UPDATE SET
                      last_seen = NOW(),
                      username = COALESCE(EXCLUDED.username, bot_users.username),
                      first_name = COALESCE(EXCLUDED.first_name, bot_users.first_name),
                      language = COALESCE(EXCLUDED.language, bot_users.language),
                      {counter_col} = bot_users.{counter_col} + 1
                """, (telegram_id, bot_name, username, first_name, language))
            conn.commit()
        finally:
            try: conn.close()
            except Exception: pass
    except Exception as e:
        try: print(f"[bot_user_tracker] fail bot={bot_name} uid={telegram_id}: {e}", flush=True)
        except Exception: pass


def get_persisted_lang(telegram_id: int, bot_name: str = None) -> str:
    """LANG_FIX: read this user's last persisted language for `bot_name` from
    bot_users.language. Returns None on miss/error so caller can fall back to
    a default. Synchronous (caller is expected to cache the result).
    If bot_name is None, uses BOT_NAME env (same default as track_user)."""
    url = _get_db_url()
    if not url:
        return None
    if not bot_name:
        bot_name = _get_bot_name()
    try:
        import psycopg2
        conn = psycopg2.connect(url, connect_timeout=5)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT language FROM bot_users WHERE telegram_id=%s AND bot_name=%s",
                    (telegram_id, bot_name),
                )
                row = cur.fetchone()
                if not row:
                    return None
                return row[0]
        finally:
            try: conn.close()
            except Exception: pass
    except Exception:
        return None


def track_user_async(telegram_id: int,
                     username: str = None,
                     first_name: str = None,
                     language: str = None,
                     action: str = "message") -> None:
    """Запускает track_user в daemon-thread — не блокирует main flow."""
    try:
        t = threading.Thread(
            target=track_user,
            kwargs={
                "telegram_id": telegram_id,
                "username": username,
                "first_name": first_name,
                "language": language,
                "action": action,
            },
            daemon=True,
        )
        t.start()
    except Exception:
        pass
