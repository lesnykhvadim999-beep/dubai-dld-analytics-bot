"""Daily pg_dump of all Railway Postgres DBs to local + offsite.

Run manually:
    python C:/Projects/shared/scripts/pg_dump_all.py

Or via Windows Task Scheduler (see BACKUP.md).

Env vars expected (with fallback to hardcoded discovery via Railway CLI):
    RESALE_DATABASE_URL_PUBLIC
    LIVE_DATABASE_URL_PUBLIC
    ARCHIVE_DATABASE_URL_PUBLIC
    INTELLIGENCE_DATABASE_URL_PUBLIC
"""
from __future__ import annotations
import subprocess
import os
import sys
import datetime
import shutil
from pathlib import Path

# Make shared/admin_notify importable
_SHARED = Path(r"C:\Projects\shared")
if str(_SHARED) not in sys.path:
    sys.path.insert(0, str(_SHARED))
try:
    from admin_notify import admin_notify  # type: ignore
except Exception:
    def admin_notify(text: str, **kw) -> bool:  # type: ignore
        print(f"[admin_notify-stub] {text}")
        return False


# Fallback DSNs (public Railway proxies). Prefer env vars if set.
_FALLBACK = {
    "resale":       os.environ.get("RESALE_DATABASE_URL", ""),
    "live":         os.environ.get("LIVE_DATABASE_URL", ""),
    "archive":      os.environ.get("ARCHIVE_DATABASE_URL", ""),
    "intelligence": os.environ.get("INTELLIGENCE_DATABASE_URL", ""),
}

DBS = {
    name: os.environ.get(f"{name.upper()}_DATABASE_URL_PUBLIC") or _FALLBACK[name]
    for name in _FALLBACK
}

BACKUP_DIR = Path(r"C:\BotsBackup\db_dumps")
BACKUP_DIR.mkdir(parents=True, exist_ok=True)

PG_DUMP = r"C:\Program Files\PostgreSQL\18\bin\pg_dump.exe"
if not Path(PG_DUMP).exists():
    PG_DUMP = "pg_dump"  # fall back to PATH

# Retention policy (local: 30 days, git push: latest only)
LOCAL_RETENTION_DAYS = 30
GIT_REMOTE_PUSH = os.environ.get("BACKUP_GIT_PUSH", "0") == "1"

today = datetime.date.today().isoformat()
results: list[tuple[str, bool, float, str]] = []

for name, dsn in DBS.items():
    if not dsn:
        print(f"[skip] {name}: no DSN")
        results.append((name, False, 0.0, "no DSN"))
        continue

    out = BACKUP_DIR / f"{name}_{today}.dump"
    print(f"[dump] {name} -> {out}")

    # Use custom format (-Fc) — compressed, restorable with pg_restore.
    cmd = [PG_DUMP, "--no-owner", "--no-acl", "-Fc", dsn]
    err = ""
    rc = -1
    try:
        with open(out, "wb") as f:
            proc = subprocess.run(
                cmd, stdout=f, stderr=subprocess.PIPE,
                timeout=1800,  # 30 min
            )
        rc = proc.returncode
        err = proc.stderr.decode("utf-8", errors="replace")[:500]
    except subprocess.TimeoutExpired:
        err = "TIMEOUT after 30 min"
    except Exception as e:
        err = f"EXCEPTION: {e}"

    if rc == 0 and out.exists() and out.stat().st_size > 0:
        sz_mb = out.stat().st_size / 1024 / 1024
        print(f"  OK {sz_mb:.1f} MB")
        results.append((name, True, sz_mb, ""))
    else:
        print(f"  FAIL rc={rc}: {err[:200]}")
        # clean up empty/partial file
        if out.exists() and out.stat().st_size == 0:
            out.unlink()
        results.append((name, False, 0.0, err[:200]))
        admin_notify(f"🔴 pg_dump FAIL: {name}\n{err[:300]}", priority="critical")


# Retention sweep: delete dumps older than LOCAL_RETENTION_DAYS
cutoff = datetime.date.today() - datetime.timedelta(days=LOCAL_RETENTION_DAYS)
pruned = 0
for f in BACKUP_DIR.glob("*.dump"):
    mtime = datetime.date.fromtimestamp(f.stat().st_mtime)
    if mtime < cutoff:
        f.unlink()
        pruned += 1
if pruned:
    print(f"[retention] pruned {pruned} dumps older than {LOCAL_RETENTION_DAYS}d")


# Disk usage
total_mb = sum(f.stat().st_size for f in BACKUP_DIR.glob("*.dump")) / 1024 / 1024
file_count = len(list(BACKUP_DIR.glob("*.dump")))
print(f"\nBackup dir: {file_count} files, {total_mb:.1f} MB total")


# Optional git push (offsite)
git_status = "skipped"
if GIT_REMOTE_PUSH and (BACKUP_DIR / ".git").exists():
    try:
        subprocess.run(["git", "add", "-A"], cwd=BACKUP_DIR, check=False, timeout=60)
        subprocess.run(
            ["git", "commit", "-m", f"Backup {today}"],
            cwd=BACKUP_DIR, check=False, timeout=60,
        )
        r = subprocess.run(
            ["git", "push", "origin", "main", "--force"],
            cwd=BACKUP_DIR, capture_output=True, timeout=600,
        )
        git_status = "pushed" if r.returncode == 0 else f"push-fail: {r.stderr.decode()[:120]}"
    except Exception as e:
        git_status = f"err: {e}"
print(f"git: {git_status}")


# Final admin notify
ok_count = sum(1 for r in results if r[1])
total_count = len(results)
lines = [f"✅ pg_dump_all {today}  {ok_count}/{total_count} OK  ({total_mb:.0f} MB local)"]
for name, ok, sz, err in results:
    mark = "✓" if ok else "✗"
    if ok:
        lines.append(f"  {mark} {name}: {sz:.1f} MB")
    else:
        lines.append(f"  {mark} {name}: {err[:60]}")
lines.append(f"git: {git_status}")
msg = "\n".join(lines)
print("\n" + msg)
admin_notify(msg, priority="normal")

# Exit non-zero if any failed (for Task Scheduler last-run-result)
sys.exit(0 if ok_count == total_count else 1)
