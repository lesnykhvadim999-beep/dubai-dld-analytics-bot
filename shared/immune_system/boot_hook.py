"""immune_system.boot_hook — one-line integration for any bot.

Usage in bot main.py:

    from shared.immune_system.boot_hook import install_immune_catcher
    install_immune_catcher(bot_name="resale")

Re-exports for backwards-compatibility with the agent_bus boot pattern.
"""
from .catcher import install_immune_catcher, record_handled_exception  # noqa: F401

__all__ = ["install_immune_catcher", "record_handled_exception"]
