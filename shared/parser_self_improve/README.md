# parser_self_improve — Phase 1

Self-improving парсер для resale-bot. Phase 1 из 5 (Roadmap PHASE BK).

## Что делает

1. **failed_collector** — log low-confidence/NULL extractions в `parser_failed_log`.
2. **discover_patterns** (weekly cron, MON 03:00) — кластеризует свежие фейлы
   через Gemini embeddings + k-means, для каждого кластера зовёт Cerebras с
   просьбой предложить regex/правило, шлёт админу в Telegram кнопку Approve/Reject.
3. **approval_handler** — обработка Telegram callback'ов.
4. **pattern_applier** — после approve добавляет regex в `parser_v2_extras.py`
   и создаёт git commit (без push).

## Архитектура (НЕ ломаем backward compat)

- parser_v2.py НЕ модифицируется напрямую.
- Auto-patterns добавляются ТОЛЬКО в `parser_v2_extras.py` между маркерами
  `# >>> AUTO_PATTERNS` / `# <<< AUTO_PATTERNS`.
- Apply патчей идёт только после Vadim approve через Telegram.

## DB

```sql
parser_failed_log         -- raw failures
parser_pattern_clusters   -- proposed rules + admin state
```

Schema создаётся idempotent через `_db.ensure_schema()` при первом запуске.

## Env

| Var | Покрытие |
| --- | --- |
| `RESALE_DATABASE_URL` / `DATABASE_URL` | основной DSN |
| `GEMINI_API_KEY` | embeddings (free 1500/day) |
| `CEREBRAS_API_KEY` → `GROQ_API_KEY` → `GEMINI_API_KEY` | LLM proposer fallback |
| `ADMIN_BOT_TOKEN` + `ADMIN_CHAT_ID` | Telegram уведомления |
| `RESALE_BOT_DIR` | путь к repo (default `C:\Projects\resale-bot`) |

## Запуск вручную

```powershell
py C:/Projects/shared/parser_self_improve/discover_patterns.py --backfill 500 --min 5
```

## Cron (Windows Task Scheduler)

```powershell
schtasks /Create /SC WEEKLY /D MON /ST 03:00 /TN "Claude Parser Self-Improve" `
  /TR "C:\Python314\pythonw.exe C:/Projects/shared/parser_self_improve/discover_patterns.py" /F
```

## Hard-rules соблюдены

- Free LLM chain only (Cerebras → Groq → Gemini).
- Postgres write-only через psycopg2 own connection.
- Никаких payed services.
- parser_v2.py modifications — только через approve.
