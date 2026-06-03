"""shared.area_rankings — data-driven area ranking system for analytics-bot.

Replaces hardcoded smart_area_universe() with DLD-derived rankings refreshed
weekly. Three goals: resale, rental, living. Storage: intelligence DB table
`area_rankings`.

Read API:
    from shared.area_rankings.query import query_area_rankings_top
    rows = query_area_rankings_top("resale", limit=8)

Refresh API (cron / manual):
    python -m shared.area_rankings.refresh        # all goals
    python -m shared.area_rankings.refresh resale # one goal
"""
