"""
pattern_mining — Phase BL Level 3 (Bot Self-Improvement).

Analyzes `bot_interactions` (from Level 1 behavior_tracking) and proposes
actionable fixes (missing aliases, slow queries, bad responses) for admin
approval through Telegram.

Modules:
- discover_bot_patterns: daily cron (03:30) — main entry
- _db: psycopg2 helper (RESALE_DATABASE_URL)
- llm_cluster_analyzer: LLM (Cerebras → Groq → Gemini) → suggested fix
- bot_admin_notifier: Telegram approve/reject keyboard
- bot_pattern_applier: auto-apply approved fixes (KG add_alias, cache config)
- approval_handler: callback handler for `bpm:*` callbacks

DB table: `bot_pattern_discoveries` (см. migrations.sql).

All LLM calls — through free chain (Cerebras → Groq → Gemini).
No paid services per CLAUDE.md hard-rule.
"""

__version__ = "0.1.0"
