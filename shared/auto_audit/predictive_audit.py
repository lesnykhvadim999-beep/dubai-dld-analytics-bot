"""Predictive Audit — runs every 4h via Windows Task Scheduler.

Pipeline:
    1. Sweep CRITICAL_METRICS and call `forecast_metric(...)` (3-day horizon).
    2. For each forecast that emits alerts, send a Telegram alert
       (throttled to once / 24h per metric+alert_type).
    3. Persist a heartbeat tick into audit_history so daily_audit cron-section
       confirms the predictor is alive.
    4. Optionally run a backtest sweep on Sundays 05:00.

Invocation:
    py -3 C:\\Projects\\shared\\auto_audit\\predictive_audit.py
    py -3 C:\\Projects\\shared\\auto_audit\\predictive_audit.py --backtest
"""
from __future__ import annotations

import sys
import time
import html as _html
from pathlib import Path
from datetime import datetime, timezone, timedelta

_HERE = Path(__file__).resolve().parent
if str(_HERE.parent) not in sys.path:
    sys.path.insert(0, str(_HERE.parent))

from auto_audit._common import (  # noqa: E402
    admin_notify,
    ensure_schema,
    record_metric,
    should_alert,
)
from auto_audit.predictor import (  # noqa: E402
    forecast_metric,
    backtest_metric,
    detect_anomaly,
    _HAS_PROPHET,
)

DUBAI_TZ = timezone(timedelta(hours=4))
NOW_DUBAI = datetime.now(DUBAI_TZ)

CRITICAL_METRICS = [
    "parser.accuracy_pct",
    "parser.active_total",
    "db.size_mb.archive",
    "db.size_mb.resale",
    "db.size_mb.live",
    "db.size_mb.intelligence",
    "staging.pending",
    "leads.intake_24h",
    "llm.chain.alive_ratio",
]

# Per-metric pretty label.
METRIC_LABEL = {
    "parser.accuracy_pct":    "Parser accuracy %",
    "parser.active_total":    "Active listings",
    "db.size_mb.archive":     "DB archive size (MB)",
    "db.size_mb.resale":      "DB resale size (MB)",
    "db.size_mb.live":        "DB live size (MB)",
    "db.size_mb.intelligence":"DB intelligence size (MB)",
    "staging.pending":        "Staging pending",
    "leads.intake_24h":       "Leads intake 24h",
    "llm.chain.alive_ratio":  "LLM chain alive ratio",
}

SEVERITY_EMOJI = {
    "CRITICAL": "🔴",
    "HIGH":     "🟠",
    "MEDIUM":   "🟡",
    "LOW":      "🟢",
}


def fmt_alert(metric: str, alert: dict, current: float, engine: str) -> str:
    sev = alert.get("severity", "MEDIUM")
    emoji = SEVERITY_EMOJI.get(sev, "ℹ️")
    label = METRIC_LABEL.get(metric, metric)
    lines = [
        f"🔮 <b>Predictive Alert {emoji}</b>",
        "",
        f"Metric: <code>{_html.escape(metric)}</code>",
        f"Severity: <b>{sev}</b>",
        f"Current: {current:.2f}",
    ]
    a_type = alert.get("type", "")
    if a_type == "disk_full":
        lines += [
            f"Threshold: {alert.get('threshold'):.0f} MB",
            f"Forecast hits threshold: <b>{alert.get('predicted_full')}</b>",
            "Action: extend Railway volume soon.",
        ]
    elif a_type == "accuracy_drop":
        lines += [
            f"Predicted low: {alert.get('predicted_low'):.2f} @ {alert.get('when')}",
            "Action: check parser regression / DLD feed.",
        ]
    elif a_type == "staging_overflow":
        lines += [
            f"Limit: {alert.get('limit')}",
            f"Forecast overflow: {alert.get('predicted_overflow')}",
            "Action: speed up staging processor.",
        ]
    elif a_type == "leads_drop":
        lines += [
            f"Predicted low: {alert.get('predicted_low'):.2f}",
            "Action: investigate funnel / channel reach.",
        ]
    elif a_type == "llm_quota":
        lines += [
            f"Predicted full: {alert.get('predicted_full')}",
            "Action: rotate to fallback provider.",
        ]
    elif a_type == "latency_rise":
        lines += [
            f"Predicted high: {alert.get('predicted_high'):.2f}",
            "Action: check bot perf / DB load.",
        ]
    else:
        for k, v in alert.items():
            if k in ("type", "severity"):
                continue
            lines.append(f"{k}: {v}")

    lines.append("")
    lines.append(f"Engine: <code>{engine}</code> (confidence ~95%)")
    return "\n".join(lines)


def run_forecasts() -> dict:
    summary = {
        "ok": 0,
        "insufficient": 0,
        "errors": 0,
        "alerts_sent": 0,
        "details": [],
    }
    for metric in CRITICAL_METRICS:
        try:
            result = forecast_metric(metric, days_ahead=3, use_cache=True)
        except Exception as e:
            summary["errors"] += 1
            summary["details"].append(
                {"metric": metric, "status": "exception", "error": str(e)}
            )
            continue
        status = result.get("status")
        if status == "insufficient_history":
            summary["insufficient"] += 1
        elif status == "ok":
            summary["ok"] += 1
            for alert in result.get("alerts", []):
                key = f"predictive::{metric}::{alert.get('type','x')}"
                if should_alert(key, ttl_seconds=24 * 3600):
                    msg = fmt_alert(
                        metric, alert, result.get("current", 0.0),
                        result.get("engine", "?"),
                    )
                    sent = admin_notify(msg, priority="critical")
                    if sent:
                        summary["alerts_sent"] += 1
        else:
            summary["errors"] += 1
        summary["details"].append(
            {
                "metric": metric,
                "status": status,
                "engine": result.get("engine"),
                "alerts": len(result.get("alerts", [])),
                "samples": result.get("samples") or result.get("have_samples"),
            }
        )
    return summary


def run_backtest() -> dict:
    """Sunday-only backtest sweep — measures MAPE per metric."""
    results: list[dict] = []
    for metric in CRITICAL_METRICS:
        try:
            r = backtest_metric(metric, days=60)
        except Exception as e:
            r = {
                "status": "exception",
                "metric": metric,
                "error": str(e),
            }
        results.append(r)
        if r.get("status") == "ok":
            record_metric(
                f"predictor.backtest.mape.{metric}",
                r["mape_pct"],
                {"engine": r["engine"], "confidence": r["confidence"]},
            )
    # Build a short admin digest.
    lines = ["🔮 <b>Predictor Backtest</b>", ""]
    for r in results:
        m = r.get("metric", "?")
        if r.get("status") != "ok":
            lines.append(
                f"• <code>{_html.escape(m)}</code>: {r.get('status')}"
            )
            continue
        emoji = {"high": "✅", "medium": "⚠️", "low": "🔴"}.get(
            r["confidence"], "ℹ️"
        )
        lines.append(
            f"{emoji} <code>{_html.escape(m)}</code> MAPE "
            f"{r['mape_pct']}% ({r['engine']}, {r['confidence']})"
        )
    msg = "\n".join(lines)
    admin_notify(msg, priority="normal")
    return {"results": results}


def main() -> int:
    t0 = time.time()
    ensure_schema()

    is_backtest = "--backtest" in sys.argv
    if is_backtest:
        out = run_backtest()
        dt = time.time() - t0
        record_metric(
            "predictor.backtest.duration_s", round(dt, 2),
            {"count": len(out["results"])},
        )
        print(f"[predictive_audit] backtest done in {dt:.1f}s")
        return 0

    summary = run_forecasts()
    dt = time.time() - t0
    record_metric("cron.tick.predictive_audit", 1, summary)
    record_metric(
        "predictor.forecast.duration_s", round(dt, 2),
        {
            "ok": summary["ok"],
            "insufficient": summary["insufficient"],
            "errors": summary["errors"],
            "alerts_sent": summary["alerts_sent"],
            "prophet": _HAS_PROPHET,
        },
    )
    print(
        f"[predictive_audit] done in {dt:.1f}s "
        f"ok={summary['ok']} insufficient={summary['insufficient']} "
        f"errors={summary['errors']} alerts_sent={summary['alerts_sent']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
