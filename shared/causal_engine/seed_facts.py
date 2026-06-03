"""Seed 50+ baseline causal facts for Dubai real-estate domain.

Run once after applying schema.sql:
    python -m shared.causal_engine.seed_facts
"""
import os
import sys
import psycopg2

DSN = os.getenv(
    "INTELLIGENCE_DB_DSN",
    os.environ.get("LIVE_DATABASE_URL") or os.environ.get("DATABASE_URL") or os.environ.get("INTELLIGENCE_DATABASE_URL") or "",
)

# (cause, effect, source, conf, domain, areas, horizon)
FACTS = [
    # --- Macro / FX ---
    ("USD strength vs EUR/GBP", "GCC peg makes Dubai cheaper for EUR/GBP buyers -> demand +5-10%", "expert", 0.75, "macro", None, "short"),
    ("USD weakness", "EUR/RUB/GBP buyers gain purchasing power -> off-plan deals +10-15%", "expert", 0.7, "macro", None, "short"),
    ("Oil price > $90/bbl sustained", "GCC liquidity rises -> cash deals share grows by ~7%", "DLD_data", 0.65, "macro", None, "mid"),
    ("Fed rate cuts", "Mortgage rates in UAE fall (peg) -> mortgaged transactions +15%", "expert", 0.8, "macro", None, "mid"),
    ("Russian capital controls easing", "Inflow of Russian cash buyers to JVC/Marina/Business Bay", "news", 0.7, "macro", ["Jumeirah Village Circle","Dubai Marina","Business Bay"], "mid"),

    # --- Infrastructure ---
    ("Metro Blue Line announcement", "Areas within 500m of stations: +8-15% price within 12 months", "DLD_data", 0.85, "price", ["Mirdif","International City","Dubai Silicon Oasis"], "mid"),
    ("Etihad Rail passenger launch", "Connected emirates commute boost -> Sharjah-border areas +5%", "news", 0.6, "price", None, "long"),
    ("New school opening in area", "Family demand +20%, 3BR rents +8% within 6mo", "expert", 0.75, "rent", None, "mid"),
    ("Major hospital opening", "Premium area demand from medical tourists -> short-rent ADR +10%", "expert", 0.65, "rent", None, "mid"),
    ("Highway upgrade reducing commute", "Outer areas (DAMAC Hills, Town Square) price +5-8%", "expert", 0.7, "price", ["DAMAC Hills","Town Square","Dubailand"], "mid"),

    # --- Supply / Off-plan ---
    ("Off-plan handover wave (>3000 units in area)", "Short-term rental supply spike -> ADR -5-10%", "DLD_data", 0.85, "rent", None, "short"),
    ("Off-plan handover wave", "Resale prices flat or -3% for 6 months due to new-stock competition", "DLD_data", 0.8, "price", None, "short"),
    ("Low completion pipeline (<500 units / year in area)", "Supply scarcity -> price +6-12% over 24mo", "DLD_data", 0.75, "price", None, "long"),
    ("Mass developer discounts (DLD-recorded incentives)", "Secondary-market sellers cut asking -3-5% to compete", "DLD_data", 0.7, "price", None, "short"),
    ("Construction-delay news on tower", "Buyer trust drops -> secondary inventory in that tower discounted 5-10%", "news", 0.6, "price", None, "short"),

    # --- Seasonality ---
    ("Summer months (Jun-Aug)", "Listings -25%, prices flat, only serious buyers remain", "DLD_data", 0.85, "demand", None, "short"),
    ("Ramadan period", "Transaction volume -30%, prices stable, negotiations slow", "DLD_data", 0.8, "demand", None, "short"),
    ("Q4 (Oct-Dec)", "Peak transaction volume, sellers hold firm on price", "DLD_data", 0.85, "demand", None, "short"),
    ("January-February", "New-year inflow of investors -> off-plan launches sell out faster", "DLD_data", 0.8, "demand", None, "short"),
    ("GITEX / COP / WEF events", "Short-term rentals ADR +30-50% for event weeks", "expert", 0.85, "rent", ["Downtown Dubai","DIFC","Business Bay"], "short"),

    # --- Regulation / Visa ---
    ("Golden Visa threshold lowered to AED 2M", "Buyer pool widens -> mid-tier (AED 2-4M) units demand +15%", "news", 0.85, "demand", None, "mid"),
    ("10-year Golden Visa expansion", "Long-term residents convert renters -> buyers, owner-occupier share +5%", "expert", 0.7, "demand", None, "long"),
    ("RERA escrow rules tightened", "Off-plan launches slow, secondary becomes more attractive", "news", 0.65, "demand", None, "mid"),
    ("Foreign-ownership freehold expansion to new zone", "Newly-freehold area: +20-40% transaction volume in first 12mo", "DLD_data", 0.85, "demand", None, "mid"),
    ("Crypto-AED on-ramp legalisation by VARA", "Crypto-native buyers enter Dubai market -> luxury segment liquidity +10%", "news", 0.55, "demand", ["Palm Jumeirah","Dubai Hills","Emirates Hills"], "long"),

    # --- Area-specific ---
    ("Dubai Marina vacancy > 8%", "Landlords cut rents 5-7% to retain tenants", "DLD_data", 0.8, "rent", ["Dubai Marina"], "short"),
    ("Downtown new-hotel opening", "Short-term rental competition -> Airbnb ADR -8%", "expert", 0.65, "rent", ["Downtown Dubai"], "mid"),
    ("Palm Jumeirah luxury record sale", "Comp signals lift Palm asking-prices 3-5% for 6 months", "DLD_data", 0.75, "price", ["Palm Jumeirah"], "short"),
    ("JVC new-handover quarter", "JVC rents soften 3-5% for one quarter before absorbing", "DLD_data", 0.8, "rent", ["Jumeirah Village Circle"], "short"),
    ("Business Bay short-rent licence cap reached", "Operators bid up existing licensed units -> price +4-6%", "expert", 0.6, "price", ["Business Bay"], "mid"),

    # --- ROI mechanics ---
    ("Service-charge hike in tower", "Net ROI drops 0.5-1.2pp -> resale price compressed 3-5%", "DLD_data", 0.8, "roi", None, "mid"),
    ("Chiller-free vs paid-chiller in building", "Paid-chiller units net ROI 0.8pp lower vs comparable chiller-free", "expert", 0.7, "roi", None, "mid"),
    ("DEWA/cooling tariff increase", "All-in rent yield -0.3-0.5pp citywide", "news", 0.6, "roi", None, "mid"),
    ("Building Airbnb ban by HOA", "Short-rent ROI collapses, owners switch to long-term -3-5% gross yield", "expert", 0.75, "roi", None, "short"),
    ("Tower gets Bayut/PF top-listing badge", "Time-on-market -30% -> effective ROI through fewer vacant days", "expert", 0.55, "roi", None, "short"),

    # --- Demand / Buyers ---
    ("Spike in CIS buyers via Telegram channels", "JVC/Marina/Business Bay 1BR demand +20%", "telegram", 0.65, "demand", ["Jumeirah Village Circle","Dubai Marina","Business Bay"], "short"),
    ("Chinese visa-free announcement to UAE", "Chinese buyer inquiries +50%, focus on Downtown & Marina luxury", "news", 0.7, "demand", ["Downtown Dubai","Dubai Marina"], "mid"),
    ("Indian rupee depreciation > 5% YoY", "Indian buyers shift to lower-ticket areas (Dubailand, IC) demand +8%", "expert", 0.55, "demand", ["Dubailand","International City"], "mid"),
    ("UK stamp-duty hike", "UK landlords reallocate to Dubai -> luxury freehold inquiries +15%", "news", 0.6, "demand", None, "mid"),
    ("Israel-Gulf tourism normalization", "Short-rent occupancy +5-8% during Jewish holidays", "expert", 0.5, "rent", ["Downtown Dubai","Palm Jumeirah"], "mid"),

    # --- Price signals ---
    ("Asking-vs-sold gap > 12%", "Market softening signal -> next-quarter prices -2-4%", "DLD_data", 0.75, "price", None, "short"),
    ("Median DOM (days-on-market) > 90", "Seller fatigue -> price drops 3-5% within 60 days", "DLD_data", 0.8, "price", None, "short"),
    ("Median DOM < 30", "Sellers raise asking 2-4% next cycle", "DLD_data", 0.8, "price", None, "short"),
    ("Cash-deal share > 60% in area", "Liquidity strong -> prices resilient even in rate-hike cycles", "DLD_data", 0.7, "price", None, "mid"),
    ("Mortgage-deal share spiking", "Rate-sensitive segment -> vulnerable to corrections on Fed hikes", "DLD_data", 0.7, "price", None, "mid"),

    # --- News / sentiment ---
    ("Major developer IPO announcement", "Sector sentiment lift -> branded-units price +3-6% for 3 months", "news", 0.55, "price", None, "short"),
    ("Negative ratings agency report on UAE", "Foreign-investor pause -> luxury inquiries -10% for 2 months", "news", 0.5, "demand", None, "short"),
    ("EXPO-style mega event announcement", "Areas adjacent to venue +5-10% within 12mo", "news", 0.65, "price", None, "mid"),
    ("Direct flight launch from new country", "Buyer inquiries from that geography +30-80%", "expert", 0.6, "demand", None, "mid"),
    ("Cryptocurrency bull market (BTC > $100k sustained)", "Crypto-rich buyers enter Palm/Emirates Hills -> luxury comps +5%", "expert", 0.5, "demand", ["Palm Jumeirah","Emirates Hills","Dubai Hills"], "short"),

    # --- Risk / negative ---
    ("Building structural-defect news", "That tower discount 8-15% for 12+ months", "news", 0.8, "price", None, "long"),
    ("Area flood event (heavy rains)", "Affected sub-areas -3-5% for 6 months", "news", 0.65, "price", None, "short"),
]


def main():
    conn = psycopg2.connect(DSN)
    conn.autocommit = True
    cur = conn.cursor()
    inserted = 0
    skipped = 0
    for cause, effect, src, conf, domain, areas, horizon in FACTS:
        try:
            cur.execute(
                """
                INSERT INTO causal_facts
                    (cause_text, effect_text, evidence_source, confidence,
                     domain, areas, time_horizon, supporting_examples, verified_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,'[]'::jsonb, NOW())
                ON CONFLICT (cause_text, effect_text) DO NOTHING
                """,
                (cause, effect, src, conf, domain, areas, horizon),
            )
            if cur.rowcount:
                inserted += 1
            else:
                skipped += 1
        except Exception as e:
            print(f"[seed] skip: {e}")
    cur.close()
    conn.close()
    print(f"[seed] inserted={inserted} skipped={skipped} total_attempted={len(FACTS)}")


if __name__ == "__main__":
    sys.exit(main())
