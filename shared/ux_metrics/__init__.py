"""
shared.ux_metrics — PHASE BO O3 RE-LAUNCH.

UX measurement + auto-rollback for UI changes.

Public API:
    from shared.ux_metrics.tracker import track_event, ensure_schema, is_opted_out
    from shared.ux_metrics.analyzer import (
        compute_funnel, compute_metric, compare_variants
    )
    from shared.ux_metrics.rollback import check_metrics, run_once
    from shared.ux_metrics.integration import (
        track_button_click, track_menu_shown, track_action_completed
    )

DSN: env-only. Order:
    LIVE_DATABASE_URL → RESALE_DATABASE_URL → DATABASE_URL → INTELLIGENCE_DATABASE_URL

Privacy:
    - Only Telegram numeric user_id is stored.
    - No names / phones / emails.
    - /no_analytics command writes into ux_opt_outs (per bot).

Vadim Realty | RERA BRN 65011
"""
from __future__ import annotations

__all__ = [
    "track_event",
    "ensure_schema",
    "is_opted_out",
    "set_opt_out",
    "compute_funnel",
    "compute_metric",
    "compare_variants",
    "check_metrics",
]

# Lazy re-exports to avoid import overhead when bot only logs events.
def __getattr__(name):  # PEP 562
    if name in ("track_event", "ensure_schema", "is_opted_out", "set_opt_out"):
        from . import tracker
        return getattr(tracker, name)
    if name in ("compute_funnel", "compute_metric", "compare_variants"):
        from . import analyzer
        return getattr(analyzer, name)
    if name == "check_metrics":
        from . import rollback
        return rollback.check_metrics
    raise AttributeError(name)
