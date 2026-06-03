"""Layer 11 — Causal Reasoning Engine.

Exposes:
    reasoner.explain(question, features) -> CausalChain
    explainer.format_chain(chain, lang='ru'|'en') -> str
    learner.run_weekly() -> int (new facts proposed)
    validator.backtest() -> dict
"""
from .reasoner import explain, CausalChain  # noqa: F401
from .explainer import format_chain  # noqa: F401
