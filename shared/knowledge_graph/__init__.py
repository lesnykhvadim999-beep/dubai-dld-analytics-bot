"""shared.knowledge_graph — Level 5 Cross-bot Knowledge Graph.

Public API: KG.add / KG.search / KG.get_aliases / KG.upvote / KG.downvote.
"""
from __future__ import annotations

from .client import KG  # noqa: F401

__all__ = ["KG"]
