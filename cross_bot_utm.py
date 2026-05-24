"""cross_bot_utm.py — shared helper для cross-bot UTM tracking (v56).

Логирует каждый /start <payload> переход между ботами экосистемы в таблицу
`cross_bot_jumps` в resale-DB (общая аналитическая БД).

Этот файл КОПИРУЕТСЯ один-в-один в каждый бот экосистемы:
  hub-bot, channel-bot, resale-bot, roi-bot, lead-bot, analytics-bot.

Никогда не бросает исключения — analytics не должен ломать UX /start.

v56 (2026-05-24): debug logger + fallback DSN (cross_bot_jumps был пустой)
  • root cause: при использовании postgres.railway.internal из других
    Railway проектов хост не резолвится → silent ConnectionError. Сейчас
    автодетект internal-host и подмена на public proxy через
    RESALE_DATABASE_URL_PUBLIC env var.
  • silent fail заменён на verbose [cbj] логи по всем веткам: пустой DSN,
    отсутствующий psycopg2, ConnectError, SQL-fail, success.

Поддерживаемые форматы payload:
  • легаси: from_<botname>[_<id>]         (from_hub / from_offplan_3190)
  • новый extended: <legacy>_utm_<source>_<campaign>[_<content>]
    пример: from_offplan_3190_utm_resale_card_share
  • legacy lead-bot префиксы: resale-NNN, proj-DEV-LOC, roi-D, area-NAME, bld-NAME

Public API:
  log_jump(to_bot: str, user_id: int, payload: str) -> None
  log_jump_async(to_bot: str, user_id: int, payload: str) -> None
  parse_payload(payload: str) -> tuple[from_bot, utm_source, utm_campaign, utm_content]

Env vars:
  RESALE_DATABASE_URL        — DSN общей analytics-DB (обязательно во всех
                               не-resale ботах). Может быть internal или public.
  RESALE_DATABASE_URL_PUBLIC — fallback public-proxy DSN, используется если
                               main DSN содержит .railway.internal и connect fail.
  DATABASE_URL               — fallback (только для resale-bot самого).
"""
import os
import sys
import threading
import traceback

_LOG_LOCK = threading.Lock()


def _log(msg: str) -> None:
    """Verbose debug logger, всегда на stderr + flush."""
    try:
        print(f"[cbj] {msg}", file=sys.stderr, flush=True)
    except Exception:
        pass


def _get_db_url() -> str:
    """В resale-bot это DATABASE_URL, в остальных ботах должен быть выставлен
    RESALE_DATABASE_URL → же URL что у resale-bot DB на Railway."""
    return (os.environ.get("RESALE_DATABASE_URL")
            or os.environ.get("DATABASE_URL")
            or "")


def _get_db_url_public() -> str:
    """Public proxy DSN для cross-Railway-project доступа."""
    return os.environ.get("RESALE_DATABASE_URL_PUBLIC", "")


def _is_internal_host(dsn: str) -> bool:
    return ".railway.internal" in (dsn or "")


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


def _try_insert(dsn: str, dsn_label: str, from_bot, to_bot, user_id,
                payload, src, camp, content) -> bool:
    """Возвращает True при успехе. Логирует все ошибки."""
    try:
        import psycopg2  # type: ignore
    except Exception as e:
        _log(f"psycopg2 import fail: {e}")
        return False
    try:
        conn = psycopg2.connect(dsn, connect_timeout=5)
    except Exception as e:
        _log(f"connect FAIL via {dsn_label}: {type(e).__name__}: {e}")
        return False
    try:
        with conn.cursor() as cur:
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
                RETURNING id
            """, (from_bot, to_bot, user_id, payload, src, camp, content))
            row = cur.fetchone()
        conn.commit()
        _log(f"OK via {dsn_label} id={row[0] if row else '?'} "
             f"from={from_bot} to={to_bot} uid={user_id} "
             f"src={src} camp={camp}")
        return True
    except Exception as e:
        _log(f"SQL FAIL via {dsn_label}: {type(e).__name__}: {e}\n"
             f"{traceback.format_exc()}")
        try: conn.rollback()
        except Exception: pass
        return False
    finally:
        try: conn.close()
        except Exception: pass


def log_jump(to_bot: str, user_id: int, payload: str = "") -> None:
    """Логирует /start <payload> в cross_bot_jumps. Verbose, но никогда не бросает."""
    if not payload and not user_id:
        return

    primary = _get_db_url()
    public  = _get_db_url_public()

    if not primary and not public:
        _log(f"SKIP no DSN env (RESALE_DATABASE_URL / DATABASE_URL / "
             f"RESALE_DATABASE_URL_PUBLIC all empty) to={to_bot} uid={user_id}")
        return

    try:
        from_bot, src, camp, content = parse_payload(payload)
    except Exception as e:
        _log(f"parse_payload fail: {e}")
        from_bot, src, camp, content = ("unknown", None, None, None)

    with _LOG_LOCK:
        # Если есть primary — пробуем его первым
        if primary:
            label = "internal" if _is_internal_host(primary) else "primary"
            if _try_insert(primary, label, from_bot, to_bot, user_id,
                           payload, src, camp, content):
                return
            # Internal не зарезолвился — fallback на public
            if _is_internal_host(primary) and public:
                _log("internal failed → fallback to public proxy")
                if _try_insert(public, "public", from_bot, to_bot, user_id,
                               payload, src, camp, content):
                    return
        elif public:
            _try_insert(public, "public-only", from_bot, to_bot, user_id,
                        payload, src, camp, content)


def log_jump_async(to_bot: str, user_id: int, payload: str = "") -> None:
    """Background thread version — для async ботов (aiogram), чтобы не блокировать."""
    try:
        t = threading.Thread(
            target=log_jump,
            args=(to_bot, user_id, payload),
            daemon=True,
            name=f"cbj-{to_bot}",
        )
        t.start()
    except Exception as e:
        _log(f"thread spawn fail: {e}")
