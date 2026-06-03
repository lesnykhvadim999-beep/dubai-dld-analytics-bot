"""
shared/behavior_tracking — Phase BL (Behavior Logging + Feedback).

Public API:
    from shared.behavior_tracking import (
        track, log_interaction, update_outcome,
        feedback_kb, handle_feedback_callback,
        ensure_schema,
    )

All DB writes are best-effort (try/except, never block user flow).
Toggle via env:
    BEHAVIOR_TRACKING_ENABLED=1      # default 1
    BEHAVIOR_FEEDBACK_ENABLED=1      # default 0 (opt-in)
"""
from .tracker import (
    track,
    log_interaction,
    update_outcome,
    ensure_schema,
    TrackingContext,
    is_enabled,
)
from .feedback import (
    feedback_kb,
    handle_feedback_callback,
    record_feedback,
    is_feedback_enabled,
)
from . import analytics  # noqa: F401

__all__ = [
    "track",
    "log_interaction",
    "update_outcome",
    "ensure_schema",
    "TrackingContext",
    "is_enabled",
    "feedback_kb",
    "handle_feedback_callback",
    "record_feedback",
    "is_feedback_enabled",
    "analytics",
]
