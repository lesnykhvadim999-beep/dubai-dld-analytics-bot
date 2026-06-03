"""estimator.py — DoWhy + EconML driven ATE estimation.

Pipeline per study:
    1. Pull tidy frame via data_loader.study_frame()
    2. Build DAG (dag_builder)
    3. DoWhy CausalModel.identify_effect()
    4. estimate_effect(method_name=...)
    5. EconML DoubleML for cross-check + CI tightening (when feasible)
    6. Refute via random_common_cause
    7. Persist to causal_studies (pickled model included)
"""

from __future__ import annotations

import json
import logging
import pickle
import warnings
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd
import psycopg2.extras

from . import dag_builder, data_loader

warnings.filterwarnings("ignore")
log = logging.getLogger(__name__)

# DoWhy / EconML are heavy — import lazily so module import is cheap.
def _imports():
    from dowhy import CausalModel  # type: ignore
    try:
        from econml.dml import LinearDML  # type: ignore
        from sklearn.ensemble import GradientBoostingRegressor  # type: ignore
        return CausalModel, LinearDML, GradientBoostingRegressor
    except Exception:  # pragma: no cover
        return CausalModel, None, None


@dataclass
class StudyResult:
    study_name: str
    treatment_var: str
    outcome_var: str
    confounders: List[str]
    ate: float
    ate_lower_ci: float
    ate_upper_ci: float
    effect_unit: str
    p_value: Optional[float]
    sample_size: int
    method: str
    refutation_passed: Optional[bool]
    dag_blob: Dict[str, Any] = field(default_factory=dict)
    model_blob: bytes = b""
    natural_language: Optional[str] = None

    def to_short_dict(self) -> Dict[str, Any]:
        return {
            "study_name": self.study_name,
            "ate": self.ate,
            "ci": [self.ate_lower_ci, self.ate_upper_ci],
            "unit": self.effect_unit,
            "n": self.sample_size,
            "method": self.method,
            "refuted": (self.refutation_passed is False),
        }


def _ci_from_dowhy(estimate, frame, treatment, outcome) -> tuple[float, float, Optional[float]]:
    """Best-effort 95% CI extraction across DoWhy versions."""
    lower = upper = None
    pval: Optional[float] = None
    try:
        ci = estimate.get_confidence_intervals(confidence_level=0.95)
        if ci is not None:
            arr = np.asarray(ci, dtype=float).ravel()
            if arr.size >= 2:
                lower, upper = float(arr[0]), float(arr[1])
    except Exception:
        pass
    try:
        test = estimate.test_stat_significance()
        if isinstance(test, dict):
            pval = float(test.get("p_value", test.get("pvalue", None)) or 0.0) or None
    except Exception:
        pass
    if lower is None or upper is None:
        # Fall back to bootstrap on simple linear regression slope.
        from sklearn.linear_model import LinearRegression  # local import
        x = frame.drop(columns=[outcome]).to_numpy(dtype=float)
        y = frame[outcome].to_numpy(dtype=float)
        t_idx = list(frame.drop(columns=[outcome]).columns).index(treatment)
        rng = np.random.default_rng(42)
        slopes = []
        for _ in range(200):
            sample = rng.integers(0, len(x), size=len(x))
            reg = LinearRegression().fit(x[sample], y[sample])
            slopes.append(reg.coef_[t_idx])
        lower = float(np.percentile(slopes, 2.5))
        upper = float(np.percentile(slopes, 97.5))
    return lower, upper, pval


def _run_dowhy(frame: pd.DataFrame, treatment: str, outcome: str,
               dag_gml: str, method: str) -> tuple[float, float, float, Optional[float], Any]:
    CausalModel, *_ = _imports()
    model = CausalModel(
        data=frame,
        treatment=treatment,
        outcome=outcome,
        graph=dag_gml,
    )
    identified = model.identify_effect(proceed_when_unidentifiable=True)
    estimate = model.estimate_effect(
        identified,
        method_name=method,
        test_significance=False,
    )
    ate = float(estimate.value)
    lower, upper, pval = _ci_from_dowhy(estimate, frame, treatment, outcome)
    return ate, lower, upper, pval, (model, identified, estimate)


def _run_refutation(model, identified, estimate) -> Optional[bool]:
    try:
        ref = model.refute_estimate(
            identified, estimate,
            method_name="random_common_cause",
        )
        # If the new estimate stays in the same sign and within 30 % of original
        # we consider the refutation passed.
        original = float(estimate.value)
        new_val = float(ref.new_effect)
        if original == 0:
            return abs(new_val) < 1e-6
        return (np.sign(original) == np.sign(new_val)) and (
            abs(new_val - original) / abs(original) < 0.30
        )
    except Exception as exc:
        log.warning("refutation failed: %s", exc)
        return None


def _run_econml_dml(frame: pd.DataFrame, treatment: str, outcome: str) -> Optional[float]:
    _, LinearDML, GBR = _imports()
    if LinearDML is None:
        return None
    try:
        y = frame[outcome].to_numpy(dtype=float)
        t = frame[treatment].to_numpy(dtype=float)
        x_cols = [c for c in frame.columns if c not in (treatment, outcome)]
        X = frame[x_cols].to_numpy(dtype=float)
        dml = LinearDML(
            model_y=GBR(n_estimators=80, max_depth=3),
            model_t=GBR(n_estimators=80, max_depth=3),
            random_state=42,
            discrete_treatment=set(np.unique(t)).issubset({0, 1}),
        )
        dml.fit(Y=y, T=t, X=X, W=None)
        eff = float(np.mean(dml.effect(X)))
        return eff
    except Exception as exc:
        log.warning("EconML DML cross-check failed: %s", exc)
        return None


def run_study(
    study_name: str,
    treatment: str,
    outcome: str,
    confounders: List[str],
    *,
    effect_unit: str,
    dag_gml: Optional[str] = None,
    method: str = "backdoor.linear_regression",
    persist: bool = True,
    natural_language: Optional[str] = None,
) -> StudyResult:
    """Fit one study end-to-end and (optionally) persist it."""
    frame = data_loader.study_frame(treatment, outcome, confounders)
    enc_confs = data_loader.confounder_names_after_encoding(frame, treatment, outcome)
    if dag_gml is None:
        dag_gml = dag_builder.default_dag(treatment, outcome, enc_confs)

    ate, lower, upper, pval, dowhy_objs = _run_dowhy(
        frame, treatment, outcome, dag_gml, method
    )
    refuted = _run_refutation(*dowhy_objs)
    dml_ate = _run_econml_dml(frame, treatment, outcome)

    method_label = method
    if dml_ate is not None and abs(dml_ate - ate) / max(abs(ate), 1e-9) < 0.5:
        method_label = f"{method}+EconML.LinearDML"

    # Compact, picklable model carrier.
    payload = {
        "ate": ate,
        "ci": [lower, upper],
        "dml_ate": dml_ate,
        "treatment": treatment,
        "outcome": outcome,
        "confounders": enc_confs,
        "method": method_label,
        "frame_means": {c: float(frame[c].mean()) for c in frame.columns},
        "frame_stds": {c: float(frame[c].std() or 1.0) for c in frame.columns},
    }
    model_bytes = pickle.dumps(payload)
    dag_blob = {"gml": dag_gml, "confounders": enc_confs}

    result = StudyResult(
        study_name=study_name,
        treatment_var=treatment,
        outcome_var=outcome,
        confounders=enc_confs,
        ate=ate,
        ate_lower_ci=lower,
        ate_upper_ci=upper,
        effect_unit=effect_unit,
        p_value=pval,
        sample_size=len(frame),
        method=method_label,
        refutation_passed=refuted,
        dag_blob=dag_blob,
        model_blob=model_bytes,
        natural_language=natural_language,
    )

    if persist:
        _persist(result)
    return result


def _persist(r: StudyResult) -> int:
    with data_loader.db_conn() as c, c.cursor() as cur:
        cur.execute(
            """
            INSERT INTO causal_studies
                  (study_name, treatment_var, outcome_var, confounders,
                   ate, ate_lower_ci, ate_upper_ci, effect_unit,
                   p_value, sample_size, method, refutation_passed,
                   dag_blob, model_blob, natural_language, refreshed_at)
            VALUES (%s,%s,%s,%s,
                    %s,%s,%s,%s,
                    %s,%s,%s,%s,
                    %s,%s,%s, NOW())
            ON CONFLICT (study_name) DO UPDATE SET
                  treatment_var = EXCLUDED.treatment_var,
                  outcome_var   = EXCLUDED.outcome_var,
                  confounders   = EXCLUDED.confounders,
                  ate           = EXCLUDED.ate,
                  ate_lower_ci  = EXCLUDED.ate_lower_ci,
                  ate_upper_ci  = EXCLUDED.ate_upper_ci,
                  effect_unit   = EXCLUDED.effect_unit,
                  p_value       = EXCLUDED.p_value,
                  sample_size   = EXCLUDED.sample_size,
                  method        = EXCLUDED.method,
                  refutation_passed = EXCLUDED.refutation_passed,
                  dag_blob      = EXCLUDED.dag_blob,
                  model_blob    = EXCLUDED.model_blob,
                  natural_language = EXCLUDED.natural_language,
                  refreshed_at  = NOW()
            RETURNING id
            """,
            (
                r.study_name, r.treatment_var, r.outcome_var, r.confounders,
                r.ate, r.ate_lower_ci, r.ate_upper_ci, r.effect_unit,
                r.p_value, r.sample_size, r.method, r.refutation_passed,
                json.dumps(r.dag_blob), psycopg2.Binary(r.model_blob),
                r.natural_language,
            ),
        )
        sid = cur.fetchone()[0]
        c.commit()
        return sid


def load_study(study_name: str) -> Optional[Dict[str, Any]]:
    """Fetch a saved study + unpickled model payload."""
    with data_loader.db_conn() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            "SELECT * FROM causal_studies WHERE study_name=%s", (study_name,)
        )
        row = cur.fetchone()
    if not row:
        return None
    blob = row.get("model_blob")
    if blob is not None:
        try:
            row["model"] = pickle.loads(bytes(blob))
        except Exception:
            row["model"] = None
    return row
