# -*- coding: utf-8 -*-
"""Shared DB helpers + area canonicalization for area_rankings builders."""
from __future__ import annotations
import os
import logging
from typing import Optional

log = logging.getLogger("area_rankings")

# DLD archive (sales + rents) — read-only
DLD_DSN = os.environ.get(
    "RESALE_DATABASE_URL",
    "postgresql://postgres:VUgxGizYpffEPDszSDHMRMPbBFvFOONz@switchback.proxy.rlwy.net:23244/railway",
)
# Intelligence DB — where rankings live
INTEL_DSN = os.environ.get(
    "INTELLIGENCE_DATABASE_URL",
    "postgresql://postgres:fbcwyUUBpQAzafMfMvjiyyFzDynKcKsz@autorack.proxy.rlwy.net:25004/railway",
)

# Canonical area_name → list of DLD area_name_en synonyms used in WHERE clauses.
# Same shape used by analytics-bot's smart_area_universe() fallback.
AREA_CATALOG: dict[str, list[str]] = {
    "Dubai Marina": ["Marsa Dubai", "Dubai Marina"],
    "Downtown Dubai": ["Burj Khalifa", "Downtown Dubai"],
    "Jumeirah Village Circle": [
        "Al Barsha South Fourth", "Al Barsha South Fifth",
        "Al Hebiah First", "Jumeirah Village Circle",
    ],
    "Business Bay": ["Business Bay"],
    "Palm Jumeirah": ["Palm Jumeirah"],
    "JLT": ["Jumeirah Lakes Towers"],
    "Sobha Hartland": ["Sobha Hartland", "Al Merkadh"],
    "Arabian Ranches": ["Arabian Ranches", "Wadi Al Safa 6", "Wadi Al Safa 7"],
    "Dubai Hills Estate": ["Dubai Hills Estate", "Hadaeq Sheikh Mohammed Bin Rashid"],
    "Mirdif": ["Mirdif"],
    "Damac Hills": ["Damac Hills", "Hadaeq Mohammed Bin Rashid"],
    "JBR": ["Jumeirah Beach Residence", "JBR"],
    "Town Square": ["Town Square", "Al Yelayiss 2", "Al Yufrah 1"],
    "Al Barsha": ["Al Barsha", "Al Barsha First", "Al Barsha Second", "Al Barsha Third"],
    "Tilal Al Ghaf": ["Tilal Al Ghaf", "Al Yelayiss 1"],
    "DAMAC Lagoons": ["DAMAC Lagoons", "Al Hebiah Fourth"],
    "MBR City": ["Mohammed Bin Rashid City", "Al Merkadh"],
    "Dubai Creek Harbour": ["Dubai Creek Harbour", "Al Jadaf"],
    "Bluewaters": ["Bluewaters Island", "Bluewaters"],
    "Dubai South": ["Dubai South", "Madinat Al Mataar"],
}


def get_dld_conn(timeout: int = 20):
    import psycopg2
    return psycopg2.connect(DLD_DSN, connect_timeout=timeout)


def get_intel_conn(timeout: int = 20):
    import psycopg2
    return psycopg2.connect(INTEL_DSN, connect_timeout=timeout)


def upsert_ranking(cur, goal: str, area_name: str, synonyms: list[str],
                   rank: int, score: float, metrics: dict,
                   data_source: str) -> None:
    """UPSERT one row into area_rankings."""
    cur.execute(
        """
        INSERT INTO area_rankings
            (goal, area_name, area_synonyms, rank, score,
             price_growth_12mo_pct, avg_psqft, deal_volume_12mo, liquidity_score,
             rental_yield_pct, avg_rent_annual, vacancy_score,
             schools_score, safety_score, amenities_score, family_friendly_score,
             walk_score, price_stability_pct,
             data_source, updated_at)
        VALUES (%s,%s,%s,%s,%s,
                %s,%s,%s,%s,
                %s,%s,%s,
                %s,%s,%s,%s,
                %s,%s,
                %s, NOW())
        ON CONFLICT (goal, area_name) DO UPDATE SET
            area_synonyms = EXCLUDED.area_synonyms,
            rank = EXCLUDED.rank,
            score = EXCLUDED.score,
            price_growth_12mo_pct = EXCLUDED.price_growth_12mo_pct,
            avg_psqft = EXCLUDED.avg_psqft,
            deal_volume_12mo = EXCLUDED.deal_volume_12mo,
            liquidity_score = EXCLUDED.liquidity_score,
            rental_yield_pct = EXCLUDED.rental_yield_pct,
            avg_rent_annual = EXCLUDED.avg_rent_annual,
            vacancy_score = EXCLUDED.vacancy_score,
            schools_score = EXCLUDED.schools_score,
            safety_score = EXCLUDED.safety_score,
            amenities_score = EXCLUDED.amenities_score,
            family_friendly_score = EXCLUDED.family_friendly_score,
            walk_score = EXCLUDED.walk_score,
            price_stability_pct = EXCLUDED.price_stability_pct,
            data_source = EXCLUDED.data_source,
            updated_at = NOW();
        """,
        (
            goal, area_name, synonyms, rank, score,
            metrics.get("price_growth_12mo_pct"),
            metrics.get("avg_psqft"),
            metrics.get("deal_volume_12mo"),
            metrics.get("liquidity_score"),
            metrics.get("rental_yield_pct"),
            metrics.get("avg_rent_annual"),
            metrics.get("vacancy_score"),
            metrics.get("schools_score"),
            metrics.get("safety_score"),
            metrics.get("amenities_score"),
            metrics.get("family_friendly_score"),
            metrics.get("walk_score"),
            metrics.get("price_stability_pct"),
            data_source,
        ),
    )


def log_refresh(goal: str, rows_written: int, error: Optional[str] = None) -> None:
    """Write a refresh-log row. Best-effort."""
    try:
        with get_intel_conn(timeout=10) as conn:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO area_rankings_refresh_log
                       (finished_at, goal, rows_written, error)
                       VALUES (NOW(), %s, %s, %s)""",
                    (goal, rows_written, (error or None)),
                )
    except Exception as e:
        log.warning("[area_rankings] log_refresh failed: %s", e)
