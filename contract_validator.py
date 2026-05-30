"""
contract_validator.py — проверка фактической схемы БД против декларативных
контрактов из db_contracts.py.

Что делает:
  1. Подключается к каждой БД из переданного словаря {db_alias: DSN}.
  2. Берёт схему из information_schema.
  3. Для каждой колонки сверяет: существование, тип, % NULL на sample.
  4. Для CRITICAL date-колонок дополнительно классифицирует форматы
     (ISO / DD-MM-YYYY / DD/MM/YYYY / прочее) на sample 100 строк.
  5. Возвращает ValidationReport.

Использование (boot-time):
    from shared.contract_validator import validate_contracts_on_boot
    rep = validate_contracts_on_boot(
        bot_name="analytics",
        dsns={"live": LIVE_DATABASE_URL, "archive": ARCHIVE_DATABASE_URL},
        contracts_filter=["dld_*"],
    )
    if rep.has_critical:
        admin_notify(f"schema drift: {rep.summary()}")

ПРАВИЛО: validator никогда не делает блокирующих SQL в main-loop.
Всё запускают через asyncio.create_task() или фоновый thread.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

try:
    import psycopg2
    from psycopg2.extras import RealDictCursor
    PSYCOPG2_OK = True
except Exception:
    PSYCOPG2_OK = False

# Шареные контракты.
try:
    from db_contracts import CONTRACTS, filter_contracts, TableContract, ColumnContract
except Exception:
    # При импорте как пакета (shared.contract_validator)
    from .db_contracts import CONTRACTS, filter_contracts, TableContract, ColumnContract  # type: ignore


# ----------------------------------------------------------------------
# Структуры отчёта
# ----------------------------------------------------------------------

@dataclass
class ContractViolation:
    table: str
    column: str
    kind: str           # 'missing_column' | 'type_mismatch' | 'null_pct' | 'format_drift'
    criticality: str    # CRITICAL | HIGH | LOW
    actual: str = ""
    expected: str = ""
    details: str = ""

    def __str__(self):
        return (f"[{self.criticality}] {self.table}.{self.column} {self.kind}: "
                f"expected={self.expected!r} actual={self.actual!r} "
                f"{self.details}").strip()


@dataclass
class ValidationReport:
    bot_name: str
    started_at: float = field(default_factory=time.time)
    duration_ms: int = 0
    tables_checked: int = 0
    violations: List[ContractViolation] = field(default_factory=list)
    db_errors: List[str] = field(default_factory=list)
    # Для логов: подробный список форматов для каждой date-колонки.
    date_format_stats: Dict[str, Dict[str, int]] = field(default_factory=dict)

    @property
    def has_critical(self) -> bool:
        return any(v.criticality == "CRITICAL" for v in self.violations)

    @property
    def has_any(self) -> bool:
        return bool(self.violations) or bool(self.db_errors)

    def severity_summary(self) -> Dict[str, int]:
        s = {"CRITICAL": 0, "HIGH": 0, "LOW": 0}
        for v in self.violations:
            s[v.criticality] = s.get(v.criticality, 0) + 1
        return s

    def summary(self) -> str:
        sev = self.severity_summary()
        return (f"tables={self.tables_checked} "
                f"CRIT={sev['CRITICAL']} HIGH={sev['HIGH']} LOW={sev['LOW']} "
                f"db_errors={len(self.db_errors)} dur={self.duration_ms}ms")

    def render_telegram(self) -> str:
        """Markdown отчёт для admin_notify."""
        sev = self.severity_summary()
        head = f"*Contract check — {self.bot_name}*\n{self.summary()}\n"
        if not self.has_any:
            return head + "OK"
        lines = [head]
        for v in self.violations[:30]:
            lines.append(f"- {v}")
        if len(self.violations) > 30:
            lines.append(f"(+{len(self.violations)-30} more)")
        for e in self.db_errors[:5]:
            lines.append(f"db: {e}")
        return "\n".join(lines)


# ----------------------------------------------------------------------
# SQL-хелперы
# ----------------------------------------------------------------------

_DATE_FORMAT_PATTERNS = {
    "ISO_DATE":      re.compile(r"^\d{4}-\d{2}-\d{2}$"),
    "ISO_DATETIME":  re.compile(r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}"),
    "DD-MM-YYYY":    re.compile(r"^\d{2}-\d{2}-\d{4}$"),
    "DD/MM/YYYY":    re.compile(r"^\d{2}/\d{2}/\d{4}$"),
    "EPOCH":         re.compile(r"^\d{10}$"),
}


def _classify_date_value(v) -> str:
    if v is None:
        return "NULL"
    s = str(v)
    for name, pat in _DATE_FORMAT_PATTERNS.items():
        if pat.match(s):
            return name
    return "OTHER"


def _normalize_type(t: str) -> str:
    """Срезаем хвосты вроде '(without time zone)' и 'character varying' → 'text'."""
    if not t:
        return ""
    return t.strip().lower()


def _type_matches(actual: str, expected) -> bool:
    a = _normalize_type(actual)
    if isinstance(expected, str):
        expected = (expected,)
    for e in expected:
        e = _normalize_type(e)
        if a == e:
            return True
        # legacy character varying ↔ text
        if a in ("character varying", "varchar") and e in ("text", "character varying"):
            return True
        if a == "text" and e == "character varying":
            return True
    return False


# ----------------------------------------------------------------------
# Проверка одной таблицы
# ----------------------------------------------------------------------

def _fetch_columns(conn, table: str) -> Dict[str, str]:
    """Возвращает {col_name: data_type} для схемы.table.
    table — 'public.dld_transactions_full' или 'dld_transactions_full'."""
    if "." in table:
        schema, tbl = table.split(".", 1)
    else:
        schema, tbl = "public", table
    sql = """
        SELECT column_name, data_type
          FROM information_schema.columns
         WHERE table_schema = %s AND table_name = %s
    """
    with conn.cursor() as cur:
        cur.execute(sql, (schema, tbl))
        rows = cur.fetchall()
    return {r[0]: r[1] for r in rows}


def _sample_null_pct(conn, table: str, columns: Sequence[str],
                     sample_rows: int = 10000) -> Dict[str, float]:
    """% NULL для каждого col на TABLESAMPLE / LIMIT sample."""
    if not columns:
        return {}
    # Безопасные имена колонок (контракт-валидатор контролирует).
    # Используем LIMIT-based sample вместо TABLESAMPLE (не на всех версиях/таблицах работает).
    null_exprs = ",\n          ".join(
        f"100.0 * SUM(CASE WHEN \"{c}\" IS NULL THEN 1 ELSE 0 END) / NULLIF(COUNT(*),0) AS \"{c}\""
        for c in columns
    )
    sql = f"""
        WITH s AS (SELECT * FROM {table} LIMIT %s)
        SELECT
          {null_exprs}
        FROM s
    """
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(sql, (sample_rows,))
        row = cur.fetchone()
    if not row:
        return {c: 100.0 for c in columns}
    return {c: float(row[c] or 0.0) for c in columns}


def _date_format_distribution(conn, table: str, column: str,
                              sample_rows: int = 100) -> Dict[str, int]:
    """Считает распределение форматов на sample значений."""
    sql = f'SELECT "{column}"::text AS v FROM {table} WHERE "{column}" IS NOT NULL LIMIT %s'
    with conn.cursor() as cur:
        cur.execute(sql, (sample_rows,))
        rows = cur.fetchall()
    dist: Dict[str, int] = {}
    for (v,) in rows:
        k = _classify_date_value(v)
        dist[k] = dist.get(k, 0) + 1
    return dist


def _validate_table(conn, contract: TableContract,
                    sample_rows: int = 10000) -> tuple:
    """Возвращает (violations, date_format_stats)."""
    violations: List[ContractViolation] = []
    date_stats: Dict[str, Dict[str, int]] = {}

    try:
        actual_cols = _fetch_columns(conn, contract.table)
    except Exception as e:
        violations.append(ContractViolation(
            table=contract.table, column="*", kind="db_error",
            criticality="CRITICAL",
            details=f"information_schema fetch failed: {e!r}",
        ))
        return violations, date_stats

    if not actual_cols:
        violations.append(ContractViolation(
            table=contract.table, column="*", kind="missing_table",
            criticality="CRITICAL",
            details="table not found in information_schema",
        ))
        return violations, date_stats

    # 1) missing columns + type mismatches
    present_for_null: List[str] = []
    for col_name, cc in contract.required_columns.items():
        actual_type = actual_cols.get(col_name)
        if actual_type is None:
            violations.append(ContractViolation(
                table=contract.table, column=col_name, kind="missing_column",
                criticality=cc.criticality,
                expected=str(cc.type),
            ))
            continue
        if not _type_matches(actual_type, cc.type):
            violations.append(ContractViolation(
                table=contract.table, column=col_name, kind="type_mismatch",
                criticality=cc.criticality,
                actual=actual_type, expected=str(cc.type),
            ))
        present_for_null.append(col_name)

    # 2) NULL % на sample
    if present_for_null:
        try:
            null_pct = _sample_null_pct(conn, contract.table, present_for_null, sample_rows=sample_rows)
        except Exception as e:
            violations.append(ContractViolation(
                table=contract.table, column="*", kind="db_error",
                criticality="HIGH",
                details=f"null-pct sample failed: {e!r}",
            ))
            null_pct = {}
        for col_name, pct in null_pct.items():
            cc = contract.required_columns[col_name]
            non_null = 100.0 - pct
            if non_null + 0.01 < cc.min_non_null_pct:
                violations.append(ContractViolation(
                    table=contract.table, column=col_name, kind="null_pct",
                    criticality=cc.criticality,
                    actual=f"{non_null:.1f}% non-null",
                    expected=f">= {cc.min_non_null_pct}% non-null",
                    details=cc.format_hint or "",
                ))

    # 3) date format drift для критичных date-колонок
    for col_name, cc in contract.required_columns.items():
        if col_name not in actual_cols:
            continue
        is_date_like = (
            "date" in str(cc.type).lower()
            or (col_name.endswith("_date") and cc.criticality in ("CRITICAL", "HIGH"))
            or col_name in ("instance_date", "contract_start_date")
        )
        if not is_date_like:
            continue
        try:
            dist = _date_format_distribution(conn, contract.table, col_name, sample_rows=100)
        except Exception:
            continue
        date_stats[f"{contract.table}.{col_name}"] = dist
        # Если есть "OTHER" >= 10% — алертим, формат вне известных regex.
        total = sum(dist.values()) or 1
        other = dist.get("OTHER", 0)
        if other / total >= 0.10:
            violations.append(ContractViolation(
                table=contract.table, column=col_name, kind="format_drift",
                criticality=cc.criticality,
                actual=f"{other}/{total} unknown format(s)",
                expected="ISO | DD-MM-YYYY | DD/MM/YYYY | EPOCH",
                details=str(dist),
            ))

    return violations, date_stats


# ----------------------------------------------------------------------
# Точки входа
# ----------------------------------------------------------------------

def validate_contracts(dsns: Dict[str, str],
                       contracts: Optional[Sequence[TableContract]] = None,
                       bot_name: str = "unknown",
                       sample_rows: int = 10000) -> ValidationReport:
    """Главный entry-point. Синхронный — для daily-cron.
    Для boot-time используй validate_contracts_on_boot()."""
    rep = ValidationReport(bot_name=bot_name)
    t0 = time.time()

    if not PSYCOPG2_OK:
        rep.db_errors.append("psycopg2 not installed")
        rep.duration_ms = int((time.time() - t0) * 1000)
        return rep

    contracts = list(contracts) if contracts is not None else list(CONTRACTS)

    # Группируем контракты по db-alias.
    by_db: Dict[str, List[TableContract]] = {}
    for c in contracts:
        by_db.setdefault(c.db, []).append(c)

    for db_alias, ctrs in by_db.items():
        dsn = dsns.get(db_alias)
        if not dsn:
            # Не падаем — просто пропускаем (бот может не иметь этого DSN).
            rep.db_errors.append(f"no DSN for db={db_alias!r} (skipped {len(ctrs)} contracts)")
            continue
        try:
            conn = psycopg2.connect(dsn, connect_timeout=10)
            conn.set_session(readonly=True, autocommit=True)
        except Exception as e:
            rep.db_errors.append(f"connect {db_alias} failed: {e!r}")
            continue
        try:
            for c in ctrs:
                rep.tables_checked += 1
                vios, ds = _validate_table(conn, c, sample_rows=sample_rows)
                rep.violations.extend(vios)
                rep.date_format_stats.update(ds)
        finally:
            try:
                conn.close()
            except Exception:
                pass

    rep.duration_ms = int((time.time() - t0) * 1000)
    return rep


def validate_contracts_on_boot(bot_name: str,
                               dsns: Dict[str, str],
                               contracts_filter: Optional[Sequence[str]] = None,
                               sample_rows: int = 5000,
                               on_violation=None) -> ValidationReport:
    """Boot-time API. Сам по себе НЕ запускает в фоне — это делает caller
    (через asyncio.create_task / threading.Thread), чтобы не задерживать polling.

    on_violation: callable(rep: ValidationReport) -> None — вызывается
    если rep.has_critical. Туда обычно прокидывают admin_notify().
    """
    contracts = filter_contracts(contracts_filter) if contracts_filter else list(CONTRACTS)
    rep = validate_contracts(dsns=dsns, contracts=contracts,
                             bot_name=bot_name, sample_rows=sample_rows)
    try:
        if rep.has_critical and on_violation:
            on_violation(rep)
    except Exception as e:
        rep.db_errors.append(f"on_violation callback failed: {e!r}")
    return rep


if __name__ == "__main__":
    # CLI: python contract_validator.py
    import os, json
    dsns = {
        "live": os.getenv("LIVE_DATABASE_URL") or os.getenv("DATABASE_URL", ""),
        "archive": os.getenv("ARCHIVE_DATABASE_URL", ""),
        "resale": os.getenv("RESALE_DATABASE_URL") or os.getenv("DATABASE_URL", ""),
        "currency": os.getenv("CURRENCY_DATABASE_URL", ""),
    }
    rep = validate_contracts(dsns=dsns, bot_name="cli", sample_rows=2000)
    print(rep.summary())
    print()
    for v in rep.violations:
        print(v)
    print()
    if rep.date_format_stats:
        print("Date format distributions:")
        print(json.dumps(rep.date_format_stats, indent=2, default=str))
