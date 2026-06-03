"""
session_continuity.intent_classifier — pick best bot for a user query.

Two-tier strategy:
  1. Fast regex/keyword rules (zero cost, ~80% coverage).
  2. Cerebras LLM fallback for ambiguous queries (free tier).

Returns {"bot": str, "intent": str, "confidence": float, "reason": str}.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Any, Dict

log = logging.getLogger("session_continuity.intent")

# ── Bot routing table ─────────────────────────────────────────────────
# bot_key → telegram username (without @), keep aligned with ecosystem map
BOTS = {
    "resale":    {"username": "DubaiResaleBot",     "label": "Resale (вторичка)"},
    "analytics": {"username": "DubaiDldAnalyticsBot","label": "DLD Analytics"},
    "roi":       {"username": "DubaiRoiBot",         "label": "ROI калькулятор"},
    "lead":      {"username": "DubaiLeadBot",        "label": "Lead / клиент"},
    "channel":   {"username": "DubaiChannelBot",     "label": "Канал-постер"},
    "hub":       {"username": "DubaiHubBot",         "label": "Hub"},
}

# Compiled regex patterns: (pattern, bot, intent, confidence)
_RULES = [
    # NOTE: Python \b doesn't anchor reliably around Cyrillic — use only leading \b for stems.
    (r"(?i)(\broi\b|окупа|доход|\byield\b|cash[\s-]?flow|\breturn\b)",     "roi",       "roi.calculate",     0.92),
    (r"(?i)(тренд|\btrend\b|аналитик|анализ|prices?\s+last|статистик|\bchart\b|\bhistory\b)", "analytics", "analytics.trend",   0.90),
    (r"(?i)(куплю|купить|купи\b|продаю|resale|вторичк|\blisting\b|listings?\s+for)", "resale",   "resale.search",     0.90),
    (r"(?i)(\blead\b|заявк|клиент|\bcontact\b|whats?app|свяжи|позвон|менеджер|контакт|передайт|вопрос по объект)", "lead", "lead.create", 0.88),
    (r"(?i)(пост(ить|им|и)?\b|канал|опубликуй|публикац|channel\s+post)",    "channel",   "channel.publish",   0.85),
    (r"(?i)(\bменю\b|помощь|\bhelp\b|\bstart\b|где найти|какой бот)",       "hub",       "hub.menu",          0.80),
]
_COMPILED = [(re.compile(p), b, i, c) for (p, b, i, c) in _RULES]


def _rule_classify(text: str) -> Dict[str, Any]:
    matches = []
    for rx, bot, intent, conf in _COMPILED:
        if rx.search(text):
            matches.append((bot, intent, conf))
    if not matches:
        return {"bot": "", "intent": "", "confidence": 0.0, "reason": "no_rule"}
    # If multiple → pick highest confidence
    matches.sort(key=lambda x: -x[2])
    bot, intent, conf = matches[0]
    return {
        "bot": bot,
        "intent": intent,
        "confidence": conf,
        "reason": f"rule:{intent}",
    }


def _llm_classify(text: str) -> Dict[str, Any]:
    """Cerebras LLM fallback. Returns same shape or empty."""
    api_key = os.environ.get("CEREBRAS_API_KEY")
    if not api_key:
        return {}
    try:
        import urllib.request
        import json as _json

        bot_list = ", ".join(f"{k}({v['label']})" for k, v in BOTS.items())
        prompt = (
            f"User query: {text!r}\n"
            f"Available Dubai real-estate bots: {bot_list}\n"
            "Respond with JSON only: "
            '{"bot":"<key>","intent":"<short_slug>","confidence":<0..1>}'
        )
        body = _json.dumps({
            "model": "llama3.1-8b",
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 80,
            "temperature": 0.1,
        }).encode("utf-8")
        req = urllib.request.Request(
            "https://api.cerebras.ai/v1/chat/completions",
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=6) as resp:
            data = _json.loads(resp.read().decode("utf-8"))
        content = data["choices"][0]["message"]["content"]
        # Extract JSON
        m = re.search(r"\{.*\}", content, re.DOTALL)
        if not m:
            return {}
        parsed = _json.loads(m.group(0))
        if parsed.get("bot") not in BOTS:
            return {}
        return {
            "bot": parsed["bot"],
            "intent": str(parsed.get("intent", "llm.classified")),
            "confidence": float(parsed.get("confidence", 0.6)),
            "reason": "llm:cerebras",
        }
    except Exception as e:
        log.info("llm classify fallback failed: %s", e)
        return {}


def classify_intent(text: str) -> Dict[str, Any]:
    """Main entry. Always returns a dict (may have empty bot if total fail)."""
    text = (text or "").strip()
    if not text:
        return {"bot": "hub", "intent": "hub.menu", "confidence": 0.3, "reason": "empty"}
    rule = _rule_classify(text)
    if rule["confidence"] >= 0.85:
        return rule
    llm = _llm_classify(text)
    if llm and llm.get("confidence", 0) >= rule.get("confidence", 0):
        return llm
    if rule["bot"]:
        return rule
    return {"bot": "hub", "intent": "hub.menu", "confidence": 0.4, "reason": "fallback"}


# Public alias (PHASE BM Hub L14 — `classify` is the documented name)
def classify(text: str) -> Dict[str, Any]:
    """Alias for classify_intent — short, audit-friendly name."""
    return classify_intent(text)
