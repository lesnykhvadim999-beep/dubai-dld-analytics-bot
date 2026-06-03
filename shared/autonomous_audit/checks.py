"""PHASE BN N4 — Audit checks registry.

Каждый check возвращает list[Finding]. Finding — dict с обязательными ключами:
  severity        : CRITICAL / HIGH / MEDIUM / LOW
  category        : коротко, e.g. "heartbeat" / "schema" / "brand"
  description     : человеческое описание
  affected_files  : list[str] — relative paths (можно пустой)

Все check-функции принимают (dsn: str) — DSN основной resale БД. Внутри сами
ходят в другие DSN через env, если нужно. Никаких внешних HTTP-вызовов: cheap
и offline-safe; запускаются часто и не должны падать. Любая ошибка → одна
Finding severity=LOW description="check crashed: <e>".

DSN env order (read directly via os.environ — DSN env vars only):
  RESALE_DATABASE_URL  → resale DB
  LIVE_DATABASE_URL    → live DB (dld_transactions_full, audit_log, ...)
  INTELLIGENCE_DATABASE_URL → meta-learning, agent_bus, ...
  DATABASE_URL         → fallback
"""
from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Callable, TypedDict

try:
    import psycopg2  # type: ignore
    import psycopg2.extras  # type: ignore
    _PG_OK = True
except Exception:  # pragma: no cover
    _PG_OK = False

PROJECT_ROOT = Path(r"C:\Projects")
SHARED_ROOT = PROJECT_ROOT / "shared"


class Finding(TypedDict, total=False):
    severity: str
    category: str
    description: str
    affected_files: list[str]


# ── DSN helpers ───────────────────────────────────────────────────────────
def _dsn_resale() -> str:
    return (os.environ.get("RESALE_DATABASE_URL_PUBLIC", "")
            or os.environ.get("RESALE_DATABASE_URL", "")
            or os.environ.get("DATABASE_URL", ""))


def _dsn_live() -> str:
    return (os.environ.get("LIVE_DATABASE_URL_PUBLIC", "")
            or os.environ.get("LIVE_DATABASE_URL", "")
            or _dsn_resale())


def _dsn_intel() -> str:
    return (os.environ.get("INTELLIGENCE_DATABASE_URL_PUBLIC", "")
            or os.environ.get("INTELLIGENCE_DATABASE_URL", "")
            or _dsn_resale())


def _q(dsn: str, sql: str, params: tuple | None = None, *, timeout: int = 8) -> list[dict]:
    if not dsn or not _PG_OK:
        return [{"_error": "no DSN or psycopg2"}]
    try:
        with psycopg2.connect(dsn, connect_timeout=timeout) as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                # NOTE: pass None (not empty tuple) when no params — psycopg2's
                # mogrify treats raw `%` in SQL as a placeholder otherwise.
                if params is None or len(params) == 0:
                    cur.execute(sql)
                else:
                    cur.execute(sql, params)
                if cur.description:
                    return [dict(r) for r in cur.fetchall()]
                return []
    except Exception as e:
        return [{"_error": str(e)[:200]}]


def _table_exists(dsn: str, table: str) -> bool:
    schema, _, name = table.partition(".")
    if not name:
        schema, name = "public", schema
    rows = _q(dsn,
              "SELECT 1 AS ok FROM information_schema.tables "
              "WHERE table_schema=%s AND table_name=%s LIMIT 1",
              (schema, name))
    return bool(rows and rows[0].get("ok") == 1)


# ── 1. heartbeat freshness ────────────────────────────────────────────────
EXPECTED_BOTS = [
    "resale-bot", "analytics-bot", "hub-bot", "lead-bot",
    "roi-bot", "channel-bot", "currency-bot", "cloud-watchdog",
]


def check_heartbeat_freshness(dsn: str | None = None) -> list[Finding]:
    """All bots heartbeat in last 5 minutes? Uses audit_history metric
    `cron.tick.<bot>` OR `bot.heartbeat.<bot>` — accept whichever exists."""
    d = dsn or _dsn_resale()
    if not d:
        return [{"severity": "LOW", "category": "heartbeat",
                 "description": "no DSN — skipped", "affected_files": []}]
    if not _table_exists(d, "public.audit_history"):
        return [{"severity": "MEDIUM", "category": "heartbeat",
                 "description": "audit_history table missing — heartbeats untracked",
                 "affected_files": ["shared/auto_audit/_common.py"]}]
    findings: list[Finding] = []
    rows = _q(d, """
        SELECT metric, MAX(ts) AS last_ts
        FROM public.audit_history
        WHERE (metric LIKE 'cron.tick.%' OR metric LIKE 'bot.heartbeat.%')
          AND ts > NOW() - INTERVAL '1 day'
        GROUP BY metric
    """)
    if rows and rows[0].get("_error"):
        return [{"severity": "MEDIUM", "category": "heartbeat",
                 "description": f"query failed: {rows[0]['_error']}",
                 "affected_files": []}]
    seen: dict[str, float] = {}
    for r in rows:
        m = (r.get("metric") or "").lower()
        # extract bot name (last token)
        bot = m.rsplit(".", 1)[-1]
        ts = r.get("last_ts")
        if ts is None:
            continue
        age = (time.time() - ts.timestamp()) if hasattr(ts, "timestamp") else 9e9
        prev = seen.get(bot, 9e9)
        if age < prev:
            seen[bot] = age
    for bot in EXPECTED_BOTS:
        age = seen.get(bot)
        if age is None:
            findings.append({
                "severity": "HIGH", "category": "heartbeat",
                "description": f"no heartbeat for {bot} in last 24h",
                "affected_files": [],
            })
        elif age > 300:
            findings.append({
                "severity": "MEDIUM" if age < 3600 else "HIGH",
                "category": "heartbeat",
                "description": f"{bot} heartbeat stale: {age/60:.1f} min",
                "affected_files": [],
            })
    return findings


# ── 2. schema integrity ──────────────────────────────────────────────────
EXPECTED_TABLES = {
    "resale": [
        "public.listings_v2", "public.audit_history", "public.bot_users",
        "public.audit_runs", "public.audit_findings",
    ],
    "live": [
        "public.dld_transactions_full", "public.bot_users",
        "public.audit_log", "public.pdf_reports",
    ],
}


def check_schema_integrity(dsn: str | None = None) -> list[Finding]:
    findings: list[Finding] = []
    for tag, tables in EXPECTED_TABLES.items():
        d = _dsn_resale() if tag == "resale" else _dsn_live()
        if not d:
            continue
        for t in tables:
            if not _table_exists(d, t):
                findings.append({
                    "severity": "CRITICAL" if t.endswith("listings_v2")
                                 or t.endswith("dld_transactions_full")
                                 else "HIGH",
                    "category": "schema",
                    "description": f"expected table {t} missing in {tag} DB",
                    "affected_files": ["shared/db_contracts.py"],
                })
    return findings


# ── 3. contract drift — declared columns vs actual ───────────────────────
def check_contract_drift(dsn: str | None = None) -> list[Finding]:
    """Compare a tiny hard-coded contract (per-table required columns) against
    information_schema. Cheap subset of full db_contracts checker."""
    contracts: dict[str, list[str]] = {
        "public.listings_v2": ["id", "area", "price", "bedrooms", "property_type"],
        "public.audit_runs":  ["id", "audit_type", "started_at", "status"],
        "public.audit_findings": ["id", "audit_run_id", "severity", "description"],
    }
    d = _dsn_resale()
    if not d:
        return []
    findings: list[Finding] = []
    for tbl, want in contracts.items():
        schema, _, name = tbl.partition(".")
        rows = _q(d,
                  "SELECT column_name FROM information_schema.columns "
                  "WHERE table_schema=%s AND table_name=%s",
                  (schema, name))
        if rows and rows[0].get("_error"):
            continue
        cols = {r["column_name"] for r in rows if "column_name" in r}
        if not cols:
            continue  # table missing — schema check handles
        missing = [c for c in want if c not in cols]
        if missing:
            findings.append({
                "severity": "HIGH", "category": "contract_drift",
                "description": f"{tbl} missing columns: {','.join(missing)}",
                "affected_files": ["shared/db_contracts.py"],
            })
    return findings


# ── 4. brand compliance ──────────────────────────────────────────────────
FORBIDDEN_BRAND_PATTERNS = [
    r"First\s*Place\s*Realtor",
    r"first[_\-\.]?place[_\-\.]?realtor",
]


def _scan_files(root: Path, patterns: list[str],
                exts: tuple[str, ...] = (".py", ".md", ".txt", ".json", ".html"),
                max_files: int = 2000) -> list[tuple[Path, str]]:
    hits: list[tuple[Path, str]] = []
    regs = [re.compile(p, re.IGNORECASE) for p in patterns]
    count = 0
    skip_dirs = {"__pycache__", ".git", "node_modules", "_pycache", ".venv", "venv"}
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if any(s in path.parts for s in skip_dirs):
            continue
        if path.suffix.lower() not in exts:
            continue
        count += 1
        if count > max_files:
            break
        try:
            txt = path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        for reg in regs:
            if reg.search(txt):
                hits.append((path, reg.pattern))
                break
    return hits


def check_brand_compliance(dsn: str | None = None) -> list[Finding]:
    findings: list[Finding] = []
    if not SHARED_ROOT.exists():
        return findings
    hits = _scan_files(SHARED_ROOT, FORBIDDEN_BRAND_PATTERNS)
    if hits:
        findings.append({
            "severity": "CRITICAL", "category": "brand",
            "description": (
                f"forbidden brand name found in {len(hits)} files "
                "(must be Vadim Realty + RERA BRN 65011 only)"
            ),
            "affected_files": [str(p.relative_to(PROJECT_ROOT)) for p, _ in hits[:20]],
        })
    return findings


# ── 5. secrets compliance ────────────────────────────────────────────────
SECRET_PATTERNS = [
    # generic password=
    r"password\s*=\s*['\"][^'\"]{6,}['\"]",
    # bot token (Telegram-style 9-12 digits : 35 chars)
    r"\b\d{9,12}:[A-Za-z0-9_\-]{30,}\b",
    # AWS-style key
    r"AKIA[0-9A-Z]{12,}",
]
# allow obvious dummies
SECRET_IGNORE = re.compile(r"(example|dummy|fake|test|placeholder|XXXX|\<|\$\{)", re.I)


def check_secrets_compliance(dsn: str | None = None) -> list[Finding]:
    findings: list[Finding] = []
    if not SHARED_ROOT.exists():
        return findings
    regs = [re.compile(p) for p in SECRET_PATTERNS]
    hits: list[tuple[Path, str]] = []
    count = 0
    skip_dirs = {"__pycache__", ".git", "node_modules", ".venv", "venv"}
    for path in SHARED_ROOT.rglob("*.py"):
        if any(s in path.parts for s in skip_dirs):
            continue
        count += 1
        if count > 1500:
            break
        try:
            for ln, line in enumerate(path.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
                if SECRET_IGNORE.search(line):
                    continue
                for reg in regs:
                    if reg.search(line):
                        hits.append((path, f"L{ln}"))
                        break
        except Exception:
            continue
    if hits:
        findings.append({
            "severity": "CRITICAL", "category": "secrets",
            "description": f"hardcoded secret-like patterns in {len(hits)} locations",
            "affected_files": [f"{p.relative_to(PROJECT_ROOT)}:{ln}" for p, ln in hits[:20]],
        })
    return findings


# ── 6. dead handlers — callback patterns без handler-ов ──────────────────
def check_handler_dead_links(dsn: str | None = None) -> list[Finding]:
    """Грубая эвристика: callback_data='X' в keyboards, но нет
    @callback_query(F.data == 'X') / @router.callback_query(... 'X' ...)."""
    findings: list[Finding] = []
    if not SHARED_ROOT.exists():
        return findings
    callbacks_declared: set[str] = set()
    callbacks_handled: set[str] = set()
    cb_decl_re = re.compile(r"callback_data\s*=\s*['\"]([a-z][a-z0-9_:.\-]{2,40})['\"]", re.I)
    cb_handle_re = re.compile(
        r"(?:F\.data\s*==\s*['\"]([a-z][a-z0-9_:.\-]{2,40})['\"])"
        r"|(?:F\.data\.startswith\(['\"]([a-z][a-z0-9_:.\-]{2,40})['\"]\))"
        r"|(?:@\w+\.callback_query.*?['\"]([a-z][a-z0-9_:.\-]{2,40})['\"])",
        re.I,
    )
    count = 0
    for path in PROJECT_ROOT.rglob("*.py"):
        if "__pycache__" in path.parts or ".git" in path.parts:
            continue
        count += 1
        if count > 3000:
            break
        try:
            txt = path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        for m in cb_decl_re.finditer(txt):
            callbacks_declared.add(m.group(1))
        for m in cb_handle_re.finditer(txt):
            for g in m.groups():
                if g:
                    callbacks_handled.add(g)
    # rough match: any prefix of declared in handled
    dead = []
    for cb in callbacks_declared:
        if cb in callbacks_handled:
            continue
        # startswith check
        if any(cb.startswith(h) or h.startswith(cb) for h in callbacks_handled):
            continue
        dead.append(cb)
    if dead:
        findings.append({
            "severity": "MEDIUM", "category": "dead_handlers",
            "description": (
                f"{len(dead)} callback_data values declared but no handler matched "
                f"(sample: {','.join(sorted(dead)[:8])})"
            ),
            "affected_files": [],
        })
    return findings


# ── 7. cron freshness — last_fired vs expected ───────────────────────────
CRON_EXPECTED_MAX_AGE_H = {
    "agent_j_rss":            6,
    "agent_j_telegram":       4,
    "agent_l_daily_content": 26,
    "agent_l_followups":      0.25,
    "agent_h_subscriber":     0.1,
}


def check_cron_freshness(dsn: str | None = None) -> list[Finding]:
    d = _dsn_resale()
    findings: list[Finding] = []
    if not d or not _table_exists(d, "public.cron_run_log"):
        return [{"severity": "LOW", "category": "cron",
                 "description": "cron_run_log table absent — fresh DB?",
                 "affected_files": []}]
    rows = _q(d, """
        SELECT job_name, MAX(started_at) AS last_fired
        FROM public.cron_run_log
        WHERE started_at > NOW() - INTERVAL '7 days'
        GROUP BY job_name
    """)
    if rows and rows[0].get("_error"):
        return []
    seen = {r["job_name"]: r["last_fired"] for r in rows if "job_name" in r}
    now_ts = time.time()
    for job, max_h in CRON_EXPECTED_MAX_AGE_H.items():
        last = seen.get(job)
        if last is None:
            findings.append({
                "severity": "MEDIUM", "category": "cron",
                "description": f"cron job {job} hasn't fired in last 7 days",
                "affected_files": ["shared/scripts/phase_bm_master_cron.py"],
            })
            continue
        try:
            age_h = (now_ts - last.timestamp()) / 3600
        except Exception:
            continue
        if age_h > max_h * 3:
            findings.append({
                "severity": "HIGH" if age_h > max_h * 6 else "MEDIUM",
                "category": "cron",
                "description": f"cron {job} stale: {age_h:.1f}h (expected ≤{max_h}h)",
                "affected_files": ["shared/scripts/phase_bm_master_cron.py"],
            })
    return findings


# ── 8. queue depth — pipeline queues not growing ─────────────────────────
def check_queue_depth(dsn: str | None = None) -> list[Finding]:
    findings: list[Finding] = []
    queues = [
        ("public.content_pipeline_queue", "pending",     500),
        ("public.immune_incidents",       "OPEN",       200),
        ("public.listings_staging",       "pending",  50000),
    ]
    d = _dsn_resale()
    for tbl, status_val, thr in queues:
        if not _table_exists(d, tbl):
            continue
        # detect status column
        schema, _, name = tbl.partition(".")
        col_rows = _q(d,
                      "SELECT column_name FROM information_schema.columns "
                      "WHERE table_schema=%s AND table_name=%s",
                      (schema, name))
        cols = {r["column_name"] for r in col_rows if "column_name" in r}
        col = "status" if "status" in cols else ("state" if "state" in cols else None)
        if not col:
            continue
        rows = _q(d, f"SELECT COUNT(*) AS n FROM {schema}.{name} WHERE {col}=%s",
                  (status_val,))
        if not rows or rows[0].get("_error"):
            continue
        n = int(rows[0].get("n") or 0)
        if n > thr:
            findings.append({
                "severity": "HIGH" if n > thr * 5 else "MEDIUM",
                "category": "queue_depth",
                "description": f"{tbl} {col}={status_val} count={n} exceeds {thr}",
                "affected_files": [],
            })
    return findings


# ── 9. open circuit breakers ─────────────────────────────────────────────
def check_open_circuit_breakers(dsn: str | None = None) -> list[Finding]:
    d = _dsn_resale()
    if not d or not _table_exists(d, "public.circuit_breakers"):
        return []
    rows = _q(d,
              "SELECT name, state, failure_count "
              "FROM public.circuit_breakers WHERE state='OPEN'")
    if rows and rows[0].get("_error"):
        return []
    findings: list[Finding] = []
    for r in rows:
        findings.append({
            "severity": "HIGH",
            "category": "circuit_breaker",
            "description": (
                f"breaker {r.get('name')} OPEN "
                f"(failures={r.get('failure_count')})"
            ),
            "affected_files": ["shared/safety_nets/circuit_breaker.py"],
        })
    return findings


# ── 10. DB disk size growth ──────────────────────────────────────────────
def check_disk_size_growth(dsn: str | None = None) -> list[Finding]:
    limit_mb = int(os.environ.get("DB_VOLUME_LIMIT_MB", "5000"))
    findings: list[Finding] = []
    for tag, getter in (("resale", _dsn_resale),
                        ("live", _dsn_live),
                        ("intel", _dsn_intel)):
        d = getter()
        if not d:
            continue
        rows = _q(d, "SELECT pg_database_size(current_database())/1024/1024 AS mb")
        if not rows or rows[0].get("_error"):
            continue
        mb = float(rows[0].get("mb") or 0)
        pct = mb / max(1, limit_mb) * 100
        if pct > 80:
            findings.append({
                "severity": "CRITICAL" if pct > 95 else "HIGH",
                "category": "disk",
                "description": f"{tag} DB size {mb:.0f}MB ({pct:.0f}% of {limit_mb}MB limit)",
                "affected_files": [],
            })
    return findings


# ── Registry ──────────────────────────────────────────────────────────────
# audit_type → list of (check_name, callable)
CHECKS: dict[str, Callable[[str | None], list[Finding]]] = {
    "check_heartbeat_freshness":   check_heartbeat_freshness,
    "check_schema_integrity":      check_schema_integrity,
    "check_contract_drift":        check_contract_drift,
    "check_brand_compliance":      check_brand_compliance,
    "check_secrets_compliance":    check_secrets_compliance,
    "check_handler_dead_links":    check_handler_dead_links,
    "check_cron_freshness":        check_cron_freshness,
    "check_queue_depth":           check_queue_depth,
    "check_open_circuit_breakers": check_open_circuit_breakers,
    "check_disk_size_growth":      check_disk_size_growth,
}

# Default audit_type → checks mapping (seeded into audit_check_registry).
AUDIT_TYPE_DEFAULTS: dict[str, list[str]] = {
    "hourly": [
        "check_heartbeat_freshness",
        "check_open_circuit_breakers",
        "check_queue_depth",
    ],
    "daily": [
        "check_heartbeat_freshness",
        "check_contract_drift",
        "check_cron_freshness",
        "check_queue_depth",
        "check_open_circuit_breakers",
        "check_disk_size_growth",
        "check_handler_dead_links",
    ],
    "weekly": list(CHECKS.keys()),
}


__all__ = [
    "Finding", "CHECKS", "AUDIT_TYPE_DEFAULTS",
    "check_heartbeat_freshness", "check_schema_integrity",
    "check_contract_drift", "check_brand_compliance",
    "check_secrets_compliance", "check_handler_dead_links",
    "check_cron_freshness", "check_queue_depth",
    "check_open_circuit_breakers", "check_disk_size_growth",
]
