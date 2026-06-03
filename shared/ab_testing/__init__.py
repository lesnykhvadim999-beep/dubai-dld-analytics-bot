"""
ab_testing — Phase BL Level 4 (A/B Testing Framework).

Public API:
    from shared.ab_testing import get_variant, record_outcome, register_experiment

DB tables: `ab_experiments`, `ab_assignments`, `ab_outcomes`.

Deterministic assignment by hash(user_id + experiment_key). Persistent through
`ab_assignments`. If experiment is paused/completed → returns winner (or default).

Auto-analysis cron: `analyze_experiments.py` (daily 04:30).
Auto-stop guardrail: variant >2σ worse → auto-pause.
"""
from .client import (
    get_variant,
    record_outcome,
    register_experiment,
    list_running,
    set_winner,
    pause_experiment,
    ensure_schema,
)

__version__ = "0.1.0"

__all__ = [
    "get_variant",
    "record_outcome",
    "register_experiment",
    "list_running",
    "set_winner",
    "pause_experiment",
    "ensure_schema",
]
