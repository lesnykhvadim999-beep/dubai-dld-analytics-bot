"""
analyze_experiments — daily cron (04:30 Asia/Dubai).

For each `running` experiment with >= MIN_SAMPLES per variant:
  - Compute outcome rates (positive_feedback rate, follow_up rate).
  - Chi-square test for primary outcome (positive_feedback).
  - If p < 0.05 AND practical significance (|rate_B - rate_A| / rate_A > 0.05):
       set_winner(experiment_key, best_variant) and notify admin.

Auto-stop guardrail: if any variant shows >2σ WORSE than control → auto-pause.

Manual run:
    py C:/Projects/shared/ab_testing/analyze_experiments.py
    py C:/Projects/shared/ab_testing/analyze_experiments.py --min 50
    py C:/Projects/shared/ab_testing/analyze_experiments.py --dry-run
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import sys
from datetime import datetime, timezone, timedelta

if __name__ == "__main__" and __package__ in (None, ""):
    import sys as _sys
    _here = os.path.dirname(os.path.abspath(__file__))
    _parent = os.path.dirname(_here)
    if _parent not in _sys.path:
        _sys.path.insert(0, _parent)
    __package__ = "ab_testing"  # noqa: F841

from ._db import conn_cursor, ensure_schema  # type: ignore

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s :: %(message)s",
)
log = logging.getLogger("ab.analyze")

MIN_SAMPLES_PER_VARIANT = 100
MIN_P_VALUE = 0.05
MIN_PRACTICAL_LIFT = 0.05    # 5% relative
ABANDON_AFTER_DAYS = 7        # no samples → abandoned
PRIMARY_OUTCOME = "positive_feedback"
FOLLOWUP_OUTCOME = "follow_up"


# ─────────── Statistics (no scipy dependency) ───────────
def _chi_square_2x2(a_succ: int, a_total: int, b_succ: int, b_total: int) -> tuple[float, float]:
    """Two-proportion chi-square. Returns (chi2, p-value approx).

    p-value approximated via the survival function for chi-square with 1 dof:
       p = exp(-chi2/2)  (good first-order approx for the magnitude we care about).
    """
    a_fail = a_total - a_succ
    b_fail = b_total - b_succ
    total = a_total + b_total
    if total == 0 or a_total == 0 or b_total == 0:
        return (0.0, 1.0)
    succ = a_succ + b_succ
    fail = a_fail + b_fail
    if succ == 0 or fail == 0:
        return (0.0, 1.0)
    # Expected values
    eA_succ = a_total * succ / total
    eA_fail = a_total * fail / total
    eB_succ = b_total * succ / total
    eB_fail = b_total * fail / total
    obs = [(a_succ, eA_succ), (a_fail, eA_fail), (b_succ, eB_succ), (b_fail, eB_fail)]
    chi2 = sum((o - e) ** 2 / e for o, e in obs if e > 0)
    # df=1 chi-square approx survival: p ≈ exp(-chi2/2)
    p = math.exp(-chi2 / 2.0)
    return (chi2, p)


def _proportion_std_err(p_hat: float, n: int) -> float:
    if n <= 0:
        return 0.0
    return math.sqrt(max(p_hat * (1 - p_hat), 0.0) / n)


# ─────────── Data fetch ───────────
def _fetch_running_experiments() -> list[dict]:
    with conn_cursor(dict_cursor=True) as (_, cur):
        cur.execute("SELECT * FROM ab_experiments WHERE status='running'")
        return [dict(r) for r in cur.fetchall()]


def _fetch_results(exp_id: int) -> dict[str, dict]:
    """Return per-variant counters: {variant: {assigned, primary_succ, follow_up_succ, ...}}."""
    out: dict[str, dict] = {}
    with conn_cursor() as (_, cur):
        cur.execute("""
            SELECT variant, COUNT(DISTINCT user_id) AS users
            FROM ab_assignments WHERE experiment_id = %s GROUP BY variant
        """, (exp_id,))
        for variant, users in cur.fetchall():
            out[variant] = {
                "assigned": int(users),
                "primary_succ": 0,
                "follow_up_succ": 0,
                "negative": 0,
                "last_event": None,
            }

        cur.execute("""
            SELECT variant, outcome_type,
                   COUNT(DISTINCT user_id) AS users,
                   MAX(ts) AS last_ts
            FROM ab_outcomes
            WHERE experiment_id = %s
            GROUP BY variant, outcome_type
        """, (exp_id,))
        for variant, otype, users, last_ts in cur.fetchall():
            d = out.setdefault(variant, {"assigned": 0, "primary_succ": 0, "follow_up_succ": 0, "negative": 0, "last_event": None})
            if otype == PRIMARY_OUTCOME:
                d["primary_succ"] = int(users)
            elif otype == FOLLOWUP_OUTCOME:
                d["follow_up_succ"] = int(users)
            elif otype == "negative_feedback":
                d["negative"] = int(users)
            if last_ts and (d["last_event"] is None or last_ts > d["last_event"]):
                d["last_event"] = last_ts
    return out


# ─────────── Decision ───────────
def _analyze_one(exp: dict) -> dict:
    """Decide winner / paused / abandoned / continue for a single experiment."""
    results = _fetch_results(exp["id"])
    variants = sorted(results.keys())
    report: dict = {
        "experiment_key": exp["experiment_key"],
        "id": exp["id"],
        "variants": results,
        "decision": "continue",
        "reason": "",
    }

    # Abandoned?
    started = exp.get("started_at")
    has_recent = any(
        v.get("last_event") and (datetime.now(timezone.utc) - v["last_event"]).days <= ABANDON_AFTER_DAYS
        for v in results.values()
    )
    if started and (datetime.now(timezone.utc) - started).days >= ABANDON_AFTER_DAYS and not has_recent:
        report["decision"] = "abandoned"
        report["reason"] = "no events in the last 7d"
        return report

    if len(variants) < 2:
        return report

    # Need control = default_variant (or first variant)
    default = exp.get("default_variant") or variants[0]
    if default not in results:
        default = variants[0]

    insufficient = [v for v in variants if results[v]["assigned"] < MIN_SAMPLES_PER_VARIANT]
    if insufficient:
        report["reason"] = f"insufficient samples: {insufficient}"
        return report

    a_total = results[default]["assigned"]
    a_succ = results[default]["primary_succ"]
    a_rate = a_succ / a_total if a_total else 0
    a_se = _proportion_std_err(a_rate, a_total)

    best = default
    best_rate = a_rate

    # Auto-stop: any variant >2σ WORSE than control on primary outcome
    for v in variants:
        if v == default:
            continue
        b_total = results[v]["assigned"]
        b_succ = results[v]["primary_succ"]
        b_rate = b_succ / b_total if b_total else 0
        b_se = _proportion_std_err(b_rate, b_total)
        # Z-score of (b - a) / sqrt(se_a^2 + se_b^2)
        combined_se = math.sqrt(a_se ** 2 + b_se ** 2)
        z = (b_rate - a_rate) / combined_se if combined_se > 0 else 0
        results[v]["rate"] = b_rate
        results[v]["z_vs_control"] = z
        if z < -2.0:
            report["decision"] = "paused"
            report["reason"] = f"variant {v} is >2σ worse (z={z:.2f}) — safety pause"
            report["paused_variant"] = v
            return report
        if b_rate > best_rate:
            best = v
            best_rate = b_rate

    results[default]["rate"] = a_rate

    # Winner detection: chi-square between best and control
    if best == default:
        report["reason"] = "no variant beats control yet"
        return report

    b_total = results[best]["assigned"]
    b_succ = results[best]["primary_succ"]
    chi2, p = _chi_square_2x2(a_succ, a_total, b_succ, b_total)
    practical = (best_rate - a_rate) / a_rate if a_rate > 0 else float("inf")
    report["chi2"] = chi2
    report["p_value"] = p
    report["practical_lift"] = practical
    if p < MIN_P_VALUE and practical >= MIN_PRACTICAL_LIFT:
        report["decision"] = "winner"
        report["winner"] = best
        report["reason"] = f"p={p:.4f}, lift={practical*100:.1f}%"
    else:
        report["reason"] = f"p={p:.4f}, lift={practical*100:.1f}% — not significant yet"
    return report


def _apply_decision(exp_id: int, report: dict) -> None:
    decision = report["decision"]
    if decision == "winner":
        with conn_cursor() as (_, cur):
            cur.execute("""
                UPDATE ab_experiments
                SET winner=%s, status='completed', ended_at=NOW(),
                    meta = COALESCE(meta, '{}'::jsonb) || %s::jsonb
                WHERE id=%s
            """, (
                report["winner"],
                json.dumps({"analysis": {"p": report.get("p_value"), "lift": report.get("practical_lift")}}),
                exp_id,
            ))
    elif decision == "paused":
        with conn_cursor() as (_, cur):
            cur.execute("""
                UPDATE ab_experiments
                SET status='paused',
                    meta = COALESCE(meta, '{}'::jsonb) || %s::jsonb
                WHERE id=%s
            """, (json.dumps({"pause_reason": report.get("reason")}), exp_id))
    elif decision == "abandoned":
        with conn_cursor() as (_, cur):
            cur.execute("""
                UPDATE ab_experiments
                SET status='abandoned', ended_at=NOW(),
                    meta = COALESCE(meta, '{}'::jsonb) || %s::jsonb
                WHERE id=%s
            """, (json.dumps({"abandon_reason": report.get("reason")}), exp_id))


def _notify(report: dict) -> None:
    """Notify admin via bot_admin_notifier (re-use Pattern Mining's)."""
    try:
        from pattern_mining.bot_admin_notifier import send_summary  # type: ignore
    except Exception:
        return
    d = report["decision"]
    key = report["experiment_key"]
    if d == "winner":
        send_summary(
            f"🏆 <b>A/B Winner: {key}</b>\n"
            f"variant=<code>{report['winner']}</code>\n"
            f"p={report.get('p_value', 0):.4f} · lift=<b>{report.get('practical_lift', 0)*100:+.1f}%</b>"
        )
    elif d == "paused":
        send_summary(
            f"⏸ <b>A/B Auto-pause: {key}</b>\n"
            f"reason: {report.get('reason')}"
        )
    elif d == "abandoned":
        send_summary(
            f"🪦 <b>A/B Abandoned: {key}</b>\n"
            f"reason: {report.get('reason')}"
        )


def run(min_samples: int = MIN_SAMPLES_PER_VARIANT, dry_run: bool = False) -> dict:
    global MIN_SAMPLES_PER_VARIANT
    MIN_SAMPLES_PER_VARIANT = min_samples

    ensure_schema()
    experiments = _fetch_running_experiments()
    log.info("Analyzing %d running experiments", len(experiments))

    grand = {"status": "ok", "analyzed": 0, "winners": 0, "paused": 0, "abandoned": 0, "details": []}
    for exp in experiments:
        try:
            report = _analyze_one(exp)
        except Exception as exc:
            log.warning("analyze failed for %s: %s", exp.get("experiment_key"), exc)
            continue
        grand["analyzed"] += 1
        grand["details"].append(report)
        if report["decision"] == "winner":
            grand["winners"] += 1
        elif report["decision"] == "paused":
            grand["paused"] += 1
        elif report["decision"] == "abandoned":
            grand["abandoned"] += 1
        log.info("[%s] decision=%s reason=%s",
                 report["experiment_key"], report["decision"], report.get("reason"))
        if dry_run:
            continue
        if report["decision"] in ("winner", "paused", "abandoned"):
            _apply_decision(exp["id"], report)
            _notify(report)
    return grand


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min", type=int, default=MIN_SAMPLES_PER_VARIANT)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    report = run(min_samples=args.min, dry_run=args.dry_run)
    print(json.dumps(report, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
