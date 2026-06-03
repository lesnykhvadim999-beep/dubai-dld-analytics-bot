"""Predictive Failure Detection — Level 7.

Forecast critical ecosystem metrics 3-7 days into the future using
Facebook Prophet (free) with a graceful fallback to a numpy linear-
regression model when Prophet is unavailable or fits poorly.

Public API
==========
    forecast_metric(metric_name, days_ahead=7, min_history_days=14) -> dict
    detect_anomaly(metric_name, current_value, days=30)              -> dict
    backtest_metric(metric_name, days=60)                            -> dict
    get_disk_threshold(metric_name)                                  -> float

All functions are best-effort and return a dict with "status" key.
They never raise — Prophet has been known to segfault on Windows under
exotic conditions; we wrap everything in try/except.

Caching: forecast results are cached on disk for 4h so daily_audit and
predictive_audit can share computation.
"""
from __future__ import annotations

import json
import os
import sys
import math
import time
import hashlib
import tempfile
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Any

_HERE = Path(__file__).resolve().parent
if str(_HERE.parent) not in sys.path:
    sys.path.insert(0, str(_HERE.parent))

from auto_audit._common import DSNS, q  # noqa: E402

# ────────────────────────── Optional deps ──────────────────────────
try:
    import pandas as pd  # type: ignore
    _HAS_PANDAS = True
except Exception:
    _HAS_PANDAS = False

try:
    import numpy as np  # type: ignore
    _HAS_NUMPY = True
except Exception:
    _HAS_NUMPY = False

try:
    # Silence the noisy "Importing plotly failed" warning on stderr.
    import logging
    logging.getLogger("prophet").setLevel(logging.ERROR)
    logging.getLogger("cmdstanpy").setLevel(logging.ERROR)
    from prophet import Prophet  # type: ignore
    _HAS_PROPHET = True
except Exception as _e:
    _HAS_PROPHET = False
    _PROPHET_ERR = str(_e)

# ────────────────────────── Disk thresholds ──────────────────────────
# Match auto_audit/daily_audit constant.
DB_LIMIT_MB = int(os.environ.get("DB_VOLUME_LIMIT_MB", "5000"))


def get_disk_threshold(metric_name: str) -> float:
    """Threshold (MB) where the DB is considered ~90% full."""
    # Per-DB override via env, fallback to 90% of the global limit.
    parts = metric_name.split(".")
    if len(parts) >= 3:
        db = parts[-1].upper()
        env_v = os.environ.get(f"{db}_DB_VOLUME_LIMIT_MB", "")
        if env_v.isdigit():
            return float(env_v) * 0.9
    return DB_LIMIT_MB * 0.9


# ────────────────────────── Cache helpers ──────────────────────────
_CACHE_DIR = Path(tempfile.gettempdir()) / "auto_audit_forecast_cache"
_CACHE_DIR.mkdir(exist_ok=True)
_CACHE_TTL_S = int(os.environ.get("FORECAST_CACHE_TTL_S", str(4 * 3600)))


def _cache_path(key: str) -> Path:
    h = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
    return _CACHE_DIR / f"{h}.json"


def _cache_get(key: str) -> dict | None:
    fp = _cache_path(key)
    if not fp.exists():
        return None
    try:
        if time.time() - fp.stat().st_mtime > _CACHE_TTL_S:
            return None
        return json.loads(fp.read_text(encoding="utf-8"))
    except Exception:
        return None


def _cache_set(key: str, payload: dict) -> None:
    try:
        _cache_path(key).write_text(
            json.dumps(payload, default=str), encoding="utf-8"
        )
    except Exception:
        pass


# ────────────────────────── DB fetch ──────────────────────────
def fetch_metric_history(metric_name: str, days: int = 90) -> list[dict]:
    """Return rows `[{'ds': datetime, 'y': float}, ...]` ordered by ds."""
    rows = q(
        DSNS["resale"],
        """
        SELECT ts AS ds, value AS y
        FROM audit_history
        WHERE metric = %s
          AND ts > NOW() - (%s::int * INTERVAL '1 day')
          AND value IS NOT NULL
        ORDER BY ts ASC
        """,
        (metric_name, days),
    )
    out = []
    for r in rows or []:
        if r.get("_error"):
            continue
        try:
            y = float(r["y"])
        except (TypeError, ValueError):
            continue
        out.append({"ds": r["ds"], "y": y})
    return out


# ────────────────────────── Linear fallback ──────────────────────────
def _linear_forecast(history: list[dict], periods_hours: int) -> dict[str, Any]:
    """Tiny least-squares linear forecast as a Prophet substitute."""
    if not _HAS_NUMPY or len(history) < 4:
        return {"status": "insufficient_history", "engine": "linear"}
    xs = np.array(
        [h["ds"].timestamp() for h in history], dtype=float
    )
    ys = np.array([h["y"] for h in history], dtype=float)
    # Normalise x for numerical stability.
    x0 = xs.min()
    xn = xs - x0
    if xn.max() == 0:
        return {"status": "insufficient_history", "engine": "linear"}
    slope, intercept = np.polyfit(xn, ys, 1)
    residual = ys - (slope * xn + intercept)
    sigma = float(residual.std()) if len(residual) > 1 else 0.0
    last_ts = history[-1]["ds"]
    forecast = []
    for h in range(1, periods_hours + 1):
        ts = last_ts + timedelta(hours=h)
        x_pred = ts.timestamp() - x0
        yhat = float(slope * x_pred + intercept)
        forecast.append(
            {
                "ds": ts,
                "yhat": yhat,
                "yhat_lower": yhat - 1.96 * sigma,
                "yhat_upper": yhat + 1.96 * sigma,
            }
        )
    return {
        "status": "ok",
        "engine": "linear",
        "slope_per_hour": float(slope * 3600),
        "sigma": sigma,
        "forecast": forecast,
    }


# ────────────────────────── Prophet wrapper ──────────────────────────
def _prophet_forecast(
    history: list[dict],
    periods_hours: int,
    metric_name: str,
) -> dict[str, Any]:
    if not (_HAS_PROPHET and _HAS_PANDAS):
        return {"status": "no_prophet"}
    try:
        df = pd.DataFrame(history)
        # Prophet requires tz-naive ds.
        df["ds"] = pd.to_datetime(df["ds"], utc=True).dt.tz_convert(None)
        seasonality_mode = (
            "multiplicative" if metric_name.endswith("_pct") else "additive"
        )
        # Disable noisy components — short history.
        m = Prophet(
            interval_width=0.95,
            changepoint_prior_scale=0.05,
            seasonality_mode=seasonality_mode,
            daily_seasonality=True,
            weekly_seasonality=len(df) >= 14 * 24,
            yearly_seasonality=False,
        )
        m.fit(df)
        future = m.make_future_dataframe(periods=periods_hours, freq="H")
        forecast = m.predict(future)
        tail = forecast.tail(periods_hours)[
            ["ds", "yhat", "yhat_lower", "yhat_upper"]
        ].copy()
        out_rows = []
        for _, r in tail.iterrows():
            out_rows.append(
                {
                    "ds": r["ds"].to_pydatetime(),
                    "yhat": float(r["yhat"]),
                    "yhat_lower": float(r["yhat_lower"]),
                    "yhat_upper": float(r["yhat_upper"]),
                }
            )
        return {"status": "ok", "engine": "prophet", "forecast": out_rows}
    except Exception as e:
        return {"status": "prophet_failed", "error": f"{type(e).__name__}: {e}"}


# ────────────────────────── Public forecast API ──────────────────────────
def forecast_metric(
    metric_name: str,
    days_ahead: int = 7,
    min_history_days: int = 14,
    use_cache: bool = True,
) -> dict[str, Any]:
    """Forecast `metric_name` and emit alert dicts for risky horizons.

    Returns dict with:
        status: ok | insufficient_history | error
        engine: prophet | linear | none
        forecast: list[{ds, yhat, yhat_lower, yhat_upper}]
        alerts: list[{type, severity, ...}]
    """
    cache_key = f"forecast::{metric_name}::{days_ahead}"
    if use_cache:
        cached = _cache_get(cache_key)
        if cached is not None:
            cached["cached"] = True
            return cached

    history = fetch_metric_history(metric_name, days=90)
    # Need at least min_history_days * 4 samples (loose — we record every 15min).
    min_samples = max(min_history_days * 4, 20)
    if len(history) < min_samples:
        result = {
            "status": "insufficient_history",
            "metric": metric_name,
            "have_samples": len(history),
            "need_samples": min_samples,
            "alerts": [],
        }
        if use_cache:
            _cache_set(cache_key, result)
        return result

    periods = days_ahead * 24
    fc = _prophet_forecast(history, periods, metric_name)
    if fc.get("status") != "ok":
        fc = _linear_forecast(history, periods)
    if fc.get("status") != "ok":
        result = {
            "status": "error",
            "metric": metric_name,
            "detail": fc,
            "alerts": [],
        }
        if use_cache:
            _cache_set(cache_key, result)
        return result

    forecast = fc["forecast"]
    current = history[-1]["y"]
    alerts: list[dict] = []

    name_l = metric_name.lower()

    # ── Accuracy / success rate drop detection ──
    if any(k in name_l for k in ("accuracy", "success_rate", "alive_ratio")):
        future_low = min(p["yhat_lower"] for p in forecast)
        when_idx = min(
            range(len(forecast)), key=lambda i: forecast[i]["yhat_lower"]
        )
        if future_low < current - 2.0:
            alerts.append(
                {
                    "type": "accuracy_drop",
                    "severity": "HIGH",
                    "current": current,
                    "predicted_low": future_low,
                    "when": forecast[when_idx]["ds"].isoformat(),
                }
            )

    # ── Disk usage growth → exhaustion ──
    if "size_mb" in name_l:
        threshold = get_disk_threshold(metric_name)
        future_high = max(p["yhat_upper"] for p in forecast)
        if future_high > threshold:
            exceed = next(
                (p for p in forecast if p["yhat_upper"] > threshold), None
            )
            if exceed is not None:
                alerts.append(
                    {
                        "type": "disk_full",
                        "severity": "CRITICAL",
                        "current": current,
                        "threshold": threshold,
                        "predicted_full": exceed["ds"].isoformat(),
                    }
                )

    # ── Staging backlog growth ──
    if name_l.startswith("staging.pending"):
        limit = int(os.environ.get("STAGING_BACKLOG_LIMIT", "50000"))
        future_high = max(p["yhat_upper"] for p in forecast)
        if future_high > limit:
            exceed = next(
                (p for p in forecast if p["yhat_upper"] > limit), None
            )
            if exceed is not None:
                alerts.append(
                    {
                        "type": "staging_overflow",
                        "severity": "HIGH",
                        "current": current,
                        "limit": limit,
                        "predicted_overflow": exceed["ds"].isoformat(),
                    }
                )

    # ── Leads intake drop ──
    if name_l.startswith("leads.intake_"):
        future_low = min(p["yhat_lower"] for p in forecast)
        # alert if predicted to drop below 50% of current and current >= 5
        if current >= 5 and future_low < current * 0.5:
            alerts.append(
                {
                    "type": "leads_drop",
                    "severity": "MEDIUM",
                    "current": current,
                    "predicted_low": future_low,
                }
            )

    # ── LLM quota approaching ──
    if "quota_used_pct" in name_l:
        future_high = max(p["yhat_upper"] for p in forecast)
        if future_high > 90.0:
            exceed = next(
                (p for p in forecast if p["yhat_upper"] > 90.0), None
            )
            alerts.append(
                {
                    "type": "llm_quota",
                    "severity": "HIGH",
                    "current": current,
                    "predicted_full": (
                        exceed["ds"].isoformat() if exceed else None
                    ),
                }
            )

    # ── Latency p95 rising trend ──
    if "latency" in name_l:
        future_high = max(p["yhat_upper"] for p in forecast)
        if current > 0 and future_high > current * 1.5:
            alerts.append(
                {
                    "type": "latency_rise",
                    "severity": "MEDIUM",
                    "current": current,
                    "predicted_high": future_high,
                }
            )

    result = {
        "status": "ok",
        "metric": metric_name,
        "engine": fc.get("engine", "unknown"),
        "current": current,
        "samples": len(history),
        "forecast_summary": {
            "yhat_first": forecast[0]["yhat"],
            "yhat_last": forecast[-1]["yhat"],
            "yhat_min": min(p["yhat_lower"] for p in forecast),
            "yhat_max": max(p["yhat_upper"] for p in forecast),
        },
        "alerts": alerts,
    }
    if use_cache:
        _cache_set(cache_key, result)
    return result


# ────────────────────────── Anomaly detection ──────────────────────────
def detect_anomaly(
    metric_name: str, current_value: float, days: int = 30
) -> dict[str, Any]:
    """Z-score based anomaly detection over the last `days` of history."""
    history = fetch_metric_history(metric_name, days=days)
    if len(history) < 10 or not _HAS_NUMPY:
        return {
            "status": "insufficient_history",
            "value": current_value,
            "n": len(history),
        }
    ys = np.array([h["y"] for h in history], dtype=float)
    mean = float(ys.mean())
    std = float(ys.std())
    z = (current_value - mean) / std if std > 0 else 0.0
    return {
        "status": "ok",
        "value": current_value,
        "mean": mean,
        "std": std,
        "z_score": z,
        "is_anomaly": abs(z) > 3.0,
        "direction": "high" if z > 0 else "low",
    }


# ────────────────────────── Backtest ──────────────────────────
def backtest_metric(metric_name: str, days: int = 60) -> dict[str, Any]:
    """Train on first 70% of history, predict last 30%, return MAPE."""
    history = fetch_metric_history(metric_name, days=days)
    if len(history) < 40 or not _HAS_NUMPY:
        return {
            "status": "insufficient_history",
            "metric": metric_name,
            "samples": len(history),
        }
    split = int(len(history) * 0.7)
    train = history[:split]
    test = history[split:]
    if not test:
        return {"status": "insufficient_history", "metric": metric_name}

    # Try Prophet first.
    engine = "linear"
    yhat_list: list[float] = []
    if _HAS_PROPHET and _HAS_PANDAS:
        try:
            df_train = pd.DataFrame(train)
            df_train["ds"] = pd.to_datetime(
                df_train["ds"], utc=True
            ).dt.tz_convert(None)
            m = Prophet(
                interval_width=0.95,
                daily_seasonality=True,
                weekly_seasonality=False,
                yearly_seasonality=False,
            )
            m.fit(df_train)
            df_test = pd.DataFrame(
                {"ds": [t["ds"] for t in test]}
            )
            df_test["ds"] = pd.to_datetime(
                df_test["ds"], utc=True
            ).dt.tz_convert(None)
            pred = m.predict(df_test)
            yhat_list = [float(v) for v in pred["yhat"].values]
            engine = "prophet"
        except Exception:
            yhat_list = []
    if not yhat_list:
        # Linear fallback
        xs = np.array([t["ds"].timestamp() for t in train], dtype=float)
        ys = np.array([t["y"] for t in train], dtype=float)
        x0 = xs.min()
        slope, intercept = np.polyfit(xs - x0, ys, 1)
        yhat_list = [
            float(slope * (t["ds"].timestamp() - x0) + intercept) for t in test
        ]

    y_true = np.array([t["y"] for t in test], dtype=float)
    y_pred = np.array(yhat_list, dtype=float)
    nonzero = np.abs(y_true) > 1e-9
    if not nonzero.any():
        return {"status": "no_signal", "metric": metric_name}
    mape = float(
        np.mean(np.abs((y_true[nonzero] - y_pred[nonzero]) / y_true[nonzero]))
        * 100
    )
    confidence = (
        "high" if mape < 10 else "medium" if mape < 25 else "low"
    )
    return {
        "status": "ok",
        "metric": metric_name,
        "engine": engine,
        "mape_pct": round(mape, 2),
        "confidence": confidence,
        "train_n": len(train),
        "test_n": len(test),
    }


__all__ = [
    "forecast_metric",
    "detect_anomaly",
    "backtest_metric",
    "fetch_metric_history",
    "get_disk_threshold",
]


# ────────────────────────── Self-test ──────────────────────────
if __name__ == "__main__":
    print(f"prophet_available = {_HAS_PROPHET}")
    print(f"pandas_available  = {_HAS_PANDAS}")
    print(f"numpy_available   = {_HAS_NUMPY}")
    sample_metrics = [
        "parser.accuracy_pct",
        "db.size_mb.archive",
        "staging.pending",
    ]
    for m in sample_metrics:
        r = forecast_metric(m, days_ahead=3, use_cache=False)
        print(f"\n{m}: status={r.get('status')} samples={r.get('have_samples') or r.get('samples')}")
        if r.get("alerts"):
            print(f"  ALERTS: {r['alerts']}")
