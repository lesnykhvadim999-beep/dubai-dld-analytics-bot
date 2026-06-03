"""
llm_cluster_analyzer — Cerebras → Groq → Gemini analyzes a cluster of failed
bot interactions and proposes a structured fix.

Free chain only (CLAUDE.md hard-rule).
"""
from __future__ import annotations

import json
import logging
import os
from typing import Sequence

import requests

log = logging.getLogger(__name__)


PROMPT_TMPL = """You analyze failed user interactions from a Dubai real-estate Telegram bot.

ISSUE TYPE: {issue_type}
BOT: {bot_name}

The {count} users below all received a BAD outcome (empty/error/slow/negative-feedback).

Find what they have in common and propose a fix.

OUTPUT STRICT JSON ONLY (no markdown, no triple backticks):
{{
  "issue_summary": "1-sentence root cause (e.g. 'users typed building alias not in our KG')",
  "suggested_fix": {{
    "kind": "add_alias" | "cache_query" | "code_change" | "improve_response" | "add_synonym",
    "category": "building" | "area" | "developer" | "query_type" | null,
    "key": "the canonical alias key (lowercase, normalized) — required if kind=add_alias|add_synonym",
    "value": ["canonical name 1", "canonical name 2"],
    "cache_ttl_minutes": 60,
    "notes": "1-sentence why this fix solves the problem"
  }},
  "confidence": 0.0
}}

GUIDELINES:
- For kind="add_alias": `key` MUST be lowercase normalized (no extra spaces, no punctuation).
  `value` MUST be a list of CANONICAL names from Dubai real-estate (Address Residences..., Emaar Beachfront, etc.).
- For kind="cache_query": only if the query pattern is hot AND idempotent.
- For kind="code_change": only if a clear bug (e.g. handler crashes on Unicode input).
- For kind="improve_response": when response is correct but user is unsatisfied (rephrase, more detail).
- Set confidence:
    0.9+ if all samples are clearly the SAME typo/missing alias
    0.7-0.85 if probable pattern but some noise
    <0.6 if mixed — STILL output, but the gate will reject.

SAMPLES (user messages from {count} failed interactions):
{samples}
"""


def _build_prompt(issue_type: str, bot_name: str, samples: Sequence[str]) -> str:
    snippets = []
    for i, t in enumerate(samples[:15], 1):
        snippets.append(f"{i}. {t[:300]}")
    return PROMPT_TMPL.format(
        issue_type=issue_type,
        bot_name=bot_name or "unknown",
        count=len(samples),
        samples="\n".join(snippets),
    )


# ─────────── Providers (free chain) ───────────
def _cerebras(prompt: str) -> str | None:
    key = os.environ.get("CEREBRAS_API_KEY")
    if not key:
        return None
    try:
        r = requests.post(
            "https://api.cerebras.ai/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={
                "model": "gpt-oss-120b",
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.1,
                "max_tokens": 1000,
            },
            timeout=45,
        )
        r.raise_for_status()
        data = r.json()
        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        return msg.get("content") or msg.get("reasoning_content")
    except Exception as exc:
        log.warning("Cerebras failed: %s", exc)
        return None


def _groq(prompt: str) -> str | None:
    key = os.environ.get("GROQ_API_KEY")
    if not key:
        return None
    try:
        r = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={
                "model": "llama-3.3-70b-versatile",
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.1,
                "max_tokens": 1000,
            },
            timeout=45,
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]
    except Exception as exc:
        log.warning("Groq failed: %s", exc)
        return None


def _gemini(prompt: str) -> str | None:
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        return None
    try:
        r = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={key}",
            json={
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"temperature": 0.1, "maxOutputTokens": 1000},
            },
            timeout=45,
        )
        r.raise_for_status()
        return r.json()["candidates"][0]["content"]["parts"][0]["text"]
    except Exception as exc:
        log.warning("Gemini failed: %s", exc)
        return None


def _strip_json(raw: str) -> str:
    s = raw.strip()
    if s.startswith("```"):
        s = s.strip("`")
        if s.startswith("json"):
            s = s[4:]
    a, b = s.find("{"), s.rfind("}")
    if a >= 0 and b > a:
        return s[a : b + 1]
    return s


def propose_fix(issue_type: str, bot_name: str, samples: Sequence[str]) -> dict | None:
    """Return dict with suggested_fix or None if all providers failed."""
    if not samples:
        return None
    prompt = _build_prompt(issue_type, bot_name, samples)
    for provider in (_cerebras, _groq, _gemini):
        raw = provider(prompt)
        if not raw:
            continue
        try:
            parsed = json.loads(_strip_json(raw))
            if not isinstance(parsed, dict):
                continue
            if not parsed.get("suggested_fix"):
                continue
            parsed["_provider"] = provider.__name__.lstrip("_")
            return parsed
        except Exception as exc:
            log.warning("Failed to parse %s output: %s", provider.__name__, exc)
            continue
    return None
