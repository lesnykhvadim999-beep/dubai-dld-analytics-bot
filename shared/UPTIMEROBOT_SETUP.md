# UptimeRobot Free — External Monitoring Setup

**Дата создания**: 2026-05-30
**Цель**: External monitor поверх Railway healthcheck — на случай если Railway сам ляжет (region outage). UptimeRobot — independent infra.

## Контекст

После reliability audit 2026-05-30 в `railway.toml` каждого бота добавлен
`healthcheckPath = "/health"`. Railway теперь рестартит контейнер на 503.

**Но**: если Railway region уйдёт в outage целиком, healthcheck не сработает.
Внешний monitor (UptimeRobot) даст алерт даже в этом случае.

`synthetic_monitor.py` на ПК Вадима — fallback, но работает только когда ПК включён.
UptimeRobot — 24/7 независимо от ПК и Railway.

## Free tier limits

- 50 monitors (нам нужно 8)
- 5-min ping interval
- Email + Telegram + Slack alerts
- Public status page (опционально)

## Шаг 1 — Регистрация

1. https://uptimerobot.com → Sign up free (email `lesnykhvadim999@gmail.com`)
2. Подтвердить email
3. В Dashboard → "Add New Monitor"

## Шаг 2 — Добавить 8 monitor'ов

Для каждой строки таблицы:

- **Type**: HTTP(s)
- **URL**: см. таблицу ниже
- **Friendly Name**: см. таблицу
- **Monitoring Interval**: 5 minutes
- **Monitor Timeout**: 30 sec
- **Alert When Down**: 1 failure (immediate)

### Public URLs (получены через `railway domain` 2026-05-30)

| # | Bot              | Friendly Name        | URL для UptimeRobot                                                          |
|---|------------------|----------------------|------------------------------------------------------------------------------|
| 1 | resale-bot       | Dubai Resale Bot     | https://resale-bot-production.up.railway.app/health                          |
| 2 | analytics-bot    | DLD Analytics Bot    | https://dubai-dld-analytics-bot-production.up.railway.app/health             |
| 3 | hub-bot          | Hub Router Bot       | https://hub-bot-production.up.railway.app/health                             |
| 4 | lead-bot         | Lead Bot             | https://telegram-bot-production-a608.up.railway.app/health                   |
| 5 | roi-bot          | ROI Bot              | https://dubai-roi-bot-production.up.railway.app/health                       |
| 6 | channel-bot      | Channel Bot          | https://channel-bot-new-production-9184.up.railway.app/health                |
| 7 | currency-bot     | Currency Bot         | https://currency-bot-production-38d9.up.railway.app/health                   |
| 8 | cloud-watchdog   | Cloud Watchdog       | https://cloud-watchdog-production.up.railway.app/health                      |

⚠️ Перед добавлением каждого URL — проверь в браузере что отвечает 200 JSON.
Если 404/502 — значит deploy с healthcheck ещё не прошёл, подожди или проверь Railway logs.

⚠️ channel-bot main bot.py пока НЕ bind HTTP на $PORT — UptimeRobot будет
показывать DOWN для этого URL пока в `bot.py` не добавят `start_health_server`
(см. TODO в `C:/Projects/channel-bot-new/Channel-Bot-new/railway.toml`).

## Шаг 3 — Alert integrations

### Telegram (рекомендуется)

1. Settings → Integrations → "Add New Alert Contact"
2. Type: **Telegram**
3. UptimeRobot покажет инструкции:
   - Найти `@UptimeRobotBot` в Telegram
   - Отправить ему команду `/start`
   - Скопировать polled chat_id
   - Вставить в форму UptimeRobot
4. Применить новый Alert Contact ко всем 8 monitor'ам

Сообщения будут приходить от @UptimeRobotBot напрямую Вадиму.

### Email

Default — `lesnykhvadim999@gmail.com` уже подключён при регистрации.
Применить ко всем monitor'ам.

### Telegram (admin bot)

Можно создать кастомный webhook через бота `@vadim_admin_bot`:
- Endpoint: `https://api.telegram.org/bot<ADMIN_BOT_TOKEN>/sendMessage?chat_id=353806371&text=*MonitorName* is DOWN`
- Тип: Webhook (POST или GET)
- Доступно только в Pro tier — пропустить для Free.

## Шаг 4 — Public Status Page (опционально)

1. My Settings → Public Status Pages → "Add Public Status Page"
2. Name: "Vadim Realty Bots Status"
3. Monitors: select all 8
4. Custom URL: vadim-bots.status.uptimerobot.com (Free даёт subdomain)

Полезно — можно показывать клиентам / самому смотреть с телефона.

## Шаг 5 — Verify

Через 10 минут после настройки в Dashboard UptimeRobot:
- Все 8 monitor'ов — зелёный "Up"
- Average Response Time < 2 sec
- Uptime: 100%

Если какой-то DOWN:
1. Открой URL в браузере — должно быть 200 JSON `{"status":"ok","db":"ok",...}`
2. Если 503 — упала БД → проверь Railway logs (`railway logs`)
3. Если 502/504 — healthcheck не пройден → ещё нет HTTP server на $PORT
4. Если 404 — Railway не запушил healthcheckPath → redeploy

## Шаг 6 — Test alerting

В Dashboard UptimeRobot → выбери любой monitor → "Pause" на 1 минуту.
Проверь что пришёл alert в Telegram/email.
Затем "Resume" — должен прийти "Up" notification.

## Долгосрочно

- Free tier 50 monitor'ов хватит на ~6 ботов больше (если экосистема вырастет)
- При переезде в Cloud Run / Fly.io — URL'ы поменяются, обнови
- Можно добавить keyword check: "JSON response должен содержать `\"status\":\"ok\"`"
  → тогда даже HTTP 200 с `"status":"degraded"` будет считаться DOWN

## Cross-references

- Railway healthcheckPath audit: см. изменения в `*/railway.toml` (commit 2026-05-30)
- Synthetic monitor (PC): `C:\Users\Вадим\.claude\synthetic_monitor.py`
- Health handler implementation: `fsst_core.py:426-514` (`start_health_server`)
