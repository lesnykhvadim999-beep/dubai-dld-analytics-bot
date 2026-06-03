"""Smoke fixtures — "canary" SQL queries that MUST return rows on a healthy prod.

If any of these returns < expect_min:
  - something broke in schema / parsing / filters / data ingestion
  - we surface it as an admin alert and a claude_autoheal_queue task

Run via smoke_fixture_runner.py (cron) or admin /smoke command.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Fixture:
    id: str
    description: str
    sql: str
    bot: str = "shared"
    dsn_env: str = "LIVE_DATABASE_URL"
    expect_min: int = 1
    # Soft fixtures: failure logs but does NOT page admin.
    soft: bool = False
    tags: list[str] = field(default_factory=list)


# NOTE: SQL uses plain WHERE clauses (no safe_date_sql wrappers) because the
# runner connects via psycopg2 directly, not via the bot's helper layer.
# Where possible we filter on canonical text fields the parser owns.

FIXTURES: list[Fixture] = [
    # ── analytics-bot / DLD ─────────────────────────────────────────────
    Fixture(
        id="dld_full_has_rows",
        description="dld_transactions_full master table is non-empty",
        bot="analytics",
        dsn_env="LIVE_DATABASE_URL",
        sql="SELECT COUNT(*) FROM public.dld_transactions_full",
        expect_min=10_000,
        tags=["smoke", "data-integrity"],
    ),
    Fixture(
        id="dld_grande_burj_12m",
        description="Grande in Burj Khalifa area, past 12 months (any prop/rooms)",
        bot="analytics",
        dsn_env="LIVE_DATABASE_URL",
        sql=(
            "SELECT COUNT(*) FROM public.dld_transactions_full "
            "WHERE LOWER(COALESCE(building_name_en,'')) LIKE '%grande%' "
            "  AND LOWER(COALESCE(area_name_en,'')) LIKE '%burj%khalifa%' "
            "  AND instance_date >= (CURRENT_DATE - INTERVAL '12 months')"
        ),
        expect_min=1,
        tags=["building", "period"],
    ),
    Fixture(
        id="dld_marina_apartment_all_time",
        description="Marina apartments — all-time count > 100",
        bot="analytics",
        dsn_env="LIVE_DATABASE_URL",
        sql=(
            "SELECT COUNT(*) FROM public.dld_transactions_full "
            "WHERE LOWER(COALESCE(area_name_en,'')) LIKE '%marina%' "
            "  AND LOWER(COALESCE(property_type_en,'')) LIKE '%unit%'"
        ),
        expect_min=100,
        tags=["area"],
    ),
    Fixture(
        id="dld_downtown_2br_sale_ever",
        description="Downtown 2BR sale all-time",
        bot="analytics",
        dsn_env="LIVE_DATABASE_URL",
        sql=(
            "SELECT COUNT(*) FROM public.dld_transactions_full "
            "WHERE LOWER(COALESCE(area_name_en,'')) LIKE '%downtown%' "
            "  AND LOWER(COALESCE(rooms_en,'')) LIKE '%2 b/r%'"
        ),
        expect_min=10,
        tags=["area", "rooms"],
    ),
    Fixture(
        id="dld_recent_30d",
        description="DLD data ingestion alive — anything within last 30d",
        bot="analytics",
        dsn_env="LIVE_DATABASE_URL",
        sql=(
            "SELECT COUNT(*) FROM public.dld_transactions_full "
            "WHERE instance_date >= (CURRENT_DATE - INTERVAL '30 days')"
        ),
        expect_min=100,
        tags=["freshness", "critical"],
    ),
    Fixture(
        id="dld_jvc_rooms_breakdown",
        description="JVC area has variety of rooms types (canonical parse works)",
        bot="analytics",
        dsn_env="LIVE_DATABASE_URL",
        sql=(
            "SELECT COUNT(DISTINCT rooms_en) FROM public.dld_transactions_full "
            "WHERE LOWER(COALESCE(area_name_en,'')) LIKE '%jumeirah village%'"
        ),
        expect_min=3,
        tags=["parser"],
    ),

    # ── resale-bot ──────────────────────────────────────────────────────
    # Schema note: the active table is `public.listings` (NOT listings_v2 —
    # that's an empty staging/v2 table). Key columns: is_active (bool),
    # area (text), property_type (text), price (numeric), created_at,
    # has_images (bool), cover_image_url (text).
    Fixture(
        id="listings_active_apartment_marina",
        description="resale-bot: active apartments in Marina",
        bot="resale",
        dsn_env="RESALE_DATABASE_URL",
        sql=(
            "SELECT COUNT(*) FROM public.listings "
            "WHERE is_active = true "
            "  AND LOWER(COALESCE(area,'')) LIKE '%marina%' "
            "  AND LOWER(COALESCE(property_type,'')) LIKE '%apart%'"
        ),
        expect_min=10,
        tags=["resale", "active"],
    ),
    Fixture(
        id="listings_fresh_with_price_7d",
        description="resale-bot fresh listings (7d) with price > 0",
        bot="resale",
        dsn_env="RESALE_DATABASE_URL",
        sql=(
            "SELECT COUNT(*) FROM public.listings "
            "WHERE created_at > NOW() - INTERVAL '7 days' "
            "  AND price > 0"
        ),
        expect_min=20,
        tags=["resale", "freshness"],
    ),
    Fixture(
        id="resale_with_images",
        description="resale-bot active listings with image",
        bot="resale",
        dsn_env="RESALE_DATABASE_URL",
        sql=(
            "SELECT COUNT(*) FROM public.listings "
            "WHERE is_active = true "
            "  AND (has_images = true OR cover_image_url IS NOT NULL)"
        ),
        expect_min=50,
        soft=True,
        tags=["resale", "images"],
    ),

    # ── channel-bot ─────────────────────────────────────────────────────
    Fixture(
        id="channel_buildings_dictionary",
        description="channel-bot: buildings dictionary populated (>100)",
        bot="channel",
        dsn_env="RESALE_DATABASE_URL",
        sql="SELECT COUNT(*) FROM public.buildings",
        expect_min=100,
        tags=["channel", "config"],
    ),

    # ── lead-bot ────────────────────────────────────────────────────────
    Fixture(
        id="lead_leads_table_has_rows",
        description="lead-bot leads table has rows (any time)",
        bot="lead",
        dsn_env="RESALE_DATABASE_URL",
        sql="SELECT COUNT(*) FROM public.leads",
        expect_min=1,
        soft=True,
        tags=["lead"],
    ),

    # ── hub-bot ─────────────────────────────────────────────────────────
    Fixture(
        id="hub_users_registered",
        description="hub-bot bot_users table has rows",
        bot="hub",
        dsn_env="RESALE_DATABASE_URL",
        sql="SELECT COUNT(*) FROM public.bot_users",
        expect_min=1,
        soft=True,
        tags=["hub"],
    ),

    # ── audit / staging ─────────────────────────────────────────────────
    Fixture(
        id="audit_log_recent",
        description="admin_actions log being written (last 7d)",
        bot="shared",
        dsn_env="RESALE_DATABASE_URL",
        sql=(
            "SELECT COUNT(*) FROM public.admin_actions "
            "WHERE timestamp > NOW() - INTERVAL '7 days'"
        ),
        expect_min=1,
        soft=True,
        tags=["audit"],
    ),

    # ── ROI bot proxy fixture (via DLD data) ────────────────────────────
    Fixture(
        id="roi_calc_marina_1br_data",
        description="ROI bot — DLD has Marina 1BR sale+rent both, so ROI computable",
        bot="roi",
        dsn_env="LIVE_DATABASE_URL",
        sql=(
            "SELECT COUNT(*) FROM public.dld_transactions_full "
            "WHERE LOWER(COALESCE(area_name_en,'')) LIKE '%marina%' "
            "  AND LOWER(COALESCE(rooms_en,'')) LIKE '%1 b/r%' "
            "  AND instance_date >= (CURRENT_DATE - INTERVAL '24 months')"
        ),
        expect_min=50,
        tags=["roi", "data"],
    ),
]


def by_id(fid: str) -> Optional[Fixture]:
    for f in FIXTURES:
        if f.id == fid:
            return f
    return None


def all_ids() -> list[str]:
    return [f.id for f in FIXTURES]
