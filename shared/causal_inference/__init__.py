"""Layer 17 — Causal Inference Engine.

Mathematical causal inference via DoWhy + EconML on historical DLD /
intelligence data. This is the math-driven sibling of Layer 11
(``shared.causal_engine``), which is LLM-driven.

Public surface
--------------
    from shared.causal_inference import (
        run_study,           # build dataset + fit estimator + persist
        run_all_prebuilt,    # 5 pre-baked analyses
        load_study,          # fetch saved ATE + model from DB
        counterfactual,      # "what-if" simulation
        format_effect_ru,    # short RU phrase for bot UI
        REGISTRY,            # pre-built study definitions
    )
"""

from .estimator import run_study, load_study  # noqa: F401
from .counterfactual import counterfactual  # noqa: F401
from .reporter import format_effect_ru  # noqa: F401
from .prebuilt import REGISTRY, run_all_prebuilt  # noqa: F401

__all__ = [
    "run_study",
    "run_all_prebuilt",
    "load_study",
    "counterfactual",
    "format_effect_ru",
    "REGISTRY",
]
