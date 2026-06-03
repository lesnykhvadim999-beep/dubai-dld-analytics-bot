"""
PHASE BM bootstrap для dubai-dld-analytics-bot.

Подключает Layer 18 (multimodal), Layer 22 (background think).
Layer 20 (virtual tours) не подключаем — у analytics-бота нет listing-flow,
tour-команды живут в resale-bot.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

SHARED_PARENT = Path(__file__).resolve().parent.parent
if str(SHARED_PARENT) not in sys.path:
    sys.path.insert(0, str(SHARED_PARENT))

log = logging.getLogger("dld.phase_bm")

BOT_NAME = "dubai-dld-analytics-bot"


def wire_phase_bm(dp) -> None:
    try:
        from shared.multimodal.bot_integrations import register_multimodal_handlers
        register_multimodal_handlers(dp, bot_name=BOT_NAME)
    except Exception as e:
        log.exception("multimodal wiring failed: %s", e)

    try:
        from shared.continuous_reasoning.bot_integrations import (
            attach_background_think, register_noreminders_command,
        )
        attach_background_think(dp, bot_name=BOT_NAME)
        register_noreminders_command(dp)
    except Exception as e:
        log.exception("continuous_reasoning wiring failed: %s", e)

    log.info("PHASE BM wired into dubai-dld-analytics-bot")
