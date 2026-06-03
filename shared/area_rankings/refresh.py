# -*- coding: utf-8 -*-
"""refresh — orchestrate weekly rebuild of area_rankings (all 3 goals).

Usage:
    python -m shared.area_rankings.refresh           # all goals
    python -m shared.area_rankings.refresh resale    # one goal
    python -m shared.area_rankings.refresh --dry     # no writes

Called weekly by phase_bm_master_cron (Sunday 03:00 UTC).
On crash, attempts to notify admin chat via shared.admin_notify (best-effort).
"""
from __future__ import annotations
import logging
import sys
import time
import traceback

log = logging.getLogger("area_rankings.refresh")


def _notify_admin(msg: str) -> None:
    try:
        from shared.admin_notify import notify_admins  # type: ignore
        notify_admins(f"[area_rankings] {msg}")
    except Exception:
        pass  # best-effort only


def refresh_all(only: str | None = None, dry_run: bool = False) -> dict:
    from . import builder_resale, builder_rental, builder_living
    builders = {
        "resale": builder_resale.run,
        "rental": builder_rental.run,
        "living": builder_living.run,
    }
    if only:
        builders = {only: builders[only]}

    results: dict[str, dict] = {}
    for name, fn in builders.items():
        t0 = time.time()
        try:
            n = fn(dry_run=dry_run)
            results[name] = {"rows": n, "elapsed_s": round(time.time() - t0, 1),
                             "error": None}
            log.info("[refresh] %s OK: %d rows in %.1fs", name, n,
                     time.time() - t0)
        except Exception as e:
            err = traceback.format_exc()[-500:]
            results[name] = {"rows": 0, "elapsed_s": round(time.time() - t0, 1),
                             "error": str(e)[:200]}
            log.error("[refresh] %s FAILED: %s\n%s", name, e, err)
            _notify_admin(f"{name} builder crashed: {str(e)[:150]}")
    return results


def main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    dry = "--dry" in argv
    args = [a for a in argv if not a.startswith("--")]
    only = args[0] if args else None
    if only and only not in {"resale", "rental", "living"}:
        log.error("unknown goal: %s", only)
        return 2
    results = refresh_all(only=only, dry_run=dry)
    print("RESULTS:")
    for k, v in results.items():
        print(f"  {k}: {v}")
    return 0 if all(r["error"] is None for r in results.values()) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
