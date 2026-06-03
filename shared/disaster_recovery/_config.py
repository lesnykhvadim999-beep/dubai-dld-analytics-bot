"""Disaster Recovery — config: DSN list, paths, retention policy.

Vadim Realty + RERA BRN 65011.
NEVER store plain passwords in backups (GPG symmetric encryption).
"""
from __future__ import annotations
import os
from pathlib import Path

# ── DSNs (env-only — no hardcoded credentials, audit B5) ────────────────────
# Pre-commit hook blocks plain passwords; all DSNs must come from env vars.
_DB_NAMES = ("resale", "live", "archive", "intelligence")

def get_dsn(name: str) -> str:
    upper = name.upper()
    return (os.environ.get(f"{upper}_DATABASE_URL_PUBLIC")
            or os.environ.get(f"{upper}_DATABASE_URL")
            or (os.environ.get("LIVE_DATABASE_URL") if name == "resale" else "")
            or (os.environ.get("DATABASE_URL") if name == "resale" else "")
            or "")

ALL_DBS = list(_DB_NAMES)

# Intelligence DB = where bookkeeping tables live (backup_runs, restore_verifications)
META_DB = "intelligence"
def meta_dsn() -> str:
    return get_dsn(META_DB)

# ── Paths ───────────────────────────────────────────────────────────────────
BACKUP_ROOT = Path(os.environ.get("BACKUP_ROOT", r"C:\BotsBackup\db_dumps_v2"))
SCHEMA_GIT_DIR = Path(os.environ.get("BACKUP_SCHEMA_GIT_DIR", r"C:\BotsBackup\schemas"))
TEMP_RESTORE_DIR = Path(os.environ.get("BACKUP_TEMP_RESTORE_DIR", r"C:\BotsBackup\_restore_tmp"))

for p in (BACKUP_ROOT, SCHEMA_GIT_DIR, TEMP_RESTORE_DIR):
    p.mkdir(parents=True, exist_ok=True)

# Subfolders by backup_type
def backup_dir(backup_type: str) -> Path:
    p = BACKUP_ROOT / backup_type
    p.mkdir(parents=True, exist_ok=True)
    return p

# ── PostgreSQL binaries ─────────────────────────────────────────────────────
_PG_BIN = Path(r"C:\Program Files\PostgreSQL\18\bin")
PG_DUMP = str(_PG_BIN / "pg_dump.exe") if (_PG_BIN / "pg_dump.exe").exists() else "pg_dump"
PG_RESTORE = str(_PG_BIN / "pg_restore.exe") if (_PG_BIN / "pg_restore.exe").exists() else "pg_restore"
PSQL = str(_PG_BIN / "psql.exe") if (_PG_BIN / "psql.exe").exists() else "psql"

# ── Encryption ──────────────────────────────────────────────────────────────
# GPG symmetric. If env unset, fall back to deterministic per-host key file.
GPG_BIN = os.environ.get("GPG_BIN", "gpg")
_PASS_FILE = Path(os.environ.get("BACKUP_GPG_PASSFILE", r"C:\BotsBackup\.gpg_pass"))

def gpg_passphrase() -> str:
    """Return passphrase; generate+persist if missing.
    Priority: env BACKUP_GPG_PASSPHRASE > local pass file."""
    p = os.environ.get("BACKUP_GPG_PASSPHRASE")
    if p:
        return p
    if _PASS_FILE.exists():
        return _PASS_FILE.read_text(encoding="utf-8").strip()
    import secrets
    p = secrets.token_urlsafe(48)
    _PASS_FILE.parent.mkdir(parents=True, exist_ok=True)
    _PASS_FILE.write_text(p, encoding="utf-8")
    try:
        # restrict permissions on Windows: best-effort
        import stat
        _PASS_FILE.chmod(stat.S_IREAD | stat.S_IWRITE)
    except Exception:
        pass
    return p

# ── Retention policy ────────────────────────────────────────────────────────
# Buckets: daily / weekly / monthly / yearly
RETENTION_DAILY_DAYS = 7
RETENTION_WEEKLY_WEEKS = 12
RETENTION_MONTHLY_MONTHS = 12
RETENTION_YEARLY_FOREVER = True

# ── Storage backend selection (free tiers only) ─────────────────────────────
# Order: R2 → B2 → GitHub releases → local (fallback always).
R2_ACCESS_KEY = os.environ.get("R2_ACCESS_KEY") or os.environ.get("REACHABLE_FREE_STORAGE_R2_KEY")
R2_SECRET_KEY = os.environ.get("R2_SECRET_KEY") or os.environ.get("REACHABLE_FREE_STORAGE_R2_SECRET")
R2_BUCKET = os.environ.get("R2_BUCKET", "bots-backups")
R2_ENDPOINT = os.environ.get("R2_ENDPOINT")  # e.g. https://<acc>.r2.cloudflarestorage.com

B2_KEY_ID = os.environ.get("B2_KEY_ID")
B2_APP_KEY = os.environ.get("B2_APP_KEY")
B2_BUCKET = os.environ.get("B2_BUCKET", "bots-backups")

GITHUB_PAT = (os.environ.get("GITHUB_PAT")
              or os.environ.get("GH_TOKEN")
              or os.environ.get("GITHUB_TOKEN"))
GITHUB_BACKUP_REPO = os.environ.get(
    "GITHUB_BACKUP_REPO", "lesnykhvadim999-beep/db-backups-private"
)
GITHUB_SCHEMA_REPO = os.environ.get(
    "GITHUB_SCHEMA_REPO", "lesnykhvadim999-beep/db-schemas-versioned"
)

def storage_backend_name() -> str:
    if R2_ACCESS_KEY and R2_SECRET_KEY and R2_ENDPOINT:
        return "r2"
    if B2_KEY_ID and B2_APP_KEY:
        return "b2"
    if GITHUB_PAT:
        return "github"
    return "local"

# ── Timeouts ────────────────────────────────────────────────────────────────
DUMP_TIMEOUT_SEC = int(os.environ.get("BACKUP_DUMP_TIMEOUT", "3600"))   # 60 min
RESTORE_TIMEOUT_SEC = int(os.environ.get("BACKUP_RESTORE_TIMEOUT", "3600"))
UPLOAD_TIMEOUT_SEC = int(os.environ.get("BACKUP_UPLOAD_TIMEOUT", "1800"))

# ── Verification thresholds ─────────────────────────────────────────────────
# Tolerance: live DBs grow, so backup row count may be slightly less than current.
VERIFY_TABLES_DELTA_MAX = 1    # tables_count must equal current ± 1
VERIFY_ROWS_DELTA_PCT = 0.10   # rows total may be within 10% of current

ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID", "353806371")
