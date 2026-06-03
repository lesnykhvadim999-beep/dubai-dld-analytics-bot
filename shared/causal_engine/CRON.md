# Causal Engine — Cron Schedule

## Weekly learner + validator

**When:** Sunday 06:00 UTC
**What:** Pulls free RSS + public Telegram channels, extracts new causal
patterns via LLM, runs backtest against DLD archive, updates confidence.

### crontab (Linux / Railway cron service)
```
0 6 * * 0  cd /app && python -m shared.causal_engine.cron_weekly_learner >> /var/log/causal_learner.log 2>&1
```

### Windows Task Scheduler
```
schtasks /Create /SC WEEKLY /D SUN /ST 06:00 ^
  /TN "CausalEngine\WeeklyLearner" ^
  /TR "python C:\Projects\shared\causal_engine\cron_weekly_learner.py"
```

### Railway (railway.json snippet)
```json
{
  "cron": [
    {"schedule": "0 6 * * 0",
     "command": "python -m shared.causal_engine.cron_weekly_learner"}
  ]
}
```

## Environment variables expected
- `INTELLIGENCE_DB_DSN` — Postgres DSN (default = hardcoded Railway DSN)
- `CEREBRAS_API_KEY` / `GROQ_API_KEY` — for free LLM extraction
- `TELEGRAM_API_ID` / `TELEGRAM_API_HASH` / `TELEGRAM_SESSION_STRING` —
  optional, enables Telegram channel scraping via Telethon
- `ADMIN_BOT_TOKEN` + `ADMIN_CHAT_ID` — admin Telegram alerts on completion
