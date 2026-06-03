"""counterfactual.py — "what-if" simulation against a saved study.

We do not re-fit DoWhy at inference time. The pickled estimator carries
ATE + per-feature means/stds; a counterfactual is computed as:

    predicted = base_mean(outcome)
              + ATE * (treatment_scenario - treatment_baseline)
              + Σ β_c * (confounder_scenario_c - confounder_mean_c)

For confounders we use a 1-feature linear nudge anchored to standard
deviations so callers can pass partial scenarios. This is intentionally
simple, transparent, and good enough for bot-side UX. Heavy CATE
(conditional ATE) work is delegated to a re-fit of EconML inside
``estimator.run_study``.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

import psycopg2.extras

from . import data_loader, estimator

log = logging.getLogger(__name__)


def _persist_cf(study_id: int, scenario: Dict[str, Any],
                pred: float, lo: float, hi: float, label: str) -> None:
    with data_loader.db_conn() as c, c.cursor() as cur:
        cur.execute(
            """
            INSERT INTO causal_counterfactuals
                  (study_id, scenario_jsonb, predicted_outcome,
                   predicted_lower, predicted_upper, label)
            VALUES (%s,%s,%s,%s,%s,%s)
            """,
            (study_id, json.dumps(scenario), pred, lo, hi, label),
        )
        c.commit()


def counterfactual(
    study_name: str,
    scenario: Dict[str, Any],
    label: Optional[str] = None,
    persist: bool = True,
) -> Dict[str, Any]:
    """Compute a what-if prediction.

    scenario example
    ----------------
        counterfactual(
            "Effect_of_metro_extension_on_prices",
            {"treatment": 1, "confounders": {"floor": 25, "size_sqft": 950}},
            label="JVC if metro arrives",
        )
    """
    saved = estimator.load_study(study_name)
    if not saved or not saved.get("model"):
        raise LookupError(f"study {study_name} not found or has no model")

    payload = saved["model"]
    means = payload["frame_means"]
    stds = payload["frame_stds"]
    outcome = payload["outcome"]
    treatment = payload["treatment"]
    ate = float(payload["ate"])
    ci = payload.get("ci", [None, None])

    base_outcome = means.get(outcome, 0.0)
    base_treatment = means.get(treatment, 0.0)

    t_scenario = float(scenario.get("treatment", base_treatment))
    delta_t = t_scenario - base_treatment
    pred = base_outcome + ate * delta_t

    # confounder nudges: linearise around mean using std-scaled small effect.
    conf_overrides = (scenario.get("confounders") or {})
    for cname, val in conf_overrides.items():
        if cname not in means:
            continue
        c_mean = means[cname]
        c_std = stds.get(cname, 1.0) or 1.0
        # 1-σ confounder shift ≈ 10 % of |ATE| as a conservative proxy.
        nudge_per_sigma = 0.10 * abs(ate)
        pred += nudge_per_sigma * (float(val) - c_mean) / c_std

    lo = pred - abs(ate - (ci[0] or ate)) * abs(delta_t) if ci[0] is not None else pred
    hi = pred + abs((ci[1] or ate) - ate) * abs(delta_t) if ci[1] is not None else pred

    out = {
        "study_name": study_name,
        "scenario": scenario,
        "predicted_outcome": pred,
        "predicted_lower": lo,
        "predicted_upper": hi,
        "outcome_var": outcome,
        "treatment_var": treatment,
        "baseline_outcome": base_outcome,
    }
    if persist:
        _persist_cf(saved["id"], scenario, pred, lo, hi, label or "")
    return out
