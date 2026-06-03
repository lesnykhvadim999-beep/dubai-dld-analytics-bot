# Vadim Realty Ecosystem — Quick Start

_Last refreshed: 2026-06-03 10:20 UTC_

Brand: **Vadim Realty** · RERA BRN **65011**.

## Table of Contents
1. [Add a new bot](#1-add-a-new-bot)
2. [Add a new LLM provider](#2-add-a-new-llm-provider)
3. [Restart the cron worker](#3-restart-the-cron-worker)
4. [Rollback a deploy](#4-rollback-a-deploy)
5. [Where to find logs](#5-where-to-find-logs)
6. [What to do when Cerebras goes down](#6-what-to-do-when-cerebras-goes-down)
7. [Add a new admin](#7-add-a-new-admin)

---

## 1. Add a new bot

1. Create the repo under `C:\Projects\<bot-name>`.
2. Add `nixpacks.toml`:
   ```
   providers = ["python"]
   [variables]
   PYTHONPATH = "/app:/app/shared"
   ```
3. Add `railway.toml` with `startCommand`, `/health` healthcheck,
   `numReplicas = 2` for user-facing bots.
4. Register the bot in:
   - `shared/_sync_to_bots.py` → `BOTS` list.
   - `shared/auto_docs_v2/__init__.py` → `BOTS` list.
5. Bundle shared/: `python C:/Projects/shared/_sync_to_bots.py --bot <bot-name>`.
6. Create Railway project + service, set env vars (DSN, BOT_TOKEN). DSN MUST
   come from env — never commit it.
7. `git push` → Railway auto-deploys.

## 2. Add a new LLM provider

1. Add the API key as an env var on every bot/service (e.g. `NEWPROV_API_KEY`).
2. Open `shared/llm_chain.py` (master copy) and add a new `_call_newprov(prompt)`
   function following the existing pattern (timeout, retry, error normalisation).
3. Insert the provider into `PROVIDERS` list in the desired position. Free
   providers go BEFORE paid ones.
4. Register provider in `llm_provider_health` table (`INSERT … ON CONFLICT DO NOTHING`).
5. Re-sync: `python C:/Projects/shared/_sync_to_bots.py`.
6. Smoke test: `python -c "from llm_chain import llm_call; print(llm_call('ping'))"`.

## 3. Restart the cron worker

`phase_bm_master_cron` runs as a Railway worker (project `vadim-dubai-bot`,
service `cron-worker`).

- Soft restart:    `C:\Temp\railway.exe service restart cron-worker`
- Inspect state:   `C:\Projects\shared\scripts\phase_bm_cron_state.json`
- Force a single job: `python -m shared.scripts.phase_bm_master_cron --run <job_name>`
  (if `--run` is supported in your build — otherwise invoke the job's argv
   from JOBS list directly).

## 4. Rollback a deploy

1. Find last-good deploy: `C:\Temp\railway.exe deploys` or open the Railway
   dashboard → service → Deploys.
2. Click "Redeploy" on the green one. OR locally:
   ```
   git log --oneline -n 10
   git revert <bad-sha>
   git push
   ```
3. Watch logs: `C:\Temp\railway.exe logs --tail 50`.

## 5. Where to find logs

- **Railway service logs**: `C:\Temp\railway.exe logs --service <service> --tail 100`.
- **Cron run history**: `cron_run_log` table — `SELECT job_name, started_at, rc, elapsed_ms FROM cron_run_log ORDER BY started_at DESC LIMIT 30;`.
- **Audit log**: `audit_log` table (shared module `shared/audit_log.py`).
- **Bot watchdog**: project `vadim-dubai-bot`, service `cloud-watchdog` — pushes
  alerts to admin Telegram chat directly.

## 6. What to do when Cerebras goes down

1. Confirm: `SELECT * FROM llm_provider_health WHERE provider='cerebras' ORDER BY checked_at DESC LIMIT 5;`
2. Chain auto-falls-back to Groq → Together → Gemini → OpenRouter → Anthropic.
3. If user-facing latency spikes:
   - Disable PDF AI summary temporarily: `C:\Temp\railway.exe variable set PDF_SUMMARY_DISABLED=1`.
   - Increase chain timeout cap: `LLM_CHAIN_TIMEOUT_MS=8000`.
4. Cerebras free quota resets daily UTC midnight — monitor `quota_tracker` table.
5. Document the incident: append to `memory/bug_knowledge_base.md`
   (next free Bxxx id).

## 7. Add a new admin

1. Get the new admin's Telegram numeric user id (forward a message to @userinfobot).
2. Append to `ADMIN_IDS` env var in EVERY bot service (comma-separated).
3. `C:\Temp\railway.exe variable set ADMIN_IDS=<existing,new>` per service.
4. Restart services. Verify `/stats` or admin commands respond for the new id.
5. Record in `memory/agents/REGISTRY.md` if they own any agents.

---

_Maintained by Vadim Realty. Do NOT commit DSNs, bot tokens, or API keys into
this document — it is published to the team and indexed locally._
