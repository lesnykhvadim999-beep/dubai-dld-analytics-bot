"""immune_system — PHASE BN N2.

System that LEARNS from runtime exceptions:
  catcher    → records every uncaught exception (dedup by signature)
  diagnosis  → free LLM root-cause analyser, status=diagnosed
  immunizer  → free LLM patch + regression test, never auto-applied
  registry   → applied immunities + weekly verification

DSN: env-only (LIVE_DATABASE_URL > DATABASE_URL > INTELLIGENCE_DATABASE_URL).
"""
from .catcher import install_immune_catcher, record_handled_exception  # noqa: F401
from .diagnosis import diagnose_pending  # noqa: F401
from .immunizer import immunize_diagnosed  # noqa: F401
from .registry import record_applied_immunity, verify_all_immunities  # noqa: F401

__all__ = [
    "install_immune_catcher",
    "record_handled_exception",
    "diagnose_pending",
    "immunize_diagnosed",
    "record_applied_immunity",
    "verify_all_immunities",
]
