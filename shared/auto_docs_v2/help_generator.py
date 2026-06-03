"""PHASE BO O4 — help_generator.

For each bot: scan main file with regexes for slash-commands + reply/inline
menu labels, then assemble a Markdown /help text. Optionally translates RU↔EN
via shared LLM chain (Cerebras → Gemini → …). Stores result in `bot_help_texts`.

Public API:
    generate_help(bot_name, language="ru") -> str
    generate_all() -> dict[(bot_name, language), str]
"""
from __future__ import annotations

import os
import re
import sys
from datetime import datetime
from typing import Optional

from . import BOTS
from .schema import upsert_help, get_help

# ── regex toolbox ─────────────────────────────────────────────────────────────
# Slash commands appearing as string literals: cmd == "foo", "/foo", startswith("/foo")
_SLASH_LITERAL_RE = re.compile(r"""['"]/([a-z][a-z_0-9]{1,30})['"]""")
_SLASH_CMP_RE = re.compile(r"""cmd\s*==\s*['"]([a-z][a-z_0-9]{1,30})['"]""")

# Reply-keyboard / inline labels: rbtn_xxx, btn_xxx in localisation tables.
_LABEL_RE = re.compile(r"""['"]r?btn_([a-z][a-z_0-9]{1,40})['"]""")

# Translation table entries: "key":  "Some label",
_T_TABLE_ENTRY_RE = re.compile(
    r"""['"]([a-z][a-z_0-9]{1,40})['"]\s*:\s*['"]([^'"\n]{1,120})['"]""",
    re.IGNORECASE,
)

# Whitelisted/blacklisted secrets — never leak.
_SECRET_BLOCKLIST = (
    "token", "password", "secret", "dsn", "database_url", "api_key",
    "bot_token", "telegram_token",
)


# ── core feature classifier ───────────────────────────────────────────────────
_CORE_LABELS = {
    "search", "search_grp", "picks", "picks_grp", "profile", "profile_grp",
    "ai", "hot", "new", "favs", "alerts", "menu", "home", "saved_searches",
    "buy", "rent", "calc", "roi",
}
_PRO_LABELS = {
    "compare", "compare_short", "map", "map_short", "heatmap", "voice",
    "ai_consult", "settings_grp", "lang", "pro_grp", "pdf", "watchlist",
    "news", "report",
}
_STD_CMDS = ("start", "menu", "help", "pro")


def _safe_label(s: str) -> str:
    s = (s or "").strip()
    # strip emoji/spaces collapse
    s = re.sub(r"\s+", " ", s)
    # drop anything that smells like a secret
    low = s.lower()
    for bad in _SECRET_BLOCKLIST:
        if bad in low:
            return ""
    return s[:80]


def _read(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read()
    except Exception:
        return ""


def _scan_handlers(main_path: str) -> tuple[set[str], dict[str, str]]:
    """Return (slash_cmds, {label_key: human_label}). Best-effort."""
    src = _read(main_path)
    if not src:
        return set(), {}

    cmds: set[str] = set()
    for m in _SLASH_LITERAL_RE.finditer(src):
        c = m.group(1).lower()
        if 1 < len(c) < 25 and not any(b in c for b in _SECRET_BLOCKLIST):
            cmds.add(c)
    for m in _SLASH_CMP_RE.finditer(src):
        c = m.group(1).lower()
        if not any(b in c for b in _SECRET_BLOCKLIST):
            cmds.add(c)

    # localisation labels: pick the RU table first (it's the canonical UX).
    labels: dict[str, str] = {}
    # Iterate over all "rbtn_xxx" used as keys in translation tables.
    for m in _T_TABLE_ENTRY_RE.finditer(src):
        key, val = m.group(1).lower(), m.group(2)
        if key.startswith(("rbtn_", "btn_")):
            label = _safe_label(val)
            if label and key not in labels:
                labels[key] = label

    return cmds, labels


def _split_core_pro(labels: dict[str, str]) -> tuple[list[str], list[str]]:
    core: list[str] = []
    pro: list[str] = []
    seen = set()
    for k, v in labels.items():
        stem = k.split("_", 1)[1] if k.startswith("rbtn_") or k.startswith("btn_") else k
        # collapse stems like "search_grp" → "search"
        head = stem.split("_")[0]
        if v in seen:
            continue
        seen.add(v)
        if stem in _CORE_LABELS or head in _CORE_LABELS:
            core.append(v)
        elif stem in _PRO_LABELS or head in _PRO_LABELS:
            pro.append(v)
    # Trim & dedupe
    return core[:6], pro[:6]


def _fallback_features(bot_name: str, role: str) -> tuple[list[str], list[str]]:
    """Default features when handler scan finds nothing usable."""
    base = {
        "resale-bot":      (["Поиск вторички Dubai", "Smart Picks (топ-объекты)", "Избранное и алерты"],
                            ["Сравнение зданий", "Тепловая карта", "PDF-отчёт", "AI консультация"]),
        "channel-bot-new": (["Парсинг Telegram-каналов", "Обогащение через Reelly", "Лента новых листингов"],
                            ["Фильтры по районам", "Подписка на алерты"]),
        "lead-bot":        (["Лид-форма", "Квалификация", "Быстрый ROI"],
                            ["AI-консультант", "PDF-отчёт"]),
        "roi-bot":         (["Калькулятор доходности", "PDF-отчёт по объекту", "Сравнение сценариев"],
                            ["Net rental yield", "Cap rate analysis"]),
        "analytics-bot":   (["DLD сделки", "Тренды по районам", "Топ зданий"],
                            ["Causal-инсайты", "Месячные отчёты"]),
        "hub-bot":         (["Навигация по экосистеме", "Быстрый старт", "Контакты"],
                            ["Кросс-бот поиск"]),
    }
    return base.get(bot_name, ([role or "—"], []))


def _build_markdown_ru(bot: dict, slash_cmds: set[str], core: list[str], pro: list[str]) -> str:
    """Assemble Russian markdown /help."""
    username = bot["username"]
    # standard commands present in all bots
    cmds_present = [f"/{c}" for c in _STD_CMDS if c in slash_cmds or c in ("start", "help", "menu")]
    extra_cmds = sorted(
        c for c in slash_cmds
        if c not in _STD_CMDS and len(c) <= 12 and not c.startswith(("alert_del_", "unwatch_"))
    )[:8]
    cmd_line = " ".join(cmds_present + [f"/{c}" for c in extra_cmds]) or "/start /menu /help"

    core_lines = "\n".join(f"— {c}" for c in core) if core else "— —"
    pro_lines = "\n".join(f"— {p}" for p in pro) if pro else "— —"

    return (
        f"👋 Бот @{username}\n\n"
        f"🎯 Что я умею:\n{core_lines}\n\n"
        f"⚡ Pro фичи:\n{pro_lines}\n\n"
        f"🔧 Команды:\n{cmd_line}\n\n"
        f"📞 Поддержка: @VadimRealty\n"
        f"_Vadim Realty (RERA BRN 65011)_\n"
    )


def _build_markdown_en(bot: dict, slash_cmds: set[str], core: list[str], pro: list[str]) -> str:
    username = bot["username"]
    cmds_present = [f"/{c}" for c in _STD_CMDS if c in slash_cmds or c in ("start", "help", "menu")]
    extra_cmds = sorted(
        c for c in slash_cmds
        if c not in _STD_CMDS and len(c) <= 12 and not c.startswith(("alert_del_", "unwatch_"))
    )[:8]
    cmd_line = " ".join(cmds_present + [f"/{c}" for c in extra_cmds]) or "/start /menu /help"

    # try free LLM translation; fallback to RU labels.
    core_t = _translate_list(core, target="en")
    pro_t = _translate_list(pro, target="en")
    core_lines = "\n".join(f"— {c}" for c in core_t) if core_t else "— —"
    pro_lines = "\n".join(f"— {p}" for p in pro_t) if pro_t else "— —"

    return (
        f"👋 Bot @{username}\n\n"
        f"🎯 What I can do:\n{core_lines}\n\n"
        f"⚡ Pro features:\n{pro_lines}\n\n"
        f"🔧 Commands:\n{cmd_line}\n\n"
        f"📞 Support: @VadimRealty\n"
        f"_Vadim Realty (RERA BRN 65011)_\n"
    )


def _translate_list(items: list[str], target: str) -> list[str]:
    if not items:
        return []
    if target == "ru":
        return items
    # Try shared free LLM chain (Cerebras → Groq → … → Gemini). Fail-open.
    try:
        from shared.auto_docs import llm_client  # type: ignore
    except Exception:
        return items
    joined = "\n".join(f"- {s}" for s in items)
    prompt = (
        "Translate the following Russian bullet points into English. "
        "Keep them short (≤6 words each), preserve order, output ONLY the bullets, no preamble.\n\n"
        f"{joined}"
    )
    try:
        out = llm_client.llm_call(prompt, max_tokens=300, timeout=15)
    except Exception:
        return items
    if not out:
        return items
    translated = []
    for line in out.splitlines():
        line = line.strip().lstrip("-•—* ").strip()
        if line:
            translated.append(line[:80])
    return translated or items


# ── public API ────────────────────────────────────────────────────────────────
def generate_help(bot_name: str, language: str = "ru", persist: bool = True) -> Optional[str]:
    bot = next((b for b in BOTS if b["bot_name"] == bot_name), None)
    if not bot:
        sys.stderr.write(f"[help_generator] unknown bot: {bot_name}\n")
        return None

    slash_cmds, labels = _scan_handlers(bot["main_file"])
    core, pro = _split_core_pro(labels)
    if not core and not pro:
        core, pro = _fallback_features(bot_name, bot.get("role", ""))

    if language == "en":
        md = _build_markdown_en(bot, slash_cmds, core, pro)
    else:
        md = _build_markdown_ru(bot, slash_cmds, core, pro)

    # final secret-scrub belt+suspenders
    for bad in _SECRET_BLOCKLIST:
        if bad in md.lower():
            # surgically drop the offending line; never publish secrets.
            md = "\n".join(ln for ln in md.splitlines() if bad not in ln.lower())

    if persist:
        upsert_help(bot_name, language, md)
    return md


def generate_all() -> dict[tuple[str, str], str]:
    out: dict[tuple[str, str], str] = {}
    for bot in BOTS:
        for lang in ("ru", "en"):
            md = generate_help(bot["bot_name"], lang, persist=True)
            if md:
                out[(bot["bot_name"], lang)] = md
    return out


def get_or_generate(bot_name: str, language: str = "ru") -> str:
    """Used by /help command in bots. Cache-first."""
    cached = get_help(bot_name, language)
    if cached:
        return cached
    fresh = generate_help(bot_name, language, persist=True)
    return fresh or "/help temporarily unavailable. Try later or contact @VadimRealty."


if __name__ == "__main__":
    result = generate_all()
    print(f"[help_generator] generated {len(result)} entries at {datetime.utcnow().isoformat()}Z")
    for k in result:
        print(f"  - {k[0]} / {k[1]}")
