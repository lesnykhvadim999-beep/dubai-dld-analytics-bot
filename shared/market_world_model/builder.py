"""builder.py — seed + retrain pipeline for the Dubai Market World Model.

Run modes:
    python -m shared.market_world_model.builder --seed
    python -m shared.market_world_model.builder --train --limit 20
    python -m shared.market_world_model.builder --weekly      # full weekly job

Seed pulls top areas/buildings/developers from the existing resale DB and
populates `market_entities`, `market_relationships`, `market_state_snapshots`.

Train fits a Prophet model per (entity, metric) on monthly aggregates and
stores the pickled model in `market_models`.

Design constraints:
- Free only: uses local Prophet 1.3.0 (already installed) and our Railway
  Postgres. No paid APIs.
- Idempotent: ON CONFLICT clauses everywhere.
- Defensive: skips entities with insufficient history (< 6 monthly points).
"""
from __future__ import annotations

import argparse
import io
import logging
import math
import os
import pickle
import sys
import warnings
from datetime import datetime, timezone
from typing import Iterable

import psycopg2
import psycopg2.extras

# Silence Prophet/cmdstan noise on import.
warnings.filterwarnings("ignore")
os.environ.setdefault("CMDSTANPY_LOG_LEVEL", "ERROR")
logging.getLogger("cmdstanpy").setLevel(logging.ERROR)
logging.getLogger("prophet").setLevel(logging.ERROR)
logging.getLogger("prophet.plot").setLevel(logging.ERROR)

from . import _db

log = logging.getLogger("mwm.builder")
logging.basicConfig(
    level=os.getenv("MWM_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)


# ---------------------------------------------------------------------------
# Schema bootstrap
# ---------------------------------------------------------------------------

def ensure_schema(conn) -> None:
    sql_path = os.path.join(os.path.dirname(__file__), "schema.sql")
    with open(sql_path, "r", encoding="utf-8") as fh:
        conn.cursor().execute(fh.read())
    conn.commit()
    log.info("schema ensured")


# ---------------------------------------------------------------------------
# Seed entities + relationships + snapshots from existing tables
# ---------------------------------------------------------------------------

TOP_AREAS_SQL = """
    SELECT area,
           COUNT(*)                                  AS listings_cnt,
           AVG(price_per_sqft) FILTER (WHERE price_per_sqft > 0) AS avg_psf,
           AVG(price::numeric) FILTER (WHERE price > 0)          AS avg_price
    FROM listings
    WHERE area IS NOT NULL AND area <> ''
    GROUP BY area
    ORDER BY listings_cnt DESC
    LIMIT %s
"""

TOP_BUILDINGS_SQL = """
    SELECT building, area, COUNT(*) AS listings_cnt,
           AVG(price_per_sqft) FILTER (WHERE price_per_sqft > 0) AS avg_psf
    FROM listings
    WHERE building IS NOT NULL AND building <> ''
    GROUP BY building, area
    ORDER BY listings_cnt DESC
    LIMIT %s
"""

# Weekly time series: median price_per_sqft + listing count (supply) + demand
# proxy. Resale DB has ~5 months of history; weekly cadence yields ~20 points
# which is enough for Prophet to fit a yearly-seasonality model with healthy
# regularisation.
MONTHLY_AREA_SQL = """
    SELECT date_trunc('week', message_date)::date AS week,
           PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY price_per_sqft) AS median_psf,
           COUNT(*) AS supply,
           COUNT(*) FILTER (WHERE created_at >= message_date - INTERVAL '30 days') AS demand_proxy
    FROM listings
    WHERE area = %s
      AND message_date IS NOT NULL
      AND price_per_sqft IS NOT NULL
      AND price_per_sqft BETWEEN 100 AND 20000
    GROUP BY 1
    ORDER BY 1
"""

MONTHLY_BUILDING_SQL = """
    SELECT date_trunc('week', message_date)::date AS week,
           PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY price_per_sqft) AS median_psf,
           COUNT(*) AS supply
    FROM listings
    WHERE building = %s
      AND message_date IS NOT NULL
      AND price_per_sqft IS NOT NULL
      AND price_per_sqft BETWEEN 100 AND 20000
    GROUP BY 1
    ORDER BY 1
"""


def _liquidity_tier(monthly_supply_median: float) -> str:
    if monthly_supply_median >= 200:
        return "A"
    if monthly_supply_median >= 60:
        return "B"
    if monthly_supply_median >= 15:
        return "C"
    return "D"


def upsert_entity(cur, entity_type: str, name: str,
                  parent_id: int | None = None,
                  attributes: dict | None = None) -> int:
    cur.execute(
        """
        INSERT INTO market_entities (entity_type, name, parent_id, attributes, updated_at)
        VALUES (%s, %s, %s, %s::jsonb, NOW())
        ON CONFLICT (entity_type, name)
        DO UPDATE SET parent_id = COALESCE(EXCLUDED.parent_id, market_entities.parent_id),
                      attributes = market_entities.attributes || EXCLUDED.attributes,
                      updated_at = NOW()
        RETURNING id
        """,
        (entity_type, name, parent_id, psycopg2.extras.Json(attributes or {})),
    )
    return cur.fetchone()[0]


def upsert_relationship(cur, from_id: int, to_id: int,
                        relation_type: str, strength: float = 1.0,
                        attributes: dict | None = None) -> None:
    cur.execute(
        """
        INSERT INTO market_relationships (from_id, to_id, relation_type, strength, attributes)
        VALUES (%s, %s, %s, %s, %s::jsonb)
        ON CONFLICT (from_id, to_id, relation_type)
        DO UPDATE SET strength = EXCLUDED.strength,
                      attributes = market_relationships.attributes || EXCLUDED.attributes
        """,
        (from_id, to_id, relation_type, strength,
         psycopg2.extras.Json(attributes or {})),
    )


def upsert_snapshot(cur, entity_id: int, captured_at: datetime,
                    metrics: dict, source: str) -> None:
    cur.execute(
        """
        INSERT INTO market_state_snapshots (entity_id, captured_at, metrics, source)
        VALUES (%s, %s, %s::jsonb, %s)
        ON CONFLICT (entity_id, captured_at, source) DO UPDATE
            SET metrics = EXCLUDED.metrics
        """,
        (entity_id, captured_at, psycopg2.extras.Json(metrics), source),
    )


def seed(top_areas: int = 50, top_buildings: int = 200,
         monthly_history: bool = True) -> dict:
    """Populate entities + monthly snapshots. Returns stats dict."""
    stats = {"areas": 0, "buildings": 0, "developers": 0,
             "snapshots": 0, "relationships": 0}
    with _db.connect() as conn:
        ensure_schema(conn)
        cur = conn.cursor()

        # 1. Areas ----------------------------------------------------------
        cur.execute(TOP_AREAS_SQL, (top_areas,))
        area_rows = cur.fetchall()
        area_ids: dict[str, int] = {}
        for area, listings_cnt, avg_psf, avg_price in area_rows:
            eid = upsert_entity(
                cur, "area", area,
                attributes={
                    "listings_total": int(listings_cnt or 0),
                    "avg_psf": float(avg_psf or 0),
                    "avg_price": float(avg_price or 0),
                },
            )
            area_ids[area] = eid
            stats["areas"] += 1
            if monthly_history:
                # build monthly snapshots
                cur2 = conn.cursor()
                cur2.execute(MONTHLY_AREA_SQL, (area,))
                rows = cur2.fetchall()
                if rows:
                    supplies = [r[2] for r in rows if r[2]]
                    tier = _liquidity_tier(
                        sorted(supplies)[len(supplies) // 2] if supplies else 0
                    )
                    # update entity with tier
                    cur.execute(
                        "UPDATE market_entities SET attributes = attributes || %s::jsonb "
                        "WHERE id=%s",
                        (psycopg2.extras.Json({"liquidity_tier": tier}), eid),
                    )
                    for month, median_psf, supply, demand in rows:
                        if median_psf is None:
                            continue
                        upsert_snapshot(
                            cur, eid,
                            datetime.combine(month, datetime.min.time(), tzinfo=timezone.utc),
                            {
                                "price_per_sqft": float(median_psf),
                                "supply": int(supply or 0),
                                "demand": int(demand or 0),
                                "liquidity_tier": tier,
                            },
                            source="resale_listings",
                        )
                        stats["snapshots"] += 1
        conn.commit()
        log.info("seeded %d areas with %d snapshots", stats["areas"], stats["snapshots"])

        # 2. Buildings ------------------------------------------------------
        cur.execute(TOP_BUILDINGS_SQL, (top_buildings,))
        for building, area, listings_cnt, avg_psf in cur.fetchall():
            parent = area_ids.get(area)
            if parent is None and area:
                # seed area on demand
                parent = upsert_entity(cur, "area", area, attributes={"seeded_via": "building"})
                area_ids[area] = parent
                stats["areas"] += 1
            bid = upsert_entity(
                cur, "building", building, parent_id=parent,
                attributes={
                    "area": area,
                    "listings_total": int(listings_cnt or 0),
                    "avg_psf": float(avg_psf or 0),
                },
            )
            stats["buildings"] += 1
            if parent:
                upsert_relationship(cur, parent, bid, "area_contains_building", 1.0)
                stats["relationships"] += 1
            if monthly_history:
                cur2 = conn.cursor()
                cur2.execute(MONTHLY_BUILDING_SQL, (building,))
                for month, median_psf, supply in cur2.fetchall():
                    if median_psf is None:
                        continue
                    upsert_snapshot(
                        cur, bid,
                        datetime.combine(month, datetime.min.time(), tzinfo=timezone.utc),
                        {
                            "price_per_sqft": float(median_psf),
                            "supply": int(supply or 0),
                        },
                        source="resale_listings",
                    )
                    stats["snapshots"] += 1
        conn.commit()
        log.info("seeded %d buildings", stats["buildings"])

        # 3. Developers (from buildings table) -----------------------------
        cur.execute(
            "SELECT DISTINCT developer FROM buildings "
            "WHERE developer IS NOT NULL AND developer <> '' "
            "ORDER BY developer LIMIT 200"
        )
        dev_ids = {}
        for (dev,) in cur.fetchall():
            did = upsert_entity(cur, "developer", dev, attributes={})
            dev_ids[dev] = did
            stats["developers"] += 1
        conn.commit()

        # link buildings -> developer where known
        cur.execute(
            "SELECT name, developer FROM buildings "
            "WHERE developer IS NOT NULL AND developer <> ''"
        )
        link_rows = cur.fetchall()
        for bname, dev in link_rows:
            # find building entity by name
            cur.execute(
                "SELECT id FROM market_entities WHERE entity_type='building' AND name=%s",
                (bname,),
            )
            r = cur.fetchone()
            if not r:
                continue
            did = dev_ids.get(dev)
            if not did:
                continue
            upsert_relationship(cur, r[0], did, "building_by_developer", 1.0)
            stats["relationships"] += 1
        conn.commit()
        log.info("seeded %d developers, total relationships %d",
                 stats["developers"], stats["relationships"])

    return stats


# ---------------------------------------------------------------------------
# Prophet training
# ---------------------------------------------------------------------------

def _import_prophet():
    try:
        from prophet import Prophet
        return Prophet
    except Exception as e:  # pragma: no cover
        log.warning("Prophet not available: %s", e)
        return None


def _fetch_series(conn, entity_id: int, metric: str):
    """Return list of (date, value) monthly observations."""
    cur = conn.cursor()
    cur.execute(
        """
        SELECT captured_at::date AS ds,
               (metrics->>%s)::float8 AS y
        FROM market_state_snapshots
        WHERE entity_id = %s
          AND metrics ? %s
        ORDER BY captured_at
        """,
        (metric, entity_id, metric),
    )
    out = [(d, v) for d, v in cur.fetchall() if v is not None and not math.isnan(v)]
    return out


def _train_prophet(Prophet, series):
    """Try Prophet; return (blob, rmse, n) or None."""
    import pandas as pd
    df = pd.DataFrame(series, columns=["ds", "y"])
    df["ds"] = pd.to_datetime(df["ds"])
    if df["ds"].nunique() < 6:
        return None
    try:
        m = Prophet(
            yearly_seasonality=False,
            weekly_seasonality=False,
            daily_seasonality=False,
            changepoint_prior_scale=0.05,
            interval_width=0.8,
        )
        old_stdout = sys.stdout
        sys.stdout = io.StringIO()
        try:
            m.fit(df)
        finally:
            sys.stdout = old_stdout
        fc = m.predict(df[["ds"]])
        resid = (df["y"].values - fc["yhat"].values)
        rmse = float((resid ** 2).mean() ** 0.5)
        return pickle.dumps(m, protocol=4), rmse, len(df), "prophet"
    except Exception:
        return None


def _train_linear(series):
    """Fallback: numpy linear regression + residual std.

    Stores a small dict in model_blob so query.py can run forecasts without
    Prophet (useful when cmdstanpy binary fails on the host).
    """
    import numpy as np
    if len(series) < 4:
        return None
    dates = [d for d, _ in series]
    ys = np.array([v for _, v in series], dtype=float)
    # x = days since epoch of first point
    x0 = dates[0]
    xs = np.array([(d - x0).days for d in dates], dtype=float)
    if xs.max() == xs.min():
        return None
    # OLS
    a = np.vstack([xs, np.ones_like(xs)]).T
    slope, intercept = np.linalg.lstsq(a, ys, rcond=None)[0]
    fit = slope * xs + intercept
    resid = ys - fit
    rmse = float(np.sqrt((resid ** 2).mean()))
    std = float(resid.std()) or rmse or 1.0
    payload = {
        "kind": "linear",
        "slope_per_day": float(slope),
        "intercept": float(intercept),
        "x0_iso": x0.isoformat() if hasattr(x0, "isoformat") else str(x0),
        "rmse": rmse,
        "resid_std": std,
        "last_value": float(ys[-1]),
        "last_date_iso": dates[-1].isoformat() if hasattr(dates[-1], "isoformat") else str(dates[-1]),
        "n": len(series),
    }
    return pickle.dumps(payload, protocol=4), rmse, len(series), "linear"


def _train_one(Prophet, entity_id: int, metric: str, series):
    if len(series) < 6:
        return None
    # Try Prophet first if available; fall back to linear.
    if Prophet is not None:
        try:
            res = _train_prophet(Prophet, series)
            if res is not None:
                return res
        except Exception as e:
            log.debug("prophet failed for %s/%s: %s — falling back to linear",
                      entity_id, metric, e)
    res = _train_linear(series)
    if res is None:
        log.warning("train fail entity=%s metric=%s: insufficient data", entity_id, metric)
    return res


def train(limit: int | None = None,
          metrics: Iterable[str] = ("price_per_sqft", "supply"),
          use_prophet: bool = True) -> dict:
    Prophet = _import_prophet() if use_prophet else None
    if Prophet is None:
        log.info("Prophet disabled or unavailable — using linear fallback")

    stats = {"trained": 0, "skipped": 0, "errors": 0}
    with _db.connect() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT e.id, e.entity_type, e.name
            FROM market_entities e
            WHERE e.entity_type IN ('area', 'building')
              AND EXISTS (
                  SELECT 1 FROM market_state_snapshots s
                  WHERE s.entity_id = e.id
              )
            ORDER BY (e.attributes->>'listings_total')::int DESC NULLS LAST
            """
        )
        entities = cur.fetchall()
        if limit:
            entities = entities[:limit]
        log.info("training %d entities x %d metrics", len(entities), len(list(metrics)))
        for eid, etype, name in entities:
            for metric in metrics:
                series = _fetch_series(conn, eid, metric)
                if not series or len(series) < 6:
                    stats["skipped"] += 1
                    continue
                out = _train_one(Prophet, eid, metric, series)
                if out is None:
                    stats["skipped"] += 1
                    continue
                blob, rmse, n, algo = out
                cur.execute(
                    """
                    INSERT INTO market_models (entity_id, metric, algo, model_blob,
                                                trained_at, rmse, samples, horizon_days, meta)
                    VALUES (%s, %s, %s, %s, NOW(), %s, %s, 365, %s::jsonb)
                    ON CONFLICT (entity_id, metric, algo) DO UPDATE
                        SET model_blob = EXCLUDED.model_blob,
                            trained_at = NOW(),
                            rmse = EXCLUDED.rmse,
                            samples = EXCLUDED.samples,
                            meta = EXCLUDED.meta
                    """,
                    (eid, metric, algo, psycopg2.Binary(blob), rmse, n,
                     psycopg2.extras.Json({"entity_type": etype, "name": name})),
                )
                conn.commit()
                stats["trained"] += 1
                log.info("trained %s/%s (%s) algo=%s rmse=%.2f n=%d",
                         etype, name, metric, algo, rmse, n)
    return stats


# ---------------------------------------------------------------------------
# Weekly orchestrator
# ---------------------------------------------------------------------------

def weekly() -> dict:
    """Full weekly job: re-seed snapshots + retrain top-200 models."""
    log.info("=== weekly retrain start ===")
    seed_stats = seed(top_areas=50, top_buildings=200, monthly_history=True)
    train_stats = train(limit=200, metrics=("price_per_sqft", "supply"))
    log.info("=== weekly retrain done ===")
    return {"seed": seed_stats, "train": train_stats}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--seed", action="store_true")
    p.add_argument("--train", action="store_true")
    p.add_argument("--weekly", action="store_true")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--top-areas", type=int, default=50)
    p.add_argument("--top-buildings", type=int, default=200)
    p.add_argument("--no-prophet", action="store_true",
                   help="Skip Prophet, use linear fallback only")
    args = p.parse_args(argv)

    if args.weekly:
        print(weekly())
        return 0
    if args.seed:
        print(seed(top_areas=args.top_areas, top_buildings=args.top_buildings))
    if args.train:
        print(train(limit=args.limit, use_prophet=not args.no_prophet))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
