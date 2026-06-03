# Layer 17 — Monthly Refresh cron

## What
Re-fits all 5 prebuilt causal studies on the freshest market_data /
DLD archive snapshot. Persists ATE + 95 % CI + pickled estimator into
`causal_studies` (UPSERT by `study_name`).

## When
Monthly: **1st of month, 04:00 UTC**.

Cron expression: `0 4 1 * *`

## How to register on Railway

1. Create a new Railway service in the **shared** project group (or attach
   to an existing scheduler service) with this repo mounted at
   `C:\Projects\shared` (or its remote equivalent).
2. Set the start command:
   ```
   python -m shared.causal_inference.cron_monthly_refresh
   ```
3. In Railway → Settings → Cron Schedule, paste:
   ```
   0 4 1 * *
   ```
4. Required env vars (all already configured for other shared services):
   - `INTELLIGENCE_DATABASE_URL` (or `ARCHIVE_DATABASE_URL` / `DATABASE_URL`)
   - *Optional:* `CEREBRAS_API_KEY` and/or `GROQ_API_KEY` — used to fill
     `causal_studies.natural_language` with an investor-friendly RU
     explanation. Job still succeeds without them.

## Manual run (smoke / disaster recovery)

```powershell
# from C:\Cloude code
.\venv_causal\Scripts\python.exe -m shared.causal_inference.cron_monthly_refresh
```

Exit codes:
- `0` — all 5 studies persisted successfully
- `2` — at least one study failed (see stdout log)

## Observability
Each run:
- prints `STUDY OK <name> ATE=… CI=… n=…` per study
- emits a single admin_notify ping summarising ok/fail counts (best effort)
- refreshes the `refreshed_at` column so `/causal_analysis` callers can
  see when data was last computed.
