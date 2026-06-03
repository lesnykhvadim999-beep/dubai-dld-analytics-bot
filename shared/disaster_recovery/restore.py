"""Disaster Recovery — restore helper.

Скачивает последний (или указанный) backup из storage backend и (опционально)
запускает pg_restore. Для DR drill хватит download + integrity check;
полный restore требует целевой пустой БД и установленного pg_restore.

CLI:
    # последний intelligence из daily bucket → ./<file>
    python -m shared.disaster_recovery.restore --db intelligence

    # restore в конкретный DSN (env var, не hardcode!)
    python -m shared.disaster_recovery.restore --db intelligence \
        --target "$LIVE_DATABASE_URL"

Vadim Realty + RERA BRN 65011.
"""
from __future__ import annotations
import argparse
import datetime as _dt
import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

from . import _config as cfg
from .storage_adapter import get_backend

log = logging.getLogger("dr.restore")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)


def _parse_dsn(dsn: str) -> dict:
    u = urlparse(dsn)
    return {
        "host": u.hostname or "",
        "port": str(u.port or 5432),
        "user": u.username or "",
        "password": u.password or "",
        "dbname": (u.path or "/").lstrip("/") or "postgres",
    }


def find_latest(db_name: str, bucket: str = "daily") -> tuple[str, int, _dt.datetime] | None:
    """Возвращает (key, size, mtime) самого свежего файла для db в bucket."""
    backend = get_backend()
    entries = backend.list(prefix=bucket)
    matched = [e for e in entries if f"/{db_name}_" in "/" + e[0]]
    if not matched:
        return None
    matched.sort(key=lambda x: x[2], reverse=True)
    return matched[0]


def download(key: str, dest: Path) -> Path:
    backend = get_backend()
    dest.parent.mkdir(parents=True, exist_ok=True)
    backend.download(key, dest)
    if not dest.exists() or dest.stat().st_size == 0:
        raise RuntimeError(f"downloaded file empty: {dest}")
    return dest


def pg_restore(file_path: Path, target_dsn: str, *, clean: bool = False) -> None:
    """Полный restore в указанный DSN. Требует pg_restore в PATH."""
    import gzip, shutil, tempfile
    p = _parse_dsn(target_dsn)
    env = os.environ.copy()
    env["PGPASSWORD"] = p["password"]

    # Если .gz — распакуем во временный .dump
    if file_path.suffix == ".gz" and file_path.name.endswith(".dump.gz"):
        tmp = Path(tempfile.mkstemp(suffix=".dump")[1])
        with gzip.open(file_path, "rb") as fin, open(tmp, "wb") as fout:
            shutil.copyfileobj(fin, fout, length=1024 * 1024)
        target_file = tmp
    else:
        target_file = file_path

    argv = [
        cfg.PG_RESTORE,
        "-h", p["host"], "-p", p["port"],
        "-U", p["user"], "-d", p["dbname"],
        "--no-owner", "--no-privileges",
    ]
    if clean:
        argv.append("--clean")
    argv.append(str(target_file))

    log.info("[dr.restore] running pg_restore → %s/%s", p["host"], p["dbname"])
    r = subprocess.run(argv, env=env, capture_output=True, text=True,
                       timeout=cfg.RESTORE_TIMEOUT_SEC)
    if r.returncode != 0:
        raise RuntimeError(f"pg_restore rc={r.returncode}: {r.stderr[:500]}")
    log.info("[dr.restore] ok")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="DR restore helper")
    ap.add_argument("--db", default="intelligence")
    ap.add_argument("--bucket", default="daily")
    ap.add_argument("--key", default=None,
                    help="Explicit key (override --db lookup)")
    ap.add_argument("--out", default=None,
                    help="Save path (default: ./<filename>)")
    ap.add_argument("--target", default=None,
                    help="DSN to restore into; if absent — only download")
    ap.add_argument("--clean", action="store_true",
                    help="pg_restore --clean (drop existing objects first)")
    args = ap.parse_args(argv)

    if args.key:
        key = args.key
    else:
        latest = find_latest(args.db, args.bucket)
        if not latest:
            print(json.dumps({"status": "failed",
                              "error": f"no backups for {args.db} in {args.bucket}"}))
            return 1
        key, size, mtime = latest
        log.info("[dr.restore] latest: %s size=%d mtime=%s", key, size, mtime)

    out = Path(args.out) if args.out else Path.cwd() / Path(key).name
    download(key, out)
    log.info("[dr.restore] downloaded → %s (%.2f MB)",
             out, out.stat().st_size / (1024 * 1024))

    if args.target:
        pg_restore(out, args.target, clean=args.clean)
        print(json.dumps({"status": "ok", "key": key, "restored_to": args.target}))
    else:
        print(json.dumps({"status": "ok", "key": key, "downloaded": str(out)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
