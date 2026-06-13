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

ИСТОРИЯ ДЕКЛАРАЦИЙ:
  v2 (2026-05-30, drift fix):
    - LIVE DLD таблицы используют новые scraper-имена (transaction_date,
      area_en, building_en, start_date) — legacy CSV-имена остались только
      в archive.
    - leads/users/area_price_benchmark переключены с db='live' на db='resale'
      (фактически живут в RESALE_DATABASE_URL).
    - listings_v2 синхронизирован с реальной resale schema
      (price а не price_aed, area/building без _name).
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


# Типы-алиасы для краткости.
_TEXTY = ("text", "character varying")
_DATEY = ("date", "timestamp without time zone", "timestamp", "text", "character varying")
_NUMY = ("numeric", "double precision", "text", "character varying", "bigint", "integer")
_TSTZ = ("timestamp with time zone", "timestamp without time zone", "timestamp")


# ----------------------------------------------------------------------
# Контракты на shared-таблицы
# ----------------------------------------------------------------------

# Live DLD sales — обновляется ежедневно из Dubai Pulse API (sales_scraper.py).
# Имена колонок — новые API-имена (transaction_date, area_en, building_en),
# а НЕ legacy CSV-имена (instance_date, area_name_en, building_name_en) —
# те живут только в архиве (dld_sale_archive).
DLD_TRANSACTIONS_FULL = TableContract(
    table="public.dld_transactions_full",
    db="live",
    description="DLD sales/mortgages/gifts (live, ~30 дней rolling) — Pulse API схема",
    consumers=("analytics-bot", "resale-bot", "roi-bot", "channel-poster",
               "lead-bot", "hub-bot"),
    required_columns={
        "transaction_date": _col(
            type=_DATEY,
            criticality="CRITICAL", min_non_null_pct=99.0,
            format_hint="TEXT — ISO 'YYYY-MM-DD' либо 'DD-MM-YYYY' — ВСЕГДА safe_date_sql()",
        ),
        "building_en": _col(
            type=_TEXTY,
            criticality="CRITICAL", min_non_null_pct=70.0,
            format_hint="Building name English (Pulse API)",
        ),
        "area_en": _col(
            type=_TEXTY,
            criticality="HIGH", min_non_null_pct=80.0,
        ),
        "rooms_en": _col(
            type=_TEXTY,
            criticality="HIGH", min_non_null_pct=70.0,
            format_hint="'1 B/R' | '2 B/R' | '3 B/R' | 'Studio' | 'PENTHOUSE'",
            expected_values_sample=("1 B/R", "2 B/R", "3 B/R", "Studio"),
        ),
        "actual_worth": _col(
            type=("numeric", "double precision"),
            criticality="CRITICAL", min_non_null_pct=95.0,
            format_hint="NUMERIC, может быть NULL для очень старых строк",
        ),
        "procedure_area": _col(
            type=("numeric", "double precision"),
            criticality="HIGH", min_non_null_pct=60.0,
        ),
    },
)

# Архивный DLD — исторические сделки, legacy CSV-import формат
# (другие имена колонок: instance_date, area_name_en, building_name_en).
DLD_SALE_ARCHIVE = TableContract(
    table="public.dld_sale_archive",
    db="archive",
    description="DLD sales archive (исторические, ETL legacy CSV)",
    consumers=("analytics-bot", "resale-bot", "roi-bot"),
    required_columns={
        "instance_date": _col(
            type=_DATEY,
            criticality="CRITICAL", min_non_null_pct=99.0,
            format_hint="Legacy 'DD-MM-YYYY' доминирует — обязательно safe_date_sql()",
        ),
        "building_name_en": _col(
            type=_TEXTY,
            criticality="CRITICAL", min_non_null_pct=70.0,
        ),
        "area_name_en": _col(
            type=_TEXTY,
            criticality="HIGH", min_non_null_pct=70.0,
        ),
        "rooms_en": _col(
            type=_TEXTY,
            criticality="HIGH", min_non_null_pct=50.0,
            expected_values_sample=("1 B/R", "2 B/R", "3 B/R", "Studio"),
        ),
        "actual_worth": _col(
            type=("numeric", "double precision", "text", "character varying"),
            criticality="CRITICAL", min_non_null_pct=90.0,
        ),
    },
)

# Live rentals — Pulse API схема (start_date / area_en / project_en).
DLD_RENTS_FULL = TableContract(
    table="public.dld_rents_full",
    db="live",
    description="DLD rent contracts (live) — Pulse API схема",
    consumers=("analytics-bot", "resale-bot", "hub-bot"),
    required_columns={
        "start_date": _col(
            type=_DATEY,
            criticality="CRITICAL", min_non_null_pct=95.0,
            format_hint="TEXT — safe_date_sql()",
        ),
        "annual_amount": _col(
            type=("numeric", "double precision"),
            criticality="CRITICAL", min_non_null_pct=85.0,
        ),
        "area_en": _col(
            type=_TEXTY,
            criticality="HIGH", min_non_null_pct=70.0,
        ),
        "project_en": _col(
            type=_TEXTY,
            criticality="HIGH", min_non_null_pct=25.0,
            format_hint="Building/project name в rent API. ~30% non-null — много старых contracts без project.",
        ),
    },
)

# Архивные rentals — legacy формат.
DLD_RENT_ARCHIVE = TableContract(
    table="public.dld_rent_archive",
    db="archive",
    description="DLD rent contracts archive",
    consumers=("analytics-bot",),
    required_columns={
        "contract_start_date": _col(
            type=_DATEY,
            criticality="CRITICAL", min_non_null_pct=95.0,
            format_hint="часто 'DD-MM-YYYY' — safe_date_sql()",
        ),
        "annual_amount": _col(
            type=("numeric", "double precision", "text", "character varying"),
            criticality="CRITICAL", min_non_null_pct=80.0,
        ),
        "area_name_en": _col(
            type=_TEXTY,
            criticality="HIGH", min_non_null_pct=60.0,
        ),
    },
)

# Resale read-model — реальные имена: price/area/building (без _aed/_name).
LISTINGS_V2 = TableContract(
    table="public.listings_v2",
    db="resale",
    description="Resale read-model (Dubai resale-bot)",
    consumers=("resale-bot", "channel-poster", "hub-bot", "lead-bot"),
    required_columns={
        "id": _col(type="integer", criticality="CRITICAL", min_non_null_pct=100.0),
        "price": _col(
            type=("bigint", "numeric", "double precision"),
            criticality="CRITICAL", min_non_null_pct=95.0,
            format_hint="raw price as posted (currency separate)",
        ),
        "bedrooms": _col(
            type=("integer", "smallint", "text"),
            criticality="HIGH", min_non_null_pct=70.0,
        ),
        "area": _col(
            type=_TEXTY,
            criticality="HIGH", min_non_null_pct=80.0,
        ),
        "building": _col(
            type=_TEXTY,
            criticality="HIGH", min_non_null_pct=60.0,
        ),
        "created_at": _col(
            type=_TSTZ,
            criticality="HIGH", min_non_null_pct=99.0,
        ),
        "status": _col(
            type=_TEXTY,
            criticality="HIGH", min_non_null_pct=95.0,
        ),
    },
)

# Leads — фактически живёт в RESALE БД (RESALE_DATABASE_URL).
# Колонки: telegram_user_id (новая) и telegram_id (legacy) сосуществуют.
LEADS = TableContract(
    table="public.leads",
    db="resale",
    description="Лиды от всех ботов (хранятся в resale БД)",
    consumers=("lead-bot", "analytics-bot", "hub-bot", "resale-bot"),
    required_columns={
        "id": _col(type=("integer", "bigint"), criticality="CRITICAL", min_non_null_pct=100.0),
        # telegram_id — фактически заполняется приложением (legacy column).
        "telegram_id": _col(
            type="bigint", criticality="CRITICAL", min_non_null_pct=95.0,
            format_hint="реально используемый user_id",
        ),
        # telegram_user_id — новая зарезервированная колонка, не пишется (LOW).
        "telegram_user_id": _col(
            type="bigint", criticality="LOW", min_non_null_pct=0.0,
            format_hint="зарезервировано; реально пишется telegram_id",
        ),
        # source_bot — задекларирован, но боты пока не пишут (TODO в lead-bot).
        "source_bot": _col(
            type=_TEXTY,
            criticality="LOW", min_non_null_pct=0.0,
            format_hint="TODO: боты должны проставлять при insert (планируется)",
            expected_values_sample=("analytics", "resale", "hub", "channel", "lead"),
        ),
        "created_at": _col(
            type=_TSTZ,
            criticality="CRITICAL", min_non_null_pct=99.0,
        ),
    },
)

# Users — фактически живёт в RESALE БД.
# Реальные имена: telegram_id (не tg_user_id), language (не lang),
# created_at (не first_seen_at).
USERS = TableContract(
    table="public.users",
    db="resale",
    description="Глобальный каталог пользователей (хранится в resale БД)",
    consumers=("hub-bot", "analytics-bot", "resale-bot", "channel-poster",
               "lead-bot", "roi-bot", "currency-bot"),
    required_columns={
        "telegram_id": _col(type="bigint", criticality="CRITICAL", min_non_null_pct=100.0),
        "language": _col(
            type=_TEXTY,
            criticality="HIGH", min_non_null_pct=80.0,
            expected_values_sample=("en", "ru"),
        ),
        "created_at": _col(
            type=_TSTZ,
            criticality="HIGH", min_non_null_pct=95.0,
        ),
    },
)

# Area price benchmark — материализованный профиль районов.
# Реальные имена: area (не area_name_en), bedrooms+property_type+deal_type ключ,
# median (не median_price_per_sqft), updated_at.
AREA_PRICE_BENCHMARK = TableContract(
    table="public.area_price_benchmark",
    db="resale",
    description="Материализованный профиль районов — цена + p10/median/p90 (resale БД)",
    consumers=("analytics-bot", "resale-bot", "roi-bot", "channel-poster"),
    required_columns={
        "area": _col(
            type=_TEXTY,
            criticality="CRITICAL", min_non_null_pct=100.0,
        ),
        "property_type": _col(
            type=_TEXTY,
            criticality="HIGH", min_non_null_pct=100.0,
        ),
        "bedrooms": _col(
            type=("integer", "smallint"),
            criticality="LOW", min_non_null_pct=0.0,
            format_hint="NULL для агрегатов «все спальни»",
        ),
        "deal_type": _col(
            type=_TEXTY,
            criticality="HIGH", min_non_null_pct=100.0,
            expected_values_sample=("sale", "rent"),
        ),
        "median": _col(
            type=("numeric", "double precision"),
            criticality="CRITICAL", min_non_null_pct=80.0,
        ),
        "sample_size": _col(
            type=("integer", "bigint"),
            criticality="HIGH", min_non_null_pct=100.0,
        ),
        "updated_at": _col(
            type=_TSTZ,
            criticality="HIGH", min_non_null_pct=95.0,
        ),
    },
)


CONTRACTS = [
    DLD_TRANSACTIONS_FULL,
    DLD_SALE_ARCHIVE,
    DLD_RENTS_FULL,
    # DLD_RENT_ARCHIVE retired 2026-06-13: table deprecated/empty (0 rows) after
    # DLD→Supabase consolidation; rent history lives in dld_rents_full. builder_rental
    # repointed; main/intelligence/daily_reports already use adaptive column detection.
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
