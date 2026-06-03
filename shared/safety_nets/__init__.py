"""safety_nets — production safety helpers.

Three minimal sub-modules:
  - circuit_breaker  : @with_circuit_breaker decorator + persistent state
  - feature_flags    : is_feature_enabled / report_failure (auto-disable on 3 fails)
  - watchdog         : heartbeat(bot_name) + check_stale_bots()

All Postgres state lives in tables created by schema.sql. DSN is read from
env vars only (LIVE_DATABASE_URL / DATABASE_URL / INTELLIGENCE_DATABASE_URL) —
never hardcoded.

Vadim Realty (RERA BRN 65011) — internal alert footer.
"""
from .circuit_breaker import (
    CircuitBreaker,
    CircuitOpenError,
    with_circuit_breaker,
)
from .feature_flags import (
    is_feature_enabled,
    register_feature,
    report_failure,
    report_success,
)
from .watchdog import (
    heartbeat,
    check_stale_bots,
    start_heartbeat_thread,
)
from .bootstrap import apply_schema, get_conn

__all__ = [
    "CircuitBreaker",
    "CircuitOpenError",
    "with_circuit_breaker",
    "is_feature_enabled",
    "register_feature",
    "report_failure",
    "report_success",
    "heartbeat",
    "check_stale_bots",
    "start_heartbeat_thread",
    "apply_schema",
    "get_conn",
]
