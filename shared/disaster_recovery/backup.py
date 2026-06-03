"""Disaster Recovery — backup runner (PHASE BO O1 simplified).

Что делает:
  • pg_dump --no-owner --no-privileges --format=custom для каждой целевой БД
    (по умолчанию: intelligence; через CLI можно указать другую или 'all')
  • gzip-сжатие custom-dump
  • Загружает в GitHub release (бакет: daily / weekly / monthly — авто по дате)
    через `gh release upload`. Если pg_dump не установлен — fallback на
    psycopg2 SELECT * из каждой пользовательской таблицы → JSONL.gz (для DR
    хотя бы данные таблиц сохранены).
  • Логирует строку в backup_runs (intelligence DB).
  • DSN: env (LIVE_DATABASE_URL / DATABASE_URL / INTELLIGENCE_DATABASE_URL),
    никогда не хранит пароли в коде.

CLI:
    python -m shared.disaster_recovery.backup                # intelligence
    python -m shared.disaster_recovery.backup --db live
    python -m shared.disaster_recovery.backup --db all

Vadim Realty + RERA BRN 65011.
"""
from __future__ import annotations
import argparse
import datetime as _dt
import gzip
import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlparse

from . import _config as cfg
from .storage_adapter import get_backend

log = logging.getLogger("dr.backup")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

CHUNK = 1024 * 1024


def _now_utc() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def _bucket_for_now(now: _dt.datetime) -> str:
    """Простая классификация по календарю.
    1-ое число → monthly; воскресенье → weekly; иначе daily."""
    if now.day == 1:
        return "monthly"
    if now.weekday() == 6:  # Sun
        return "weekly"
    return "daily"


def _parse_dsn(dsn: str) -> dict:
    u = urlparse(dsn)
    return {
        "host": u.hostname or "",
        "port": str(u.port or 5432),
        "user": u.username or "",
        "password": u.password or "",
        "dbname": (u.path or "/").lstrip("/") or "postgres",
    }


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for blk in iter(lambda: f.read(CHUNK), b""):
            h.update(blk)
    return h.hexdigest()


def _gzip(src: Path, dst: Path) -> None:
    with open(src, "rb") as fin, gzip.open(dst, "wb", compresslevel=6) as fout:
        shutil.copyfileobj(fin, fout, length=CHUNK)


# ── meta logging (best-effort) ─────────────────────────────────────────────
def _meta_conn():
    dsn = cfg.meta_dsn()
    if not dsn:
        return None
    try:
        import psycopg2  # type: ignore
        return psycopg2.connect(dsn, connect_timeout=10)
    except Exception as e:
        log.warning("[dr.backup] meta conn failed: %s", e)
        return None


def _log_start(db_name: str, backup_type: str) -> int | None:
    conn = _meta_conn()
    if conn is None:
        return None
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                """INSERT INTO backup_runs(db_name, backup_type, started_at, status, encrypted)
                   VALUES (%s, %s, NOW(), 'running', FALSE)
                   RETURNING id""",
                (db_name, backup_type),
            )
            row = cur.fetchone()
            return int(row[0]) if row else None
    except Exception as e:
        log.warning("[dr.backup] log_start failed: %s", e)
        return None
    finally:
        try: conn.close()
        except Exception: pass


def _log_finish(run_id: int | None, *, status: str, size_mb: float | None,
                storage_url: str | None, storage_backend: str | None,
                sha256: str | None, error_msg: str | None) -> None:
    if run_id is None:
        return
    conn = _meta_conn()
    if conn is None:
        return
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                """UPDATE backup_runs
                      SET finished_at=NOW(),
                          status=%s,
                          size_mb=%s,
                          storage_url=%s,
                          storage_backend=%s,
                          sha256=%s,
                          error_msg=%s
                    WHERE id=%s""",
                (status, size_mb, storage_url, storage_backend, sha256,
                 (error_msg or "")[:1000] or None, run_id),
            )
    except Exception as e:
        log.warning("[dr.backup] log_finish failed: %s", e)
    finally:
        try: conn.close()
        except Exception: pass


# ── pg_dump path ──────────────────────────────────────────────────────────
def _have_pg_dump() -> bool:
    bin_ = cfg.PG_DUMP
    if Path(bin_).exists():
        return True
    return shutil.which(bin_) is not None


def _run_pg_dump(dsn: str, out_path: Path) -> None:
    """Выполняет pg_dump --format=custom --no-owner --no-privileges.
    PGPASSWORD через env (никогда в argv)."""
    parts = _parse_dsn(dsn)
    env = os.environ.copy()
    env["PGPASSWORD"] = parts["password"]
    argv = [
        cfg.PG_DUMP,
        "-h", parts["host"], "-p", parts["port"],
        "-U", parts["user"], "-d", parts["dbname"],
        "--no-owner", "--no-privileges",
        "--format=custom",
        "--compress=0",  # gzip отдельно
        "-f", str(out_path),
    ]
    log.info("[dr.backup] pg_dump → %s", out_path.name)
    r = subprocess.run(argv, env=env, capture_output=True, text=True,
                       timeout=cfg.DUMP_TIMEOUT_SEC)
    if r.returncode != 0:
        raise RuntimeError(f"pg_dump rc={r.returncode}: {r.stderr[:400]}")


# ── psycopg2 JSONL fallback ───────────────────────────────────────────────
def _fallback_jsonl_dump(dsn: str, out_path: Path) -> None:
    """Если pg_dump не установлен: SELECT * со всех публичных таблиц → JSONL.gz.
    Это не настоящий dump (нет схемы, нет sequences), но позволяет восстановить
    данные. Для DR drill это лучше чем ничего."""
    import psycopg2  # type: ignore
    import psycopg2.extras  # type: ignore
    log.warning("[dr.backup] pg_dump unavailable — using psycopg2 JSONL fallback")
    conn = psycopg2.connect(dsn, connect_timeout=30)
    try:
        with gzip.open(out_path, "wt", encoding="utf-8") as fout:
            cur = conn.cursor()
            cur.execute(
                """SELECT table_schema, table_name
                     FROM information_schema.tables
                    WHERE table_type='BASE TABLE'
                      AND table_schema NOT IN ('pg_catalog','information_schema')
                    ORDER BY table_schema, table_name"""
            )
            tables = cur.fetchall()
            cur.close()
            for schema, name in tables:
                fq = f'"{schema}"."{name}"'
                try:
                    cur = conn.cursor(name=f"_dr_{schema}_{name}",
                                      cursor_factory=psycopg2.extras.RealDictCursor)
                    cur.itersize = 5000
                    cur.execute(f"SELECT * FROM {fq}")
                    fout.write(json.dumps({
                        "_meta": True, "schema": schema, "table": name,
                    }, default=str) + "\n")
                    n = 0
                    for row in cur:
                        fout.write(json.dumps(dict(row), default=str) + "\n")
                        n += 1
                    fout.write(json.dumps({"_endtable": True, "rows": n}) + "\n")
                    log.info("[dr.backup] fallback %s.%s → %d rows", schema, name, n)
                    cur.close()
                except Exception as e:
                    log.warning("[dr.backup] fallback skip %s.%s: %s", schema, name, e)
    finally:
        try: conn.close()
        except Exception: pass


# ── orchestration ─────────────────────────────────────────────────────────
def backup_one(db_name: str, *, bucket: str | None = None) -> dict:
    """Одна БД → один файл → upload → meta log. Возвращает dict со статусом."""
    now = _now_utc()
    bucket = bucket or _bucket_for_now(now)
    dsn = cfg.get_dsn(db_name)
    if not dsn:
        return {"db": db_name, "status": "failed", "error": "DSN missing"}

    stamp = now.strftime("%Y-%m-%d_%H-%M")
    using_pgdump = _have_pg_dump()
    ext = "dump.gz" if using_pgdump else "jsonl.gz"
    fname = f"{db_name}_{stamp}.{ext}"
    key = f"{bucket}/{fname}"

    run_id = _log_start(db_name, bucket)
    backend = get_backend()
    backend_name = getattr(backend, "name", "?")
    if backend_name == "failover":
        backend_name = getattr(backend.primary, "name", "failover")

    tmpdir = Path(tempfile.mkdtemp(prefix="dr_backup_"))
    try:
        gz_path = tmpdir / fname
        if using_pgdump:
            raw = tmpdir / f"{db_name}_{stamp}.dump"
            _run_pg_dump(dsn, raw)
            _gzip(raw, gz_path)
            try: raw.unlink()
            except Exception: pass
        else:
            _fallback_jsonl_dump(dsn, gz_path)

        size_mb = gz_path.stat().st_size / (1024 * 1024)
        sha = _sha256(gz_path)
        log.info("[dr.backup] %s ready: %.2f MB sha=%s", fname, size_mb, sha[:12])

        # Upload
        url = backend.upload(gz_path, key)
        log.info("[dr.backup] uploaded → %s", url)

        _log_finish(run_id, status="ok", size_mb=round(size_mb, 3),
                    storage_url=url, storage_backend=backend_name,
                    sha256=sha, error_msg=None)
        return {"db": db_name, "status": "ok", "url": url,
                "size_mb": round(size_mb, 2), "key": key,
                "engine": "pg_dump" if using_pgdump else "psycopg2"}
    except Exception as e:
        log.exception("[dr.backup] %s FAILED", db_name)
        _log_finish(run_id, status="failed", size_mb=None,
                    storage_url=None, storage_backend=backend_name,
                    sha256=None, error_msg=str(e))
        return {"db": db_name, "status": "failed", "error": str(e)}
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="DR backup runner (PHASE BO O1)")
    ap.add_argument("--db", default="intelligence",
                    help="DB name from _config (intelligence/live/archive/resale) or 'all'")
    ap.add_argument("--bucket", default=None,
                    help="Force bucket: daily/weekly/monthly")
    args = ap.parse_args(argv)

    targets = cfg.ALL_DBS if args.db == "all" else [args.db]
    overall_ok = True
    results = []
    for name in targets:
        r = backup_one(name, bucket=args.bucket)
        results.append(r)
        if r["status"] != "ok":
            overall_ok = False
        print(json.dumps(r, ensure_ascii=False, default=str))
    return 0 if overall_ok else 1


if __name__ == "__main__":
    sys.exit(main())
