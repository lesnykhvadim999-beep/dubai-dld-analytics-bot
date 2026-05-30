"""
db_contracts.py — декларативные контракты на shared-таблицы экосистемы.

Контракт описывает что должно быть в таблице: какие колонки,
каких типов, какая критичность, какой максимальный % NULL допустим.

Зачем:
  - boot-time check выбрасывает алерт когда схема дрейфует
  - daily cron шлёт сводку «всё ок / поломалось»
  - новые боты сразу видят какие колонки им гарантированы

Как добавить новую таблицу:
  1. Опиши TableContract.
  2. Добавь в список CONTRACTS внизу файла.
  3. Закомить + задеплой одного бота → следующий boot-time check
     зафиксирует базлайн.

Уровни criticality:
  CRITICAL — поломка ⇒ silent NULL ⇒ юзер видит «нет данных».
             Алертим сразу. STRICT_CONTRACTS=1 ⇒ боты падают на старте.
  HIGH     — поломка ⇒ деградация UX (например, "rooms_en" пустой ⇒
             фильтр по комнатам не работает).
  LOW      — желательное поле; алерт только в daily-cron сводке.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Sequence, Tuple, Union


# ----------------------------------------------------------------------
# Типы
# ----------------------------------------------------------------------

# Допустимые SQL-типы у колонки.
# Принимаем кортеж, если допустим несколько типов
# (например, instance_date может быть date | timestamp | text — в архиве часто text).
TypeSpec = Union[str, Tuple[str, ...]]


@dataclass(frozen=True)
class ColumnContract:
    """Контракт на одну колонку."""
    type: TypeSpec
    criticality: str = "HIGH"                  # CRITICAL | HIGH | LOW
    min_non_null_pct: float = 0.0              # 0..100
    format_hint: Optional[str] = None          # для логов/алертов
    expected_values_sample: Sequence[str] = () # для enum-подобных полей


@dataclass(frozen=True)
class TableContract:
    """Контракт на таблицу."""
    table: str                                 # 'public.dld_transactions_full'
    db: str                                    # 'live' | 'archive' | 'resale' | 'currency' | ...
    required_columns: dict                     # name -> ColumnContract
    description: str = ""
    # Если True — для daily-cron делать full-scan NULL %.
    # Если False (default) — sample 10k rows.
    full_scan: bool = False
    # Боты, которые активно используют эту таблицу.
    # Используется только в daily-cron сводке для контекста.
    consumers: Sequence[str] = field(default_factory=tuple)


def _col(type, criticality="HIGH", min_non_null_pct=0.0,
         format_hint=None, expected_values_sample=()):
    """Сахар для краткости деклараций."""
    return ColumnContract(
        type=type,
        criticality=criticality,
        min_non_null_pct=min_non_null_pct,
        format_hint=format_hint,
        expected_values_sample=tuple(expected_values_sample),
    )


# ----------------------------------------------------------------------
# Контракты на shared-таблицы
# ----------------------------------------------------------------------

# Live DLD — обновляется ежедневно из Dubai Pulse.
DLD_TRANSACTIONS_FULL = TableContract(
    table="public.dld_transactions_full",
    db="live",
    description="DLD sales/mortgages/gifts (live, ~30 дней rolling)",
    consumers=("analytics-bot", "resale-bot", "roi-bot", "channel-poster",
               "lead-bot", "hub-bot"),
    required_columns={
        "instance_date": _col(
            type=("date", "timestamp without time zone", "timestamp", "text", "character varying"),
            criticality="CRITICAL", min_non_null_pct=99.0,
            format_hint="ISO 'YYYY-MM-DD' или 'DD-MM-YYYY' — ВСЕГДА safe_date_sql()",
        ),
        "building_name_en": _col(
            type=("text", "character varying"),
            criticality="CRITICAL", min_non_null_pct=80.0,
        ),
        "area_name_en": _col(
            type=("text", "character varying"),
            criticality="HIGH", min_non_null_pct=80.0,
        ),
        "rooms_en": _col(
            type=("text", "character varying"),
            criticality="HIGH", min_non_null_pct=70.0,
            format_hint="'1 B/R' | '2 B/R' | '3 B/R' | 'Studio' | 'PENTHOUSE'",
            expected_values_sample=("1 B/R", "2 B/R", "3 B/R", "Studio"),
        ),
        "actual_worth": _col(
            type=("numeric", "double precision", "text", "character varying"),
            criticality="CRITICAL", min_non_null_pct=95.0,
            format_hint="всегда safe_num_sql() — может прийти как text c запятыми",
        ),
        "procedure_area": _col(
            type=("numeric", "double precision", "text", "character varying"),
            criticality="HIGH", min_non_null_pct=60.0,
        ),
    },
)

# Архивный DLD — исторические сделки, формат старее, часто DD-MM-YYYY.
DLD_SALE_ARCHIVE = TableContract(
    table="public.dld_sale_archive",
    db="archive",
    description="DLD sales archive (исторические, ETL legacy)",
    consumers=("analytics-bot", "resale-bot", "roi-bot"),
    required_columns={
        "instance_date": _col(
            type=("date", "timestamp without time zone", "timestamp", "text", "character varying"),
            criticality="CRITICAL", min_non_null_pct=99.0,
            format_hint="Legacy 'DD-MM-YYYY' доминирует — обязательно safe_date_sql()",
        ),
        "building_name_en": _col(
            type=("text", "character varying"),
            criticality="CRITICAL", min_non_null_pct=70.0,
        ),
        "area_name_en": _col(
            type=("text", "character varying"),
            criticality="HIGH", min_non_null_pct=70.0,
        ),
        "rooms_en": _col(
            type=("text", "character varying"),
            criticality="HIGH", min_non_null_pct=50.0,
            expected_values_sample=("1 B/R", "2 B/R", "3 B/R", "Studio"),
        ),
        "actual_worth": _col(
            type=("numeric", "double precision", "text", "character varying"),
            criticality="CRITICAL", min_non_null_pct=90.0,
        ),
    },
)

# Live rentals.
DLD_RENTS_FULL = TableContract(
    table="public.dld_rents_full",
    db="live",
    description="DLD rent contracts (live)",
    consumers=("analytics-bot", "resale-bot", "hub-bot"),
    required_columns={
        "contract_start_date": _col(
            type=("date", "timestamp without time zone", "timestamp", "text", "character varying"),
            criticality="CRITICAL", min_non_null_pct=95.0,
            format_hint="safe_date_sql()",
        ),
        "annual_amount": _col(
            type=("numeric", "double precision", "text", "character varying"),
            criticality="CRITICAL", min_non_null_pct=85.0,
        ),
        "area_name_en": _col(
            type=("text", "character varying"),
            criticality="HIGH", min_non_null_pct=70.0,
        ),
        "building_name_en": _col(
            type=("text", "character varying"),
            criticality="HIGH", min_non_null_pct=50.0,
        ),
    },
)

# Архивные rentals.
DLD_RENT_ARCHIVE = TableContract(
    table="public.dld_rent_archive",
    db="archive",
    description="DLD rent contracts archive",
    consumers=("analytics-bot",),
    required_columns={
        "contract_start_date": _col(
            type=("date", "timestamp without time zone", "timestamp", "text", "character varying"),
            criticality="CRITICAL", min_non_null_pct=95.0,
            format_hint="часто 'DD-MM-YYYY' — safe_date_sql()",
        ),
        "annual_amount": _col(
            type=("numeric", "double precision", "text", "character varying"),
            criticality="CRITICAL", min_non_null_pct=80.0,
        ),
        "area_name_en": _col(
            type=("text", "character varying"),
            criticality="HIGH", min_non_null_pct=60.0,
        ),
    },
)

# Resale read-model.
LISTINGS_V2 = TableContract(
    table="public.listings_v2",
    db="resale",
    description="Resale read-model (Dubai resale-bot)",
    consumers=("resale-bot", "channel-poster", "hub-bot", "lead-bot"),
    required_columns={
        "id": _col(type="bigint", criticality="CRITICAL", min_non_null_pct=100.0),
        "price_aed": _col(
            type=("numeric", "double precision"),
            criticality="CRITICAL", min_non_null_pct=95.0,
        ),
        "bedrooms": _col(
            type=("integer", "smallint", "text"),
            criticality="HIGH", min_non_null_pct=85.0,
        ),
        "area_name": _col(
            type=("text", "character varying"),
            criticality="HIGH", min_non_null_pct=90.0,
        ),
        "building_name": _col(
            type=("text", "character varying"),
            criticality="HIGH", min_non_null_pct=80.0,
        ),
        "created_at": _col(
            type=("timestamp with time zone", "timestamp without time zone", "timestamp"),
            criticality="HIGH", min_non_null_pct=99.0,
        ),
        "status": _col(
            type=("text", "character varying"),
            criticality="HIGH", min_non_null_pct=99.0,
            expected_values_sample=("active", "sold", "removed"),
        ),
    },
)

# Leads — общая для lead-bot + analytics.
LEADS = TableContract(
    table="public.leads",
    db="live",
    description="Лиды от всех ботов (single source of truth)",
    consumers=("lead-bot", "analytics-bot", "hub-bot"),
    required_columns={
        "id": _col(type="bigint", criticality="CRITICAL", min_non_null_pct=100.0),
        "tg_user_id": _col(type="bigint", criticality="CRITICAL", min_non_null_pct=95.0),
        "source_bot": _col(
            type=("text", "character varying"),
            criticality="HIGH", min_non_null_pct=99.0,
            expected_values_sample=("analytics", "resale", "hub", "channel"),
        ),
        "created_at": _col(
            type=("timestamp with time zone", "timestamp without time zone", "timestamp"),
            criticality="CRITICAL", min_non_null_pct=100.0,
        ),
    },
)

# Users — каталог пользователей.
USERS = TableContract(
    table="public.users",
    db="live",
    description="Глобальный каталог пользователей",
    consumers=("hub-bot", "analytics-bot", "resale-bot", "channel-poster",
               "lead-bot", "roi-bot", "currency-bot"),
    required_columns={
        "tg_user_id": _col(type="bigint", criticality="CRITICAL", min_non_null_pct=100.0),
        "lang": _col(
            type=("text", "character varying"),
            criticality="HIGH", min_non_null_pct=90.0,
            expected_values_sample=("en", "ru"),
        ),
        "first_seen_at": _col(
            type=("timestamp with time zone", "timestamp without time zone", "timestamp"),
            criticality="HIGH", min_non_null_pct=95.0,
        ),
    },
)

# Area price benchmark — материализованный профиль районов.
AREA_PRICE_BENCHMARK = TableContract(
    table="public.area_price_benchmark",
    db="live",
    description="Материализованный профиль районов — цена/м² + p25/p75",
    consumers=("analytics-bot", "resale-bot", "roi-bot", "channel-poster"),
    required_columns={
        "area_name_en": _col(
            type=("text", "character varying"),
            criticality="CRITICAL", min_non_null_pct=100.0,
        ),
        "rooms_en": _col(
            type=("text", "character varying"),
            criticality="HIGH", min_non_null_pct=95.0,
        ),
        "median_price_per_sqft": _col(
            type=("numeric", "double precision"),
            criticality="CRITICAL", min_non_null_pct=90.0,
        ),
        "sample_size": _col(
            type=("integer", "bigint"),
            criticality="HIGH", min_non_null_pct=100.0,
        ),
        "updated_at": _col(
            type=("timestamp with time zone", "timestamp without time zone", "timestamp"),
            criticality="HIGH", min_non_null_pct=100.0,
        ),
    },
)


CONTRACTS = [
    DLD_TRANSACTIONS_FULL,
    DLD_SALE_ARCHIVE,
    DLD_RENTS_FULL,
    DLD_RENT_ARCHIVE,
    LISTINGS_V2,
    LEADS,
    USERS,
    AREA_PRICE_BENCHMARK,
]


def filter_contracts(patterns: Sequence[str]) -> list:
    """Отбирает контракты по списку wildcard-паттернов: ['dld_*','listings_v2']."""
    import fnmatch
    out = []
    for c in CONTRACTS:
        # public.dld_transactions_full -> dld_transactions_full
        bare = c.table.split(".", 1)[-1]
        for p in patterns:
            if fnmatch.fnmatchcase(bare, p) or fnmatch.fnmatchcase(c.table, p):
                out.append(c)
                break
    return out


if __name__ == "__main__":
    print(f"Total contracts: {len(CONTRACTS)}")
    for c in CONTRACTS:
        crit_cols = [n for n, cc in c.required_columns.items() if cc.criticality == "CRITICAL"]
        print(f"  {c.table:40s} [{c.db:8s}]  crit={len(crit_cols)}  total_cols={len(c.required_columns)}")
    print()
    print("Filter test ['dld_*']:")
    for c in filter_contracts(["dld_*"]):
        print(f"  -> {c.table}")
