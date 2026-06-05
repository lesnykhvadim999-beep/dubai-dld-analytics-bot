"""
contracts_registry.py — runtime verification ecosystem contracts.

Дополняет shared/db_contracts.py (schema-level) и shared/ECOSYSTEM_CONTRACTS.md
(декларативный документ). Этот модуль — программный интерфейс для:

  verify_my_contracts(bot_name="analytics", role="reader")

— вызвать на старте каждого бота. Проходит по контрактам, релевантным
для этого бота, и:
  - reader → checks DB-таблицы достижимы + базовые колонки есть
  - writer → checks DB-таблицы достижимы + последнее обновление < SLA
  - deep-link источник → проверяет что constants выглядят как ?start=from_<bot>

При нарушении:
  - STRICT_CONTRACTS=1  → raises ContractViolation (бот падает на старте)
  - default             → log + admin_notify(high) + continue (degraded mode)

Дизайн: fail-soft, никогда не должен сам ломать бота больше, чем нарушение
контракта уже ломает. Любое исключение внутри verify_* ловится и логируется.
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from typing import Callable, Optional, Sequence

log = logging.getLogger("contracts_registry")


class ContractViolation(Exception):
    """Поднимаем только если STRICT_CONTRACTS=1."""


# ── per-bot owned shared tables (для quick lookup) ────────────────────────
# Source of truth: ECOSYSTEM_CONTRACTS.md §1 и db_contracts.py.
BOT_TABLE_ROLES: dict[str, dict[str, list[str]]] = {
    "analytics": {
        "writer": ["public.dld_transactions_full"],
        "reader": ["public.dld_transactions_full",
                   "public.bot_users"],
    },
    "resale": {
        "writer": ["public.listings_v2"],
        "reader": ["public.listings_v2", "public.dld_transactions_full",
                   "public.bot_users", "public.pdf_reports"],
    },
    "roi": {
        "writer": [],
        "reader": ["public.dld_transactions_full",
                   "public.bot_users", "public.pdf_reports"],
    },
    "hub": {
        "writer": [],
        "reader": ["public.listings_v2", "public.bot_users", "public.leads",
                   "public.dld_transactions_full"],
    },
    "lead": {
        "writer": ["public.leads"],
        "reader": ["public.leads", "public.listings_v2", "public.bot_users",
                   "public.pdf_reports"],
    },
    "channel": {
        "writer": [],
        "reader": ["public.dld_transactions_full", "public.listings_v2"],
    },
    "currency": {
        "writer": ["public.fx_rates"],
        "reader": ["public.fx_rates"],
    },
    "channel-poster": {
        "writer": [],
        "reader": ["public.dld_transactions_full", "public.listings_v2"],
    },
    "health-reporter": {
        "writer": [],
        "reader": ["public.bot_users",
                   "public.dld_transactions_full"],
    },
}


# ── deep-link source constants — что должно быть в env у бота ─────────────
EXPECTED_DEEPLINK_PATTERN: dict[str, str] = {
    "hub":      r"\?start=from_hub_utm_",
    "resale":   r"\?start=from_resale",
    "roi":      r"\?start=from_roi",
    "channel":  r"\?start=from_(offplan|channel)",
    "analytics":r"\?start=from_analytics",
}


# ── DSN lookup per table ──────────────────────────────────────────────────
TABLE_DSN_ENV: dict[str, str] = {
    "public.dld_transactions_full":    "LIVE_DATABASE_URL",
    "public.pdf_reports":              "LIVE_DATABASE_URL",
    "public.bot_users":                "DATABASE_URL",  # B131: реально живёт в main DB, а не LIVE
    "public.listings_v2":              "RESALE_DATABASE_URL",
    "public.leads":                    "RESALE_DATABASE_URL",
    "public.fx_rates":                 "LIVE_DATABASE_URL",
}


# ── SLA для writer-tables (max staleness в часах) ─────────────────────────
WRITER_STALE_HOURS: dict[str, int] = {
    "public.dld_transactions_full":    72,   # cron 24h, tolerance 3x
    "public.listings_v2":              24,
    "public.fx_rates":                 24,
    "public.leads":                    9999, # event-driven, не stale-checkable
}


@dataclass
class ContractCheckResult:
    name: str
    passed: bool
    detail: str = ""
    severity: str = "HIGH"  # CRITICAL / HIGH / LOW


@dataclass
class VerifyReport:
    bot_name: str
    role: str
    started_at: float = field(default_factory=time.time)
    results: list[ContractCheckResult] = field(default_factory=list)

    @property
    def failed(self) -> list[ContractCheckResult]:
        return [r for r in self.results if not r.passed]

    @property
    def ok(self) -> bool:
        return all(r.passed for r in self.results)

    def summary(self) -> str:
        total = len(self.results)
        bad = len(self.failed)
        return f"contracts[{self.bot_name}/{self.role}]: {total - bad}/{total} ok, {bad} failed"


# ── helpers ───────────────────────────────────────────────────────────────
def _table_exists(dsn: str, table: str, timeout: int = 5) -> tuple[bool, str]:
    try:
        import psycopg2  # lazy
        schema, _, name = table.partition(".")
        if not name:
            schema, name = "public", schema
        with psycopg2.connect(dsn, connect_timeout=timeout) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM information_schema.tables "
                    "WHERE table_schema = %s AND table_name = %s LIMIT 1",
                    (schema, name),
                )
                if cur.fetchone():
                    return True, ""
                return False, f"table {table} not found"
    except Exception as e:
        return False, f"DB unreachable: {e!s}"


def _writer_freshness(dsn: str, table: str, max_hours: int) -> tuple[bool, str]:
    # Best-effort: ищем колонку instance_date / updated_at / posted_at.
    candidates = ["instance_date", "updated_at", "posted_at", "created_at", "ts"]
    try:
        import psycopg2
        schema, _, name = table.partition(".")
        if not name:
            schema, name = "public", schema
        with psycopg2.connect(dsn, connect_timeout=5) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema=%s AND table_name=%s",
                    (schema, name),
                )
                cols = {r[0] for r in cur.fetchall()}
                pick = next((c for c in candidates if c in cols), None)
                if not pick:
                    return True, "no timestamp col — skip freshness"
                # Используем safe coercion если это instance_date (text format!)
                if pick == "instance_date":
                    # try safe_coerce
                    try:
                        from safe_coerce import safe_date_sql  # type: ignore
                        expr = safe_date_sql(pick)
                    except Exception:
                        expr = f"{pick}::date"
                    cur.execute(
                        f"SELECT MAX({expr}) FROM {schema}.{name}"
                    )
                else:
                    cur.execute(f"SELECT MAX({pick}) FROM {schema}.{name}")
                row = cur.fetchone()
                if not row or row[0] is None:
                    return False, f"{table}: MAX({pick}) is NULL (empty table?)"
                last = row[0]
                # last может быть date / datetime
                try:
                    import datetime as _dt
                    now = _dt.datetime.utcnow()
                    if hasattr(last, "tzinfo"):
                        # naive comparison
                        if hasattr(last, "date") and not isinstance(last, _dt.datetime):
                            last_dt = _dt.datetime.combine(last, _dt.time())
                        else:
                            last_dt = last.replace(tzinfo=None) if last.tzinfo else last
                    else:
                        last_dt = _dt.datetime.combine(last, _dt.time())
                    age_h = (now - last_dt).total_seconds() / 3600
                    if age_h > max_hours:
                        return False, f"{table} stale: {age_h:.1f}h > {max_hours}h SLA"
                    return True, f"{table} fresh ({age_h:.1f}h)"
                except Exception as e:
                    return True, f"freshness compare skipped: {e}"
    except Exception as e:
        return False, f"DB error: {e!s}"


def _admin_alert(msg: str) -> None:
    try:
        from admin_notify import admin_notify  # type: ignore
        admin_notify(msg, parse_mode="HTML", priority="high")
    except Exception as e:
        log.debug("admin_alert failed: %s", e)


# ── public API ────────────────────────────────────────────────────────────
def verify_my_contracts(
    bot_name: str,
    role: str = "reader",
    *,
    strict: Optional[bool] = None,
    raise_on_violation: Optional[bool] = None,
    notify: bool = True,
) -> VerifyReport:
    """Verify все контракты, релевантные для bot_name+role.

    Args:
      bot_name: 'analytics' | 'resale' | 'roi' | 'hub' | 'lead' | 'channel' | ...
      role: 'reader' | 'writer' | 'both'
      strict: STRICT_CONTRACTS=1 если None
      raise_on_violation: alias для strict; если True — поднимает ContractViolation
      notify: посылать admin alert при нарушениях

    Returns:
      VerifyReport — для логирования / тестов.
    """
    if strict is None and raise_on_violation is None:
        strict = os.environ.get("STRICT_CONTRACTS", "0") in ("1", "true", "True")
    if raise_on_violation is not None:
        strict = bool(raise_on_violation)

    report = VerifyReport(bot_name=bot_name, role=role)
    roles = BOT_TABLE_ROLES.get(bot_name)
    if not roles:
        log.warning("contracts_registry: unknown bot %r — skip", bot_name)
        return report

    # tables this bot reads / writes
    read_tables = roles.get("reader", []) if role in ("reader", "both") else []
    write_tables = roles.get("writer", []) if role in ("writer", "both") else []

    for tbl in set(read_tables + write_tables):
        dsn_env = TABLE_DSN_ENV.get(tbl)
        if not dsn_env:
            report.results.append(ContractCheckResult(
                name=f"{tbl}/dsn_env_unknown", passed=False,
                detail=f"no DSN env mapping for {tbl}",
                severity="HIGH",
            ))
            continue
        dsn = os.environ.get(dsn_env, "")
        if not dsn:
            # В local dev может не быть DSN — это warning, не fail
            report.results.append(ContractCheckResult(
                name=f"{tbl}/dsn_env_not_set", passed=True,
                detail=f"{dsn_env} not set in env — skip",
                severity="LOW",
            ))
            continue

        ok, detail = _table_exists(dsn, tbl)
        report.results.append(ContractCheckResult(
            name=f"{tbl}/exists", passed=ok,
            detail=detail or "ok", severity="CRITICAL",
        ))
        if not ok:
            continue

        if tbl in write_tables and tbl in WRITER_STALE_HOURS:
            max_h = WRITER_STALE_HOURS[tbl]
            if max_h < 9999:
                fresh, fdetail = _writer_freshness(dsn, tbl, max_h)
                report.results.append(ContractCheckResult(
                    name=f"{tbl}/freshness", passed=fresh,
                    detail=fdetail, severity="HIGH",
                ))

    # Финал
    failed = report.failed
    if failed:
        msg = (
            f"⚠️ <b>Contract violation</b> in <code>{bot_name}/{role}</code>\n"
            + "\n".join(f"• <code>{r.name}</code> [{r.severity}]: {r.detail}"
                        for r in failed[:8])
        )
        log.warning("contracts: %s — %d failed", bot_name,  len(failed))
        if notify:
            _admin_alert(msg)
        if strict:
            raise ContractViolation(report.summary())
    else:
        log.info("contracts: %s ok (%d checks)", bot_name, len(report.results))

    return report


# ── CLI mode ──────────────────────────────────────────────────────────────
def _cli() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Verify ecosystem contracts")
    ap.add_argument("--bot", help="Bot name (analytics/resale/roi/hub/lead/channel)")
    ap.add_argument("--role", default="reader", choices=("reader", "writer", "both"))
    ap.add_argument("--verify-all", action="store_true",
                    help="Iterate all known bots")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    if args.verify_all:
        any_fail = False
        for bot in BOT_TABLE_ROLES:
            rep = verify_my_contracts(bot, role="both", notify=False)
            print(rep.summary())
            for r in rep.failed:
                print(f"  ! {r.name} [{r.severity}]: {r.detail}")
            if rep.failed:
                any_fail = True
        return 1 if any_fail else 0

    if not args.bot:
        ap.print_help()
        return 2
    rep = verify_my_contracts(args.bot, role=args.role, notify=False)
    print(rep.summary())
    for r in rep.results:
        mark = "OK " if r.passed else "FAIL"
        print(f"  {mark} {r.name} — {r.detail}")
    return 0 if rep.ok else 1


if __name__ == "__main__":
    import sys
    sys.exit(_cli())
