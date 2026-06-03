"""PHASE BN N5 — dead code scanner (vulture wrapper).

Runs `vulture` over C:/Projects/shared and persists findings to
`dead_code_candidates`.  No code is auto-removed.

Usage:
    python -m shared.optimizer.dead_code [--target PATH] [--min-confidence 80]
"""
from __future__ import annotations

import argparse
import logging
import re
import shutil
import subprocess
import sys
from pathlib import Path

from . import _db

log = logging.getLogger("optimizer.dead_code")
if not log.handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(message)s",
    )

PYTHON = sys.executable or "python"
DEFAULT_TARGET = Path(r"C:\Projects\shared")

# vulture lines look like:
#   path/to/file.py:42: unused function 'foo' (90% confidence)
_LINE_RE = re.compile(
    r"^(?P<path>.+?):(?P<line>\d+):\s+"
    r"unused\s+(?P<kind>\w+)\s+'(?P<name>[^']+)'\s+"
    r"\((?P<conf>\d+)% confidence\)\s*$",
    re.IGNORECASE,
)


def _ensure_vulture() -> bool:
    if shutil.which("vulture"):
        return True
    # try importing as a module
    try:
        subprocess.run([PYTHON, "-c", "import vulture"],
                       check=True, capture_output=True)
        return True
    except Exception:
        pass
    log.info("vulture not installed — pip install vulture")
    try:
        subprocess.run([PYTHON, "-m", "pip", "install", "--quiet", "vulture"],
                       check=True, capture_output=True, timeout=180)
        return True
    except Exception as e:
        log.warning("pip install vulture failed: %s", e)
        return False


def _run_vulture(target: Path, min_conf: int) -> list[str]:
    cmd = [PYTHON, "-m", "vulture", str(target),
           "--min-confidence", str(min_conf)]
    log.info("running: %s", " ".join(cmd))
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    except subprocess.TimeoutExpired:
        log.warning("vulture timed out")
        return []
    # vulture exits 0 when clean, 3 when it found dead code — both expected.
    lines = (r.stdout or "").splitlines()
    return [ln for ln in lines if ln.strip()]


def _parse(lines: list[str]) -> list[dict]:
    out: list[dict] = []
    for ln in lines:
        m = _LINE_RE.match(ln.strip())
        if not m:
            continue
        out.append({
            "file_path": m.group("path"),
            "line_no": int(m.group("line")),
            "item_type": m.group("kind"),
            "item_name": m.group("name"),
            "confidence": int(m.group("conf")),
        })
    return out


def _persist(items: list[dict]) -> int:
    conn = _db.connect()
    if conn is None:
        log.warning("no DSN — findings not persisted (%d items)", len(items))
        return 0
    inserted = 0
    try:
        with conn.cursor() as cur:
            for it in items:
                try:
                    cur.execute(
                        """INSERT INTO dead_code_candidates
                           (file_path, line_no, item_name, item_type, confidence)
                           VALUES (%s,%s,%s,%s,%s)""",
                        (it["file_path"][:1000], it["line_no"],
                         it["item_name"][:300], it["item_type"][:60],
                         it["confidence"]),
                    )
                    inserted += 1
                except Exception as e:
                    log.warning("insert fail (%s:%s): %s",
                                it["file_path"], it["line_no"], e)
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return inserted


def scan(target: Path = DEFAULT_TARGET, min_conf: int = 80) -> list[dict]:
    if not _ensure_vulture():
        return []
    if not target.exists():
        log.warning("target %s does not exist", target)
        return []
    raw = _run_vulture(target, min_conf)
    items = _parse(raw)
    log.info("vulture found %d candidates (min_conf=%d)", len(items), min_conf)
    inserted = _persist(items)
    log.info("persisted %d", inserted)
    return items


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--target", default=str(DEFAULT_TARGET))
    p.add_argument("--min-confidence", type=int, default=80)
    a = p.parse_args(argv)
    items = scan(Path(a.target), a.min_confidence)
    print(f"[optimizer.dead_code] found={len(items)}")
    for it in items[:10]:
        print(f"  {it['file_path']}:{it['line_no']} "
              f"{it['item_type']} {it['item_name']} ({it['confidence']}%)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
