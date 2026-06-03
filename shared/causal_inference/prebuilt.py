"""prebuilt.py — registry of the five canned analyses.

Each entry maps to a single ``run_study(...)`` invocation. The list is
the source of truth for the monthly cron job and the analytics-bot UI.
"""

from __future__ import annotations

import logging
from typing import Dict, List

from . import dag_builder, estimator, reporter

log = logging.getLogger(__name__)


REGISTRY: Dict[str, Dict] = {
    "Effect_of_metro_extension_on_prices": {
        "treatment": "metro_opened",
        "outcome": "price_per_sqft",
        "confounders": ["demand_score", "supply_index", "year_built",
                        "yoy_change_pct", "size_sqft"],
        "effect_unit": "AED/sqft",
        "dag": dag_builder.dag_metro_on_price,
        "method": "backdoor.linear_regression",
        "label_ru": "Эффект открытия метро на цену",
    },
    "Effect_of_handover_wave_on_rental_yield": {
        "treatment": "handover_wave",
        "outcome": "rental_yield",
        "confounders": ["supply_index", "demand_score", "is_top_developer",
                        "is_sea_view", "size_sqft"],
        "effect_unit": "п.п. yield",
        "dag": dag_builder.dag_handover_on_yield,
        "method": "backdoor.linear_regression",
        "label_ru": "Эффект волны сдачи на доходность",
    },
    "Effect_of_developer_brand_premium": {
        "treatment": "is_top_developer",
        "outcome": "price_per_sqft",
        "confounders": ["size_sqft", "floor", "is_sea_view", "year_built",
                        "demand_score"],
        "effect_unit": "AED/sqft",
        "dag": dag_builder.dag_developer_premium,
        "method": "backdoor.linear_regression",
        "label_ru": "Премиум застройщика (Emaar/Damac/…)",
    },
    "Effect_of_view_premium": {
        "treatment": "is_sea_view",
        "outcome": "price_per_sqft",
        "confounders": ["size_sqft", "floor", "is_top_developer",
                        "year_built", "demand_score"],
        "effect_unit": "AED/sqft",
        "dag": dag_builder.dag_view_premium,
        "method": "backdoor.linear_regression",
        "label_ru": "Премиум за море/марину",
    },
    "Effect_of_floor_height_on_price": {
        "treatment": "floor",
        "outcome": "price_per_sqft",
        "confounders": ["size_sqft", "is_sea_view", "is_top_developer",
                        "year_built", "demand_score"],
        "effect_unit": "AED/sqft за этаж",
        "dag": dag_builder.dag_floor_on_price,
        "method": "backdoor.linear_regression",
        "label_ru": "Эффект высоты этажа на цену",
    },
}


def run_all_prebuilt(use_llm: bool = False) -> List[Dict]:
    """Fit + persist every registered study. Returns short dicts."""
    out = []
    for name, spec in REGISTRY.items():
        try:
            # Build DAG with encoded confounders so DoWhy never sees a
            # categorical name. We compute confs after one-hot in the
            # estimator; the DAG just gets the raw names which is fine
            # because we always also fall back to default_dag inside
            # the estimator if the user passes None.
            dag_fn = spec.get("dag")
            dag_gml = dag_fn(spec["confounders"]) if dag_fn else None
            result = estimator.run_study(
                study_name=name,
                treatment=spec["treatment"],
                outcome=spec["outcome"],
                confounders=spec["confounders"],
                effect_unit=spec["effect_unit"],
                dag_gml=dag_gml,
                method=spec.get("method", "backdoor.linear_regression"),
                persist=True,
            )
            short = result.to_short_dict()
            short["label_ru"] = spec.get("label_ru", name)
            # Try to fill natural_language via LLM (optional, slow).
            if use_llm:
                try:
                    nl = reporter.llm_explain({
                        "study_name": name,
                        "treatment_var": spec["treatment"],
                        "outcome_var": spec["outcome"],
                        "ate": result.ate,
                        "ate_lower_ci": result.ate_lower_ci,
                        "ate_upper_ci": result.ate_upper_ci,
                        "effect_unit": spec["effect_unit"],
                        "sample_size": result.sample_size,
                        "method": result.method,
                        "refutation_passed": result.refutation_passed,
                    })
                    if nl:
                        # rewrite natural_language only (cheap UPDATE)
                        from . import data_loader
                        with data_loader.db_conn() as c, c.cursor() as cur:
                            cur.execute(
                                "UPDATE causal_studies SET natural_language=%s WHERE study_name=%s",
                                (nl, name),
                            )
                            c.commit()
                        short["natural_language"] = nl
                except Exception as exc:
                    log.warning("LLM explain skipped for %s: %s", name, exc)
            out.append(short)
            log.info("study %s: ATE=%.4f CI=[%.4f;%.4f] n=%s",
                     name, result.ate, result.ate_lower_ci,
                     result.ate_upper_ci, result.sample_size)
        except Exception as exc:
            log.exception("study %s failed: %s", name, exc)
            out.append({"study_name": name, "error": str(exc)})
    return out
