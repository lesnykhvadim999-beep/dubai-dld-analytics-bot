# DB Backup Strategy

Disaster-recovery setup for all Railway-hosted Postgres databases.

## Databases backed up

| Name | Public host | Approx size (2026-05-30) | Notes |
|------|-------------|--------------------------|-------|
| `resale` | `tramway.proxy.rlwy.net:23228` | ~217 MB | Resale listings, smart-pick, master tables |
| `live` | `yamanote.proxy.rlwy.net:43494` | ~85 MB | DLD live transactions |
| `archive` | `switchback.proxy.rlwy.net:23244` | **~3909 MB (78%)** | DLD archives — see "Volume extend" below |
| `intelligence` | `autorack.proxy.rlwy.net:25004` | ~98 MB | DLD intelligence read-model |

## Backups location

* **Local**: `C:\BotsBackup\db_dumps\<name>_<YYYY-MM-DD>.dump`
  * Format: `pg_dump -Fc` (custom, restorable with `pg_restore`)
  * Retention: 30 days (older files auto-pruned)
* **Offsite (optional, opt-in)**: git-push to a private GitHub repo.
  * Enabled by `BACKUP_GIT_PUSH=1` env var on the host.
  * Repo `C:\BotsBackup\db_dumps` is already `git init`ed (branch `main`).
  * To finish setup: `gh auth login` then `gh repo create lesnykhvadim999-beep/db-backups-private --private --source=C:\BotsBackup\db_dumps --push`
  * **Warning**: GitHub free repo soft-limit is ~1 GB. ARCHIVE dump compressed is ~500 MB → keep at most last 1-2 days in git. For full 30d retention use **Backblaze B2** (10 GB free) or **Cloudflare R2** (10 GB free).

## Schedule

Windows Task Scheduler (created via `schtasks`):

| Task | When | Action |
|------|------|--------|
| `Claude DB Backup` | Daily 03:00 local | `pythonw.exe C:\Projects\shared\scripts\pg_dump_all.py` |
| `Claude DB Disk Check` | Daily 03:30 local | `pythonw.exe C:\Projects\shared\scripts\db_disk_check.py` |

Manual run any time:

```powershell
python C:\Projects\shared\scripts\pg_dump_all.py
python C:\Projects\shared\scripts\db_disk_check.py
```

## Required env vars (User scope, already set)

```
ADMIN_BOT_TOKEN        — for Telegram alerts (admin_notify.py)
ADMIN_CHAT_ID          — 353806371 (Vadim)
RESALE_DATABASE_URL_PUBLIC
LIVE_DATABASE_URL_PUBLIC
ARCHIVE_DATABASE_URL_PUBLIC
INTELLIGENCE_DATABASE_URL_PUBLIC
```

The scripts also embed fallback DSN constants so they keep working if env vars are wiped.

## Disk-fill alert thresholds

`db_disk_check.py` against Railway's 5 GB plan:
* `>= 80%` → 🟡 WARN (Telegram alert)
* `>= 90%` → 🔴 CRIT (Telegram alert + non-zero exit)

Override via env: `DB_VOLUME_LIMIT_MB=10000` after volume extend.

## Recovery procedure

```powershell
# 1. Pick a dump
$dump = "C:\BotsBackup\db_dumps\archive_2026-05-30.dump"

# 2. Restore to a fresh DB (NEVER overwrite production directly)
& "C:\Program Files\PostgreSQL\18\bin\pg_restore.exe" `
    --no-owner --no-acl --clean --if-exists `
    -d "postgresql://postgres:PASS@HOST:PORT/railway" `
    $dump

# Alternative: via Railway dashboard → service → Backups (if enabled there too)
```

## ⚠️ Action item for Vadim

**ARCHIVE volume at 78% of 5 GB and growing.** Extend now:

1. Railway dashboard → project **devoted-passion** → service **Rent-sale-arhiv**
2. Settings → Storage → Increase volume to **10 GB**
3. After resize, update env var on host machine:
   ```powershell
   [Environment]::SetEnvironmentVariable("DB_VOLUME_LIMIT_MB","10000","User")
   ```

## Files

* `C:\Projects\shared\scripts\pg_dump_all.py` — dump runner
* `C:\Projects\shared\scripts\db_disk_check.py` — daily disk-usage check
* `C:\Projects\shared\BACKUP.md` — this file
* `C:\BotsBackup\db_dumps\` — local backup storage
