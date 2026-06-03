"""
parser_self_improve — Phase 1 self-improving parser for resale-bot.

Modules:
- failed_collector: писать failed extractions в parser_failed_log
- cluster_engine:   k-means через Gemini embeddings
- llm_pattern_proposer: Cerebras LLM → suggested regex/rule
- approval_handler: Telegram callbacks Approve/Reject
- pattern_applier:  auto-edit parser_v2.py + git commit (gated by approve)
- discover_patterns: weekly cron entry

DB table: parser_failed_log (см. migrations.sql).

Все LLM calls — через free chain (Cerebras → Groq → Gemini), без paid services.
"""

__version__ = "0.1.0"
