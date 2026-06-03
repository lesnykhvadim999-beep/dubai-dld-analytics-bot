"""Monthly refresh cron entrypoint for Layer 17 causal_inference.

Schedule
--------
    1st of each month at 04:00 UTC.

Railway (or any cron host) should invoke:
    python -m shared.causal_inference.cron_monthly_refresh

The script:
    1. Re-fits every prebuilt study with the freshest market_data /
       DLD archive snapshot.
    2. Persists ATEs + 95 % CIs + pickled models into causal_studies
       (UPSERT by study_name).
    3. Best-effort populates `natural_language` via Cerebras → Groq
       (skipped silently if no LLM key is set).
"""

from __future__ import annotations

import logging
import os
import sys
import time


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    # DoWhy is noisy on INFO — pin it to WARNING.
    logging.getLogger("dowhy").setLevel(logging.WARNING)


def main() -> int:
    _setup_logging()
    log = logging.getLogger("causal_inference.cron")
    started = time.time()
    log.info("Layer 17 monthly refresh starting")
    use_llm = bool(os.environ.get("CEREBRAS_API_KEY") or os.environ.get("GROQ_API_KEY"))
    from shared.causal_inference import run_all_prebuilt
    results = run_all_prebuilt(use_llm=use_llm)
    ok = sum(1 for r in results if "error" not in r)
    fail = sum(1 for r in results if "error" in r)
    log.info("Layer 17 refresh done in %.1fs — ok=%s fail=%s", time.time() - started, ok, fail)
    for r in results:
        if "error" in r:
            log.error("STUDY FAIL %s: %s", r.get("study_name"), r.get("error"))
        else:
            log.info("STUDY OK %s ATE=%.4f CI=%s n=%s", r["study_name"], r["ate"], r["ci"], r["n"])
    # Optional: notify admin via shared.admin_notify (fail-soft).
    try:
        from admin_notify import admin_notify  # type: ignore
        admin_notify(f"📊 Layer 17 causal refresh: ok={ok} fail={fail}")
    except Exception:
        pass
    return 0 if fail == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
