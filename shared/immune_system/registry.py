"""Applied immunities registry.

When an admin merges a proposed patch + regression test, call
`record_applied_immunity` with the commit sha + test path. Once a week the
master cron runs `verify_all_immunities` to ensure none of the regression
tests have regressed.
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import _db

log = logging.getLogger("immune_system.registry")


def record_applied_immunity(
    incident_signature: str,
    commit_sha: str,
    regression_test_path: str,
    proposal_id: Optional[int] = None,
) -> Optional[int]:
    """Insert a row into applied_immunities. Returns inserted id or None."""
    conn = _db.connect()
    if conn is None:
        log.warning("record_applied_immunity: no DB connection")
        return None
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO applied_immunities
                    (incident_signature, proposal_id, patch_commit_sha,
                     regression_test_path, applied_at, verification_status)
                VALUES (%s, %s, %s, %s, NOW(), 'pending')
                RETURNING id
                """,
                (incident_signature, proposal_id, commit_sha, regression_test_path),
            )
            new_id = cur.fetchone()[0]
            # Mark the originating incident as immunized so it stops being re-proposed.
            cur.execute(
                """
                UPDATE immune_incidents
                   SET status = 'immunized'
                 WHERE signature = %s
                """,
                (incident_signature,),
            )
        return int(new_id)
    except Exception as e:
        log.warning("record_applied_immunity failed: %s", e)
        return None
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _run_pytest(test_path: str, timeout: int = 120) -> tuple[bool, str]:
    """Run a single regression test. Returns (passed, short_note)."""
    if not test_path or not Path(test_path).exists():
        return False, f"missing:{test_path}"
    try:
        r = subprocess.run(
            [sys.executable, "-m", "pytest", "-x", "-q", test_path],
            capture_output=True, text=True, timeout=timeout,
        )
        note = (r.stdout or "")[-300:] + (r.stderr or "")[-300:]
        return r.returncode == 0, note.strip()
    except subprocess.TimeoutExpired:
        return False, "TIMEOUT"
    except Exception as e:
        return False, f"crash:{e}"[:300]


def verify_all_immunities() -> dict:
    """Re-run every regression_test_path on file. Update last_verified +
    verification_status. Returns summary dict.

    Alert path: caller (cron) can compare returned 'failed' count vs prev
    snapshot and route to admin-bot. We don't ping anyone from here so the
    function stays side-effect free outside of DB.
    """
    conn = _db.connect()
    if conn is None:
        log.warning("verify_all_immunities: no DB connection")
        return {"passed": 0, "failed": 0, "missing": 0, "items": []}
    summary = {"passed": 0, "failed": 0, "missing": 0, "items": []}
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, incident_signature, regression_test_path
                FROM applied_immunities
                WHERE regression_test_path IS NOT NULL
                  AND regression_test_path <> ''
                """
            )
            rows = cur.fetchall()

        for aid, sig, test_path in rows:
            ok, note = _run_pytest(test_path)
            status = "passed" if ok else ("missing" if note.startswith("missing:") else "failed")
            summary[status] += 1
            summary["items"].append({"id": aid, "sig": sig, "status": status, "note": note})
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE applied_immunities
                           SET last_verified = %s,
                               verification_status = %s
                         WHERE id = %s
                        """,
                        (datetime.now(timezone.utc), status, aid),
                    )
            except Exception as e:
                log.warning("verify update failed for %s: %s", aid, e)
        return summary
    finally:
        try:
            conn.close()
        except Exception:
            pass


if __name__ == "__main__":
    import json as _json
    print(_json.dumps(verify_all_immunities(), indent=2, default=str))
