"""PHASE BO O4 RE-LAUNCH: auto-generated /help texts + admin runbook + quickstart.

Modules:
- help_generator: per-bot /help markdown stored in `bot_help_texts` table.
- runbook_generator: merged admin runbook → C:/Projects/shared/docs/ADMIN_RUNBOOK.md.
- quickstart: hardcoded reference guide → C:/Projects/shared/docs/QUICK_START.md.

All public docs MUST NOT leak DSN/tokens/passwords. Brand: Vadim Realty (RERA BRN 65011).
"""
from __future__ import annotations

__all__ = [
    "help_generator",
    "runbook_generator",
    "quickstart",
    "schema",
    "BOTS",
]

# Canonical bot registry — synced with shared/_sync_to_bots.py.
# (bot_name, telegram_username, repo_root, main_file, brief_role)
BOTS: list[dict] = [
    {
        "bot_name": "resale-bot",
        "username": "VadimResaleBot",
        "repo": r"C:\Projects\resale-bot",
        "main_file": r"C:\Projects\resale-bot\resale_bot.py",
        "role": "Dubai secondary-market search, Smart Picks, PDF reports",
    },
    {
        "bot_name": "channel-bot-new",
        "username": "VadimChannelBot",
        "repo": r"C:\Projects\channel-bot-new\Channel-Bot-new\telegram-bot",
        "main_file": r"C:\Projects\channel-bot-new\Channel-Bot-new\telegram-bot\bot.py",
        "role": "Telegram channel parsing + Reelly enrichment",
    },
    {
        "bot_name": "lead-bot",
        "username": "VadimLeadBot",
        "repo": r"C:\Projects\lead-bot\Lead-bot\telegram-bot",
        "main_file": r"C:\Projects\lead-bot\Lead-bot\telegram-bot\bot.py",
        "role": "Lead generation, qualification, ROI quick-check",
    },
    {
        "bot_name": "roi-bot",
        "username": "VadimROIBot",
        "repo": r"C:\Projects\roi-bot\ROI-bot",
        "main_file": r"C:\Projects\roi-bot\ROI-bot\bot.py",
        "role": "Investment ROI calculator + PDF",
    },
    {
        "bot_name": "analytics-bot",
        "username": "VadimAnalyticsBot",
        "repo": r"C:\Projects\dubai-dld-analytics-bot-main",
        "main_file": r"C:\Projects\dubai-dld-analytics-bot-main\bot.py",
        "role": "DLD market analytics & reports",
    },
    {
        "bot_name": "hub-bot",
        "username": "VadimHubBot",
        "repo": r"C:\Projects\hub-bot",
        "main_file": r"C:\Projects\hub-bot\bot.py",
        "role": "Central router + admin /stats",
    },
]
