# Ecosystem Contracts

Декларативный реестр кросс-бот зависимостей: shared-таблицы, межбот-вызовы,
deep-links, cron-цепочки, внешние API. Каждый контракт = ожидание одного
компонента от другого. Нарушение контракта — потенциальный silent bug в проде.

Этот файл дополняет:
- `shared/db_contracts.py` — schema-level (колонки, типы, % NULL)
- `shared/contracts_registry.py` — runtime verification (boot-time)
- `shared/safe_coerce.py` — SQL helpers (safe_date_sql и др.)
- `shared/empty_guard.py` — runtime empty-result alarm

**Если меняешь контракт**: одновременно обнови соответствующий decl в
`contracts_registry.py` и прогони `python C:/Projects/shared/contracts_registry.py --verify-all`.

---

## 1. Shared Tables

### 1.1 `public.dld_transactions_full` (LIVE DB — `LIVE_DATABASE_URL`)

- **Owner (writer)**: `dubai-dld-analytics-bot-main/dld_sync.py` — cron every 24h
- **Readers**:
  - analytics-bot — main read path
  - resale-bot — cross-check / area benchmarks (`build_area_benchmarks.py`, `build_area_profiles.py`)
  - roi-bot — area benchmarks для ROI расчёта
  - channel-poster — "deal of the day"
  - hub-bot — diagnostics + watchdog
  - lead-bot — area scoring
- **Schema contract**: `shared/db_contracts.py:DLD_TRANSACTIONS_FULL`
- **Date format**: `instance_date` поддерживает ISO `YYYY-MM-DD` + legacy `DD-MM-YYYY` (DLD csv export).
  **ОБЯЗАТЕЛЬНО** использовать `safe_date_sql('instance_date')` — иначе silent NULL.
  Антипаттерн ловится pre-commit hook'ом (`inline_date_regex`).
- **Stale threshold**: данные должны быть ≤72h old. dld_sync падает → analytics показывает degraded mode.
- **Failure mode (writer down)**: readers видят несвежие данные. boot-time check бьёт алерт админу.
- **Failure mode (silent NULL)**: пустой ответ от фильтра по периоду → empty_guard ловит → admin alert.

### 1.2 `public.dld_transactions_archive` (LIVE DB)

- **Owner**: `dubai-dld-analytics-bot-main/dld_sync.py` — append-only history
- **Readers**: analytics-bot (historic queries), roi-bot (long-horizon ROI)
- **Schema contract**: `shared/db_contracts.py:DLD_TRANSACTIONS_ARCHIVE`
- **Same date-format gotcha** как в 1.1 — использовать `safe_date_sql()`.
- **Failure mode**: missing → historic graphs пустые. boot-check существование таблицы.

### 1.3 `public.listings_v2` (RESALE DB — `RESALE_DATABASE_URL`)

- **Owner**: `resale-bot/parser_v2` (write)
- **Readers**: resale-bot (search), hub-bot (detail view), lead-bot (matching), channel-poster (sharing)
- **Required columns** (см. `db_contracts.py`): `id, status, area, building, price_aed, bedrooms, area_sqft, source_url, posted_at, parsed_ok, photos`
- **Status enum**: `active | inactive | spam | deleted`
- **Idempotency key**: `(source_url, posted_at)` — duplicate detection
- **Failure mode**: readers видят stale listings или пустой результат. empty_guard alert.

### 1.4 `public.pdf_reports` (LIVE DB)

- **Owner**: `shared/vadim_pdf.py` (write/read через `pdf_cache`)
- **Readers**: resale-bot, roi-bot, lead-bot, hub-bot, analytics-bot — общий 10-стр PDF
- **Contract**: 1 row per (lang, scope, name, period) — idempotent
- **Failure mode**: cache miss → fallback на regeneration через Cerebras. Допустимо.

### 1.5 `public.leads` (RESALE DB)

- **Owner**: `lead-bot/main.py` (write/read)
- **Readers**: hub-bot (показать ушедшие leads), staging-processor (audit)
- **Required cols**: `id, user_id, source_bot, payload, created_at, status`
- **status enum**: `new | qualified | rejected | converted`

### 1.6 `public.audit_log` (LIVE DB)

- **Owner**: `shared/audit_log.py` (multi-writer)
- **Readers**: admin-bot-handler, qa-tester, health-reporter
- **Required cols**: `id, ts, bot, user_id, action, payload_json`
- **Retention**: 90 дней (cron в health-reporter)

### 1.7 `public.bot_users` (LIVE DB)

- **Owner**: каждый бот пишет своих юзеров (shared writer pattern)
- **Readers**: hub-bot (cross-bot anchors), health-reporter (MAU/DAU)
- **Required cols**: `bot, user_id, lang, first_seen, last_seen`
- **lang values**: `en | ru | ar` — соответствует i18n contract (см. §4)

---

## 2. Bot → Bot Communication

### 2.1 lead-bot ← all bots (entry point для лидов)

- **URL pattern**: `https://t.me/dubai_fpr_lead_bot?start=<payload>`
- **payload formats**:
  - `from_<bot>` — простой источник (from_resale, from_roi, from_hub, from_analytics)
  - `from_<bot>_utm_<campaign>` — c UTM (from_hub_utm_hub_lead)
  - `proj-<dev>-<loc>-<prc>-<comp>` — конкретный проект (из channel-bot offplan)
  - `listing_<id>` — конкретный лот из resale-bot
  - `area_<name>` — район из analytics
- **lead-bot контракт**: должен резолвить любой `?start=` payload ≤5s.
  Неизвестный payload → fallback на главное меню + log.
- **SLA**: 5s response, 99% uptime (deep-link landing).

### 2.2 resale-bot ← (hub, channel-bot, analytics, roi)

- **URL patterns**:
  - `https://t.me/dubai_resale_fpr_bot?start=from_<bot>` — generic
  - `?start=from_offplan_<proj_id>` — из channel-bot
  - `?start=area_<area_name_urlencoded>` — из analytics-bot
  - `?start=bld_<building_name_urlencoded>` — из analytics-bot building view
  - `?start=listing_<id>` — конкретный лот
- **Resolution SLA**: 3s. Не нашли listing → меню "search by area" с подставленным районом.

### 2.3 roi-bot ← (hub, channel, resale, analytics)

- **URL pattern**: `https://t.me/dubai_roi_fpr_bot?start=from_<bot>[_utm_*]`
- **Special payloads**:
  - `from_resale_utm_resale_card_detail` — из карточки в resale → запустить ROI калькулятор с building
  - `area_<name>` — посчитать ROI для района

### 2.4 channel-bot (projects monitor) ← (hub, analytics, roi)

- **URL**: `https://t.me/dubai_projects_monitor_bot?start=area_<name>` или `?start=from_<bot>`
- **Note**: репо `channel-bot-new/Channel-Bot-new/telegram-bot/` (старое имя сохранено)

### 2.5 hub-bot → all bots

- **Hub-bot** = entry point. Каждый исходящий deep-link имеет UTM:
  `?start=from_hub_utm_hub_<feature>`
- **Hub-bot контракт**: при boot читает `BOT_*` константы из env, валидирует что они оканчиваются на `?start=from_hub_*`. Если нет — alert.

### 2.6 admin-bot-handler ← all bots (admin notifications)

- **Mechanism**: `shared/admin_notify.py:admin_notify(text, parse_mode, priority)`
- **Channel**: HTTP запрос к Telegram API через `ADMIN_BOT_TOKEN` env.
- **Admin chat_id**: `353806371` (Vadim)
- **Priority levels**: `low | normal | high | critical`
- **Failure mode**: если токен пуст → log.warning, не падать. Боты не должны зависеть от факта доставки.

### 2.7 currency-bot — read-only API для всех

- **Interface**: `from currency import to_aed(amount, ccy)` (shared/ или импорт)
- **Source**: `currency-bot` обновляет таблицу `public.fx_rates` каждые 4h.
- **Failure mode**: stale > 24h → fallback на hard-coded базовые курсы + alert.

---

## 3. Cron / Background Service Contracts

### 3.1 dld_sync (analytics-bot)

- **Schedule**: every 24h (Railway cron)
- **Output**: writes to `public.dld_transactions_full` + archive
- **Heartbeat**: `health-reporter` следит за `pg_last_xact_replay` + last `instance_date`
- **If late >72h**: admin alert, downstream боты переходят в degraded mode.

### 3.2 staging-processor

- **Schedule**: every 5 min
- **Reads**: `public.staging_queue`
- **MAX_ATTEMPTS**: 4 (после — DELETE; см. `feedback_staging_rules`)
- **Dup detection**: instant-delete (см. правила)

### 3.3 audit-queue-processor

- **Schedule**: every 1 min
- **Cleanup rule**: audit без area+price → DELETE (см. `feedback_db_cleanup_rules`)

### 3.4 reelly-enrichment-service

- **Schedule**: triggered (по pull queue)
- **Input**: listing_ids без enrichment
- **Output**: updates `listings_v2.enriched_*`
- **SLA**: best-effort, не блокирует publish.

### 3.5 health-reporter

- **Schedule**: every 1h (sweep), daily summary at 09:00 GMT+4
- **Reads**: все боты' user_count, error_rate, last_seen
- **Outputs**: admin chat summary
- **Triggers** boot-check для всех contracts.

### 3.6 cloud-watchdog

- **Schedule**: 30s heartbeat ping всем ботам
- **If down >3 ping**: admin alert.

### 3.7 channel-poster

- **Schedule**: cron 10:00 GMT+4 (daily deal)
- **Reads**: `dld_transactions_full` + `listings_v2`
- **Whitelist**: статичный пост шлёт только утверждённый whitelist
  (см. `feedback_fliplux_static_post`)

### 3.8 personal-poster

- **Schedule**: manual/triggered
- **Output**: персональные посты Vadim Realty (RERA BRN 65011)

---

## 4. i18n Contract

- **Source of truth**: user's `lang` в `public.bot_users` (en|ru|ar)
- **Lookup**: `from shared.i18n import lang_of; lang = lang_of(user_id)` — каждый бот.
- **Fallback chain**: lookup БД → Telegram `user.language_code` → `en` (default).
- **Anti-pattern**: `tr(None)`, `lang(None)`, `_NO_DATA_BODY["en"]` без `lang_of()` —
  ловится pre-commit hook'ом (`i18n_missing_user_id`, `hardcoded_en_body`).
- **Welcome message**: EN+RU bilingual block по умолчанию (см. `feedback_logos_and_welcome`).

---

## 5. External API Contracts

### 5.1 DLD Pulse (Dubai government)

- **URL**: `https://www.dubaipulse.gov.ae/...`
- **Used by**: dld_sync.py
- **Auth**: anonymous (public dataset)
- **Rate limit**: ~10 req/min (soft)
- **Failure mode**: 503 / DDoS protection → exponential backoff, retry 3x, fallback cached.

### 5.2 Reelly API (off-plan deals enrichment)

- **Used by**: reelly-enrichment-service, channel-bot
- **Auth**: API key (env `REELLY_API_KEY`)
- **Rate limit**: ~60/min
- **Failure mode**: 403 → degraded mode (без enrichment).

### 5.3 LLM Providers Chain (Cerebras → Groq → OpenRouter → Anthropic)

- **Reference**: `memory/reference_llm_providers.md`
- **Used by**: vadim_pdf.py (summary), photo_ocr.py, intelligence_router.py
- **Failure mode**: provider down → switch next в chain. Anthropic last resort (платный).
- **Constraint**: НИКАКИХ платных вызовов вне Anthropic fallback (см. `feedback_no_paid_services`).

### 5.4 Telegram Bot API

- **Used by**: все боты
- **Token env**: каждый бот свой `*_BOT_TOKEN` (см. `reference_ecosystem_map`)
- **Rate limit**: 30 msg/sec/bot
- **Failure mode**: 429 → backoff. 401 → бот падает (токен ревокнут).

### 5.5 Cloudflare Worker LLM Proxy

- **URL**: `cf-worker-llm-proxy` (private worker)
- **Used by**: некоторые боты вместо прямого LLM
- **Auth**: shared secret
- **Failure mode**: 502 → fallback на direct LLM chain.

---

## 6. Failure Contracts (Global)

- **Boot-time contract violation** → log + admin alert (high priority) + bot starts in degraded mode.
- **Runtime contract violation** (e.g., shared table missing column) →
  empty_guard catches empty result → admin alert (normal priority).
- **Cron-chain break** (writer down >SLA) → health-reporter alerts → readers degrade.
- **STRICT_CONTRACTS=1 env** → contract violation at boot CRASHES the bot
  (use in staging/CI; в проде False по умолчанию).

---

## 7. Versioning / Change Procedure

1. Поменять контракт → обновить этот файл + `db_contracts.py` + `contracts_registry.py`.
2. Запустить `python C:/Projects/shared/contracts_registry.py --verify-all` локально.
3. Развернуть owner-сервис первым (тот, кто пишет таблицу/шлёт сообщение).
4. Развернуть readers следом.
5. После 24h без алертов — закрыть change.
6. Если есть «breaking» (например, переименована колонка) — пометить как `MIGRATION_REQUIRED`
   в контракте, чтобы каждый reader сделал coerce + fallback оба варианта на 7+ дней.

---

## 8. Inventory (текущее состояние)

| Контракт | Тип | Owner | Readers | Verified by |
|---|---|---|---|---|
| dld_transactions_full | shared-table | analytics | analytics, resale, roi, channel, lead, hub | db_contracts.py |
| dld_transactions_archive | shared-table | analytics | analytics, roi | db_contracts.py |
| listings_v2 | shared-table | resale | resale, hub, lead, channel | db_contracts.py |
| pdf_reports | shared-table | vadim_pdf | 5 ботов | smoke_test |
| leads | shared-table | lead-bot | hub, staging | manual |
| audit_log | shared-table | shared | admin, qa, health | manual |
| bot_users | shared-table | каждый бот | hub, health | manual |
| lead-bot deep-links | bot-bot | lead-bot | все боты | resolver smoke |
| resale-bot deep-links | bot-bot | resale | hub, channel, analytics, roi | resolver smoke |
| roi-bot deep-links | bot-bot | roi | hub, channel, resale | resolver smoke |
| channel-bot deep-links | bot-bot | channel | hub, analytics, roi | resolver smoke |
| hub-bot UTM outflow | bot-bot | hub | все боты | boot-check |
| admin_notify | bot-service | admin-bot | все боты | smoke |
| currency to_aed | shared-lib | currency-bot | все боты | smoke |
| dld_sync cron | cron | dld_sync | downstream | health-reporter |
| staging-processor cron | cron | staging | resale | health-reporter |
| audit-queue cron | cron | audit-proc | shared | health-reporter |
| health-reporter | cron | health | meta | own heartbeat |
| cloud-watchdog | cron | watchdog | meta | own heartbeat |
| channel-poster cron | cron | channel-poster | TG channel | health-reporter |
| DLD Pulse API | external | govt | dld_sync | dld_sync retry |
| Reelly API | external | reelly | channel, reelly-svc | degraded mode |
| LLM chain | external | shared | 7+ ботов | reference_llm_providers |
| Telegram Bot API | external | telegram | все | n/a |

Минимум 23 задокументированных контракта.

## Schema Drift Log (auto)
Auto-generated by Claude (PHASE BL Level 6). Append-only.

- 2026-06-03 `f9e1745` **CREATE TABLE** `IF`
- 2026-06-03 `f9e1745` **ALTER TABLE** `ADD`
