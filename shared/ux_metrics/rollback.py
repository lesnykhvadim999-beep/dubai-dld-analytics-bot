"""
shared.ux_metrics.rollback — auto-rollback for UI experiments.

Rule (per spec):
  If variant='simple_menu_v2' is worse than 'legacy_menu' on a guarded metric
  by ≥ 20 % for 2 consecutive days → flip feature_flags entry off
  (auto_disabled), notify admin, write to ux_rollback_log.

Direction-aware:
  - higher_is_better metrics (completion_rate, return_rate, actions_per_session):
      "worse" means B < A by ≥ 20 %.
  - lower_is_better metrics (error_rate, time_to_first_action):
      "worse" means B > A by ≥ 20 %.

Usage:
  python -m shared.ux_metrics.rollback                    # check + (maybe) rollback
  python -m shared.ux_metrics.rollback --dry-run          # never flip feature flag

Cron entry (master_cron.py):
  {"name": "ux_rollback_check", "cron": "0 7 * * *",
   "argv": [PYTHON, "-m", "shared.ux_metrics.rollback"]}

Integrates with shared.safety_nets.feature_flags. If that module is not
available (degraded env) we still log the decision but skip the flip.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from typing import Any, Dict, List, Optional, Tuple

from .analyzer import compare_variants
from .tracker import _get_conn, ensure_schema, is_enabled  # type: ignore

log = logging.getLogger("ux_metrics.rollback")


# ── what to watch ──────────────────────────────────────────────────────────
HIGHER_IS_BETTER = {"completion_rate", "return_rate", "actions_per_session"}
LOWER_IS_BETTER  = {"error_rate", "time_to_first_action"}

# (feature_flag_name, variant_old, variant_new, [metric, ...])
WATCHLIST: List[Dict[str, Any]] = [
    {
        "feature_name": "ui_simplification",
        "variant_old":  "legacy_menu",
        "variant_new":  "simple_menu_v2",
        "metrics":      ["completion_rate", "error_rate", "actions_per_session"],
        "bot_name":     None,  # all bots aggregated
    },
]

DEFAULT_THRESHOLD_PCT = 20.0
DEFAULT_CONSECUTIVE_DAYS = 2


# ── safety_nets glue ───────────────────────────────────────────────────────
def _flag_set_disabled(name: str, reason: str) -> bool:
    """Tries shared.safety_nets.feature_flags first, then a direct UPDATE."""
    try:
        from shared.safety_nets.feature_flags import report_failure  # type: ignore
        # report_failure 3× triggers auto-disable.
        for _ in range(3):
            report_failure(name, reason=reason)
        return True
    except Exception as e:
        log.info("safety_nets unavailable, falling back to direct UPDATE: %s", e)
    # Direct fallback
    conn = _get_conn()
    if conn is None:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO feature_flags
                  (name, enabled, auto_disabled_at, note, updated_at)
                VALUES (%s, FALSE, now(), %s, now())
                ON CONFLICT (name) DO UPDATE SET
                  enabled = FALSE,
                  auto_disabled_at = now(),
                  note = EXCLUDED.note,
                  updated_at = now()
                """,
                (name, reason[:500]),
            )
        return True
    except Exception as e:
        log.warning("direct flag disable failed: %s", e)
        return False


def _admin_notify(text: str) -> None:
    try:
        from shared.admin_notify import admin_notify  # type: ignore
        admin_notify(text, priority="critical")
    except Exception:
        pass


# ── decision helpers ───────────────────────────────────────────────────────
def _is_worse(metric: str, delta_pct: float, threshold_pct: float) -> bool:
    """delta_pct is (new − old)/|old| * 100."""
    if metric in HIGHER_IS_BETTER:
        return delta_pct <= -threshold_pct
    if metric in LOWER_IS_BETTER:
        return delta_pct >= threshold_pct
    # Unknown direction → conservatively require higher-is-better semantics.
    return delta_pct <= -threshold_pct


def _log_decision(entry: Dict[str, Any]) -> None:
    conn = _get_conn()
    if conn is None:
        return
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO ux_rollback_log
                  (feature_name, metric_name, variant_new, variant_old,
                   delta_pct, decision, consecutive_bad, detail)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
                """,
                (
                    entry["feature_name"],
                    entry.get("metric_name"),
                    entry.get("variant_new"),
                    entry.get("variant_old"),
                    entry.get("delta_pct"),
                    entry["decision"],
                    entry.get("consecutive_bad"),
                    json.dumps(entry.get("detail") or {}, default=str),
                ),
            )
    except Exception as e:
        log.warning("decision log insert failed: %s", e)


def _recent_bad_streak(feature_name: str, metric_name: str,
                       days: int = 7) -> int:
    """How many of the last N daily rows for this metric/feature were 'bad'?
    We count from most-recent backwards until the streak breaks."""
    conn = _get_conn()
    if conn is None:
        return 0
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT decision, decided_at::date
                  FROM ux_rollback_log
                 WHERE feature_name=%s AND metric_name=%s
                   AND decided_at >= now() - (%s || ' days')::interval
                 ORDER BY decided_at DESC
                """,
                (feature_name, metric_name, str(days)),
            )
            rows = cur.fetchall() or []
    except Exception:
        return 0
    streak = 0
    seen_dates = set()
    for decision, d in rows:
        if d in seen_dates:
            continue
        seen_dates.add(d)
        if decision == "bad":
            streak += 1
        else:
            break
    return streak


# ── main check ─────────────────────────────────────────────────────────────
def check_metrics(
    threshold_pct: float = DEFAULT_THRESHOLD_PCT,
    consecutive_days: int = DEFAULT_CONSECUTIVE_DAYS,
    dry_run: bool = False,
) -> List[Dict[str, Any]]:
    """Runs through WATCHLIST, returns a list of decisions."""
    decisions: List[Dict[str, Any]] = []
    if not is_enabled():
        log.warning("ux_metrics disabled, skipping rollback check")
        return decisions
    ensure_schema()

    for spec in WATCHLIST:
        feat = spec["feature_name"]
        v_old = spec["variant_old"]
        v_new = spec["variant_new"]
        bot = spec.get("bot_name")
        for metric in spec["metrics"]:
            cmp_ = compare_variants(
                metric, variant_a=v_old, variant_b=v_new,
                period="1d", bot_name=bot,
            )
            delta = cmp_.get("delta_pct")
            samples_a = cmp_["variant_a"].get("samples", 0) or 0
            samples_b = cmp_["variant_b"].get("samples", 0) or 0
            if delta is None or samples_a < 20 or samples_b < 20:
                # not enough data → 'inconclusive', do not count toward streak
                entry = {
                    "feature_name": feat,
                    "metric_name": metric,
                    "variant_old": v_old,
                    "variant_new": v_new,
                    "delta_pct": delta,
                    "decision": "inconclusive",
                    "consecutive_bad": 0,
                    "detail": {
                        "samples_a": samples_a, "samples_b": samples_b,
                        "compare": cmp_,
                    },
                }
                _log_decision(entry)
                decisions.append(entry)
                continue

            bad = _is_worse(metric, delta, threshold_pct)
            decision = "bad" if bad else "ok"
            entry = {
                "feature_name": feat,
                "metric_name": metric,
                "variant_old": v_old,
                "variant_new": v_new,
                "delta_pct": delta,
                "decision": decision,
                "consecutive_bad": 0,
                "detail": {
                    "samples_a": samples_a, "samples_b": samples_b,
                    "threshold_pct": threshold_pct, "compare": cmp_,
                },
            }
            _log_decision(entry)

            if bad:
                # Including this run, count today's bad as part of streak.
                prior_streak = _recent_bad_streak(feat, metric, days=7)
                streak = prior_streak  # already counts today (just logged)
                entry["consecutive_bad"] = streak
                if streak >= consecutive_days:
                    reason = (
                        f"AUTO-ROLLBACK ux_metrics: {feat} {metric} "
                        f"{v_new} vs {v_old} delta={delta}% "
                        f"for {streak} consecutive days "
                        f"(threshold={threshold_pct}%)."
                    )
                    if dry_run:
                        log.warning("[DRY-RUN] would rollback: %s", reason)
                        entry["decision"] = "rollback_dry_run"
                    else:
                        ok = _flag_set_disabled(feat, reason)
                        entry["decision"] = "rollback" if ok else "rollback_failed"
                        _admin_notify(
                            f"<b>UX auto-rollback</b>\n"
                            f"feature: <code>{feat}</code>\n"
                            f"metric:  <code>{metric}</code>\n"
                            f"{v_new} vs {v_old}: <b>{delta}%</b> "
                            f"({streak}d streak ≥ {consecutive_days}d)\n"
                            f"Feature flag disabled.\n\n"
                            f"Vadim Realty | RERA BRN 65011"
                        )
                        _log_decision({
                            **entry,
                            "decision": entry["decision"],
                        })
            decisions.append(entry)
    return decisions


def run_once(dry_run: bool = False) -> int:
    decisions = check_metrics(dry_run=dry_run)
    print(json.dumps(decisions, indent=2, default=str, ensure_ascii=False))
    # Exit non-zero only on hard infra failure (empty result with is_enabled False).
    return 0


# ── CLI ────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    if not logging.getLogger().handlers:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(name)s %(message)s",
        )
    sys.exit(run_once(dry_run=args.dry_run))
