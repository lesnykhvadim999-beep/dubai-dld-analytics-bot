"""cross_bot_utm.py — shared helper для cross-bot UTM tracking (v55).

Логирует каждый /start <payload> переход между ботами экосистемы в таблицу
`cross_bot_jumps` в resale-DB (общая аналитическая БД).

Этот файл КОПИРУЕТСЯ один-в-один в каждый бот экосистемы:
  hub-bot, channel-bot, resale-bot, roi-bot, lead-bot, analytics-bot.

Никогда не бросает исключения — analytics не должен ломать UX /start.

Поддерживаемые форматы payload:
  • легаси: from_<botname>[_<id>]         (from_hub / from_offplan_3190)
  • новый extended: <legacy>_utm_<source>_<campaign>[_<content>]
    пример: from_offplan_3190_utm_resale_card_share
  • legacy lead-bot префиксы: resale-NNN, proj-DEV-LOC, roi-D, area-NAME, bld-NAME

Public API:
  log_jump(to_bot: str, user_id: int, payload: str) -> None
  parse_payload(payload: str) -> tuple[from_bot, utm_source, utm_campaign, utm_content]
"""
import os
import threading

# Cache конн-пула не делаем — Telegram /start редок относительно SQL.
# Ленивая инициализация psycopg2, лазер-safe.

_LOG_LOCK = threading.Lock()


def _get_db_url() -> str:
    """В resale-bot это DATABASE_URL, в остальных ботах должен быть выставлен
    RESALE_DATABASE_URL → же URL что у resale-bot DB на Railway."""
    return (os.environ.get("RESALE_DATABASE_URL")
            or os.environ.get("DATABASE_URL")
            or "")


def parse_payload(payload: str):
    """Парсит deep-link payload → (from_bot, utm_source, utm_campaign, utm_content).

    Никогда не бросает: на любой ошибке вернёт ('unknown', None, None, None).
    """
    if not payload:
        return ("unknown", None, None, None)
    try:
        p = payload.strip()
        utm_source = utm_campaign = utm_content = None
        if "_utm_" in p:
            head, _, tail = p.partition("_utm_")
            parts = tail.split("_")
            if len(parts) >= 1: utm_source   = parts[0] or None
            if len(parts) >= 2: utm_campaign = parts[1] or None
            if len(parts) >= 3: utm_content  = "_".join(parts[2:]) or None
            p = head

        from_bot = "unknown"
        if p.startswith("from_"):
            tail = p[len("from_"):]
            first = tail.split("_", 1)[0] if tail else ""
            alias = {"offplan": "channel", "channel": "channel",
                     "resale": "resale", "hub": "hub", "roi": "roi",
                     "analytics": "analytics", "dld": "analytics",
                     "lead": "lead"}
            from_bot = alias.get(first.lower(), first.lower() or "unknown")
        elif p.startswith("proj-") or p.startswith("proj_") or p.startswith("proj|"):
            from_bot = "channel"
        elif p.startswith("resale-") or p.startswith("resale_"):
            from_bot = "resale"
        elif p.startswith("roi-") or p.startswith("roi_"):
            from_bot = "roi"
        elif p.startswith("area-") or p.startswith("area_") or \
             p.startswith("bld-") or p.startswith("bld_"):
            from_bot = "analytics"
        return (from_bot, utm_source, utm_campaign, utm_content)
    except Exception:
        return ("unknown", None, None, None)


def log_jump(to_bot: str, user_id: int, payload: str = "") -> None:
    """Логирует /start <payload> в cross_bot_jumps. Silent fail."""
    if not payload and not user_id:
        return
    url = _get_db_url()
    if not url:
        return
    try:
        import psycopg2  # type: ignore
    except Exception:
        return
    try:
        from_bot, src, camp, content = parse_payload(payload)
        with _LOG_LOCK:
            conn = psycopg2.connect(url, connect_timeout=5)
            try:
                with conn.cursor() as cur:
                    # сначала пробуем — таблица может не существовать в чужой
                    # БД; в этом случае CREATE TABLE IF NOT EXISTS.
                    cur.execute("""
                        CREATE TABLE IF NOT EXISTS cross_bot_jumps (
                          id           BIGSERIAL PRIMARY KEY,
                          from_bot     TEXT,
                          to_bot       TEXT,
                          user_id      BIGINT,
                          payload      TEXT,
                          utm_source   TEXT,
                          utm_campaign TEXT,
                          utm_content  TEXT,
                          jumped_at    TIMESTAMPTZ DEFAULT NOW()
                        );
                    """)
                    cur.execute("""
                        INSERT INTO cross_bot_jumps
                            (from_bot, to_bot, user_id, payload,
                             utm_source, utm_campaign, utm_content)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """, (from_bot, to_bot, user_id, payload,
                          src, camp, content))
                conn.commit()
            finally:
                conn.close()
    except Exception as e:
        # Silent fail — не ломаем UX
        try: print(f"[cbj] log fail to={to_bot} uid={user_id}: {e}", flush=True)
        except Exception: pass


def log_jump_async(to_bot: str, user_id: int, payload: str = "") -> None:
    """Background thread version — для async ботов (aiogram), чтобы не блокировать."""
    try:
        t = threading.Thread(
            target=log_jump,
            args=(to_bot, user_id, payload),
            daemon=True,
        )
        t.start()
    except Exception:
        pass
