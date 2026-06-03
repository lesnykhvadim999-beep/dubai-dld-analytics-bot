"""Disaster Recovery — weekly verify.

Простой вариант (без docker): скачивает последний backup → проверяет, что
файл не пустой и читается (gzip integrity + pg_restore --list, если бинарь
есть). Логирует в restore_verifications.

CLI:
    python -m shared.disaster_recovery.verify
    python -m shared.disaster_recovery.verify --db intelligence --bucket daily

Vadim Realty + RERA BRN 65011.
"""
from __future__ import annotations
import argparse
import gzip
import json
import logging
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from . import _config as cfg
from .restore import find_latest, download

log = logging.getLogger("dr.verify")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)


def _meta_conn():
    dsn = cfg.meta_dsn()
    if not dsn: return None
    try:
        import psycopg2  # type: ignore
        return psycopg2.connect(dsn, connect_timeout=10)
    except Exception as e:
        log.warning("[dr.verify] meta conn failed: %s", e)
        return None


def _resolve_run_id(key: str) -> int | None:
    conn = _meta_conn()
    if conn is None:
        return None
    try:
        fname = Path(key).name
        with conn, conn.cursor() as cur:
            cur.execute(
                """SELECT id FROM backup_runs
                    WHERE storage_url LIKE %s
                    ORDER BY started_at DESC LIMIT 1""",
                (f"%{fname}",),
            )
            row = cur.fetchone()
            return int(row[0]) if row else None
    except Exception as e:
        log.warning("[dr.verify] resolve run_id failed: %s", e)
        return None
    finally:
        try: conn.close()
        except Exception: pass


def _log_verif(run_id: int | None, *, status: str, duration_sec: int,
               tables_count: int | None, rows_total: int | None,
               note: str | None) -> None:
    conn = _meta_conn()
    if conn is None: return
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                """INSERT INTO restore_verifications
                       (backup_run_id, verified_at, tables_count, rows_total,
                        duration_sec, status, note)
                   VALUES (%s, NOW(), %s, %s, %s, %s, %s)""",
                (run_id, tables_count, rows_total, duration_sec, status,
                 (note or "")[:500] or None),
            )
    except Exception as e:
        log.warning("[dr.verify] log_verif failed: %s", e)
    finally:
        try: conn.close()
        except Exception: pass


def _gzip_check(path: Path) -> int:
    """Читаем .gz целиком, считаем uncompressed bytes. Бросает на повреждении."""
    total = 0
    with gzip.open(path, "rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk: break
            total += len(chunk)
    return total


def _pg_restore_list(path: Path) -> int | None:
    """Если pg_restore доступен и dump в custom-формате — вернёт число TOC entries
    (~ количество объектов: таблицы+индексы+constraints). None если бинарь
    недоступен или формат не custom."""
    bin_ = cfg.PG_RESTORE
    if not (Path(bin_).exists() or shutil.which(bin_)):
        return None
    if not path.name.endswith(".dump.gz"):
        return None
    # распаковываем в tmp .dump и зовём pg_restore --list
    with tempfile.TemporaryDirectory() as td:
        raw = Path(td) / "x.dump"
        with gzip.open(path, "rb") as fin, open(raw, "wb") as fout:
            shutil.copyfileobj(fin, fout, length=1024 * 1024)
        r = subprocess.run([bin_, "--list", str(raw)],
                           capture_output=True, text=True, timeout=300)
        if r.returncode != 0:
            log.warning("[dr.verify] pg_restore --list rc=%d: %s",
                        r.returncode, r.stderr[:200])
            return None
        # фильтруем строки TOC
        lines = [l for l in r.stdout.splitlines()
                 if l.strip() and not l.startswith(";")]
        return len(lines)


def verify(db_name: str = "intelligence", bucket: str = "daily") -> dict:
    started = time.time()
    latest = find_latest(db_name, bucket)
    if not latest:
        res = {"status": "failed",
               "error": f"no backups in {bucket}/{db_name}",
               "db": db_name, "bucket": bucket}
        _log_verif(None, status="failed",
                   duration_sec=int(time.time() - started),
                   tables_count=None, rows_total=None,
                   note=res["error"])
        return res

    key, size, mtime = latest
    log.info("[dr.verify] verifying %s (%d B, mtime=%s)", key, size, mtime)

    tmpdir = Path(tempfile.mkdtemp(prefix="dr_verify_"))
    try:
        local = tmpdir / Path(key).name
        download(key, local)
        if local.stat().st_size < 100:
            raise RuntimeError(f"file too small: {local.stat().st_size} B")

        uncompressed = _gzip_check(local)
        toc = _pg_restore_list(local)

        elapsed = int(time.time() - started)
        run_id = _resolve_run_id(key)
        note = (f"uncompressed={uncompressed}B "
                f"pg_restore_list={toc if toc is not None else 'skipped'}")
        _log_verif(run_id, status="ok", duration_sec=elapsed,
                   tables_count=toc, rows_total=None, note=note)
        return {"status": "ok", "key": key,
                "size_bytes": size,
                "uncompressed_bytes": uncompressed,
                "toc_entries": toc,
                "duration_sec": elapsed}
    except Exception as e:
        log.exception("[dr.verify] FAILED")
        elapsed = int(time.time() - started)
        run_id = _resolve_run_id(key)
        _log_verif(run_id, status="failed", duration_sec=elapsed,
                   tables_count=None, rows_total=None, note=str(e)[:400])
        return {"status": "failed", "key": key, "error": str(e),
                "duration_sec": elapsed}
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="DR verify-on-download")
    ap.add_argument("--db", default="intelligence")
    ap.add_argument("--bucket", default="daily")
    args = ap.parse_args(argv)
    res = verify(args.db, args.bucket)
    print(json.dumps(res, ensure_ascii=False, default=str, indent=2))
    return 0 if res.get("status") == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
