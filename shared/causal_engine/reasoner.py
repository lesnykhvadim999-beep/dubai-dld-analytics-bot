"""Causal reasoner — LLM-driven chain builder.

Given a question (e.g. "почему ROI Marina упал?") and current features
(price, supply, transactions), retrieves relevant causal_facts, asks the
free-LLM chain to compose a 2-4 step explanation, and stores the chain.
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional

import psycopg2
import psycopg2.extras

logger = logging.getLogger(__name__)

# Intelligence DB holds causal_facts/causal_chains. PRIMARY = INTELLIGENCE_DATABASE_URL
# (resale_bot DATABASE_URL points at a different DB without the causal schema).
DSN = (
    os.environ.get("INTELLIGENCE_DB_DSN")
    or os.environ.get("INTELLIGENCE_DATABASE_URL")
    or os.environ.get("LIVE_DATABASE_URL")
    or os.environ.get("DATABASE_URL")
    or ""
)


@dataclass
class CausalChain:
    question: str
    pattern: str
    steps: List[Dict[str, Any]] = field(default_factory=list)  # each: {fact_id,cause,effect,conf}
    final_explanation: str = ""
    llm_provider: str = ""
    features_snapshot: Dict[str, Any] = field(default_factory=dict)
    chain_id: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ----------------------------- LLM chain -----------------------------

def _call_llm(prompt: str) -> tuple[str, str]:
    """Call the free LLM stack. Returns (text, provider_name).
    Tries shared.llm_router if available; otherwise falls back to a stub."""
    try:
        from shared import llm_router  # type: ignore
        text, provider = llm_router.complete(prompt, max_tokens=500)
        return text or "", provider or "unknown"
    except Exception:
        pass
    # Try a single direct provider as last resort
    try:
        import requests
        key = os.getenv("CEREBRAS_API_KEY") or os.getenv("GROQ_API_KEY")
        if key:
            url = "https://api.cerebras.ai/v1/chat/completions" if os.getenv("CEREBRAS_API_KEY") else "https://api.groq.com/openai/v1/chat/completions"
            model = "llama3.1-8b" if os.getenv("CEREBRAS_API_KEY") else "llama-3.1-8b-instant"
            r = requests.post(
                url,
                headers={"Authorization": f"Bearer {key}"},
                json={"model": model, "messages": [{"role": "user", "content": prompt}], "max_tokens": 500, "temperature": 0.3},
                timeout=30,
            )
            r.raise_for_status()
            data = r.json()
            return data["choices"][0]["message"]["content"], "cerebras" if "CEREBRAS_API_KEY" in os.environ else "groq"
    except Exception as e:
        logger.warning("llm direct fallback failed: %s", e)
    return "", "none"


# ------------------------- Pattern detection -------------------------

_PATTERNS = [
    (re.compile(r"почему.*roi|why.*roi", re.I),                  "why_roi"),
    (re.compile(r"почему.*цена|price.*down|почему.*стоит", re.I), "why_price"),
    (re.compile(r"почему.*аренд|why.*rent", re.I),                "why_rent"),
    (re.compile(r"почему.*спрос|demand", re.I),                   "why_demand"),
    (re.compile(r"тренд|trend|объясни.*график", re.I),            "explain_trend"),
]


def _detect_pattern(question: str, features: Dict[str, Any]) -> str:
    for rx, name in _PATTERNS:
        if rx.search(question):
            area = features.get("area") or ""
            return f"{name}:{area}" if area else name
    return "generic"


# ------------------------- Facts retrieval ---------------------------

def _retrieve_facts(domain_hint: str, area: Optional[str], limit: int = 12) -> List[Dict[str, Any]]:
    """Get candidate facts ordered by confidence, filtered by domain/area."""
    sql = """
        SELECT id, cause_text, effect_text, evidence_source, confidence,
               domain, areas, time_horizon
        FROM causal_facts
        WHERE (%(domain)s IS NULL OR domain = %(domain)s OR domain IS NULL)
          AND (%(area)s   IS NULL OR areas IS NULL OR %(area)s = ANY(areas))
        ORDER BY confidence DESC
        LIMIT %(lim)s
    """
    out: List[Dict[str, Any]] = []
    try:
        with psycopg2.connect(DSN) as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(sql, {"domain": domain_hint or None, "area": area, "lim": limit})
                out = [dict(r) for r in cur.fetchall()]
    except Exception as e:
        logger.warning("retrieve_facts failed: %s", e)
    return out


def _domain_from_pattern(pat: str) -> Optional[str]:
    head = pat.split(":", 1)[0]
    return {
        "why_roi":   "roi",
        "why_price": "price",
        "why_rent":  "rent",
        "why_demand":"demand",
    }.get(head)


# ------------------------- Main entry ---------------------------

def explain(
    question: str,
    features: Optional[Dict[str, Any]] = None,
    *,
    user_id: Optional[int] = None,
    bot_source: Optional[str] = None,
    persist: bool = True,
) -> CausalChain:
    """Build a causal chain for the given question.

    `features` is a free-form dict, e.g.:
        {"area":"Dubai Marina","price_yoy":-0.04,"supply_yoy":+0.18,"tx_yoy":-0.10}
    """
    features = features or {}
    pattern  = _detect_pattern(question, features)
    domain   = _domain_from_pattern(pattern)
    area     = features.get("area")
    candidates = _retrieve_facts(domain, area)

    if not candidates:
        chain = CausalChain(question=question, pattern=pattern,
                            final_explanation="Недостаточно данных для причинного объяснения.",
                            features_snapshot=features)
        return chain

    # Compose prompt for LLM — pick 2-4 most relevant facts and string them into chain
    fact_lines = [
        f"[{c['id']}] (conf={c['confidence']:.2f}, src={c['evidence_source']}) "
        f"CAUSE: {c['cause_text']} => EFFECT: {c['effect_text']}"
        for c in candidates
    ]
    prompt = (
        "You are a Dubai real-estate causal-reasoning engine.\n"
        f"Question: {question}\n"
        f"Current features: {json.dumps(features, ensure_ascii=False)}\n\n"
        "Candidate causal facts:\n" + "\n".join(fact_lines) + "\n\n"
        "Pick 2-4 facts that BEST explain the situation as a causal CHAIN "
        "(each step's effect should feed next step's cause if possible).\n"
        "Reply STRICT JSON:\n"
        '{"steps":[{"fact_id":<int>,"why":"<one-line rationale>"}],'
        '"final_explanation":"<2-3 sentences in same language as question, '
        "mentioning concrete numbers from features when relevant\"}\n"
    )

    raw, provider = _call_llm(prompt)
    steps: List[Dict[str, Any]] = []
    final = ""
    parsed: Dict[str, Any] = {}
    if raw:
        try:
            # extract JSON robustly
            m = re.search(r"\{.*\}", raw, re.S)
            parsed = json.loads(m.group(0)) if m else {}
        except Exception as e:
            logger.warning("llm JSON parse failed: %s | raw=%r", e, raw[:200])

    if parsed and isinstance(parsed.get("steps"), list):
        fact_by_id = {c["id"]: c for c in candidates}
        for s in parsed["steps"][:4]:
            fid = s.get("fact_id")
            if fid in fact_by_id:
                f = fact_by_id[fid]
                steps.append({
                    "fact_id": fid,
                    "cause":   f["cause_text"],
                    "effect":  f["effect_text"],
                    "conf":    float(f["confidence"]),
                    "why":     s.get("why", ""),
                    "source":  f["evidence_source"],
                })
        final = parsed.get("final_explanation", "")

    # Fallback: top-3 by confidence
    if not steps:
        for f in candidates[:3]:
            steps.append({
                "fact_id": f["id"], "cause": f["cause_text"], "effect": f["effect_text"],
                "conf": float(f["confidence"]), "why": "high-confidence baseline fact",
                "source": f["evidence_source"],
            })
        final = "На основе baseline-фактов: " + " ".join(s["effect"] for s in steps[:2])

    chain = CausalChain(
        question=question, pattern=pattern, steps=steps,
        final_explanation=final, llm_provider=provider,
        features_snapshot=features,
    )

    if persist:
        try:
            with psycopg2.connect(DSN) as conn:
                conn.autocommit = True
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO causal_chains
                            (query_pattern, question_text, chain_steps, final_explanation,
                             llm_provider, features_snapshot, user_id, bot_source)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                        RETURNING id
                        """,
                        (pattern, question, json.dumps(steps, ensure_ascii=False),
                         final, provider, json.dumps(features, ensure_ascii=False),
                         user_id, bot_source),
                    )
                    chain.chain_id = cur.fetchone()[0]
        except Exception as e:
            logger.warning("persist chain failed: %s", e)

    return chain
