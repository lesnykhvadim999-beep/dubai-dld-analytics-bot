# DB Schema Contracts

Защита от schema drift в shared-таблицах экосистемы.

## Корневая причина существования

Регрессия B055 (Grande / Burj Khalifa "нет данных", 2026-05-30):
SQL-fragment `instance_date ~ '^\d{4}-\d{2}-\d{2}' THEN ::date` не матчил
формат `DD-MM-YYYY` (legacy DLD csv). Результат: `safe_date = NULL` на всех
строках архива, фильтр периода `safe_date >= CURRENT_DATE - 12 months` всё
отрезал, юзер видел "нет данных" вместо реальных сделок.

Один и тот же inline regex был скопирован в `base_from()`,
`date_expr_from_cols()`, `_date_expr_v67`. Фикс в одном месте не помогал.

**Решение**: единые helpers + декларативные контракты + boot-time
проверка + daily cron.

## Что лежит в `C:\Projects\shared\`

| Файл                       | Назначение                                              |
|----------------------------|----------------------------------------------------------|
| `safe_coerce.py`           | SQL-helpers: `safe_date_sql`, `safe_num_sql`, `safe_int_sql`, `safe_bool_sql`, `safe_text_sql` |
| `db_contracts.py`          | Декларативные `TableContract` + `CONTRACTS` список       |
| `contract_validator.py`    | Проверка фактической схемы против контракта             |
| `contract_boot_hook.py`    | `install_contract_check` / `async_contract_check` для main() |
| `cron_drift_check.py`      | Daily-report через @vadim_admin_bot (живёт в cloud-watchdog) |

## Как использовать в SQL

**Никогда не пишите inline `::date` после regex-проверки!**
Всегда:

```python
from safe_coerce import safe_date_sql

sql = f"""
  SELECT {safe_date_sql("instance_date")} AS d FROM public.dld_transactions_full
"""
```

Поддерживаемые форматы:
- ISO `'YYYY-MM-DD'` и `'YYYY-MM-DD HH:MM:SS'`
- legacy `'DD-MM-YYYY'`
- Dubai Pulse `'DD/MM/YYYY'`
- epoch seconds (`^\d{10}$`)
- неизвестный формат → `NULL` (без exception)

## Boot-time integration в боте

В `main()` каждого бота (после health-server, до polling):

```python
# sync main (resale-bot, lead-bot, hub-bot, channel-bot, cloud-watchdog):
from contract_boot_hook import install_contract_check
from admin_notify import admin_notify
install_contract_check(
    bot_name="resale",
    dsns={"resale": DATABASE_URL, "live": LIVE_DATABASE_URL},
    contracts_filter=["listings_v2", "users", "leads"],
    admin_notify=admin_notify,
)

# async main (analytics-bot, roi-bot, currency-bot):
import asyncio
from contract_boot_hook import async_contract_check
asyncio.create_task(async_contract_check(
    bot_name="analytics",
    dsns={"live": LIVE_DATABASE_URL, "archive": ARCHIVE_DATABASE_URL},
    contracts_filter=["dld_*", "users"],
    admin_notify=admin_notify,
))
```

Поведение:
- запускается в фоне через ~5 сек после старта бота (не задерживает polling)
- читает information_schema + sample NULL-pct
- если найден CRITICAL drift → шлёт алерт через admin_notify
- если `STRICT_CONTRACTS=1` → дополнительно `os._exit(1)`
- логи: `[contract] OK ...` / `[contract] drift detected: ...`

## Что делать, когда drift detected

1. **Прочитать сводку в @vadim_admin_bot** или в логах Railway:
   - `missing_column` — кто-то дропнул колонку из shared-таблицы
   - `type_mismatch` — тип изменился (например, `text` → `numeric`)
   - `null_pct` — > X% NULL в критической колонке (парсер сломался)
   - `format_drift` — `date_format_distribution` показал unknown форматы
   - `missing_table` — таблица не существует в БД (worst case)

2. **Найти source-of-truth**:
   - DLD таблицы: `dld_sync.py` в analytics-bot (Dubai Pulse API)
   - listings_v2: resale-bot parser pipeline
   - users / leads: hub-bot + bot_user_tracker модули

3. **Если регресс в источнике (парсер пишет broken data)** — исправить парсер,
   подождать следующий ETL цикл, drift исчезнет сам.

4. **Если намеренная миграция** — обновить контракт в
   `shared/db_contracts.py`, задеплоить во все 6 ботов + watchdog,
   следующий boot-check будет OK.

## Как добавить новую таблицу в контракты

1. Открыть `shared/db_contracts.py`.
2. Описать `TableContract` (см. `DLD_TRANSACTIONS_FULL` как пример).
3. Добавить в список `CONTRACTS`.
4. Скопировать обновлённый файл во все боты (см. ниже sync-команду).
5. Закомитить.

## Sync-команда (копия в каждый бот)

shared/ модули должны быть физически в каждом боте (Railway деплоит
бот-каталог отдельно, sys.path не указывает на shared). Команда:

```powershell
$SRC = "C:\Projects\shared"
$dst = @(
  "C:\Projects\dubai-dld-analytics-bot-main",
  "C:\Projects\resale-bot",
  "C:\Projects\roi-bot\ROI-bot",
  "C:\Projects\hub-bot",
  "C:\Projects\currency-bot",
  "C:\Projects\lead-bot\Lead-bot\telegram-bot",
  "C:\Projects\channel-bot-new\Channel-Bot-new\telegram-bot",
  "C:\Projects\cloud-watchdog"
)
$files = @("safe_coerce.py","db_contracts.py","contract_validator.py","contract_boot_hook.py")
foreach ($d in $dst) { foreach ($f in $files) { Copy-Item "$SRC\$f" "$d\$f" -Force } }
# cron_drift_check.py — только в cloud-watchdog
Copy-Item "$SRC\cron_drift_check.py" "C:\Projects\cloud-watchdog\cron_drift_check.py" -Force
```

## Env-flags

| Var                          | Default | Эффект                                         |
|------------------------------|---------|------------------------------------------------|
| `STRICT_CONTRACTS`           | `0`     | `1` → бот падает на старте при CRITICAL drift |
| `SCHEMA_DRIFT_INTERVAL_SEC`  | `86400` | Период daily cron в cloud-watchdog            |

## Daily cron

Лежит в `cloud-watchdog/cron_drift_check.py`. Запускается из
`cloud_watchdog.py:check_schema_drift()` раз в `SCHEMA_DRIFT_INTERVAL_SEC`
(default = 24h). Шлёт markdown-сводку в @vadim_admin_bot:

```
✅ DLD Schema Drift Check — 2026-05-31
tables=8 CRIT=0 HIGH=0 LOW=0 db_errors=0 dur=1234ms

✅ public.dld_transactions_full: 0 violations
✅ public.dld_sale_archive: 0 violations
⚠️ public.dld_rent_archive: CRIT=0 HIGH=1
   • contract_start_date: format_drift (12/100 unknown format(s))
✅ public.listings_v2: 0 violations
...
```
