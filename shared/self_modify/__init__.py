"""
PHASE BM Layer 21 — Self-Modifying Code (с human approval).

Pipeline:
  proposer.py  -> Cerebras анализирует bug_kb + auto_audit, генерирует unified diff
  sandbox.py   -> применяет patch в временный venv/worktree, прогоняет pytest
  approval.py  -> admin-bot inline-кнопки '✅/❌'
  applier.py   -> после approve: git commit + Railway redeploy

ВАЖНО: НИКОГДА не auto-apply без admin approval (Vadim).
"""
from .proposer import propose_fix
from .sandbox import run_sandbox
from .approval import request_approval, handle_decision, list_pending_proposals
from .applier import apply_approved

__all__ = [
    "propose_fix", "run_sandbox",
    "request_approval", "handle_decision", "list_pending_proposals",
    "apply_approved",
]
