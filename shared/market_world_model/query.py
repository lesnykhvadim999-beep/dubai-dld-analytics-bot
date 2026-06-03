"""query.py — high-level API for forecasts, what-if, compare.

Loads pickled Prophet models from `market_models` and runs lightweight
inference. Designed to be called from bot handlers, so all results are
JSON-serialisable.
"""
from __future__ import annotations

import io
import math
import pickle
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from . import _db


@dataclass
class ForecastPoint:
    ds: str
    yhat: float
    yhat_lower: float
    yhat_upper: float

    def to_dict(self) -> dict:
        return {
            "ds": self.ds,
            "yhat": round(self.yhat, 2),
            "yhat_lower": round(self.yhat_lower, 2),
            "yhat_upper": round(self.yhat_upper, 2),
        }


# ---------------------------------------------------------------------------
# Entity lookup
# ---------------------------------------------------------------------------

def resolve_entity(conn, entity: str | int, entity_type: str | None = None) -> dict | None:
    """Resolve a name or id to a market_entities row."""
    cur = conn.cursor()
    if isinstance(entity, int) or (isinstance(entity, str) and entity.isdigit()):
        cur.execute(
            "SELECT id, entity_type, name, attributes FROM market_entities WHERE id=%s",
            (int(entity),),
        )
    else:
        if entity_type:
            cur.execute(
                "SELECT id, entity_type, name, attributes FROM market_entities "
                "WHERE entity_type=%s AND LOWER(name)=LOWER(%s) LIMIT 1",
                (entity_type, entity),
            )
        else:
            # try exact then fuzzy LIKE
            cur.execute(
                "SELECT id, entity_type, name, attributes FROM market_entities "
                "WHERE LOWER(name)=LOWER(%s) ORDER BY entity_type LIMIT 1",
                (entity,),
            )
            if cur.rowcount == 0:
                cur.execute(
                    "SELECT id, entity_type, name, attributes FROM market_entities "
                    "WHERE name ILIKE %s ORDER BY entity_type LIMIT 1",
                    (f"%{entity}%",),
                )
    row = cur.fetchone()
    if not row:
        return None
    return {"id": row[0], "entity_type": row[1], "name": row[2], "attributes": row[3]}


# ---------------------------------------------------------------------------
# Forecast
# ---------------------------------------------------------------------------

def _load_model(conn, entity_id: int, metric: str):
    """Load best available model for (entity, metric).

    Prefers Prophet; falls back to the linear payload stored under algo='linear'.
    Returns (model_obj_or_payload, meta dict including algo) or (None, None).
    """
    cur = conn.cursor()
    cur.execute(
        "SELECT algo, model_blob, rmse, samples, trained_at FROM market_models "
        "WHERE entity_id=%s AND metric=%s "
        "ORDER BY CASE algo WHEN 'prophet' THEN 0 ELSE 1 END, trained_at DESC LIMIT 1",
        (entity_id, metric),
    )
    row = cur.fetchone()
    if not row:
        return None, None
    algo = row[0]
    try:
        model = pickle.loads(bytes(row[1]))
    except Exception:
        return None, None
    meta = {"algo": algo, "rmse": row[2], "samples": row[3], "trained_at": row[4]}
    return model, meta


def _predict_linear(payload: dict, horizon_months: int):
    """Predict from a serialised linear payload (see builder._train_linear)."""
    from datetime import datetime as _dt
    last_date = _dt.fromisoformat(payload["last_date_iso"])
    x0 = _dt.fromisoformat(payload["x0_iso"])
    slope = payload["slope_per_day"]
    intercept = payload["intercept"]
    std = payload.get("resid_std") or 0.0
    out = []
    for i in range(1, horizon_months + 1):
        target = last_date + timedelta(days=30 * i)
        days = (target - x0).days
        yhat = intercept + slope * days
        out.append(ForecastPoint(
            ds=target.isoformat(),
            yhat=float(yhat),
            yhat_lower=float(yhat - 1.28 * std),
            yhat_upper=float(yhat + 1.28 * std),
        ))
    return out


def _fallback_trend(conn, entity_id: int, metric: str, horizon_months: int):
    """Naive linear trend when no Prophet model exists.

    Uses last 12 snapshots, fits a simple linear regression by hand (no
    sklearn). Returns ForecastPoint list at monthly cadence.
    """
    cur = conn.cursor()
    cur.execute(
        """
        SELECT captured_at::date, (metrics->>%s)::float8
        FROM market_state_snapshots
        WHERE entity_id=%s AND metrics ? %s
        ORDER BY captured_at DESC LIMIT 12
        """,
        (metric, entity_id, metric),
    )
    rows = [(d, v) for d, v in cur.fetchall() if v is not None]
    if len(rows) < 2:
        return []
    rows.sort(key=lambda r: r[0])
    # x = month index, y = value
    xs = list(range(len(rows)))
    ys = [r[1] for r in rows]
    n = len(rows)
    mx = sum(xs) / n
    my = sum(ys) / n
    num = sum((xs[i] - mx) * (ys[i] - my) for i in range(n))
    den = sum((xs[i] - mx) ** 2 for i in range(n)) or 1.0
    slope = num / den
    intercept = my - slope * mx
    last_date = rows[-1][0]
    out = []
    for i in range(1, horizon_months + 1):
        x = len(rows) - 1 + i
        yhat = intercept + slope * x
        # +/- 10% band as cheap uncertainty proxy
        out.append(ForecastPoint(
            ds=(last_date + timedelta(days=30 * i)).isoformat(),
            yhat=float(yhat),
            yhat_lower=float(yhat * 0.9),
            yhat_upper=float(yhat * 1.1),
        ))
    return out


def forecast(entity: str | int, metric: str = "price_per_sqft",
             horizon_months: int = 6) -> dict:
    """Forecast a metric for an entity.

    Returns dict ready for JSON: entity info + list of monthly points +
    summary (current, predicted, pct_change, confidence).
    """
    with _db.connect() as conn:
        ent = resolve_entity(conn, entity)
        if not ent:
            return {"ok": False, "error": "entity_not_found", "query": str(entity)}

        # current value (latest snapshot)
        cur = conn.cursor()
        cur.execute(
            """
            SELECT captured_at, (metrics->>%s)::float8
            FROM market_state_snapshots
            WHERE entity_id=%s AND metrics ? %s
            ORDER BY captured_at DESC LIMIT 1
            """,
            (metric, ent["id"], metric),
        )
        last_row = cur.fetchone()
        current = float(last_row[1]) if last_row and last_row[1] is not None else None

        model, meta = _load_model(conn, ent["id"], metric)
        points: list[ForecastPoint] = []
        source = "none"
        if model is not None and meta:
            algo = meta.get("algo")
            if algo == "prophet":
                try:
                    old = sys.stdout
                    sys.stdout = io.StringIO()
                    try:
                        weeks = max(1, int(horizon_months * 4.33))
                        future = model.make_future_dataframe(
                            periods=weeks, freq="W", include_history=False
                        )
                        fc = model.predict(future)
                        if len(fc) >= horizon_months * 4:
                            fc = fc.iloc[::4].head(horizon_months)
                        else:
                            fc = fc.head(horizon_months)
                    finally:
                        sys.stdout = old
                    for _, r in fc.iterrows():
                        points.append(ForecastPoint(
                            ds=r["ds"].isoformat() if hasattr(r["ds"], "isoformat") else str(r["ds"]),
                            yhat=float(r["yhat"]),
                            yhat_lower=float(r["yhat_lower"]),
                            yhat_upper=float(r["yhat_upper"]),
                        ))
                    source = "prophet"
                except Exception as e:
                    points = _fallback_trend(conn, ent["id"], metric, horizon_months)
                    source = f"fallback_after_prophet_error:{e}"
            elif algo == "linear" and isinstance(model, dict):
                points = _predict_linear(model, horizon_months)
                source = "linear_model"
        if not points:
            points = _fallback_trend(conn, ent["id"], metric, horizon_months)
            if source == "none":
                source = "linear_trend_onthefly"

        predicted = points[-1].yhat if points else None
        pct_change = None
        if current and predicted:
            try:
                pct_change = (predicted - current) / current * 100.0
            except ZeroDivisionError:
                pct_change = None

        return {
            "ok": True,
            "entity": ent,
            "metric": metric,
            "horizon_months": horizon_months,
            "current": current,
            "predicted": predicted,
            "pct_change": pct_change,
            "source": source,
            "model_meta": meta,
            "points": [p.to_dict() for p in points],
        }


# ---------------------------------------------------------------------------
# What-if scenarios
# ---------------------------------------------------------------------------

def what_if(entity: str | int, scenario: str,
            metric: str = "price_per_sqft",
            horizon_months: int = 6) -> dict:
    """Apply a simple scenario shift on top of the base forecast.

    Supported scenario keywords (free text, parsed):
      "supply +30%"   — supply increase tends to depress prices (elasticity -0.3)
      "supply -20%"   — supply decrease tends to lift prices
      "demand +25%"   — demand boost (elasticity +0.4)
      "demand -10%"
      "rates +1%"     — interest rate hike (elasticity -0.8 over 12m)
      "rates -1%"
    """
    base = forecast(entity, metric=metric, horizon_months=horizon_months)
    if not base.get("ok"):
        return base

    # parse scenario
    s = scenario.lower()
    elasticity = 0.0
    delta_pct = 0.0
    if "supply" in s:
        try:
            delta_pct = float(s.split("supply")[1].replace("%", "").strip().split()[0])
        except Exception:
            delta_pct = 0.0
        elasticity = -0.3
    elif "demand" in s:
        try:
            delta_pct = float(s.split("demand")[1].replace("%", "").strip().split()[0])
        except Exception:
            delta_pct = 0.0
        elasticity = 0.4
    elif "rates" in s or "rate" in s:
        try:
            delta_pct = float(s.split("rate")[1].replace("s", "").replace("%", "").strip().split()[0])
        except Exception:
            delta_pct = 0.0
        elasticity = -0.8

    shift_pct = elasticity * delta_pct  # percent shift applied to forecast

    scenario_points = []
    for p in base["points"]:
        mult = 1.0 + shift_pct / 100.0
        scenario_points.append({
            "ds": p["ds"],
            "yhat": round(p["yhat"] * mult, 2),
            "yhat_lower": round(p["yhat_lower"] * mult, 2),
            "yhat_upper": round(p["yhat_upper"] * mult, 2),
        })

    return {
        "ok": True,
        "entity": base["entity"],
        "metric": metric,
        "horizon_months": horizon_months,
        "scenario": scenario,
        "scenario_shift_pct": round(shift_pct, 2),
        "base_predicted": base["predicted"],
        "scenario_predicted": scenario_points[-1]["yhat"] if scenario_points else None,
        "points_base": base["points"],
        "points_scenario": scenario_points,
    }


# ---------------------------------------------------------------------------
# Compare
# ---------------------------------------------------------------------------

def compare(entity_a: str | int, entity_b: str | int,
            metric: str = "price_per_sqft",
            horizon_months: int = 6) -> dict:
    fa = forecast(entity_a, metric, horizon_months)
    fb = forecast(entity_b, metric, horizon_months)
    if not (fa.get("ok") and fb.get("ok")):
        return {"ok": False, "a": fa, "b": fb}

    winner = None
    if fa["pct_change"] is not None and fb["pct_change"] is not None:
        winner = fa["entity"]["name"] if fa["pct_change"] > fb["pct_change"] else fb["entity"]["name"]

    return {
        "ok": True,
        "a": {
            "entity": fa["entity"]["name"],
            "current": fa["current"],
            "predicted": fa["predicted"],
            "pct_change": fa["pct_change"],
        },
        "b": {
            "entity": fb["entity"]["name"],
            "current": fb["current"],
            "predicted": fb["predicted"],
            "pct_change": fb["pct_change"],
        },
        "winner_by_growth": winner,
        "metric": metric,
        "horizon_months": horizon_months,
    }
