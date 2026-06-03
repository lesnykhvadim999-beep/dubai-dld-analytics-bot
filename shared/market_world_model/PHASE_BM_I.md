# PHASE BM Agent I — Layer 13: Dubai Market World Model

**Дата:** 2026-06-03
**Статус:** готово (production-ready с linear fallback; Prophet опционально)
**Канонический путь:** C:\Users\Вадим\.claude\projects\C--Cloude-code\memory\agents\PHASE_BM_I.md
(скопировать туда руками — sandbox запрещает запись в .claude/)

## Что построено

Единая модель рынка Дубая, отвечающая на любые «что будет с X через Y?»
вопросы. Доступна всем ботам как `from shared.market_world_model import api as market`.

### Схема БД (Railway resale Postgres)

4 таблицы (см. `shared/market_world_model/schema.sql`):

| Таблица | Назначение |
|---|---|
| `market_entities` | areas / buildings / property_types / developers (parent_id для иерархии, JSONB attrs, embedding bytea на будущее) |
| `market_state_snapshots` | весь state entity на момент captured_at (price_per_sqft, supply, demand, liquidity_tier) — для historical replay и обучения |
| `market_relationships` | типизированные рёбра (area_contains_building, building_by_developer, ...) |
| `market_models` | pickled Prophet (или linear payload) per (entity, metric) + rmse/samples |

### Текущая населённость (после initial seed + train)

- entities: 255 (57 areas + 198 buildings)
- snapshots: 1324 (недельная агрегация ~5 мес. истории listings)
- relationships: 200
- models: 130 linear (88 area + 42 building × 2 metrics: price_per_sqft, supply)

### Файлы (C:\Projects\shared\market_world_model\)

- `schema.sql` — 4 таблицы, идемпотентно
- `_db.py` — DSN + connect helper (env override MWM_DB_URL/RESALE_DB_URL/DATABASE_URL)
- `builder.py` — seed (top-50 areas + top-200 buildings из listings) + train (Prophet → fallback linear) + `weekly()` orchestrator. CLI: `--seed --train --weekly --no-prophet --limit N`
- `query.py` — forecast(entity, metric, horizon_months), what_if(entity, scenario), compare(a, b). Загружает оба типа моделей по приоритету prophet > linear
- `explainer.py` — NL объяснения RU/EN с подписью «Vadim Realty — RERA BRN 65011», hook на shared.causal_engine.client.explain
- `api.py` — фасад + `ask("свободный текст")` regex-парсер (horizon/compare/scenario)
- `cron_weekly.bat` — wrapper для Scheduled Task

### Интеграции в боты

- **analytics-bot** (C:\Projects\dubai-dld-analytics-bot-main\main.py):
  - Кнопка «🔮 Прогноз рынка» добавлена в main_menu (5-й ряд)
  - State `mwm_forecast_query` принимает свободный текст → `market.ask(text, lang="ru")`
- **resale-bot** (C:\Projects\resale-bot\resale_bot.py):
  - Кнопка-эмодзи 🔮 в обеих emoji-карточках (короткая + расширенная)
  - Callback `mwmfc|{lid}` — берёт building или area из listing и зовёт `forecast(target, "price_per_sqft", 12)`

### Cron weekly retrain

- Scheduled Task `mwm-weekly-retrain`, cron `0 2 * * 0` (вс 02:00 локально)
- Запускает `python -m shared.market_world_model.builder --weekly --no-prophet`
- Лог: `C:\Projects\shared\market_world_model\cron.log`

## Известные ограничения

- cmdstanpy на Python 3.14 + кириллическое имя пользователя «Вадим» в C:\Temp падает с STATUS_STACK_BUFFER_OVERRUN (0xC0000409). Primary algo на этой машине = linear regression (numpy-only) с RMSE как мерой ошибки и std резидуалов как 80%-CI. Prophet остаётся в pipeline (use_prophet=True по умолчанию), модели автоматически выбираются по приоритету. На Railway/Linux Prophet будет работать без правок.
- listings история ~5 мес. → недельная агрегация (~22 точки/area). Yearly seasonality отключена.
- developers пока 0 (поле buildings.developer пусто). Граф developer→building построится автоматически после заполнения этого поля.
- Эластичности в what_if (supply -0.3, demand +0.4, rates -0.8) — empirical, не калиброваны на DLD. Когда archive Pulse войдёт в snapshots — переоценить.

## Free-only

- DSN: единый Railway Postgres (resale)
- LLM: ноль. explainer использует только шаблоны + опциональный causal_engine
- Vadim Realty + RERA BRN 65011 в подписи каждого forecast-сообщения

## Точки расширения

1. Включить Prophet на Railway (Linux) — заработает автоматически
2. Прикрутить behavior_tracking (PHASE BL) как demand метрику в snapshots
3. Включить DLD archive (Dubai Pulse API) как второй source в market_state_snapshots
4. Сгенерировать area↔metro / area↔mall edges из публичных геоданных
5. channel-bot-new ежемесячный «Market Outlook» = top-5 forecasts из market_models (TODO)

## Smoke test (повторить)

```
cd C:\Projects
$env:PYTHONIOENCODING='utf-8'
python -m shared.market_world_model._smoke
```

Ожидаемый вывод: forecast Dubai Marina с current/predicted/pct_change, compare с winner_by_growth, what_if с scenario_shift_pct, ask на RU.
